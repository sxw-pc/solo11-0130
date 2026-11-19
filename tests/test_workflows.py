import hashlib,json
from subtitlehandoff import operations,report,alignment,rights
from subtitlehandoff.store import connect

def write(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj,ensure_ascii=False),encoding='utf-8');return p
def track(id='T1',language='zh',cues=None,media='H1'):
    return dict(id=id,media_hash=media,language=language,duration=60,
                cues=cues if cues is not None else [{'start':1,'end':3,'text':'你好'}])
def signature(db,ids):
    conn=connect(db);rows=[conn.execute('SELECT * FROM subtitle_cue WHERE id=?',(id,)).fetchone() for id in ids]
    return hashlib.sha256(json.dumps([(r['id'],r['version'],r['body']) for r in rows],sort_keys=True).encode()).hexdigest()
def pass_review(root,db,ids):
    rows=[{'id':f'P{i}','track':track_id,'track_version':'v1','severity':'none',
           'closed':True,'cue_index':0} for i,track_id in enumerate(ids)]
    return operations.second(db,write(root,'pass.json',{'version':'v1','rows':rows}))

def test_track_review_delivery_full_chain(tmp_path):
    result=operations.demo(tmp_path)
    assert [x[0]['status'] for x in result]==['可审校','通过','已交付']
    data=json.loads(report.build(tmp_path/'ledger.db',tmp_path/'report.json').read_text(encoding='utf-8'))
    assert len(data['sources'])==5 and all(x['verified'] for x in data['sources'])

def test_media_version_overlap_and_range(tmp_path):
    db=tmp_path/'db';rows=[track('T1'),track('T2',media='old'),
        track('T3',cues=[{'start':1,'end':3},{'start':2,'end':4}]),
        track('T4',cues=[{'start':59,'end':61}])]
    result=operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':rows}))
    assert [x['reason'] for x in result]==['timeline_valid','media_version_mismatch','cue_overlap','cue_out_of_range']

def test_major_review_and_stale_revision(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track()]}))
    rows=[{'id':'I1','track':'T1','track_version':'v1','severity':'major','closed':False,'cue_index':0},
          {'id':'I2','track':'T1','track_version':'old','severity':'minor','closed':True,'cue_index':0}]
    result=operations.second(db,write(tmp_path,'review.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['major_issue_open','revision_stale']

def test_multilanguage_missing_review_open_and_ack_mismatch(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track()]}))
    operations.second(db,write(tmp_path,'review.json',{'version':'v1','rows':[
        {'id':'I1','track':'T1','track_version':'v1','severity':'major','closed':False,'cue_index':0}]}))
    rows=[{'id':'D1','tracks':['T1'],'required_languages':['zh','en'],'ack_hash':'wrong'},
          {'id':'D2','tracks':['T1'],'required_languages':['zh'],'ack_hash':'wrong'}]
    result=operations.third(db,write(tmp_path,'delivery.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['language_missing','review_open']

def test_client_ack_hash_must_match_frozen_track(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track()]}))
    pass_review(tmp_path,db,['T1'])
    rows=[{'id':'D1','tracks':['T1'],'required_languages':['zh'],'ack_hash':'old'}]
    result=operations.third(db,write(tmp_path,'delivery.json',{'version':'v1','rows':rows}))
    assert result[0]['reason']=='ack_version_mismatch'
    assert connect(db).execute("SELECT status FROM delivery_package WHERE id='D1'").fetchone()[0]=='争议'

def test_bilingual_timing_drift_blocks_otherwise_complete_delivery(tmp_path):
    db=tmp_path/'db';zh=track();en=track('T2','en',cues=[{'start':5,'end':7,'text':'Hello'}])
    operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[zh,en]}))
    align={'version':'v1','rows':[{'id':'A1','left':'T1','right':'T2','max_total_shift':0.5}]}
    assert alignment.evaluate(db,write(tmp_path,'align.json',align))[0]['reason']=='timing_drift'
    result=operations.third(db,write(tmp_path,'delivery.json',{'version':'v1','rows':[
        {'id':'D1','tracks':['T1','T2'],'required_languages':['zh','en'],'ack_hash':'irrelevant'}]}))
    assert result[0]['reason']=='alignment_unresolved'

def test_client_embargo_and_scope_block_complete_language_bundle(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track()]}))
    pass_review(tmp_path,db,['T1'])
    rights.register(db,write(tmp_path,'rights.json',{'version':'v1','rows':[{'id':'CLIENT','languages':['zh'],
        'media_hash':'H1','start':'2026-02-01T00:00:00','end':'2026-12-31T00:00:00'}]}))
    rows=[{'id':'D1','tracks':['T1'],'required_languages':['zh'],'client':'CLIENT','at':'2026-01-01T00:00:00','ack_hash':'x'},
          {'id':'D2','tracks':['T1'],'required_languages':['zh'],'client':'CLIENT','at':'2027-01-01T00:00:00','ack_hash':'x'}]
    result=operations.third(db,write(tmp_path,'delivery.json',{'version':'v1','rows':rows}))
    assert [x['reason'] for x in result]==['embargo_active','rights_expired']

def test_rights_revocation_reopens_delivered_package(tmp_path):
    db=tmp_path/'ledger.db';operations.demo(tmp_path)
    file=write(tmp_path,'revoke.json',{'version':'v1','rows':[{'id':'R1','client':'CLIENT','approver':'manager','reason':'license_end'}]})
    assert rights.revoke(db,file)==rights.revoke(db,file)
    conn=connect(db)
    assert conn.execute("SELECT status FROM delivery_package WHERE id='D1'").fetchone()[0]=='已撤回'
    assert conn.execute("SELECT status FROM delivery_recall WHERE id='recall:D1'").fetchone()[0]=='待回收'

def test_alignment_requires_same_media_and_distinct_languages(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track(),track('T2','zh')]}))
    result=alignment.evaluate(db,write(tmp_path,'align.json',{'version':'v1','rows':[
        {'id':'A1','left':'T1','right':'T2','max_total_shift':1}]}))
    assert result[0]['reason']=='incompatible_tracks'

def test_review_issue_must_reference_current_cue(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track()]}))
    result=operations.second(db,write(tmp_path,'review.json',{'version':'v1','rows':[
        {'id':'I1','track':'T1','track_version':'v1','severity':'major','closed':True,'cue_index':8}]}))
    assert result[0]['reason']=='cue_not_found'

def test_client_license_cannot_expand_language_rights(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track(language='en')]}))
    pass_review(tmp_path,db,['T1'])
    rights.register(db,write(tmp_path,'rights.json',{'version':'v1','rows':[{'id':'CLIENT','languages':['zh'],
        'media_hash':'H1','start':'2026-01-01T00:00:00','end':'2026-12-31T00:00:00'}]}))
    result=operations.third(db,write(tmp_path,'delivery.json',{'version':'v1','rows':[
        {'id':'D1','tracks':['T1'],'required_languages':['en'],'client':'CLIENT','at':'2026-02-01T00:00:00','ack_hash':'x'}]}))
    assert result[0]['reason']=='rights_scope_mismatch'

def test_delivery_requires_review_even_when_ack_matches(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track()]}))
    result=operations.third(db,write(tmp_path,'delivery.json',{'version':'v1','rows':[
        {'id':'D1','tracks':['T1'],'required_languages':['zh'],'ack_hash':signature(db,['T1'])}]}))
    assert result[0]['reason']=='review_missing'

def test_multilanguage_bundle_requires_all_tracks_connected(tmp_path):
    db=tmp_path/'db';tracks=[track(),track('T2','en'),track('T3','fr')]
    operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':tracks}))
    pass_review(tmp_path,db,['T1','T2','T3'])
    alignment.evaluate(db,write(tmp_path,'align.json',{'version':'v1','rows':[
        {'id':'A1','left':'T1','right':'T2','max_total_shift':0.5}]}))
    bundle={'id':'D1','tracks':['T1','T2','T3'],'required_languages':['zh','en','fr'],
            'ack_hash':signature(db,['T1','T2','T3'])}
    assert operations.third(db,write(tmp_path,'delivery.json',{'version':'v1','rows':[bundle]}))[0]['reason']=='alignment_incomplete'

def test_review_revision_can_resolve_open_issue_and_restore_delivery(tmp_path):
    db=tmp_path/'db';operations.first(db,write(tmp_path,'tracks.json',{'version':'v1','media_hash':'H1','rows':[track()]}))
    operations.second(db,write(tmp_path,'open.json',{'version':'v1','rows':[
        {'id':'I1','track':'T1','track_version':'v1','severity':'major','closed':False,'cue_index':0}]}))
    bundle={'id':'D1','tracks':['T1'],'required_languages':['zh'],'ack_hash':signature(db,['T1'])}
    delivery=write(tmp_path,'delivery.json',{'version':'v1','rows':[bundle]})
    assert operations.third(db,delivery)[0]['reason']=='review_open'
    assert operations.second(db,write(tmp_path,'revision.json',{'version':'v1','rows':[
        {'id':'I2','track':'T1','track_version':'v1','severity':'major','closed':True,
         'cue_index':0,'supersedes':'I1'}]}))[0]['status']=='通过'
    assert operations.third(db,delivery)[0]['status']=='已交付'
    assert connect(db).execute("SELECT status FROM review_issue WHERE id='I1'").fetchone()[0]=='已解决'
