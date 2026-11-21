import sqlite3
from pathlib import Path
TABLES=['project_export', 'subtitle_cue', 'review_issue', 'revision', 'delivery_package', 'client_ack', 'alignment_policy', 'alignment_pair', 'client_contract', 'rights_window', 'rights_revocation', 'delivery_recall', 'access_trace', 'incident_case',
        'account_ownership', 'device_session', 'approval_receipt', 'approval_revocation', 'project_revision',
        'permission_contract', 'emergency_window', 'emergency_cosign']
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
CREATE TABLE IF NOT EXISTS access_trace(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS incident_case(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS account_ownership(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS device_session(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approval_receipt(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approval_revocation(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS project_revision(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS permission_contract(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS emergency_window(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS emergency_cosign(id TEXT PRIMARY KEY,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS audit_evaluation(id INTEGER PRIMARY KEY,trace_id TEXT NOT NULL,seq INT NOT NULL,source_id INT REFERENCES export_file(id),version TEXT NOT NULL,body TEXT NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP,UNIQUE(trace_id,seq));
CREATE TABLE IF NOT EXISTS evidence_change(id INTEGER PRIMARY KEY,trace_id TEXT NOT NULL,old_status TEXT,new_status TEXT NOT NULL,source_id INT REFERENCES export_file(id),detail TEXT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS report_snapshot(id INTEGER PRIMARY KEY,path TEXT NOT NULL,hash TEXT NOT NULL,source_count INT NOT NULL,created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS report_snapshot_path_hash ON report_snapshot(path,hash);
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
def latest_evaluation(db,trace_id):
    return db.execute('SELECT * FROM audit_evaluation WHERE trace_id=? ORDER BY seq DESC LIMIT 1',(str(trace_id),)).fetchone()
def append_evaluation(db,trace_id,source_id,version,body,status,reason):
    """Append-only evaluation history; every evidence version gets its own seq row."""
    import json
    prev=latest_evaluation(db,trace_id)
    seq=(prev['seq']+1) if prev else 1
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    db.execute('INSERT INTO audit_evaluation(trace_id,seq,source_id,version,body,status,reason) VALUES (?,?,?,?,?,?,?)',
               (str(trace_id),seq,source_id,version,encoded,status,reason))
    if prev and prev['status']!=status:
        db.execute('INSERT INTO evidence_change(trace_id,old_status,new_status,source_id,detail) VALUES (?,?,?,?,?)',
                   (str(trace_id),prev['status'],status,source_id,f'{prev["reason"]} -> {reason}'))
    elif not prev:
        db.execute('INSERT INTO evidence_change(trace_id,old_status,new_status,source_id,detail) VALUES (?,?,?,?,?)',
                   (str(trace_id),None,status,source_id,f'initial:{reason}'))
    return seq
def record_case_change(db,trace_id,old_status,new_status,source_id,detail):
    db.execute('INSERT INTO evidence_change(trace_id,old_status,new_status,source_id,detail) VALUES (?,?,?,?,?)',
               (str(trace_id),old_status,new_status,source_id,detail))
def save_case(db,key,source_id,version,body,status,reason):
    """案件随证据版本更新：正文允许变化（每次证据版本本身留痕），不触发身份冲突。"""
    import json
    encoded=json.dumps(body,sort_keys=True,ensure_ascii=False)
    old=db.execute('SELECT * FROM incident_case WHERE id=?',(key,)).fetchone()
    if old and old['body']!=encoded:
        db.execute('INSERT INTO issue(source_id,code,detail) VALUES (?,?,?)',
                   (source_id,'case_body_updated',f'incident_case:{key}'))
    db.execute('''INSERT INTO incident_case(id,source_id,version,body,status,reason)
                  VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                  source_id=excluded.source_id,version=excluded.version,
                  body=excluded.body,status=excluded.status,reason=excluded.reason''',
               (key,source_id,version,encoded,status,reason))
