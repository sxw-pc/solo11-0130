"""Desktop subtitle tracks, review dispositions, and client language bundles."""
import hashlib,json
from .ingest import capture
from .store import connect,save,decide


def track_ok(row):
    cues=sorted(row.get('cues',[]),key=lambda x:x['start'])
    if not cues:return 'cue_missing'
    for i,cue in enumerate(cues):
        if cue['start']<0 or cue['end']<=cue['start'] or cue['end']>row['duration']:return 'cue_out_of_range'
        if i and cue['start']<cues[i-1]['end']:return 'cue_overlap'
    return None


def first(path,file):
    source,payload=capture(path,'desktop_subtitle_project',file)
    db=connect(path);results=[]
    try:
        with db:
            save(db,'project_export',f'project:{source}',source,payload['version'],
                 {'media_hash':payload['media_hash'],'tracks':[r['id'] for r in payload.get('rows',[])]},'已采录','project_frozen')
            for row in payload.get('rows',[]):
                key=str(row['id']);issue=track_ok(row)
                if row.get('media_hash')!=payload['media_hash']:status,reason='待修','media_version_mismatch'
                elif issue:status,reason='待修',issue
                else:status,reason='可审校','timeline_valid'
                save(db,'subtitle_cue',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'字幕轨采录',key,status,reason,source))
        return results
    finally:db.close()


def second(path,file):
    source,payload=capture(path,'desktop_review_export',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);track=db.execute('SELECT * FROM subtitle_cue WHERE id=?',(str(row['track']),)).fetchone()
                if not track or track['status']!='可审校':status,reason='退审','track_not_valid'
                elif row.get('track_version')!=track['version']:status,reason='退审','revision_stale'
                elif row.get('severity')=='major' and not row.get('closed'):status,reason='退审','major_issue_open'
                elif row.get('cue_index',0)>=len(json.loads(track['body'])['cues']):status,reason='退审','cue_not_found'
                else:status,reason='通过','issue_resolved'
                save(db,'review_issue',key,source,payload['version'],row,status,reason)
                save(db,'revision',f'revision:{key}',source,payload['version'],
                     {'track':row['track'],'version':row.get('track_version'),'closed':row.get('closed',False)},status,reason)
                results.append(decide(db,'审校修订',key,status,reason,source))
        return results
    finally:db.close()


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
                alignments=[x for x in db.execute('SELECT * FROM alignment_policy') if
                            set((json.loads(x['body'])['left'],json.loads(x['body'])['right'])).issubset(set(row.get('tracks',[])))]
                signature=hashlib.sha256(json.dumps([(t['id'],t['version'],t['body']) for t in tracks],sort_keys=True).encode()).hexdigest()
                if not set(row['required_languages']).issubset(languages):status,reason='待交付','language_missing'
                elif any(t['id'] in unresolved for t in tracks):status,reason='退审','review_open'
                elif len(row['required_languages'])>1 and (not alignments or any(x['status']!='已对齐' for x in alignments)):
                    status,reason='待修','alignment_unresolved'
                elif row.get('client') or db.execute('SELECT 1 FROM client_contract LIMIT 1').fetchone():
                    from .rights import check
                    rights_status,rights_reason=check(db,row.get('client'),languages,
                        json.loads(tracks[0]['body'])['media_hash'] if tracks else '',row.get('at','1970-01-01T00:00:00'))
                    if rights_status!='已授权':status,reason='待授权',rights_reason
                    elif row.get('ack_hash')!=signature:status,reason='争议','ack_version_mismatch'
                    else:status,reason='已交付','client_confirmed'
                elif row.get('ack_hash')!=signature:status,reason='争议','ack_version_mismatch'
                else:status,reason='已交付','client_confirmed'
                save(db,'delivery_package',key,source,payload['version'],
                     {'tracks':row.get('tracks',[]),'required_languages':row['required_languages'],'hash':signature,
                      'client':row.get('client')},status,reason)
                save(db,'client_ack',f'ack:{key}',source,payload['version'],{'ack_hash':row.get('ack_hash')},status,reason)
                results.append(decide(db,'语言交付',key,status,reason,source))
        return results
    finally:db.close()


def demo(root):
    db=root/'ledger.db';track={'id':'T1','media_hash':'H1','language':'zh','duration':60,
                               'cues':[{'start':1,'end':3,'text':'你好'}]}
    other=dict(track,id='T2',language='en',cues=[{'start':1,'end':3,'text':'Hello'}])
    a={'version':'v1','media_hash':'H1','rows':[track,other]}
    b={'version':'v1','rows':[{'id':'I1','track':'T1','track_version':'v1','severity':'major','closed':True,'cue_index':0}]}
    files=[]
    for i,data in enumerate((a,b)):
        p=root/f'export{i}.json';p.write_text(json.dumps(data),encoding='utf-8');files.append(p)
    first_result=first(db,files[0]);second_result=second(db,files[1])
    from .alignment import evaluate
    from .rights import register
    p=root/'alignment.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'A1','left':'T1','right':'T2','max_total_shift':0.5}]}),encoding='utf-8');evaluate(db,p)
    p=root/'rights.json';p.write_text(json.dumps({'version':'v1','rows':[{'id':'CLIENT','languages':['zh','en'],
        'media_hash':'H1','start':'2026-01-01T00:00:00','end':'2026-12-31T00:00:00'}]}),encoding='utf-8');register(db,p)
    signature=hashlib.sha256(json.dumps([('T1','v1',json.dumps(track,sort_keys=True,ensure_ascii=False)),
        ('T2','v1',json.dumps(other,sort_keys=True,ensure_ascii=False))],sort_keys=True).encode()).hexdigest()
    c={'version':'v1','rows':[{'id':'D1','tracks':['T1','T2'],'required_languages':['zh','en'],'ack_hash':signature,
                            'client':'CLIENT','at':'2026-01-02T00:00:00'}]}
    p=root/'export2.json';p.write_text(json.dumps(c),encoding='utf-8')
    return [first_result,second_result,third(db,p)]
