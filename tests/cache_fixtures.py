"""Offline fixtures for executable cache-prune acceptance tests.

Everything here is synthetic local data: no network, no real media.  Cache
files live under a per-test cache root; durable archives deliberately live
outside that root and must never become eviction targets.
"""
import hashlib
import json
from pathlib import Path


def write_file(root, rel, content):
    path = Path(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content if isinstance(content, bytes) else str(content).encode('utf-8')
    path.write_bytes(payload)
    return path


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def cache_row(cid, rel, size_mb, root, depends_on=None, gen_cost=0.0,
              physical_id=None, durable=False, last_used='2026-01-01T08:00:00'):
    path = write_file(root, rel, f'content-of-{cid}')
    row = {'id': cid, 'path': rel, 'sha256': digest(path), 'size_mb': size_mb,
           'last_used': last_used, 'depends_on': depends_on or [],
           'gen_cost_seconds': gen_cost}
    if physical_id:
        row['physical_id'] = physical_id
    if durable:
        row['durable'] = True
    return row


def write_json(root, name, obj):
    path = Path(root) / name
    path.write_text(json.dumps(obj, ensure_ascii=False), encoding='utf-8')
    return path


def acceptance_fixture(tmp_path, *, b_second_class=False, active_a=True):
    """A=40MB wave+composite (20+20), B=40MB, shared parent P=20MB, C needs 30MB temp."""
    cache_root = tmp_path / 'cache'
    cache_root.mkdir()
    rows = [
        cache_row('P', 'decode/P.parent', 20, cache_root, gen_cost=60),
        cache_row('WA', 'wave/A.wave', 20, cache_root, depends_on=['P'], gen_cost=90),
        cache_row('CA', 'composite/A.comp', 20, cache_root, depends_on=['P', 'WA'], gen_cost=90),
        cache_row('WB', 'wave/B.wave', 20, cache_root, depends_on=['P'], gen_cost=90),
        cache_row('CB', 'composite/B.comp', 20, cache_root, depends_on=['P', 'WB'], gen_cost=90),
    ]
    references = []
    if active_a:
        references = [{'id': 'REF-A', 'target_id': 'WA', 'session': 'review-A',
                       'active_from': '2026-01-01T00:00:00',
                       'active_until': '2026-01-02T00:00:00'}]
    manifest = {'version': 'v2', 'cache_root': str(cache_root), 'rows': rows,
                'references': references}
    # Durable engineering archive outside the cache root.
    archive_dir = tmp_path / 'engineering-archive'
    archive = write_file(archive_dir, 'durable/ARCHIVE.eng', 'durable-archive-bytes')
    manifest['outside_cache'] = [str(archive)]
    return cache_root, manifest


def c_task(tmp_path, cache_root, *, start='2026-01-01T12:00:00', deadline=180.0,
           temp=30.0, output_size=20.0, gen_cost=90.0):
    return {'id': 'TASK-C', 'start_at': start,
            'inputs': ['P'], 'temp_mb': temp, 'gen_cost_seconds': gen_cost,
            'deadline_seconds': deadline,
            'output': {'id': 'VC', 'path': 'composite/C.comp', 'size_mb': output_size,
                       'depends_on': ['P']}}


def prune_request(request_id, mode, tmp_path, cache_root, tasks, *, execute=False,
                  protected=None, at='2026-01-01T12:00:00'):
    return {'version': 'v2', 'request_id': request_id, 'mode': mode, 'at': at,
            'cache_root': str(cache_root),
            'evict_root': str(tmp_path / 'retired'), 'quota_mb': 100,
            'protected': protected or [], 'tasks': tasks, 'execute': execute}
