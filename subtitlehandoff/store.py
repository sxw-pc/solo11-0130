import sqlite3
from pathlib import Path
TABLES=['project_export', 'subtitle_cue', 'review_issue', 'revision', 'delivery_package', 'client_ack', 'alignment_policy', 'alignment_pair', 'client_contract', 'rights_window', 'rights_revocation', 'delivery_recall', 'preview_cache', 'cache_prune',
        'cache_physical', 'cache_dependency', 'cache_reference', 'cache_plan', 'cache_plan_step', 'cache_release', 'cache_task_admission']
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS export_file(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,hash TEXT NOT NULL,body BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(kind,hash));
CREATE TABLE IF NOT EXISTS export_record(id INTEGER PRIMARY KEY,source_id INT NOT NULL,line_no INT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(source_id,line_no));
CREATE TABLE IF NOT EXISTS issue(id INTEGER PRIMARY KEY,source_id INT REFERENCES export_file(id),code TEXT NOT NULL,detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS decision_log(id INTEGER PRIMARY KEY,subsystem TEXT NOT NULL,subject TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,source_id INT REFERENCES export_file(id),created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(subsystem,subject,status,reason,source_id));
CREATE TABLE IF NOT EXISTS project_export(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS subtitle_cue(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_issue(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS revision(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_package(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS client_ack(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alignment_policy(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alignment_pair(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS client_contract(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rights_window(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rights_revocation(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS delivery_recall(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preview_cache(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache_prune(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS cache_physical(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),sha256 TEXT NOT NULL,size_mb REAL NOT NULL,body TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cache_dependency(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),child_id TEXT NOT NULL,parent_id TEXT NOT NULL,body TEXT NOT NULL,UNIQUE(child_id,parent_id));
CREATE TABLE IF NOT EXISTS cache_reference(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),target_id TEXT NOT NULL,session TEXT,active_from TEXT,active_until TEXT,body TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cache_plan(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),mode TEXT,quota_mb REAL,status TEXT NOT NULL,reason TEXT NOT NULL,attempt INT NOT NULL DEFAULT 1,body TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS cache_plan_step(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,seq INT NOT NULL,kind TEXT NOT NULL,subject TEXT,status TEXT NOT NULL,reason TEXT,body TEXT NOT NULL,UNIQUE(request_id,seq));
CREATE TABLE IF NOT EXISTS cache_release(id INTEGER PRIMARY KEY AUTOINCREMENT,request_id TEXT NOT NULL,physical_id TEXT NOT NULL,path TEXT NOT NULL,moved_to TEXT,bytes INTEGER NOT NULL,sha256 TEXT,body TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(request_id,physical_id,path));
CREATE TABLE IF NOT EXISTS cache_task_admission(id TEXT PRIMARY KEY,request_id TEXT NOT NULL,task_id TEXT NOT NULL,status TEXT NOT NULL,reason TEXT,deadline_seconds REAL,needed_seconds REAL,body TEXT NOT NULL,UNIQUE(request_id,task_id));
"""
def connect(path):
    p=Path(path);p.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(p)
    db.row_factory=sqlite3.Row
    db.executescript(SCHEMA)
    return db
def save(db,table,key,source_id,version,body,status,reason):
    import json
    if table not in TABLES:raise ValueError('unknown_table')
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    old=db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    if old and (old['version'],old['body'])!=(version,encoded):
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',(source_id,'identity_conflict',f'{table}:{key}'))
        raise ValueError('identity_conflict')
    db.execute(f'INSERT INTO {table}(id,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason',
               (key,source_id,version,encoded,status,reason))
def decide(db,subsystem,subject,status,reason,source_id):
    db.execute('INSERT OR IGNORE INTO decision_log(subsystem,subject,status,reason,source_id) VALUES (?,?,?,?,?)',
               (subsystem,subject,status,reason,source_id))
    return {'subject':subject,'status':status,'reason':reason}
def issue(db,source_id,code,detail):
    db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',(source_id,code,detail))
def save_physical(db,physical_id,source_id,sha256,size_mb,body):
    import json
    db.execute('INSERT INTO cache_physical(id,source_id,sha256,size_mb,body) VALUES (?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET size_mb=excluded.size_mb,body=excluded.body',
               (physical_id,source_id,sha256,float(size_mb),json.dumps(body,sort_keys=True,ensure_ascii=False)))
def save_dependency(db,child_id,parent_id,source_id,body=None):
    import json
    db.execute('INSERT OR IGNORE INTO cache_dependency(id,source_id,child_id,parent_id,body) VALUES (?,?,?,?,?)',
               (f'{child_id}->{parent_id}',source_id,child_id,parent_id,json.dumps(body or {},sort_keys=True,ensure_ascii=False)))
def save_reference(db,ref_id,source_id,target_id,session,active_from,active_until,body):
    import json
    db.execute('INSERT INTO cache_reference(id,source_id,target_id,session,active_from,active_until,body) VALUES (?,?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET target_id=excluded.target_id,session=excluded.session,'
               'active_from=excluded.active_from,active_until=excluded.active_until,body=excluded.body',
               (ref_id,source_id,target_id,session,active_from,active_until,json.dumps(body,sort_keys=True,ensure_ascii=False)))
def active_references(db,at):
    """Reference rows whose active window covers the planning moment."""
    out=[]
    for row in db.execute('SELECT * FROM cache_reference'):
        start,end=row['active_from'],row['active_until']
        if (not start or start<=at) and (not end or at<end):
            out.append(dict(row))
    return out
def save_plan(db,request_id,source_id,mode,quota_mb,status,reason,body,attempt=1):
    import json
    db.execute('INSERT INTO cache_plan(id,source_id,mode,quota_mb,status,reason,attempt,body) VALUES (?,?,?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET status=excluded.status,reason=excluded.reason,attempt=excluded.attempt,body=excluded.body',
               (request_id,source_id,mode,quota_mb,status,reason,attempt,json.dumps(body,sort_keys=True,ensure_ascii=False)))
def replace_plan_steps(db,request_id,steps):
    import json
    db.execute('DELETE FROM cache_plan_step WHERE request_id=?',(request_id,))
    for i,step in enumerate(steps):
        db.execute('INSERT INTO cache_plan_step(id,request_id,seq,kind,subject,status,reason,body) VALUES (?,?,?,?,?,?,?,?)',
                   (f'{request_id}:{i}',request_id,i,step['kind'],step.get('subject'),step['status'],step.get('reason'),
                    json.dumps(step,sort_keys=True,ensure_ascii=False)))
def save_release(db,request_id,physical_id,path,moved_to,size_bytes,sha256,body):
    import json
    db.execute('INSERT OR IGNORE INTO cache_release(request_id,physical_id,path,moved_to,bytes,sha256,body) '
               'VALUES (?,?,?,?,?,?,?)',
               (request_id,physical_id,path,moved_to,int(size_bytes),sha256,
                json.dumps(body,sort_keys=True,ensure_ascii=False)))
def save_admission(db,request_id,task_id,status,reason,deadline_seconds,needed_seconds,body):
    import json
    db.execute('INSERT INTO cache_task_admission(id,request_id,task_id,status,reason,deadline_seconds,needed_seconds,body) '
               'VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(request_id,task_id) DO UPDATE SET status=excluded.status,'
               'reason=excluded.reason,deadline_seconds=excluded.deadline_seconds,needed_seconds=excluded.needed_seconds,body=excluded.body',
               (f'{request_id}:{task_id}',request_id,task_id,status,reason,deadline_seconds,needed_seconds,
                json.dumps(body,sort_keys=True,ensure_ascii=False)))
def load_preview_rows(db):
    import json
    return [dict(r,body=json.loads(r['body'])) for r in db.execute('SELECT * FROM preview_cache')]
