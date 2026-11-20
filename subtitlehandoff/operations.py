"""Desktop subtitle tracks, review dispositions, and client language bundles."""
import hashlib,json,itertools
from .ingest import capture
from .store import connect,save,decide
from . import paragraphs as P


def track_ok(row):
    cues=sorted(row.get('cues',[]),key=lambda x:x['start'])
    if not cues:return 'cue_missing'
    for i,cue in enumerate(cues):
        if cue['start']<0 or cue['end']<=cue['start'] or cue['end']>row['duration']:return 'cue_out_of_range'
        if i and cue['start']<cues[i-1]['end']:return 'cue_overlap'
    return None


def _canonical(db,media_hash):
    """Load the frozen canonical segmentation for a media release."""
    for project in db.execute('SELECT * FROM project_export'):
        body=json.loads(project['body'])
        if body.get('media_hash')==media_hash and body.get('paragraphs'):
            canon_ids,paragraph_shot=P.canonical(body.get('paragraphs'))
            shots=P.shot_map(body.get('shots'))
            return canon_ids,paragraph_shot,shots
    return None,None,None


def first(path,file):
    source,payload=capture(path,'desktop_subtitle_project',file)
    db=connect(path);results=[]
    try:
        with db:
            media_hash=payload['media_hash']
            save(db,'project_export',f'project:{source}',source,payload['version'],
                 {'media_hash':media_hash,'tracks':[r['id'] for r in payload.get('rows',[])],
                  'shots':payload.get('shots',[]),'paragraphs':payload.get('paragraphs',[])},
                 '已采录','project_frozen')
            if payload.get('shots'):
                save(db,'shot_schedule',f'shot:{media_hash}:{source}',source,payload['version'],
                     {'media_hash':media_hash,'shots':payload['shots']},'已采录','shot_schedule_frozen')
            canon_ids,paragraph_shot,shots=_canonical(db,media_hash) if payload.get('paragraphs') else (None,None,None)
            for row in payload.get('rows',[]):
                key=str(row['id']);issue=track_ok(row)
                if row.get('media_hash')!=media_hash:status,reason='待修','media_version_mismatch'
                elif issue:status,reason='待修',issue
                else:status,reason='可审校','timeline_valid'
                save(db,'subtitle_cue',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'字幕轨采录',key,status,reason,source))
                if canon_ids is not None:
                    cues=sorted(row.get('cues',[]),key=lambda x:x['start'])
                    evidence_cues=[{'cue_index':i,'paragraphs':cue.get('paragraphs',[])}
                                   for i,cue in enumerate(cues)]
                    conflicts=P.validate_evidence(canon_ids,paragraph_shot,shots,cues,evidence_cues)
                    ev_status='证据有效' if not conflicts else '证据冲突'
                    ev_reason='paragraph_evidence_valid' if not conflicts else conflicts[0]['code']
                    save(db,'paragraph_evidence',key,source,payload['version'],
                         {'track':key,'media_hash':media_hash,'canonical_order':canon_ids,
                          'evidence':evidence_cues,'conflicts':conflicts},ev_status,ev_reason)
                    results.append(decide(db,'段落证据',key,ev_status,ev_reason,source))
        return results
    finally:db.close()


def service_register(path,file):
    """Freeze the pre-agreed service policy (normal vs sentence-by-sentence)."""
    source,payload=capture(path,'desktop_service_policy',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id'])
                if not isinstance(row.get('sentence_cue_required'),bool):
                    status,reason='拒绝','invalid_service_policy'
                else:status,reason='已生效','service_policy_frozen'
                save(db,'service_policy',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'服务口径',key,status,reason,source))
        return results
    finally:db.close()


def second(path,file):
    source,payload=capture(path,'desktop_review_export',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);old=db.execute('SELECT * FROM review_issue WHERE id=?',(key,)).fetchone()
                if old:
                    if old['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):raise ValueError('review_identity_conflict')
                    results.append(decide(db,'审校修订',key,old['status'],old['reason'],source));continue
                track=db.execute('SELECT * FROM subtitle_cue WHERE id=?',(str(row['track']),)).fetchone()
                superseded=db.execute('SELECT * FROM review_issue WHERE id=?',(str(row.get('supersedes')),)).fetchone() if row.get('supersedes') else None
                if not track or track['status']!='可审校':status,reason='退审','track_not_valid'
                elif row.get('track_version')!=track['version']:status,reason='退审','revision_stale'
                elif row.get('severity')=='major' and not row.get('closed'):status,reason='退审','major_issue_open'
                elif not isinstance(row.get('cue_index'),int) or not 0<=row['cue_index']<len(json.loads(track['body'])['cues']):status,reason='退审','cue_not_found'
                elif row.get('supersedes') and (not superseded or json.loads(superseded['body'])['track']!=row['track'] or superseded['status']!='退审'):
                    status,reason='退审','invalid_supersession'
                else:status,reason='通过','issue_resolved'
                save(db,'review_issue',key,source,payload['version'],row,status,reason)
                save(db,'revision',f'revision:{key}',source,payload['version'],
                     {'track':row['track'],'version':row.get('track_version'),'closed':row.get('closed',False)},status,reason)
                if status=='通过' and superseded:
                    db.execute("UPDATE review_issue SET status='已解决',reason='superseded_by_current_review' WHERE id=?",(superseded['id'],))
                results.append(decide(db,'审校修订',key,status,reason,source))
        return results
    finally:db.close()


def _track_evidence(db,track_ids,canon_ids):
    """Load frozen paragraph evidence for the bundle's tracks."""
    views={};track_conflicts={};missing=[];versions={}
    for track_id in track_ids:
        ev=db.execute('SELECT * FROM paragraph_evidence WHERE id=?',(str(track_id),)).fetchone()
        if not ev:missing.append(str(track_id));continue
        body=json.loads(ev['body']);versions[str(track_id)]=ev['version']
        views[str(track_id)]=body['evidence']
        track_conflicts[str(track_id)]=body['conflicts']
    return views,track_conflicts,missing,versions


def _pair_alignments_ok(db,track_ids):
    required={frozenset(pair) for pair in itertools.combinations((str(x) for x in track_ids),2)}
    if not required:return True
    seen=set()
    for row in db.execute("SELECT * FROM alignment_pair WHERE status='已对齐'"):
        body=json.loads(row['body'])
        seen.add(frozenset((str(body['left']),str(body['right']))))
    return required.issubset(seen)


def third(path,file):
    source,payload=capture(path,'desktop_client_handoff',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);tracks=[]
                for track_id in row.get('tracks',[]):
                    found=db.execute('SELECT * FROM subtitle_cue WHERE id=?',(str(track_id),)).fetchone()
                    if found:tracks.append(dict(found))
                languages={json.loads(t['body'])['language'] for t in tracks if t['status']=='可审校'}
                unresolved={json.loads(x['body'])['track'] for x in db.execute("SELECT body FROM review_issue WHERE status='退审'")}
                passed={json.loads(x['body'])['track'] for x in db.execute("SELECT body FROM review_issue WHERE status='通过'")}
                signature=hashlib.sha256(json.dumps([(t['id'],t['version'],t['body']) for t in tracks],sort_keys=True).encode()).hexdigest()
                if len(tracks)!=len(row.get('tracks',[])):status,reason='待交付','track_missing'
                elif not set(row['required_languages']).issubset(languages):status,reason='待交付','language_missing'
                elif any(t['id'] in unresolved for t in tracks):status,reason='退审','review_open'
                elif len(row['required_languages'])>1 and not _pair_alignments_ok(db,row['tracks']):
                    status,reason='待修','alignment_unresolved'
                else:
                    status,reason=None,None
                # Paragraph-level shared mapping, frozen per delivery package.
                correspondence=[];mapping_conflicts=[];service_body=None;service_id=row.get('service')
                views={};ev_versions={}
                media_hash=json.loads(tracks[0]['body'])['media_hash'] if tracks else ''
                canon_ids,paragraph_shot,shots=_canonical(db,media_hash)
                if status is None and canon_ids:
                    views,track_conflicts,missing,ev_versions=_track_evidence(db,row['tracks'],canon_ids)
                    if missing:
                        status,reason='待修',P.EVIDENCE_INCOMPLETE
                        mapping_conflicts=[{'code':P.EVIDENCE_INCOMPLETE,'track':tid,'paragraph':None,
                                            'cue_index':None,'detail':'frozen paragraph evidence missing'} for tid in missing]
                    else:
                        mapping,conflicts=P.global_mapping(canon_ids,paragraph_shot,views,track_conflicts)
                        if conflicts:
                            status,reason='待修',P.COMMON_MAPPING_CONFLICT;mapping_conflicts=conflicts
                        else:correspondence=mapping
                if status is None and canon_ids:
                    policy=db.execute('SELECT * FROM service_policy WHERE id=?',(str(service_id),)).fetchone() if service_id else None
                    if not policy:
                        status,reason='待修',P.SERVICE_POLICY_MISSING
                        mapping_conflicts.append({'code':P.SERVICE_POLICY_MISSING,'track':None,'paragraph':None,
                                                  'cue_index':None,'detail':'service must be chosen from a pre-registered policy'})
                    else:
                        service_body=json.loads(policy['body'])
                        service_conflicts=P.service_gate(service_body,views,canon_ids)
                        if service_conflicts:
                            status,reason='返修',P.SENTENCE_CUE_REQUIRED
                            mapping_conflicts.extend(service_conflicts)
                if status is None and any(t['id'] not in passed for t in tracks):status,reason='退审','review_missing'
                receipt={'package':key,'service':service_id,
                         'languages':sorted(languages),'track_versions':ev_versions if canon_ids else {},
                         'paragraph_correspondence':correspondence,'conflicts':mapping_conflicts}
                if status is None and (row.get('client') or db.execute('SELECT 1 FROM client_contract LIMIT 1').fetchone()):
                    from .rights import check
                    rights_status,rights_reason=check(db,row.get('client'),languages,media_hash,row.get('at','1970-01-01T00:00:00'))
                    if rights_status!='已授权':status,reason='待授权',rights_reason
                    elif row.get('ack_hash')!=signature:status,reason='争议','ack_version_mismatch'
                    else:status,reason='已交付','client_confirmed'
                elif status is None and row.get('ack_hash')!=signature:status,reason='争议','ack_version_mismatch'
                elif status is None:status,reason='已交付','client_confirmed'
                # Frozen bodies keep only immutable evidence; the disposition
                # lives in table columns and the decision log, so re-evaluating
                # a checked-out package never raises identity_conflict.
                frozen_receipt={**receipt,'status':None,'rejection_reason':None}
                receipt['status']=status;receipt['rejection_reason']=reason
                save(db,'delivery_package',key,source,payload['version'],
                     {'tracks':row.get('tracks',[]),'required_languages':row['required_languages'],'hash':signature,
                      'client':row.get('client'),'service':service_id,'receipt':frozen_receipt},status,reason)
                save(db,'client_ack',f'ack:{key}',source,payload['version'],
                     {'ack_hash':row.get('ack_hash'),'receipt_summary':frozen_receipt},status,reason)
                if canon_ids:
                    save(db,'paragraph_correspondence',f'corr:{key}',source,payload['version'],
                         {'package':key,'service':service_id,'mapping':correspondence,
                          'track_versions':ev_versions},status,reason)
                    save(db,'mapping_conflict',f'conflict:{key}',source,payload['version'],
                         {'package':key,'conflicts':mapping_conflicts,'reason':reason},status,reason)
                results.append(decide(db,'语言交付',key,status,reason,source))
        return results
    finally:db.close()


def demo(root):
    db=root/'ledger.db'
    zh={'id':'T1','media_hash':'H1','language':'zh','duration':60,'cues':[
        {'start':1,'end':6,'text':'你好 世界',
         'paragraphs':[{'id':'U1','start':1,'end':3.5},{'id':'U2','start':3.5,'end':6}]}]}
    en=dict(zh,id='T2',language='en',cues=[
        {'start':1,'end':3.5,'text':'Hello','paragraphs':[{'id':'U1','start':1,'end':3.5}]},
        {'start':3.5,'end':6,'text':'world','paragraphs':[{'id':'U2','start':3.5,'end':6}]}])
    project={'version':'v1','media_hash':'H1','shots':[{'id':'S1','start':0,'end':60}],
             'paragraphs':[{'id':'U1','shot':'S1'},{'id':'U2','shot':'S1'}],'rows':[zh,en]}
    b={'version':'v1','rows':[{'id':'I1','track':'T1','track_version':'v1','severity':'major','closed':True,'cue_index':0},
                              {'id':'I2','track':'T2','track_version':'v1','severity':'none','closed':True,'cue_index':0}]}
    files=[]
    for i,data in enumerate((project,b)):
        p=root/f'export{i}.json';p.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');files.append(p)
    first_result=first(db,files[0]);second_result=second(db,files[1])
    from .alignment import evaluate
    from .rights import register
    p=root/'service.json';p.write_text(json.dumps({'version':'v1','rows':[
        {'id':'SVC_STANDARD','name':'standard_subtitle','sentence_cue_required':False}]}),encoding='utf-8')
    service_result=service_register(db,p)
    p=root/'alignment.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'A1','left':'T1','right':'T2','max_total_shift':0.5}]}),encoding='utf-8');evaluate(db,p)
    p=root/'rights.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'CLIENT','languages':['zh','en'],
        'media_hash':'H1','start':'2026-01-01T00:00:00','end':'2026-12-31T00:00:00'}]}),encoding='utf-8');register(db,p)
    signature=hashlib.sha256(json.dumps([('T1','v1',json.dumps(zh,sort_keys=True,ensure_ascii=False)),
        ('T2','v1',json.dumps(en,sort_keys=True,ensure_ascii=False))],sort_keys=True).encode()).hexdigest()
    c={'version':'v1','rows':[{'id':'D1','tracks':['T1','T2'],'required_languages':['zh','en'],'ack_hash':signature,
                            'client':'CLIENT','at':'2026-01-02T00:00:00','service':'SVC_STANDARD'}]}
    p=root/'export2.json';p.write_text(json.dumps(c,ensure_ascii=False),encoding='utf-8')
    return [first_result,second_result+service_result,third(db,p)]
