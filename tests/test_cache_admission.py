"""Acceptance tests for executable preview-cache cleanup and task admission."""
import json
from pathlib import Path

from subtitlehandoff import cache_inventory, cache_prune, report
from subtitlehandoff.store import connect
from tests.cache_fixtures import (write_json, write_file, cache_row,
                                  acceptance_fixture, c_task, prune_request)


def _import(tmp_path, manifest, name='inventory.json'):
    return cache_inventory.import_cache(tmp_path / 'ledger.db',
                                        write_json(tmp_path, name, manifest))


# --- shared parent asset -----------------------------------------------------

def test_shared_parent_eviction_is_blocked_by_active_reference(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req = prune_request('R-online', 'online_review', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root)], execute=False)
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is True
    evicted = [s['subject'] for s in result['steps'] if s['kind'] == 'evict']
    # Active reference on WA protects WA, its composite CA and ancestor P.
    assert 'WA' not in evicted and 'CA' not in evicted and 'P' not in evicted
    # The old scene B (WB, CB) is the evictable material, not A.
    assert set(evicted).issubset({'WB', 'CB'})
    # Peak must respect the 30MB transient workspace (70 kept + 30 temp = 100).
    assert result['peak_mb'] <= 100
    assert all(s['kind'] in ('evict', 'generate') for s in result['steps'])


def test_dependency_ordering_children_before_parent(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path, active_a=False)
    _import(tmp_path, manifest)
    req = prune_request('R-night', 'night_batch', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root, deadline=None)],
                        execute=False)
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is True
    evict_order = [s['subject'] for s in result['steps'] if s['kind'] == 'evict']
    # A child may never be removed while it still depends on a present parent;
    # equivalently WA/WB/CB/CA must be gone before P can be removed.
    present = set(evict_order)
    for child, parent in (('WA', 'P'), ('WB', 'P'), ('CA', 'WA'), ('CB', 'WB')):
        if child in evict_order and parent in evict_order:
            assert evict_order.index(child) < evict_order.index(parent)
    # Parents removed before a task generates would invalidate the task input.
    gen_index = next(i for i, s in enumerate(result['steps']) if s['kind'] == 'generate')
    assert all(s['subject'] != 'P' for s in result['steps'][:gen_index])


# --- same content, different names: bytes counted once -----------------------

def test_same_content_copies_share_physical_identity_and_bytes(tmp_path):
    cache_root = tmp_path / 'cache'
    cache_root.mkdir()
    rows = [
        cache_row('D1', 'thumbs/x.thumb', 15, cache_root, physical_id='phys:SAME'),
        cache_row('D2', 'thumbs/y.thumb', 15, cache_root, physical_id='phys:SAME'),
    ]
    _import(tmp_path, {'version': 'v2', 'cache_root': str(cache_root), 'rows': rows})
    req = {'version': 'v2', 'request_id': 'R-dup', 'mode': 'night_batch',
           'at': '2026-01-01T12:00:00', 'cache_root': str(cache_root),
           'evict_root': str(tmp_path / 'retired'), 'quota_mb': 0,
           'protected': [], 'tasks': [], 'execute': False}
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    # 30MB of named records are only 15MB of physical content: both copies must
    # go before the 0MB quota is met, and the two steps share one identity.
    evicted = [s for s in result['steps'] if s['kind'] == 'evict']
    assert {s['subject'] for s in evicted} == {'D1', 'D2'}
    assert len({s['physical_id'] for s in evicted}) == 1
    # Peak is the highest occupancy seen along the sequence (start 15MB),
    # while the terminal occupancy after both copies leave is 0MB.
    assert result['peak_mb'] == 15.0


def test_same_content_release_bytes_booked_once_on_execution(tmp_path):
    cache_root = tmp_path / 'cache'
    cache_root.mkdir()
    rows = [
        cache_row('D1', 'thumbs/x.thumb', 15, cache_root, physical_id='phys:SAME'),
        cache_row('D2', 'thumbs/y.thumb', 15, cache_root, physical_id='phys:SAME'),
    ]
    _import(tmp_path, {'version': 'v2', 'cache_root': str(cache_root), 'rows': rows})
    req = {'version': 'v2', 'request_id': 'R-dup-exec', 'mode': 'night_batch',
           'at': '2026-01-01T12:00:00', 'cache_root': str(cache_root),
           'evict_root': str(tmp_path / 'retired'), 'quota_mb': 0,
           'protected': [], 'tasks': [], 'execute': True}
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['status'] == cache_prune.EXECUTED
    db = connect(tmp_path / 'ledger.db')
    releases = db.execute('SELECT * FROM cache_release ORDER BY id').fetchall()
    # Two named copies moved, but they are one physical identity: the first
    # move frees zero bytes (twin still present), only the last disappearance
    # books real bytes, so total released bytes equals one file, never twice.
    assert len(releases) == 2
    assert {r['physical_id'] for r in releases} == {'phys:SAME'}
    assert sorted(r['bytes'] for r in releases) == [0, len(b'content-of-D1')]
    assert sum(r['bytes'] for r in releases) == len(b'content-of-D1')
    db.close()


# --- transient peak can block even when the final set fits -------------------

def test_transient_peak_shortfall_reports_minimum_missing_mb(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    task = c_task(tmp_path, cache_root, temp=30.0)
    # Online review must keep A (40) and P (20); B (40) is the only evictable
    # content.  With a 60MB quota, kept 60 + temp 30 = 90 > 60: missing 30MB,
    # even though the final retained set (60 + 20 output) fits in 60? It does
    # not, but the binding constraint reported is the transient peak.
    req = prune_request('R-tight', 'online_review', tmp_path, cache_root, [task])
    req['quota_mb'] = 60
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is False
    assert result['reason'] == 'peak_quota_exceeded'
    assert result['shortfall_mb'] == 30.0
    assert result['blocking_step']['kind'] == 'generate'
    assert result['blocking_step']['subject'] == 'TASK-C'


# --- two workstation purposes over the same evidence -------------------------

def test_online_review_finds_path_that_keeps_scene_a(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req = prune_request('R-online', 'online_review', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root)])
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is True
    assert result['peak_mb'] == 100.0
    evicted = {s['subject'] for s in result['steps'] if s['kind'] == 'evict'}
    assert evicted == {'WB', 'CB'}
    # Three-minute watchability window is part of the admission evidence.
    admission = connect(tmp_path / 'ledger.db').execute(
        "SELECT * FROM cache_task_admission WHERE task_id='TASK-C'").fetchone()
    assert admission['status'] == cache_prune.STARTABLE
    assert admission['deadline_seconds'] == 180.0


def test_night_batch_may_clear_a_but_keeps_next_batch_input_b(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path, active_a=False)
    _import(tmp_path, manifest)
    task = c_task(tmp_path, cache_root, deadline=None)
    req = prune_request('R-night', 'night_batch', tmp_path, cache_root, [task],
                        protected=['WB', 'CB'])
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is True
    evicted = {s['subject'] for s in result['steps'] if s['kind'] == 'evict'}
    assert {'WA', 'CA'}.issubset(evicted)
    assert not ({'WB', 'CB'} & evicted)
    # P is still needed as the C task input, so it cannot be evicted first.
    gen_index = next(i for i, s in enumerate(result['steps']) if s['kind'] == 'generate')
    assert all(s['subject'] != 'P' for s in result['steps'][:gen_index])


def test_online_path_blocks_when_b_is_also_protected(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req = prune_request('R-blocked', 'online_review', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root)],
                        protected=['WB', 'CB'])
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    # Everything is pinned (A by reference, B by protection, P as parent):
    # 100 present + 30 transient = 130, quota 100 -> at least 30MB short.
    assert result['startable'] is False
    assert result['shortfall_mb'] == 30.0
    assert result['status'] == cache_prune.BLOCKED


def test_three_minute_deadline_blocks_too_slow_recompute(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path, active_a=False)
    _import(tmp_path, manifest)
    # Forcing P out means C cannot generate from its input; instead make C's
    # own recompute exceed the 180s online window.
    task = c_task(tmp_path, cache_root, gen_cost=240.0)
    req = prune_request('R-slow', 'online_review', tmp_path, cache_root, [task],
                        protected=['WB', 'CB', 'WA', 'CA', 'P'])
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is False
    assert result['reason'] == 'recompute_deadline_unmet'


# --- execution: verification, real move, release evidence, durable archive ---

def test_execution_moves_files_and_records_real_release(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req = prune_request('R-exec', 'online_review', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root)], execute=True)
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['status'] == cache_prune.EXECUTED
    # B's files physically left the cache root and landed in the retired root.
    assert not (cache_root / 'wave' / 'B.wave').exists()
    assert not (cache_root / 'composite' / 'B.comp').exists()
    retired = list((tmp_path / 'retired').rglob('*'))
    assert any(p.is_file() for p in retired)
    # The kept A scene and shared parent remain untouched in place.
    assert (cache_root / 'wave' / 'A.wave').exists()
    assert (cache_root / 'decode' / 'P.parent').exists()
    db = connect(tmp_path / 'ledger.db')
    rows = {r['id']: r['status'] for r in db.execute('SELECT id,status FROM preview_cache')}
    assert rows['WB'] == '已淘汰' and rows['WA'] == '已缓存'
    releases = db.execute('SELECT bytes,path,moved_to FROM cache_release').fetchall()
    assert len(releases) == 2 and all(r['bytes'] > 0 for r in releases)
    db.close()
    # The durable archive outside the root still exists and was never targeted.
    assert (tmp_path / 'engineering-archive' / 'durable' / 'ARCHIVE.eng').exists()


def test_replay_does_not_double_count_released_space(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req_file = write_json(tmp_path, 'prune.json',
                          prune_request('R-replay', 'online_review', tmp_path,
                                        cache_root, [c_task(tmp_path, cache_root)],
                                        execute=True))
    first = cache_prune.prune(tmp_path / 'ledger.db', req_file)
    assert first['status'] == cache_prune.EXECUTED
    second = cache_prune.prune(tmp_path / 'ledger.db', req_file)
    assert second['replayed'] is True
    assert second['status'] == cache_prune.EXECUTED
    db = connect(tmp_path / 'ledger.db')
    assert db.execute('SELECT COUNT(*) FROM cache_release').fetchone()[0] == 2
    assert db.execute("SELECT COUNT(*) FROM cache_plan WHERE id='R-replay'").fetchone()[0] == 1
    db.close()


def test_external_rewrite_stops_step_and_replans_from_survivors(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req_file = write_json(tmp_path, 'prune.json',
                          prune_request('R-rewrite', 'online_review', tmp_path,
                                        cache_root, [c_task(tmp_path, cache_root)],
                                        execute=True))

    def tamper_generator(task, target):  # pragma: no cover - generator still runs
        from tests.cache_fixtures import write_file as wf
        return wf(cache_root, 'composite/C.comp', 'generated')

    # Rewrite the second eviction target between plan and execution: the first
    # target (CB) still moves, then the WB digest check fails.  The surviving
    # tampered file keeps occupying bytes but can no longer be trusted/moved.
    (cache_root / 'wave' / 'B.wave').write_bytes(b'externally-rewritten-bytes')
    result = cache_prune.prune(tmp_path / 'ledger.db', req_file,
                               generator=tamper_generator)
    assert result['status'] == cache_prune.INTERRUPTED
    assert result['reason'].startswith('replan_blocked:')
    # CB already left (80MB present: WA20+CA20+P20+WB20); generating C needs
    # another 30MB transient -> 110MB peak, 10MB over the 100MB quota.
    assert result['shortfall_mb'] == 10.0
    db = connect(tmp_path / 'ledger.db')
    issues = {r['code'] for r in db.execute('SELECT DISTINCT code FROM issue')}
    assert 'cache_file_rewritten' in issues
    # The cleanly moved CB is booked; the tampered WB is never booked as clean.
    assert db.execute("SELECT * FROM cache_release WHERE path LIKE '%B.wave%'").fetchall() == []
    cb = db.execute("SELECT * FROM cache_release WHERE path LIKE '%B.comp%'").fetchall()
    assert len(cb) == 1 and cb[0]['bytes'] > 0
    db.close()


def test_new_occupancy_inside_cache_root_blocks_that_step(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    # An undeclared file appears under the cache root before execution.
    write_file(cache_root, 'surprise/new.bin', 'unaccounted-occupancy')
    req = prune_request('R-newocc', 'online_review', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root)], execute=True)
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['status'] == cache_prune.INTERRUPTED
    assert result['reason'] == 'new_occupancy_detected'


def test_move_failure_interrupts_and_keeps_ledger_consistent(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req = prune_request('R-movefail', 'online_review', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root)], execute=True)

    def failing_move(src, dst):
        raise OSError('simulated move failure')

    original_move = cache_prune.shutil.move
    cache_prune.shutil.move = failing_move
    try:
        result = cache_prune.prune(tmp_path / 'ledger.db',
                                   write_json(tmp_path, 'prune.json', req))
    finally:
        cache_prune.shutil.move = original_move
    assert result['status'] == cache_prune.INTERRUPTED
    db = connect(tmp_path / 'ledger.db')
    # Nothing was booked as released because nothing actually moved.
    assert db.execute('SELECT COUNT(*) FROM cache_release').fetchone()[0] == 0
    assert (cache_root / 'wave' / 'B.wave').exists()
    db.close()


# --- shortest recompute: an already cached output needs zero work ------------

def test_already_cached_output_prefers_zero_recompute_path(tmp_path):
    cache_root = tmp_path / 'cache'
    cache_root.mkdir()
    # VC (the next task output) already exists in the cache; evicting anything
    # else must never be chosen over simply starting.
    rows = [
        cache_row('P', 'decode/P.parent', 10, cache_root, gen_cost=60),
        cache_row('VC', 'composite/C.comp', 10, cache_root,
                  depends_on=['P'], gen_cost=90),
        cache_row('J', 'junk/J.old', 50, cache_root, gen_cost=30),
    ]
    _import(tmp_path, {'version': 'v2', 'cache_root': str(cache_root), 'rows': rows})
    task = {'id': 'TASK-C', 'start_at': '2026-01-01T12:00:00', 'inputs': ['P'],
            'temp_mb': 0, 'gen_cost_seconds': 90, 'deadline_seconds': 180,
            'output': {'id': 'VC', 'path': 'composite/C.comp', 'size_mb': 10,
                       'depends_on': ['P']}}
    req = prune_request('R-pre', 'online_review', tmp_path, cache_root, [task])
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is True
    assert result['recompute_seconds'] == 0.0
    assert [s for s in result['steps'] if s['kind'] == 'generate'] == []
    # P and the satisfied output VC are pinned; only junk is evictable.
    evicted = {s['subject'] for s in result['steps'] if s['kind'] == 'evict'}
    assert evicted <= {'J'}


# --- two preview tasks must honour their start ordering -----------------------

def test_two_tasks_follow_start_order_and_peak_covers_both(tmp_path):
    cache_root = tmp_path / 'cache'
    cache_root.mkdir()
    rows = [cache_row('P', 'decode/P.parent', 40, cache_root, gen_cost=60),
            cache_row('J', 'junk/J.old', 50, cache_root, gen_cost=30)]
    _import(tmp_path, {'version': 'v2', 'cache_root': str(cache_root), 'rows': rows})
    t1 = {'id': 'T1', 'start_at': '2026-01-01T12:00:00', 'inputs': ['P'],
          'temp_mb': 10, 'gen_cost_seconds': 20, 'deadline_seconds': 180,
          'output': {'id': 'V1', 'path': 'o/V1.pre', 'size_mb': 5, 'depends_on': ['P']}}
    t2 = {'id': 'T2', 'start_at': '2026-01-01T13:00:00', 'inputs': ['P'],
          'temp_mb': 10, 'gen_cost_seconds': 20, 'deadline_seconds': 180,
          'output': {'id': 'V2', 'path': 'o/V2.pre', 'size_mb': 5, 'depends_on': ['P']}}
    req = prune_request('R-two', 'online_review', tmp_path, cache_root, [t2, t1])
    req['quota_mb'] = 90
    result = cache_prune.prune(tmp_path / 'ledger.db',
                               write_json(tmp_path, 'prune.json', req))
    assert result['startable'] is True
    order = [s['subject'] for s in result['steps'] if s['kind'] == 'generate']
    assert order == ['T1', 'T2']
    # Peak covers the whole sequence: start at 90MB.  During generation the
    # transient workspace adds 10MB on top of P40 and prior outputs:
    # 50MB at T1 (40+10), 55MB at T2 (40+5+10); temp is released afterwards.
    assert result['peak_mb'] == 90.0
    gen_steps = [s for s in result['steps'] if s['kind'] == 'generate']
    assert [s['peak_mb'] for s in gen_steps] == [50.0, 55.0]
    # A quota of 50 makes even the best ordering infeasible and names the gap.
    req['request_id'] = 'R-two-blocked'
    req['quota_mb'] = 50
    blocked = cache_prune.prune(tmp_path / 'ledger.db',
                                write_json(tmp_path, 'prune2.json', req))
    assert blocked['startable'] is False
    assert blocked['reason'] == 'peak_quota_exceeded'
    assert blocked['shortfall_mb'] == 5.0


# --- a replan preserves the steps already executed ----------------------------

def test_replan_keeps_steps_completed_before_the_missing_file(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req_file = write_json(tmp_path, 'prune.json',
                          prune_request('R-carry', 'online_review', tmp_path,
                                        cache_root, [c_task(tmp_path, cache_root)],
                                        execute=True))
    # The first planned eviction (CB) passes its pre-flight check and moves;
    # before the second step WB vanishes (external process took it), forcing a
    # replan that must still carry CB's completed release evidence.
    original_scan = cache_prune._scan_cache
    calls = {'n': 0}

    def scan_with_wb_disappearing(root):
        calls['n'] += 1
        found = original_scan(root)
        if calls['n'] >= 2:
            found.pop(str((cache_root / 'wave' / 'B.wave').resolve()), None)
        return found

    cache_prune._scan_cache = scan_with_wb_disappearing
    try:
        result = cache_prune.prune(tmp_path / 'ledger.db', req_file)
    finally:
        cache_prune._scan_cache = original_scan
    assert result['status'] == cache_prune.EXECUTED
    # CB's completed eviction is carried through the replan, not re-executed.
    assert any(s['kind'] == 'evict' and s['subject'] == 'CB'
               and s.get('released_bytes', 0) > 0 for s in result['steps'])
    db = connect(tmp_path / 'ledger.db')
    cb = db.execute("SELECT * FROM cache_release WHERE path LIKE '%B.comp%'").fetchall()
    assert len(cb) == 1
    assert db.execute("SELECT status FROM preview_cache WHERE id='CB'").fetchone()[0] == '已淘汰'
    db.close()


# --- report traceability ------------------------------------------------------

def test_report_reads_back_full_admission_rationale(tmp_path):
    cache_root, manifest = acceptance_fixture(tmp_path)
    _import(tmp_path, manifest)
    req = prune_request('R-report', 'online_review', tmp_path, cache_root,
                        [c_task(tmp_path, cache_root)], execute=True)
    cache_prune.prune(tmp_path / 'ledger.db',
                      write_json(tmp_path, 'prune.json', req))
    out = report.build(tmp_path / 'ledger.db', tmp_path / 'report.json')
    data = json.loads(Path(out).read_text(encoding='utf-8'))
    trace = next(t for t in data['admissions'] if t['request_id'] == 'R-report')
    assert trace['status'] == cache_prune.EXECUTED
    assert trace['peak_mb'] == 100.0
    assert trace['active_references'][0]['target_id'] == 'WA'
    kinds = [s['kind'] for s in trace['planned_steps']]
    assert 'evict' in kinds and 'generate' in kinds
    assert len(trace['release_evidence']) == 2
    assert trace['task_admissions'][0]['task_id'] == 'TASK-C'
    # Physical identities and dependencies are themselves persisted entities.
    assert data['entities']['cache_physical']
    assert {r['child_id'] for r in data['entities']['cache_dependency']} >= {'WA', 'CA'}


# --- legacy behaviour stays intact --------------------------------------------

def test_legacy_record_only_prune_still_planned(tmp_path):
    db = tmp_path / 'ledger.db'
    cache = {'version': 'v1', 'rows': [
        {'id': 'C1', 'size_mb': 10, 'last_used': '2026-01-01T09:00:00'},
        {'id': 'C2', 'size_mb': 10, 'last_used': '2026-01-01T10:00:00'}]}
    cache_inventory.import_cache(db, write_json(tmp_path, 'cache.json', cache))
    request = {'version': 'v1', 'request_id': 'R1', 'quota_mb': 10}
    result = cache_prune.prune(db, write_json(tmp_path, 'prune.json', request))
    assert result['status'] == '已规划'
