"""Ledger report plus an internal, reviewer-authorized evidence read-back."""
import hashlib
import json
from pathlib import Path

from .store import connect, TABLES


def _rows(db, table):
    return [dict(row) for row in db.execute(f'SELECT * FROM {table} ORDER BY id')]


def build(path, out):
    db = connect(path)
    try:
        sources = []
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            entry = dict(item)
            entry.pop('body')
            entry['verified'] = hashlib.sha256(item['body']).hexdigest() == item['hash']
            sources.append(entry)
        data = {'sources': sources, 'entities': {},
                'issues': [dict(x) for x in db.execute('SELECT * FROM issue')],
                'decisions': [dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            data['entities'][table] = _rows(db, table)
        # Audience-facing answers are read back explicitly: did each generated
        # fixture leak, and why did the external sample lose information?
        data['privacy'] = {
            'authorizations': _rows(db, 'privacy_authorization'),
            'leak_checks': _rows(db, 'privacy_leak_check'),
            'information_loss': _information_loss(db),
            'external_plan': _external_plan(db),
        }
        out = Path(out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return out
    finally:
        db.close()


def _external_plan(db):
    for row in db.execute("SELECT * FROM privacy_entity WHERE id='plan:external'"):
        return json.loads(row['body'])
    return None


def _information_loss(db):
    loss = {}
    for row in db.execute('SELECT * FROM privacy_fixture ORDER BY id'):
        body = json.loads(row['body'])
        items = body.get('information_loss') or []
        if items:
            loss[row['id']] = items
    return loss


def review(path, audience=None, project=None):
    """Local-only evidence read-back for an authorized reviewer.

    Returns the original fragment and the redacted fragment for every generated
    fixture row, together with the source cue index.  This view must never be
    used to generate the external JSON: :func:`build` deliberately omits the
    raw fragments.
    """
    db = connect(path)
    try:
        rows = []
        for fixture in db.execute('SELECT * FROM privacy_fixture ORDER BY id'):
            body = json.loads(fixture['body'])
            if audience and body.get('audience') != audience:
                continue
            found = False
            for source in db.execute('SELECT * FROM privacy_source ORDER BY id'):
                source_body = json.loads(source['body'])
                if str(source_body.get('id')) != str(body.get('source')):
                    continue
                found = True
                occurrences = source_body.get('occurrences', [])
                rows.append({
                    'fixture': fixture['id'],
                    'source': source_body.get('id'),
                    'cue_index': source_body.get('cue_index'),
                    'audience': body.get('audience'),
                    'original': source_body.get('text'),
                    'redacted': body.get('text'),
                    'fragments': [{'raw': o['raw'], 'kind': o['kind'],
                                   'cue': o['cue'], 'segment': o['segment'],
                                   'start': o['start'], 'end': o['end'],
                                   'subject': o.get('subject'),
                                   'source': o.get('source')}
                                  for o in occurrences],
                    'loss': body.get('information_loss') or [],
                    'publish': body.get('publish'),
                })
            if not found:
                rows.append({'fixture': fixture['id'], 'source': body.get('source'),
                             'cue_index': None, 'audience': body.get('audience'),
                             'original': None, 'redacted': body.get('text'),
                             'fragments': [], 'loss': body.get('information_loss') or [],
                             'publish': body.get('publish')})
        return rows
    finally:
        db.close()
