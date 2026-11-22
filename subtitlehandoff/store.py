import sqlite3
from pathlib import Path
TABLES=['project_export', 'subtitle_cue', 'review_issue', 'revision', 'delivery_package', 'client_ack', 'alignment_policy', 'alignment_pair', 'client_contract', 'rights_window', 'rights_revocation', 'delivery_recall', 'export_queue', 'export_job', 'dispatch_result',
        'export_plan', 'plan_job', 'plan_phase', 'workstation_slot', 'license_lease',
        'execution_event', 'execution_diff', 'reassignment', 'bottleneck', 'contract_lock', 'plan_revision']
SCHEMA="""
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS export_file(id INTEGER PRIMARY KEY,kind TEXT NOT NULL,version TEXT NOT NULL,hash TEXT NOT NULL,body BLOB NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(kind,hash));
CREATE TABLE IF NOT EXISTS export_record(id INTEGER PRIMARY KEY,source_id INT NOT NULL REFERENCES export_file(id),line_no INT NOT NULL,identity TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,UNIQUE(source_id,line_no));
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
CREATE TABLE IF NOT EXISTS export_queue(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_job(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dispatch_result(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS export_plan(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_job(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_phase(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workstation_slot(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS license_lease(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_event(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS execution_diff(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reassignment(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS bottleneck(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contract_lock(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS plan_revision(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
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


# --- enhanced export planning -------------------------------------------------
# ``save`` deliberately refuses to rewrite an entity with a different body: it is
# the identity guard used by the subtitle/review/delivery records.  A frozen
# export plan, however, has to be superseded (fault, duration revision, new hard
# deadline); ``upsert`` is the explicit, revision-tracked counterpart.
def upsert(db,table,key,source_id,version,body,status,reason):
    import json
    if table not in TABLES:raise ValueError('unknown_table')
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    db.execute(f'''INSERT INTO {table}(id,source_id,version,body,status,reason)
                   VALUES (?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET
                     source_id=excluded.source_id,version=excluded.version,
                     body=excluded.body,status=excluded.status,reason=excluded.reason''',
               (key,source_id,version,encoded,status,reason))


def set_status(db,table,key,status,reason):
    if table not in TABLES:raise ValueError('unknown_table')
    db.execute(f'UPDATE {table} SET status=?,reason=? WHERE id=?',(status,reason,key))


def body(db,table,key):
    import json
    row=db.execute(f'SELECT * FROM {table} WHERE id=?',(key,)).fetchone()
    return (row,json.loads(row['body'])) if row else (None,None)


def rows(db,table):
    import json
    return [(row,json.loads(row['body'])) for row in db.execute(f'SELECT * FROM {table} ORDER BY id')]

