"""Drive the offline example exports end to end.

Run from the repository root:

    python3 -I scripts/run_examples.py

The script imports every JSON file under ``examples/`` (treated as untrusted
input: read by path, never executed), fills the client ack hashes from the
frozen tracks, and writes a fresh ledger plus report under ``build/examples/``.
"""
import hashlib,json,sys
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from subtitlehandoff import operations,alignment,report  # noqa: E402
from subtitlehandoff.store import connect  # noqa: E402

EX=ROOT/'examples';OUT=ROOT/'build'/'examples'


def load(name):
    return json.loads((EX/name).read_text(encoding='utf-8'))


def signature(db,ids):
    conn=connect(db);rows=[conn.execute('SELECT * FROM subtitle_cue WHERE id=?',(i,)).fetchone() for i in ids]
    conn.close()
    return hashlib.sha256(json.dumps([(r['id'],r['version'],r['body']) for r in rows],sort_keys=True).encode()).hexdigest()


def main():
    OUT.mkdir(parents=True,exist_ok=True)
    db=OUT/'ledger.db'
    if db.exists():db.unlink()
    project=load('01_project_three_tracks.json')
    pf=OUT/'01_project.json';pf.write_text(json.dumps(project,ensure_ascii=False),encoding='utf-8')
    operations.first(db,pf)
    review={'version':'v1','rows':[
        {'id':f'P{t["id"]}','track':t['id'],'track_version':'v1','severity':'none',
         'closed':True,'cue_index':0} for t in project['rows']]}
    rf=OUT/'02_review.json';rf.write_text(json.dumps(review,ensure_ascii=False),encoding='utf-8')
    operations.second(db,rf)
    sf=OUT/'03_services.json';sf.write_text(json.dumps(load('05_service_policies.json'),ensure_ascii=False),encoding='utf-8')
    operations.service_register(db,sf)
    af=OUT/'04_alignment.json';af.write_text(json.dumps(load('06_alignment_three_pairs.json'),ensure_ascii=False),encoding='utf-8')
    alignment.evaluate(db,af)
    tracks=[t['id'] for t in project['rows']]
    delivery=load('07_delivery_packages.json')
    for row in delivery['rows']:
        row['ack_hash']=signature(db,tracks)
    df=OUT/'05_delivery.json';df.write_text(json.dumps(delivery,ensure_ascii=False),encoding='utf-8')
    result=operations.third(db,df)
    print(json.dumps(result,ensure_ascii=False,indent=2))
    report.build(db,OUT/'report.json')
    print('report:',OUT/'report.json')


if __name__=='__main__':
    main()
