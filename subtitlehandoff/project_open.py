"""Check whether a local project lists only installed plugin names."""
import json
from .ingest import capture
from .store import connect, save, decide


def check(path, file):
    source, payload = capture(path, 'desktop_project_requirements', file)
    db = connect(path)
    results = []
    try:
        with db:
            installed = {json.loads(x['body'])['name'] for x in db.execute('SELECT body FROM plugin_inventory')}
            for row in payload.get('rows', []):
                key = str(row['id'])
                if set(row.get('required_plugins', [])).issubset(installed):
                    status, reason = '可打开', 'plugin_names_present'
                else:
                    status, reason = '待安装', 'plugin_name_missing'
                save(db, 'project_open', key, source, payload['version'], row, status, reason)
                results.append(decide(db, '工程打开核验', key, status, reason, source))
        return results
    finally:
        db.close()
