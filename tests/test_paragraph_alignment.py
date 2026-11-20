"""Paragraph-level cross-language correspondence, service gates and revisions."""
import json
from subtitlehandoff import operations,alignment,report
from subtitlehandoff.store import connect
from subtitlehandoff.paragraphs import (validate_evidence,canonical,shot_map,pair_conflicts,
                                        global_mapping,service_gate,COMMON_MAPPING_CONFLICT,
                                        SENTENCE_CUE_REQUIRED,CROSS_SHOT_MERGE_GAP,
                                        PARAGRAPH_ORDER_CONFLICT,PARAGRAPH_TIMING_DRIFT)


def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj,ensure_ascii=False),encoding='utf-8');return p


def p(id,start,end,shot='S1'):
    return {'id':id,'start':start,'end':end,'shot':shot}


def cue(start,end,text,paragraphs):
    return {'start':start,'end':end,'text':text,
            'paragraphs':[{'id':pid,'start':a,'end':b} for pid,a,b in paragraphs]}


def track(tid,language,cues,media='H1',duration=60):
    return {'id':tid,'media_hash':media,'language':language,'duration':duration,'cues':cues}


def project(rows,paragraphs,shots=None,media='H1'):
    return {'version':'v1','media_hash':media,
            'shots':shots or [{'id':'S1','start':0.0,'end':60.0}],
            'paragraphs':paragraphs,'rows':rows}


def review_pass(root,db,tracks):
    rows=[{'id':f'P{i}','track':t,'track_version':'v1','severity':'none','closed':True,'cue_index':0}
          for i,t in enumerate(tracks)]
    return operations.second(db,write(root,'review.json',{'version':'v1','rows':rows}))


def register_services(root,db):
    return operations.service_register(db,write(root,'services.json',{'version':'v1','rows':[
        {'id':'SVC_STANDARD','name':'standard','sentence_cue_required':False},
        {'id':'SVC_ACCESSIBLE','name':'accessible','sentence_cue_required':True}]}))


def sign(db,ids):
    conn=connect(db)
    rows=[conn.execute('SELECT * FROM subtitle_cue WHERE id=?',(i,)).fetchone() for i in ids]
    import hashlib
    return hashlib.sha256(json.dumps([(r['id'],r['version'],r['body']) for r in rows],sort_keys=True).encode()).hexdigest()


def three_tracks():
    """zh merges U1+U2 in one cue; en/fr split one cue per paragraph."""
    zh=track('ZH','zh',[cue(1,6,'你好 世界',[('U1',1,3.5),('U2',3.5,6)])])
    en=track('EN','en',[cue(1,3.5,'Hello',[('U1',1,3.5)]),cue(3.5,6,'world',[('U2',3.5,6)])])
    fr=track('FR','fr',[cue(1.05,3.5,'Bonjour',[('U1',1.05,3.5)]),cue(3.5,6,'monde',[('U2',3.5,6)])])
    return zh,en,fr


def deliver(root,db,package_id,tracks,service):
    rows=[{'id':package_id,'tracks':tracks,'required_languages':['zh','en','fr'],
           'service':service,'ack_hash':sign(db,tracks)}]
    return operations.third(db,write(root,f'delivery_{package_id}.json',{'version':'v1','rows':rows}))[0]


def setup_three_track_bundle(root,max_shift=0.6):
    db=root/'db';zh,en,fr=three_tracks()
    operations.first(db,write(root,'project.json',project([zh,en,fr],[p('U1',1,3.5),p('U2',3.5,6)])))
    review_pass(root,db,['ZH','EN','FR']);register_services(root,db)
    alignment.evaluate(db,write(root,'align.json',{'version':'v1','rows':[
        {'id':'A1','left':'ZH','right':'EN','max_total_shift':max_shift},
        {'id':'A2','left':'ZH','right':'FR','max_total_shift':max_shift},
        {'id':'A3','left':'EN','right':'FR','max_total_shift':max_shift}]}))
    return db


# ---------------------------------------------------------------- narrative

def test_standard_service_delivers_on_common_paragraph_mapping(tmp_path):
    db=setup_three_track_bundle(tmp_path)
    result=deliver(tmp_path,db,'PKG_STD',['ZH','EN','FR'],'SVC_STANDARD')
    assert (result['status'],result['reason'])==('已交付','client_confirmed')
    conn=connect(db)
    body=json.loads(conn.execute("SELECT body FROM paragraph_correspondence WHERE id='corr:PKG_STD'").fetchone()[0])
    mapping={m['paragraph']:m['coverage'] for m in body['mapping']}
    # merged Chinese cue covers both paragraphs; English/French split them
    assert mapping['U1']['ZH']==[0] and mapping['U2']['ZH']==[0]
    assert mapping['U1']['EN']==[0] and mapping['U2']['EN']==[1]
    assert mapping['U1']['FR']==[0] and mapping['U2']['FR']==[1]


def test_accessibility_service_rejects_merged_chinese_and_cannot_switch_policy(tmp_path):
    db=setup_three_track_bundle(tmp_path)
    result=deliver(tmp_path,db,'PKG_ACC',['ZH','EN','FR'],'SVC_ACCESSIBLE')
    assert (result['status'],result['reason'])==('返修',SENTENCE_CUE_REQUIRED)
    conn=connect(db)
    conflicts=json.loads(conn.execute("SELECT body FROM mapping_conflict WHERE id='conflict:PKG_ACC'").fetchone()[0])['conflicts']
    assert {c['paragraph'] for c in conflicts if c['code']==SENTENCE_CUE_REQUIRED}=={'U1','U2'}
    assert all('ZH' in c['detail'] for c in conflicts)
    # The same evidence cannot be pushed through under another service at
    # delivery time: the standard package is a separate, pre-registered choice.
    ok=deliver(tmp_path,db,'PKG_STD',['ZH','EN','FR'],'SVC_STANDARD')
    assert ok['status']=='已交付'
    assert connect(db).execute("SELECT status FROM delivery_package WHERE id='PKG_ACC'").fetchone()[0]=='返修'


def test_inverted_french_paragraph_order_has_no_common_mapping(tmp_path):
    db=tmp_path/'db';zh,en,_=three_tracks()
    # Cue times match the other tracks cue-for-cue (local timing passes), but
    # the editor's paragraph IDs are attached in reversed order.
    fr_bad=track('FR','fr',[cue(1.05,3.5,'monde',[('U2',1.05,3.5)]),cue(3.5,6,'Bonjour',[('U1',3.5,6)])])
    operations.first(db,write(tmp_path,'project.json',project([zh,en,fr_bad],[p('U1',1,3.5),p('U2',3.5,6)])))
    review_pass(tmp_path,db,['ZH','EN','FR']);register_services(tmp_path,db)
    pairs=alignment.evaluate(db,write(tmp_path,'align.json',{'version':'v1','rows':[
        {'id':'A1','left':'ZH','right':'EN','max_total_shift':0.6},
        {'id':'A2','left':'ZH','right':'FR','max_total_shift':6.0},
        {'id':'A3','left':'EN','right':'FR','max_total_shift':6.0}]}))
    # With a tolerance covering the cue positions, every local pair check
    # passes on timing/coverage alone...
    assert all(x['status']=='已对齐' for x in pairs)
    # ...but no service can sign: the shared order mapping does not exist.
    for service in ('SVC_STANDARD','SVC_ACCESSIBLE'):
        result=deliver(tmp_path,db,f'PKG_{service}',['ZH','EN','FR'],service)
        assert result['reason']==COMMON_MAPPING_CONFLICT
        assert result['status']=='待修'
    conn=connect(db)
    conflicts=json.loads(conn.execute("SELECT body FROM mapping_conflict WHERE id='conflict:PKG_SVC_STANDARD'").fetchone()[0])['conflicts']
    order=[c for c in conflicts if c['code']==PARAGRAPH_ORDER_CONFLICT and c['track']=='FR']
    # Both French cues participate in the reversed mapping: U2 is announced
    # where the film order expects U1, then U1 appears backwards.
    assert {c['paragraph'] for c in order}=={'U1','U2'}
    assert conn.execute("SELECT COUNT(*) FROM paragraph_correspondence WHERE status='已交付'").fetchone()[0]==0


# ----------------------------------------------------------------- shots

def test_cue_exactly_on_shot_boundary_is_accepted(tmp_path):
    db=tmp_path/'db'
    shots=[{'id':'S1','start':0,'end':10},{'id':'S2','start':10,'end':30}]
    zh=track('ZH','zh',[cue(2,10,'第一镜头',[('U1',2,10)]),cue(10,14,'第二镜头',[('U2',10,14)])],duration=30)
    en=track('EN','en',[cue(2,10,'shot one',[('U1',2,10)]),cue(10,14,'shot two',[('U2',10,14)])],duration=30)
    paragraphs=[p('U1',2,10,'S1'),p('U2',10,14,'S2')]
    operations.first(db,write(tmp_path,'project.json',project([zh,en],paragraphs,shots)))
    assert connect(db).execute("SELECT reason FROM paragraph_evidence WHERE id='ZH'").fetchone()[0]=='paragraph_evidence_valid'


def test_cross_shot_merge_hiding_next_shot_gap_is_rejected(tmp_path):
    db=tmp_path/'db'
    shots=[{'id':'S1','start':0,'end':10},{'id':'S2','start':10,'end':30}]
    bad=track('ZH','zh',[cue(2,14,'跨镜头',[('U1',2,9),('U2',9,10)])],duration=30)
    paragraphs=[p('U1',2,10,'S1'),p('U2',10,14,'S2')]
    operations.first(db,write(tmp_path,'project.json',project([bad],paragraphs,shots)))
    body=json.loads(connect(db).execute("SELECT body FROM paragraph_evidence WHERE id='ZH'").fetchone()[0])
    assert any(c['code']==CROSS_SHOT_MERGE_GAP and c['paragraph']=='U2' for c in body['conflicts'])


def test_cross_shot_merge_with_full_in_shot_evidence_is_allowed(tmp_path):
    db=tmp_path/'db'
    shots=[{'id':'S1','start':0,'end':10},{'id':'S2','start':10,'end':30}]
    merged=track('ZH','zh',[cue(2,14,'跨镜头但证据完整',[('U1',2,9.5),('U2',10.5,14)])],duration=30)
    paragraphs=[p('U1',2,10,'S1'),p('U2',10,14,'S2')]
    operations.first(db,write(tmp_path,'project.json',project([merged],paragraphs,shots)))
    assert connect(db).execute("SELECT status FROM paragraph_evidence WHERE id='ZH'").fetchone()[0]=='证据有效'


# ------------------------------------------------------- repeated text etc

def test_repeated_text_uses_paragraph_identity_not_nearest_time(tmp_path):
    db=tmp_path/'db'
    zh=track('ZH','zh',[cue(1,3,'好的',[('U1',1,3)]),cue(3,5,'中间',[('U2',3,5)]),cue(5,7,'好的',[('U3',5,7)])])
    en=track('EN','en',[cue(1,3,'OK',[('U1',1,3)]),cue(3,5,'middle',[('U2',3,5)]),cue(5,7,'OK',[('U3',5,7)])])
    paragraphs=[p('U1',1,3),p('U2',3,5),p('U3',5,7)]
    operations.first(db,write(tmp_path,'project.json',project([zh,en],paragraphs)))
    review_pass(tmp_path,db,['ZH','EN'])
    res=alignment.evaluate(db,write(tmp_path,'align.json',{'version':'v1','rows':[
        {'id':'A1','left':'ZH','right':'EN','max_total_shift':0.5}]}))
    assert res[0]['reason']=='paragraph_correspondence_aligned'
    pair=json.loads(connect(db).execute("SELECT body FROM alignment_pair WHERE id='pair:A1'").fetchone()[0])
    assert [c['paragraph'] for c in pair['correspondence']]==['U1','U2','U3']
    # A greedy nearest-text match would have paired the two identical "OK" cues
    # across U2; paragraph identity keeps U3 on cue 2 of both tracks.
    assert pair['correspondence'][2]['left_cues']==[2]==pair['correspondence'][2]['right_cues']


def test_same_timestamp_adjacent_blocks_sharing_paragraph_are_valid(tmp_path):
    # Adjacent back-to-back cues may jointly express one paragraph.
    db=tmp_path/'db'
    zh=track('ZH','zh',[cue(1,3.5,'你',[('U1',1,3.5)]),
                        cue(3.5,6,'好 世界',[('U1',3.5,4.5),('U2',4.5,6)])])
    en=track('EN','en',[cue(1,3.5,'Hello',[('U1',1,3.5)]),cue(3.5,6,'world',[('U2',3.5,6)])])
    paragraphs=[p('U1',1,4.5),p('U2',4.5,6)]
    operations.first(db,write(tmp_path,'project.json',project([zh,en],paragraphs)))
    assert connect(db).execute("SELECT status FROM paragraph_evidence WHERE id='ZH'").fetchone()[0]=='证据有效'


def test_timing_drift_on_paragraph_evidence_is_reported_per_paragraph(tmp_path):
    db=tmp_path/'db'
    zh=track('ZH','zh',[cue(1,3.5,'你好',[('U1',1,3.5)]),cue(3.5,6,'世界',[('U2',3.5,6)])])
    en=track('EN','en',[cue(5,7.5,'Hello',[('U1',5,7.5)]),cue(7.5,10,'world',[('U2',7.5,10)])])
    paragraphs=[p('U1',1,3.5),p('U2',3.5,6)]
    operations.first(db,write(tmp_path,'project.json',project([zh,en],paragraphs)))
    res=alignment.evaluate(db,write(tmp_path,'align.json',{'version':'v1','rows':[
        {'id':'A1','left':'ZH','right':'EN','max_total_shift':0.5}]}))
    assert res[0]['reason']==PARAGRAPH_TIMING_DRIFT
    conflicts=json.loads(connect(db).execute("SELECT body FROM alignment_pair WHERE id='pair:A1'").fetchone()[0])['conflicts']
    assert {c['paragraph'] for c in conflicts}=={'U1','U2'}


# --------------------------------------------------------------- revisions

def test_track_revision_freezes_new_package_but_leaves_old_package_evidence(tmp_path):
    db=setup_three_track_bundle(tmp_path)
    old=deliver(tmp_path,db,'PKG_OLD',['ZH','EN','FR'],'SVC_STANDARD')
    assert old['status']=='已交付'
    conn=connect(db)
    old_corr=conn.execute("SELECT body,version FROM paragraph_correspondence WHERE id='corr:PKG_OLD'").fetchone()
    old_corr_body=old_corr[0];old_pkg=conn.execute("SELECT body FROM delivery_package WHERE id='PKG_OLD'").fetchone()[0]
    # A late editor revision arrives as a new track version (new identity).
    zh2=track('ZH2','zh',[cue(1,3.5,'你好',[('U1',1,3.5)]),cue(3.5,6,'世界',[('U2',3.5,6)])])
    en2=track('EN2','en',[cue(1,3.5,'Hello',[('U1',1,3.5)]),cue(3.5,6,'world',[('U2',3.5,6)])])
    fr2=track('FR2','fr',[cue(1.05,3.5,'Bonjour',[('U1',1.05,3.5)]),cue(3.5,6,'monde',[('U2',3.5,6)])])
    project_v2={'version':'v2','media_hash':'H1','shots':[{'id':'S1','start':0,'end':60}],
                'paragraphs':[{'id':'U1','shot':'S1'},{'id':'U2','shot':'S1'}],'rows':[zh2,en2,fr2]}
    operations.first(db,write(tmp_path,'project_v2.json',project_v2))
    operations.second(db,write(tmp_path,'review_v2.json',{'version':'v2','rows':[
        {'id':f'P2{i}','track':t,'track_version':'v2','severity':'none','closed':True,'cue_index':0}
        for i,t in enumerate(['ZH2','EN2','FR2'])]}))
    alignment.evaluate(db,write(tmp_path,'align_v2.json',{'version':'v2','rows':[
        {'id':'B1','left':'ZH2','right':'EN2','max_total_shift':0.6},
        {'id':'B2','left':'ZH2','right':'FR2','max_total_shift':0.6},
        {'id':'B3','left':'EN2','right':'FR2','max_total_shift':0.6}]}))
    new=deliver(tmp_path,db,'PKG_NEW',['ZH2','EN2','FR2'],'SVC_ACCESSIBLE')
    assert new['status']=='已交付'  # revised zh now has one cue per paragraph
    # Re-delivering the old package row is idempotent and keeps the old evidence.
    again=operations.third(db,write(tmp_path,'redelivery_old.json',{'version':'v1','rows':[
        {'id':'PKG_OLD','tracks':['ZH','EN','FR'],'required_languages':['zh','en','fr'],
         'service':'SVC_STANDARD','ack_hash':sign(db,['ZH','EN','FR'])}]}))[0]
    assert again['status']=='已交付'
    conn=connect(db)
    assert conn.execute("SELECT body FROM paragraph_correspondence WHERE id='corr:PKG_OLD'").fetchone()[0]==old_corr_body
    assert conn.execute("SELECT body FROM delivery_package WHERE id='PKG_OLD'").fetchone()[0]==old_pkg
    new_body=json.loads(conn.execute("SELECT body FROM paragraph_correspondence WHERE id='corr:PKG_NEW'").fetchone()[0])
    assert set(new_body['track_versions'])=={'ZH2','EN2','FR2'}


# ------------------------------------------------------------------ report

def test_report_reads_back_coverage_and_rejection_reasons(tmp_path):
    db=setup_three_track_bundle(tmp_path)
    deliver(tmp_path,db,'PKG_STD',['ZH','EN','FR'],'SVC_STANDARD')
    deliver(tmp_path,db,'PKG_ACC',['ZH','EN','FR'],'SVC_ACCESSIBLE')
    data=json.loads(report.build(db,tmp_path/'report.json').read_text(encoding='utf-8'))
    tables=data['entities']
    corr={x['id']:x for x in tables['paragraph_correspondence']}
    std=json.loads(corr['corr:PKG_STD']['body'])
    assert std['service']=='SVC_STANDARD' and len(std['mapping'])==2
    ack={x['id']:json.loads(x['body']) for x in tables['client_ack']}
    summary=ack['ack:PKG_ACC']['receipt_summary']
    assert summary['conflicts'] and summary['conflicts'][0]['code']==SENTENCE_CUE_REQUIRED
    assert tables['mapping_conflict'] and tables['service_policy']
    # Every language block resolves to the paragraphs it covers.
    for m in std['mapping']:
        assert set(m['coverage'])=={'ZH','EN','FR'}


# ------------------------------------------------------------- pure engine

def test_pure_engine_rejects_skip_reverse_and_missing_paragraph():
    ids,shot=canonical([{'id':'U1'},{'id':'U2'},{'id':'U3'}]);shots=shot_map([{'id':'S1','start':0,'end':60}])
    two_ids,two_shot=canonical([{'id':'U1'},{'id':'U2'}])
    cues=[{'start':1,'end':3},{'start':3,'end':5}]
    good=[{'cue_index':0,'paragraphs':[{'id':'U1','start':1,'end':3}]},
          {'cue_index':1,'paragraphs':[{'id':'U2','start':3,'end':5}]}]
    assert validate_evidence(two_ids,two_shot,shots,cues,good)==[]
    skipping=[{'cue_index':0,'paragraphs':[{'id':'U1','start':1,'end':3}]},
              {'cue_index':1,'paragraphs':[{'id':'U3','start':3,'end':5}]}]
    assert any(c['code']==PARAGRAPH_ORDER_CONFLICT for c in validate_evidence(ids,shot,shots,cues,skipping))
    reverse=[{'cue_index':0,'paragraphs':[{'id':'U2','start':1,'end':3}]},
             {'cue_index':1,'paragraphs':[{'id':'U1','start':3,'end':5}]}]
    assert any(c['code']==PARAGRAPH_ORDER_CONFLICT for c in validate_evidence(two_ids,two_shot,shots,cues,reverse))
    only_u1=[{'cue_index':0,'paragraphs':[{'id':'U1','start':1,'end':3}]},
             {'cue_index':1,'paragraphs':[{'id':'U1','start':3,'end':5}]}]
    assert any(c['code']=='paragraph_missing' for c in validate_evidence(two_ids,two_shot,shots,cues,only_u1))


def test_service_gate_unknown_policy_and_mapping_without_conflicts():
    ids,shot=canonical([{'id':'U1'},{'id':'U2'}])
    split=[{'cue_index':0,'paragraphs':[{'id':'U1','start':1,'end':3}]},
           {'cue_index':1,'paragraphs':[{'id':'U2','start':3,'end':5}]}]
    merged=[{'cue_index':0,'paragraphs':[{'id':'U1','start':1,'end':3},{'id':'U2','start':3,'end':5}]}]
    assert service_gate(None,{'T':split},ids)[0]['code']=='service_policy_missing'
    assert service_gate({'sentence_cue_required':True},{'T':split},ids)==[]
    gate=service_gate({'sentence_cue_required':True},{'T':merged},ids)
    assert {c['paragraph'] for c in gate}=={'U1','U2'}
    mapping,conflicts=global_mapping(ids,shot,{'A':split,'B':split},{})
    assert not conflicts and [m['paragraph'] for m in mapping]==ids


def test_pair_conflicts_include_split_merge_but_not_order_decision():
    ids,shot=canonical([{'id':'U1'},{'id':'U2'}])
    merged=[{'cue_index':0,'paragraphs':[{'id':'U1','start':1,'end':3},{'id':'U2','start':3,'end':5}]}]
    split=[{'cue_index':0,'paragraphs':[{'id':'U1','start':1,'end':3}]},
           {'cue_index':1,'paragraphs':[{'id':'U2','start':3,'end':5}]}]
    conflicts,corr=pair_conflicts(ids,merged,split,0.5)
    assert conflicts==[]
    assert corr[0]['left_cues']==[0] and corr[0]['right_cues']==[0]
    assert corr[1]['left_cues']==[0] and corr[1]['right_cues']==[1]
