"""Build the offline synthetic cache-prune evidence pack.

Run from the repository root::

    python3 -I examples/cache_prune/build_examples.py

Everything is synthetic and local: small placeholder files stand in for wave,
thumbnail and composite previews.  The durable engineering archive is written
*outside* the cache root on purpose, to demonstrate it is never a cleanup
target.  The pack contains:

* ``cache/``                    physical preview files (A scene, B scene, parent)
* ``engineering-archive/``      durable archive outside the cache root
* ``inventory.json``            v2 cache inventory with path/digest/identity
* ``prune-online.json``         online review request (keeps scene A, 3 min SLA)
* ``prune-night.json``          night batch request (B is next batch input)
"""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode('utf-8'))
    return path


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    cache = ROOT / 'cache'
    spec = [
        ('P', 'decode/parent.decode', 20, [], 60),
        ('WA', 'wave/scene-A.wave', 20, ['P'], 90),
        ('CA', 'composite/scene-A.comp', 20, ['P', 'WA'], 90),
        ('WB', 'wave/scene-B.wave', 20, ['P'], 90),
        ('CB', 'composite/scene-B.comp', 20, ['P', 'WB'], 90),
    ]
    rows = []
    for cid, rel, size, deps, cost in spec:
        path = write(cache / rel, f'synthetic-preview-bytes:{cid}')
        rows.append({'id': cid, 'path': rel, 'sha256': sha(path),
                     'size_mb': size, 'last_used': '2026-01-01T08:00:00',
                     'depends_on': deps, 'gen_cost_seconds': cost})
    # Two named thumbnails of the same physical content: bytes count once.
    thumb = write(cache / 'thumbs/frame-001.thumb', b'shared-thumbnail-bytes')
    for rel in ('thumbs/frame-001-copy.thumb',):
        write(cache / rel, b'shared-thumbnail-bytes')
    thumb_hash = sha(thumb)
    rows += [
        {'id': 'T1', 'path': 'thumbs/frame-001.thumb', 'sha256': thumb_hash,
         'size_mb': 0, 'last_used': '2026-01-01T08:00:00',
         'physical_id': 'phys:shared-thumb', 'depends_on': [], 'gen_cost_seconds': 10},
        {'id': 'T2', 'path': 'thumbs/frame-001-copy.thumb', 'sha256': thumb_hash,
         'size_mb': 0, 'last_used': '2026-01-01T08:00:00',
         'physical_id': 'phys:shared-thumb', 'depends_on': [], 'gen_cost_seconds': 10},
    ]
    archive = write(ROOT / 'engineering-archive' / 'durable' / 'SCENE-A.eng',
                    b'durable-engineering-archive-never-evicted')
    inventory = {
        'version': 'v2', 'cache_root': str(cache), 'rows': rows,
        'references': [{'id': 'REF-WAVE-A', 'target_id': 'WA',
                        'session': 'live-review-2026-01-01',
                        'active_from': '2026-01-01T00:00:00',
                        'active_until': '2026-01-02T00:00:00'}],
        'outside_cache': [str(archive)]}
    write(ROOT / 'inventory.json', json.dumps(inventory, ensure_ascii=False, indent=2))
    # The night batch runs after the review session ends: the same physical
    # evidence is re-captured without an active reference.
    night_inventory = dict(inventory, references=[])
    write(ROOT / 'inventory-night.json',
          json.dumps(night_inventory, ensure_ascii=False, indent=2))

    task_c = {'id': 'TASK-C', 'start_at': '2026-01-01T12:00:00', 'inputs': ['P'],
              'temp_mb': 30, 'gen_cost_seconds': 90, 'deadline_seconds': 180,
              'output': {'id': 'VC', 'path': 'composite/scene-C.comp',
                         'size_mb': 20, 'depends_on': ['P']}}
    common = {'version': 'v2', 'cache_root': str(cache),
              'evict_root': str(ROOT / 'retired'), 'quota_mb': 100,
              'at': '2026-01-01T12:00:00'}
    online = dict(common, request_id='REQ-ONLINE', mode='online_review',
                  protected=[], tasks=[task_c], execute=True)
    night_task = dict(task_c, deadline_seconds=None)
    night = dict(common, request_id='REQ-NIGHT', mode='night_batch',
                 protected=['WB', 'CB'], tasks=[night_task], execute=False)
    write(ROOT / 'prune-online.json', json.dumps(online, ensure_ascii=False, indent=2))
    write(ROOT / 'prune-night.json', json.dumps(night, ensure_ascii=False, indent=2))
    print('wrote', ROOT)


if __name__ == '__main__':
    main()
