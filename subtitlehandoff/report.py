import hashlib,json
from pathlib import Path
from .store import connect,TABLES
def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['admissions']=_admission_trace(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()
def _admission_trace(db):
    """One traceable entry per prune request: why each task may or may not start."""
    traces=[]
    for plan in db.execute('SELECT * FROM cache_plan ORDER BY id'):
        body=json.loads(plan['body'])
        steps=[dict(x,body=json.loads(x['body'])) for x in db.execute(
            'SELECT * FROM cache_plan_step WHERE request_id=? ORDER BY seq',(plan['id'],))]
        releases=[dict(x,body=json.loads(x['body'])) for x in db.execute(
            'SELECT * FROM cache_release WHERE request_id=? ORDER BY id',(plan['id'],))]
        refs=[]
        for ref in db.execute('SELECT * FROM cache_reference'):
            ref_body=dict(ref,body=json.loads(ref['body']))
            refs.append(ref_body)
        admissions=[dict(x,body=json.loads(x['body'])) for x in db.execute(
            'SELECT * FROM cache_task_admission WHERE request_id=? ORDER BY task_id',(plan['id'],))]
        traces.append({'request_id':plan['id'],'mode':plan['mode'],'quota_mb':plan['quota_mb'],
                       'status':plan['status'],'reason':plan['reason'],'attempt':plan['attempt'],
                       'peak_mb':body.get('peak_mb'),'shortfall_mb':body.get('shortfall_mb'),
                       'recompute_seconds':body.get('recompute_seconds'),
                       'blocking_step':body.get('blocking_step'),
                       'active_references':refs,'planned_steps':steps,
                       'release_evidence':releases,'task_admissions':admissions})
    return traces
