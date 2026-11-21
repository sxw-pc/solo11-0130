import hashlib,json
from pathlib import Path
from .store import connect,TABLES


def _archive_recovery(db):
    """回读每个工程版本的可恢复性与保全证据缺口（基于账本在案状态）。"""
    tasks={}
    for row in db.execute('SELECT * FROM archive_task'):
        tasks[row['id']]=json.loads(row['body'])
    shards={}
    for row in db.execute('SELECT * FROM archive_shard'):
        shards.setdefault(row['id'].rsplit(':',1)[0],[]).append(row)
    commits={}
    for row in db.execute('SELECT * FROM archive_commit'):
        commits.setdefault(row['id'].rsplit(':commit:',1)[0],[]).append(row)
    recovery=[]
    for row in db.execute('SELECT * FROM archive_generation ORDER BY id'):
        g=json.loads(row['body']);gen_id=row['id']
        task=tasks.get(g['project'],{});roster=task.get('roster',{})
        gshards=sorted(shards.get(gen_id,[]),key=lambda x:x['id'])
        receipt_state={}
        for c in commits.get(gen_id,[]):
            receipt_state[json.loads(c['body'])['disk']]=c['status']
        verified=sorted(d for d,s in receipt_state.items() if s=='已校验')
        gap=[d for d in roster if d not in verified]
        recovery.append({
            'generation':gen_id,'project':g['project'],'project_version':g['project_version'],
            'purpose':task.get('purpose'),'status':row['status'],'committed':row['status']=='已提交',
            'file_sha256':g['file_sha256'],'file_length':g['file_length'],'chunk':g['chunk'],
            'shards':[{'index':json.loads(s['body'])['shard_index'],'disk':json.loads(s['body'])['disk'],
                       'status':s['status'],'length':json.loads(s['body'])['length'],
                       'sha256':json.loads(s['body'])['sha256']} for s in gshards],
            'commit_receipts':receipt_state,'verified_commit_receipts':verified,
            'commit_quorum':len(verified)>=2,
            'recoverable_with_any_two_disks':row['status']=='已提交'
                and sum(1 for s in gshards if s['status']=='已校验')>=2,
            'preservation_complete':bool(roster) and not gap,
            'preservation_gap':sorted(gap)})
    return recovery


def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')],
              'archive_recovery':_archive_recovery(db)}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
