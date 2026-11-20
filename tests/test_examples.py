"""The checked-in offline JSON samples under examples/ must run end to end."""
import json
from pathlib import Path
from subtitlehandoff import operations,alignment,report
from subtitlehandoff.store import connect

EX=Path(__file__).resolve().parent.parent/'examples'


def load(name):
    return json.loads((EX/name).read_text(encoding='utf-8'))


def stage(root,name,obj):
    p=root/name;p.write_text(json.dumps(obj,ensure_ascii=False),encoding='utf-8');return p


def sign(db,ids):
    import hashlib
    conn=connect(db)
    rows=[conn.execute('SELECT * FROM subtitle_cue WHERE id=?',(i,)).fetchone() for i in ids]
    return hashlib.sha256(json.dumps([(r['id'],r['version'],r['body']) for r in rows],sort_keys=True).encode()).hexdigest()


def test_three_track_samples_produce_two_service_outcomes(tmp_path):
    db=tmp_path/'ledger.db'
    project=load('01_project_three_tracks.json')
    operations.first(db,stage(tmp_path,'project.json',project))
    review={'version':'v1','rows':[
        {'id':f'P{t["id"]}','track':t['id'],'track_version':'v1','severity':'none',
         'closed':True,'cue_index':0} for t in project['rows']]}
    operations.second(db,stage(tmp_path,'review.json',review))
    operations.service_register(db,stage(tmp_path,'services.json',load('05_service_policies.json')))
    alignment.evaluate(db,stage(tmp_path,'alignment.json',load('06_alignment_three_pairs.json')))
    delivery=load('07_delivery_packages.json')
    for row in delivery['rows']:
        row['ack_hash']=sign(db,['ZH1','EN1','FR1'])
    result=operations.third(db,stage(tmp_path,'delivery.json',delivery))
    assert [(r['status'],r['reason']) for r in result]==[
        ('已交付','client_confirmed'),('返修','sentence_cue_required')]
    data=json.loads(report.build(db,tmp_path/'report.json').read_text(encoding='utf-8'))
    corr=json.loads(next(x['body'] for x in data['entities']['paragraph_correspondence']
                         if x['id']=='corr:PKG_STANDARD'))
    assert {m['paragraph'] for m in corr['mapping']}=={'U1','U2'}
    u2=next(m for m in corr['mapping'] if m['paragraph']=='U2')
    assert u2['coverage']['ZH1']==[0] and u2['coverage']['EN1']==[1] and u2['coverage']['FR1']==[1]


def test_inverted_french_sample_blocks_every_service(tmp_path):
    db=tmp_path/'ledger.db'
    project=load('02_project_inverted_french.json')
    operations.first(db,stage(tmp_path,'project.json',project))
    review={'version':'v1','rows':[
        {'id':f'P{t["id"]}','track':t['id'],'track_version':'v1','severity':'none',
         'closed':True,'cue_index':0} for t in project['rows']]}
    operations.second(db,stage(tmp_path,'review.json',review))
    operations.service_register(db,stage(tmp_path,'services.json',load('05_service_policies.json')))
    pairs=alignment.evaluate(db,stage(tmp_path,'alignment.json',{'version':'v1','rows':[
        {'id':'A1','left':'ZH1','right':'EN1','max_total_shift':6.0},
        {'id':'A2','left':'ZH1','right':'FR_BAD','max_total_shift':6.0},
        {'id':'A3','left':'EN1','right':'FR_BAD','max_total_shift':6.0}]}))
    assert all(p['status']=='已对齐' for p in pairs)
    delivery={'version':'v1','rows':[
        {'id':f'PKG_{svc}','tracks':['ZH1','EN1','FR_BAD'],'required_languages':['zh','en','fr'],
         'service':svc,'ack_hash':sign(db,['ZH1','EN1','FR_BAD'])}
        for svc in ('SVC_STANDARD','SVC_ACCESSIBLE')]}
    result=operations.third(db,stage(tmp_path,'delivery.json',delivery))
    assert all(r['reason']=='common_mapping_conflict' for r in result)


def test_shot_boundary_sample_records_cross_shot_gap(tmp_path):
    db=tmp_path/'ledger.db'
    operations.first(db,stage(tmp_path,'project.json',load('03_project_shot_boundary.json')))
    conn=connect(db)
    good=json.loads(conn.execute("SELECT body FROM paragraph_evidence WHERE id='ZH_CUT'").fetchone()[0])
    assert good['conflicts']==[]
    bad=json.loads(conn.execute("SELECT body FROM paragraph_evidence WHERE id='ZH_MERGED_GAP'").fetchone()[0])
    assert any(c['code']=='cross_shot_merge_gap' and c['paragraph']=='U2' for c in bad['conflicts'])


def test_repeated_text_sample_keeps_identical_cues_on_their_own_paragraphs(tmp_path):
    db=tmp_path/'ledger.db'
    operations.first(db,stage(tmp_path,'project.json',load('04_project_repeated_text.json')))
    alignment.evaluate(db,stage(tmp_path,'alignment.json',{'version':'v1','rows':[
        {'id':'A1','left':'ZH1','right':'EN1','max_total_shift':0.5}]}))
    pair=json.loads(connect(db).execute("SELECT body FROM alignment_pair WHERE id='pair:A1'").fetchone()[0])
    assert [c['paragraph'] for c in pair['correspondence']]==['U1','U2','U3']
    third=pair['correspondence'][2]
    assert third['left_cues']==[2] and third['right_cues']==[2]
