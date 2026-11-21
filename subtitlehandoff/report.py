import hashlib,json
from pathlib import Path
from .store import connect,TABLES
def build(path,out):
    db=connect(path)
    try:
        with db:
            sources=[]
            for item in db.execute('SELECT * FROM export_file ORDER BY id'):
                x=dict(item);x.pop('body')
                x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
                sources.append(x)
            data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
                  'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')]}
            for table in TABLES:
                data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
            data['duty_audit']=_duty_audit(db)
            data['evidence_changes']=[dict(x) for x in db.execute(
                'SELECT * FROM evidence_change ORDER BY id')]
            data['evaluation_history']=[{k:r[k] for k in
                ('id','trace_id','seq','version','status','reason','created')}
                for r in db.execute('SELECT * FROM audit_evaluation ORDER BY trace_id,seq')]
            encoded=json.dumps(data,ensure_ascii=False,indent=2)
            requested=Path(out);requested.parent.mkdir(parents=True,exist_ok=True)
            digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            # 快照只按原始请求路径跟踪；已签出报告不被原地改写，新内容另存带序号版本
            snapshots=db.execute('SELECT id,hash FROM report_snapshot WHERE path=? ORDER BY id',
                                 (str(requested),)).fetchall()
            latest_hash=snapshots[-1]['hash'] if snapshots else None
            out_path=requested
            if latest_hash is not None and latest_hash != digest and requested.exists():
                stem=requested.stem;suffix=requested.suffix
                out_path=requested.with_name(f'{stem}.rev{len(snapshots)+1}{suffix}')
                encoded=json.dumps(data,ensure_ascii=False,indent=2)
                digest=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
            out_path.write_text(encoded,encoding='utf-8')
            db.execute('''INSERT OR IGNORE INTO report_snapshot(path,hash,source_count)
                          VALUES (?,?,?)''',
                       (str(requested),digest,len(sources)))
            return out_path
    finally:db.close()


def _duty_audit(db):
    """回读确定违规的见证，或待查案件的最小缺证；已排除案件给出核证链摘要。"""
    audit=[]
    for row in db.execute('SELECT * FROM audit_evaluation ORDER BY trace_id,seq'):
        body=json.loads(row['body'])
        entry={'trace_id':row['trace_id'],'seq':row['seq'],'status':row['status'],
               'reason':row['reason'],'regime':body.get('regime'),
               'possible_chains':body.get('possible_chains',0),
               'compliant_chains':body.get('compliant_chains',0),
               'violating_chains':body.get('violating_chains',0),
               'indeterminate_chains':body.get('indeterminate_chains',0),
               'export_outcomes':body.get('export_outcomes',[]),
               'attribution':body.get('attribution',{}),
               'notes':body.get('notes',[])}
        if row['status']=='确定违规':
            entry['violation_witnesses']=body.get('witnesses',[])
        elif row['status']=='待调查':
            entry['minimal_missing_evidence']=body.get('missing_evidence',[])
        else:
            entry['verified_chain']={'sessions':body.get('sessions',[]),
                                     'exports':body.get('export_outcomes',[])}
        case=db.execute('SELECT id,status,reason FROM incident_case WHERE id LIKE ? ORDER BY id',
                        (f'%{row["trace_id"]}%',)).fetchall()
        entry['linked_cases']=[dict(x) for x in case]
        audit.append(entry)
    return audit
