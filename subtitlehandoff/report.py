"""Evidence report over the SQLite ledger.

In addition to the raw entities, the report answers the operational question
the desk actually asks: *why* was a job delayed or missed, which phase and
which license lease were in the way, and what did execution do differently
from the frozen plan.
"""
import hashlib, json
from pathlib import Path
from .store import connect, TABLES


def _json(row, field='body'):
    try:
        return json.loads(row[field])
    except (TypeError, ValueError):
        return None


def _plan_views(db):
    plans = []
    for row in db.execute("SELECT * FROM export_plan ORDER BY id"):
        item = dict(row)
        plan = _json(row) or {}
        queue_id = item['id']
        jobs = []
        for job_row in db.execute("SELECT * FROM plan_job WHERE id LIKE ? ORDER BY id",
                                  (f'plan:{queue_id}:%',)):
            job = _json(job_row) or {}
            phases = [_json(x) for x in db.execute(
                "SELECT * FROM plan_phase WHERE id LIKE ? ORDER BY id",
                (f'plan:{queue_id}:{job.get("id")}:%',))]
            leases = [_json(x) for x in db.execute(
                "SELECT * FROM license_lease WHERE id LIKE ? ORDER BY id",
                (f'lease:{queue_id}:%',))]
            jobs.append({'id': job.get('id'), 'status': job_row['status'],
                         'reason': job_row['reason'], 'project': job.get('project'),
                         'hard': job.get('hard'), 'deadline': job.get('deadline'),
                         'target': job.get('target'), 'weight': job.get('weight'),
                         'planned': job.get('planned'),
                         'resume_from': job.get('resume_from'),
                         'resume_state': job.get('resume_state'),
                         'phases': [p for p in phases if p and p.get('job') == job.get('id')],
                         'license_leases': [l for l in leases
                                            if l and l.get('job') == job.get('id')]})
        plans.append({'queue_id': queue_id, 'status': item['status'],
                      'reason': item['reason'], 'contract': plan.get('contract'),
                      'revision': plan.get('revision'), 'order': plan.get('order'),
                      'objective': plan.get('objective'),
                      'unscheduled': plan.get('unscheduled'),
                      'carried': plan.get('carried'), 'jobs': jobs,
                      'license': plan.get('license')})
    return plans


def _bottlenecks(db):
    out = []
    for row in db.execute("SELECT * FROM bottleneck ORDER BY id"):
        body = _json(row) or {}
        body.update({'id': row['id'], 'status': row['status'], 'reason': row['reason']})
        out.append(body)
    return out


def _execution(db):
    return {
        'diffs': [dict(row, body=_json(row)) for row in
                  db.execute("SELECT * FROM execution_diff ORDER BY id")],
        'reassignments': [dict(row, body=_json(row)) for row in
                          db.execute("SELECT * FROM reassignment ORDER BY id")],
        'events': [dict(row, body=_json(row)) for row in
                   db.execute("SELECT * FROM execution_event ORDER BY id")],
    }


def explain(path, job_id=None, queue_id=None):
    """Return why each requested job was delayed, unscheduled or moved."""
    db = connect(path)
    try:
        def matches(item):
            if job_id is not None and str(item.get('job')) != str(job_id):
                return False
            if queue_id is not None and str(item.get('queue_id')) != str(queue_id):
                return False
            return True

        def collect(table):
            return [(dict(row), _json(row) or {}) for row in
                    db.execute(f'SELECT * FROM {table} ORDER BY id')]

        bottlenecks = [body for _, body in collect('bottleneck') if matches(body)]
        diffs = [body for _, body in collect('execution_diff') if matches(body)]
        reassigned = [body for _, body in collect('reassignment') if matches(body)]
        return {'job': job_id, 'queue_id': queue_id,
                'bottlenecks': bottlenecks, 'diffs': diffs,
                'reassignments': reassigned}
    finally:
        db.close()


def build(path, out):
    db = connect(path)
    try:
        sources = []
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x = dict(item)
            x.pop('body')
            x['verified'] = hashlib.sha256(item['body']).hexdigest() == item['hash']
            sources.append(x)
        data = {'sources': sources, 'entities': {},
                'issues': [dict(x) for x in db.execute('SELECT * FROM issue')],
                'decisions': [dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            data['entities'][table] = [dict(x) for x in
                                       db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['plans'] = _plan_views(db)
        data['bottlenecks'] = _bottlenecks(db)
        data['execution'] = _execution(db)
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return out
    finally:
        db.close()
