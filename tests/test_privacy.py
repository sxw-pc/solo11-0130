import json

import pytest

from subtitlehandoff import privacy_scan, privacy_fixture, report
from subtitlehandoff.store import connect


def write(root, name, data):
    path = root / name
    path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    return path


def authorized(payload, vendor='vendor-a', **extra):
    payload = dict(payload)
    payload['authorization'] = {
        'audiences': [
            {'audience': 'internal', 'purpose': 'internal review',
             'authorized_by': 'lead-internal', 'at': '2026-11-20T09:00:00'},
            {'audience': 'external', 'vendor': vendor, 'purpose': 'supplier review',
             'authorized_by': 'lead-external', 'at': '2026-11-20T09:05:00'},
        ]}
    payload.update(extra)
    return payload


def fixture_id(audience, source, vendor=None):
    if audience == 'internal':
        return f'I-{source}'
    return f'X{(vendor or "vendor-a").split("-")[-1].upper()}-{source}'


def fixture_rows(ids, audience='internal', vendor=None):
    return {'version': 'v1',
            'rows': [{'id': fixture_id(audience, i, vendor), 'source': i} for i in ids]}


def body(db, table, key):
    row = connect(db).execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    return json.loads(row['body']) if row else None


def status(db, table, key):
    row = connect(db).execute(f'SELECT * FROM {table} WHERE id=?', (key,)).fetchone()
    return (row['status'], row['reason']) if row else None


def scan_and_build(tmp_path, payload, audience='external', vendor='vendor-a'):
    db = tmp_path / 'ledger.db'
    source = write(tmp_path, 'source.json', payload)
    ids = [row['id'] for row in payload['rows']]
    fixture = dict(fixture_rows(ids, audience, vendor), audience=audience)
    if audience == 'external':
        fixture['vendor'] = vendor
    privacy_scan.scan(db, source)
    privacy_fixture.build(db, write(tmp_path, f'fixture-{audience}.json', fixture))
    return db


def out(db, audience='external', vendor='vendor-a', source='C1'):
    return body(db, 'privacy_fixture', fixture_id(audience, source, vendor))


# ---------------------------------------------------------------------------
# cross-block entities and masking
# ---------------------------------------------------------------------------

def test_email_split_across_blocks_is_masked_in_both_blocks(tmp_path):
    payload = authorized({'version': 'v1', 'rows': [
        {'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': 'mail alan@'},
        {'id': 'C2', 'scene': 'S1', 'start': 2, 'end': 3, 'text': 'example.com now'}]})
    db = scan_and_build(tmp_path, payload)
    first, second = out(db, source='C1'), out(db, source='C2')
    assert 'alan@' not in first['text'] and 'example.com' not in first['text']
    assert 'example.com' not in second['text']
    assert first['text'].count('[REDACTED]') == 1
    assert second['text'].count('[REDACTED]') == 1
    source = body(db, 'privacy_source', 'C2')
    assert source['findings'] == ['alan@example.com']
    assert {span['cue'] for span in source['entity_spans']['alan@example.com']} == {'C1', 'C2'}


def test_fragments_are_not_joined_across_non_adjacent_blocks(tmp_path):
    # C1 and C3 are in the same scene but not adjacent in the export: C2 (a
    # different scene) sits between them.  "alan@" and "example.com" must not
    # be reconstructed into one identifier by skipping over C2.
    payload = authorized({'version': 'v1', 'rows': [
        {'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': 'alan@'},
        {'id': 'C2', 'scene': 'S2', 'start': 2, 'end': 3, 'text': '别的东西'},
        {'id': 'C3', 'scene': 'S1', 'start': 3, 'end': 4, 'text': 'example.com'}]})
    db = scan_and_build(tmp_path, payload)
    for key in ('C1', 'C3'):
        assert not any(o['kind'] == 'email'
                       for o in body(db, 'privacy_source', key)['occurrences'])


def test_email_and_persona_in_same_scene_join_one_subject_and_both_masked(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': 'A', 'persona_names': ['阿岚', 'alan@example.com'],
                      'scenes': ['S1']}],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2,
                  'text': '阿岚 alan@example.com'},
                 {'id': 'C2', 'scene': 'S1', 'start': 2, 'end': 3, 'text': '好'}]})
    db = scan_and_build(tmp_path, payload, audience='internal')
    entity = body(db, 'privacy_entity', 'subject:A')
    assert set(entity['kinds']) == {'persona', 'email'}
    assert entity['cues'] == ['C1']
    first = out(db, audience='internal')
    assert '阿岚' not in first['text'] and 'alan@example.com' not in first['text']
    assert first['text'].count('[REDACTED]') == 2


def test_name_split_across_blocks_is_masked_in_both_blocks(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': 'A', 'persona_names': ['阿岚'], 'scenes': ['S1']}],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': '她是阿'},
                 {'id': 'C2', 'scene': 'S1', 'start': 2, 'end': 3, 'text': '岚，你好'}]})
    db = scan_and_build(tmp_path, payload)
    assert '阿' not in out(db, source='C1')['text']
    assert '岚' not in out(db, source='C2')['text']


# ---------------------------------------------------------------------------
# alias evidence
# ---------------------------------------------------------------------------

def test_insufficient_alias_evidence_is_pending_not_guessed(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': 'A', 'persona_names': ['林菲'], 'scenes': ['S1']}],
        'aliases': [{'id': 'AL1', 'alias': 'Lily', 'subject': 'A', 'scene': 'S1'}],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': 'Lily 来了'},
                 {'id': 'C2', 'scene': 'S1', 'start': 2, 'end': 3, 'text': '林菲也来'}]})
    db = scan_and_build(tmp_path, payload)
    pending = body(db, 'privacy_entity', 'pending:AL1')
    assert pending is not None and pending['reason'] == 'alias_evidence_insufficient'
    assert status(db, 'privacy_alias', 'alias:AL1')[0] == '待核'
    first = out(db)
    assert first['publish'] is False
    assert first['information_loss'][0]['reason'] == 'alias_evidence_insufficient'
    assert first['text'] == ''


def test_authorized_alias_binds_subject_and_is_masked(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': 'A', 'persona_names': ['林菲'], 'scenes': ['S1']}],
        'aliases': [{'id': 'AL1', 'alias': 'Lily', 'subject': 'A', 'scene': 'S1',
                     'evidence': {'kind': 'consent', 'ref': 'c1',
                                  'authorized_by': 'lead', 'at': '2026-11-19T10:00:00'}}],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': 'Lily 来了'}]})
    db = scan_and_build(tmp_path, payload)
    assert body(db, 'privacy_entity', 'subject:A')['aliases'][0]['authorized'] is True
    assert 'Lily' not in out(db)['text']


# ---------------------------------------------------------------------------
# repeated short names / scene boundaries
# ---------------------------------------------------------------------------

def test_same_short_name_in_different_scenes_is_not_one_person(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': 'B', 'persona_names': ['小周'], 'scenes': ['S1']},
                     {'id': 'C', 'persona_names': ['小周'], 'scenes': ['S2']}],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': '小周在'},
                 {'id': 'C2', 'scene': 'S2', 'start': 2, 'end': 3, 'text': '小周在'}]})
    db = scan_and_build(tmp_path, payload)
    assert body(db, 'privacy_source', 'C1')['occurrences'][0]['subject'] == 'B'
    assert body(db, 'privacy_source', 'C2')['occurrences'][0]['subject'] == 'C'


def test_same_short_name_outside_declared_scene_is_held_for_review(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': 'B', 'persona_names': ['小周'], 'scenes': ['S1']}],
        'rows': [{'id': 'C1', 'scene': 'S2', 'start': 1, 'end': 2, 'text': '小周来了'}]})
    db = scan_and_build(tmp_path, payload)
    occurrence = body(db, 'privacy_source', 'C1')['occurrences'][0]
    assert occurrence['status'] == 'pending'
    assert occurrence['source'] == 'persona:out_of_scene'
    assert out(db)['publish'] is False


# ---------------------------------------------------------------------------
# quasi identifiers and joint optimization
# ---------------------------------------------------------------------------

def quasi_payload(subjects, locations, jobs=None, dates=None, fields=('location', 'job', 'date')):
    jobs = jobs or {}
    dates = dates or {}
    rows = []
    for subject in subjects:
        rows.append({'subject': subject, 'field': 'location', 'value': locations[subject],
                     'levels': {'0': locations[subject], '1': locations[subject][:2],
                                '2': '地区'}})
        rows.append({'subject': subject, 'field': 'job', 'value': jobs.get(subject, '演员'),
                     'levels': {'0': jobs.get(subject, '演员'), '1': '演员', '2': '演员'}})
        if subject in dates:
            rows.append({'subject': subject, 'field': 'date', 'value': dates[subject],
                         'levels': {'0': dates[subject], '1': dates[subject][:7],
                                    '2': dates[subject][:4]}})
    return {'quasi_fields': list(fields), 'quasi': rows}


def test_unique_location_generalised_for_external_but_kept_internal(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [
            {'id': 'A', 'persona_names': ['阿岚'], 'scenes': ['S1']},
            {'id': 'B', 'persona_names': ['小周'], 'scenes': ['S1']},
            {'id': 'C', 'persona_names': ['老陈'], 'scenes': ['S1']}],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2,
                  'text': '阿岚在徐汇滨江'},
                 {'id': 'C2', 'scene': 'S1', 'start': 2, 'end': 3,
                  'text': '小周在徐汇区'},
                 {'id': 'C3', 'scene': 'S1', 'start': 3, 'end': 4,
                  'text': '老陈在徐汇区'}]})
    payload.update(quasi_payload(['A', 'B', 'C'],
                                 {'A': '徐汇滨江', 'B': '徐汇区', 'C': '徐汇区'}))
    internal = scan_and_build(tmp_path, payload, audience='internal')
    assert '徐汇滨江' in out(internal, 'internal', source='C1')['text']
    assert out(internal, 'internal', source='C1')['pseudonym'].startswith('角色')

    external = scan_and_build(tmp_path, payload, audience='external')
    plan = body(external, 'privacy_entity', 'plan:external')
    assert plan['status'] == 'ok'
    assert [len(group['members']) for group in plan['groups']] == [3]
    assert plan['groups'][0]['profile']['location'] != '徐汇滨江'
    assert '徐汇滨江' not in out(external, source='C1')['text']
    assert '徐汇区' in out(external, source='C2')['text'] or \
        '徐汇区' not in out(external, source='C1')['text']


def test_generalized_still_unique_is_pending_not_released(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': 'A', 'persona_names': ['阿岚'], 'scenes': ['S1']}],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': '阿岚来了'}]})
    payload.update(quasi_payload(['A'], {'A': '徐汇滨江'}))
    db = scan_and_build(tmp_path, payload)
    assert body(db, 'privacy_entity', 'plan:external')['status'] == 'unsatisfiable'
    fixture = out(db)
    assert fixture['publish'] is False
    assert any(item['reason'] == 'quasi_group_not_satisfied'
               for item in fixture['information_loss'])


def test_cross_field_combination_prevents_greedy_three_person_group(tmp_path):
    # Each field pair alone leaves fewer than three people equal, and no single
    # field creates a three-person class.  A per-field greedy pass could claim
    # success here; the joint plan must not.
    payload = authorized({
        'version': 'v1',
        'quasi_fields': ['location', 'job', 'date'],
        'subjects': [{'id': subject, 'persona_names': [f'名字{subject}'], 'scenes': ['S1']}
                     for subject in 'ABCD'],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2,
                  'text': '名字A、名字B、名字C、名字D'}]})
    rows = []
    for subject, location in zip('ABCD', ['L1', 'L1', 'L2', 'L2']):
        rows.append({'subject': subject, 'field': 'location', 'value': location,
                     'levels': {'0': location, '1': location}})
    for subject, job in zip('ABCD', ['J1', 'J2', 'J2', 'J1']):
        rows.append({'subject': subject, 'field': 'job', 'value': job,
                     'levels': {'0': job, '1': job}})
    for subject, date in zip('ABCD', ['D1', 'D2', 'D2', 'D1']):
        rows.append({'subject': subject, 'field': 'date', 'value': date,
                     'levels': {'0': date, '1': date}})
    payload['quasi'] = rows
    db = scan_and_build(tmp_path, payload)
    plan = body(db, 'privacy_entity', 'plan:external')
    assert plan['status'] == 'unsatisfiable'
    assert plan['conflict_fields']
    assert all(len(group['members']) >= 3 for group in plan['groups'])
    assert out(db)['publish'] is False


def test_cheapest_information_loss_partition_is_chosen(tmp_path):
    # A/B/C need one level of location generalisation; D/E/F need none because
    # they share a job.  A field-by-field greedy that locks location first
    # would pay more overall than this joint partition.
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': subject, 'persona_names': [f'名字{subject}'], 'scenes': ['S1']}
                     for subject in 'ABCDEF'],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2,
                  'text': '名字A名字B名字C名字D名字E名字F'}]})
    rows = []
    for subject, location in zip('ABCDEF', ['X1', 'X2', 'X3', 'X4', 'X5', 'X6']):
        rows.append({'subject': subject, 'field': 'location', 'value': location,
                     'levels': {'0': location, '1': '同区'}})
    for subject, job in zip('ABCDEF', ['J1', 'J1', 'J1', 'J2', 'J2', 'J2']):
        rows.append({'subject': subject, 'field': 'job', 'value': job,
                     'levels': {'0': job, '1': job}})
    payload['quasi'] = rows
    db = scan_and_build(tmp_path, payload)
    plan = body(db, 'privacy_entity', 'plan:external')
    assert plan['status'] == 'ok'
    assert sorted(len(group['members']) for group in plan['groups']) == [3, 3]
    assert sum(group['cost'] for group in plan['groups']) == 6
    assert out(db)['publish'] is True


def test_group_boundary_exactly_three_is_publishable(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': subject, 'persona_names': [f'名字{subject}'], 'scenes': ['S1']}
                     for subject in 'ABC'],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2,
                  'text': '名字A名字B名字C'}]})
    payload.update(quasi_payload(['A', 'B', 'C'],
                                 {'A': '徐汇滨江', 'B': '徐汇区', 'C': '徐汇区'}))
    db = scan_and_build(tmp_path, payload)
    assert body(db, 'privacy_entity', 'plan:external')['status'] == 'ok'
    assert out(db)['publish'] is True


# ---------------------------------------------------------------------------
# pseudonym isolation, timing preservation, audiences, replay
# ---------------------------------------------------------------------------

def multi_vendor_payload():
    payload = authorized({
        'version': 'v1', 'project': 'proj-alpha',
        'subjects': [{'id': subject, 'persona_names': [f'名字{subject}'], 'scenes': ['S1']}
                     for subject in 'ABC'],
        'rows': [{'id': 'C1', 'scene': 'S1', 'speaker': 'SPK-A', 'start': 1, 'end': 2,
                  'text': '名字A名字B名字C'}]})
    payload.update(quasi_payload(['A', 'B', 'C'],
                                 {'A': '徐汇滨江', 'B': '徐汇区', 'C': '徐汇区'}))
    return payload


def test_speakers_remain_distinguishable_within_a_shot(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': subject, 'persona_names': [f'名字{subject}'], 'scenes': ['S1']}
                     for subject in 'ABC'],
        'rows': [
            {'id': 'C1', 'scene': 'S1', 'speaker': 'SPK-A', 'start': 1, 'end': 2,
             'text': '名字A'},
            {'id': 'C2', 'scene': 'S1', 'speaker': 'SPK-B', 'start': 2, 'end': 3,
             'text': '名字B'},
            {'id': 'C3', 'scene': 'S1', 'speaker': 'SPK-C', 'start': 3, 'end': 4,
             'text': '名字C'}]})
    payload.update(quasi_payload(['A', 'B', 'C'],
                                 {'A': '徐汇滨江', 'B': '徐汇区', 'C': '徐汇区'}))
    db = scan_and_build(tmp_path, payload)
    labels = [out(db, source=source)['speaker'] for source in ('C1', 'C2', 'C3')]
    assert len(set(labels)) == 3
    first = out(db, source='C1')
    assert (first['start'], first['end']) == (1, 2)


def test_external_pseudonyms_differ_per_vendor_and_internal_is_stable(tmp_path):
    payload = multi_vendor_payload()
    db = tmp_path / 'ledger.db'
    privacy_scan.scan(db, write(tmp_path, 'source.json', payload))
    privacy_fixture.build(db, write(tmp_path, 'internal.json',
                                    dict(fixture_rows(['C1'], 'internal'),
                                         audience='internal')))

    payload_b = dict(payload)
    payload_b['authorization'] = {'audiences': [
        {'audience': 'internal', 'purpose': 'internal review',
         'authorized_by': 'lead-internal', 'at': '2026-11-20T09:00:00'},
        {'audience': 'external', 'vendor': 'vendor-b', 'purpose': 'other supplier',
         'authorized_by': 'lead-external', 'at': '2026-11-20T09:10:00'}]}
    privacy_scan.scan(db, write(tmp_path, 'source-b.json', payload_b))
    privacy_fixture.build(db, write(tmp_path, 'vendor-a.json',
                                    dict(fixture_rows(['C1'], 'external', 'vendor-a'),
                                         audience='external', vendor='vendor-a')))
    privacy_fixture.build(db, write(tmp_path, 'vendor-b.json',
                                    dict(fixture_rows(['C1'], 'external', 'vendor-b'),
                                         audience='external', vendor='vendor-b')))

    internal_label = body(db, 'privacy_pseudonym', 'pseudonym:A:internal:-')['pseudonym']
    vendor_a_label = body(db, 'privacy_pseudonym', 'pseudonym:A:external:vendor-a')['pseudonym']
    vendor_b_label = body(db, 'privacy_pseudonym', 'pseudonym:A:external:vendor-b')['pseudonym']
    assert len({internal_label, vendor_a_label, vendor_b_label}) == 3
    assert internal_label.startswith('角色')
    assert vendor_a_label != vendor_b_label


def test_cue_count_order_and_times_are_preserved(tmp_path):
    rows = [
        {'id': 'C1', 'scene': 'S1', 'speaker': 'SPK-A', 'start': 1.0, 'end': 3.5,
         'text': '阿岚 alan@'},
        {'id': 'C2', 'scene': 'S1', 'speaker': 'SPK-A', 'start': 3.5, 'end': 6.0,
         'text': 'example.com'},
        {'id': 'C3', 'scene': 'S1', 'speaker': 'SPK-B', 'start': 6.0, 'end': 8.0,
         'text': '好'}]
    payload = authorized({'version': 'v1', 'rows': rows})
    db = scan_and_build(tmp_path, payload)
    for index, original in enumerate(rows):
        built = out(db, source=original['id'])
        assert (built['start'], built['end']) == (original['start'], original['end'])
        assert built['cue_index'] == index


def test_unapproved_audience_is_not_downgraded(tmp_path):
    payload = {'version': 'v1', 'authorization': {'audiences': [
        {'audience': 'internal', 'purpose': 'internal review',
         'authorized_by': 'lead', 'at': '2026-11-20T09:00:00'}]},
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': 'a@b.com'}]}
    db = tmp_path / 'ledger.db'
    privacy_scan.scan(db, write(tmp_path, 'source.json', payload))
    result = privacy_fixture.build(
        db, write(tmp_path, 'external.json',
                  dict(fixture_rows(['C1'], 'external'), audience='external')))
    assert result[0]['status'] == '待核'
    assert result[0]['reason'] == 'audience_not_authorized'
    assert out(db)['publish'] is False


def test_legacy_payload_without_authorization_is_internal_only(tmp_path):
    payload = {'version': 'v1', 'rows': [
        {'id': 'C1', 'text': 'Call me at a@example.com'}]}
    db = tmp_path / 'ledger.db'
    assert privacy_scan.scan(db, write(tmp_path, 'source.json', payload))[0]['status'] == '待遮盖'
    assert privacy_fixture.build(
        db, write(tmp_path, 'fixture.json',
                  {'version': 'v1', 'rows': [{'id': 'F1', 'source': 'C1'}]}))[0]['status'] == '已生成'
    external = privacy_fixture.build(
        db, write(tmp_path, 'external.json',
                  dict(fixture_rows(['C1'], 'external'), audience='external')))
    assert external[0]['reason'] == 'audience_not_authorized'


def test_repeated_generation_and_replay_are_stable(tmp_path):
    payload = multi_vendor_payload()
    db = tmp_path / 'ledger.db'
    source = write(tmp_path, 'source.json', payload)
    fixture = write(tmp_path, 'fixture.json',
                    dict(fixture_rows(['C1'], 'external'),
                         audience='external', vendor='vendor-a'))
    first = privacy_scan.scan(db, source)
    privacy_fixture.build(db, fixture)
    first_body = out(db)
    first_plan = body(db, 'privacy_entity', 'plan:external')
    second = privacy_scan.scan(db, source)
    privacy_fixture.build(db, fixture)
    assert first == second
    assert out(db) == first_body
    assert body(db, 'privacy_entity', 'plan:external') == first_plan


def test_reviewer_readback_shows_original_and_redacted_but_export_does_not(tmp_path):
    payload = authorized({'version': 'v1', 'rows': [
        {'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2, 'text': 'mail alan@'},
        {'id': 'C2', 'scene': 'S1', 'start': 2, 'end': 3, 'text': 'example.com'}]})
    db = scan_and_build(tmp_path, payload)
    reviewed = report.review(db, audience='external')
    c1 = next(row for row in reviewed
              if row['fixture'] == fixture_id('external', 'C1'))
    assert c1['original'] == 'mail alan@'
    assert 'alan@' not in c1['redacted']
    assert c1['fragments'][0]['raw'] == 'alan@example.com'
    assert c1['fragments'][0]['cue'] == 'C1'
    out_path = privacy_fixture.export(db, tmp_path / 'external.json', audience='external')
    data = json.loads(out_path.read_text(encoding='utf-8'))
    encoded = json.dumps(data, ensure_ascii=False)
    assert 'alan@example.com' not in encoded
    assert '角色' not in encoded
    assert data['publishable'] is True


def test_leak_check_records_clean_external_and_internal_output(tmp_path):
    payload = multi_vendor_payload()
    internal = scan_and_build(tmp_path, payload, audience='internal')
    checks = [json.loads(row['body']) for row in
              connect(internal).execute('SELECT * FROM privacy_leak_check')]
    assert checks and all(check['clean'] for check in checks)
    external = scan_and_build(tmp_path, payload, audience='external')
    checks = [json.loads(row['body']) for row in
              connect(external).execute('SELECT * FROM privacy_leak_check')]
    assert checks and all(check['clean'] for check in checks)


def test_scan_rejects_external_authorization_without_vendor(tmp_path):
    payload = {'version': 'v1', 'authorization': {'audiences': [
        {'audience': 'external', 'purpose': 'x', 'authorized_by': 'y',
         'at': '2026-11-20T09:00:00'}]},
        'rows': [{'id': 'C1', 'text': 'x'}]}
    with pytest.raises(ValueError, match='authorization_vendor_missing'):
        privacy_scan.scan(tmp_path / 'ledger.db', write(tmp_path, 'source.json', payload))


def test_external_export_is_isolated_per_vendor(tmp_path):
    payload = multi_vendor_payload()
    db = tmp_path / 'ledger.db'
    privacy_scan.scan(db, write(tmp_path, 'source.json', payload))
    privacy_fixture.build(db, write(tmp_path, 'va.json',
                                    dict(fixture_rows(['C1'], 'external', 'vendor-a'),
                                         audience='external', vendor='vendor-a')))
    vendor_a = privacy_fixture.export(db, tmp_path / 'va-out.json', 'external',
                                      project='proj-alpha', vendor='vendor-a')
    vendor_b = privacy_fixture.export(db, tmp_path / 'vb-out.json', 'external',
                                      project='proj-alpha', vendor='vendor-b')
    data_a = json.loads(vendor_a.read_text(encoding='utf-8'))
    data_b = json.loads(vendor_b.read_text(encoding='utf-8'))
    assert data_a['authorized'] is True and data_a['publishable'] is True
    assert data_b['authorized'] is False and data_b['publishable'] is False
    assert data_b['cues'] == []  # vendor-b has no generated sample to emit


def test_subject_missing_one_quasi_field_gets_field_suppressed(tmp_path):
    payload = authorized({
        'version': 'v1',
        'subjects': [{'id': subject, 'persona_names': [f'名字{subject}'], 'scenes': ['S1']}
                     for subject in 'ABC'],
        'rows': [{'id': 'C1', 'scene': 'S1', 'start': 1, 'end': 2,
                  'text': '名字A名字B名字C'}]})
    payload['quasi_fields'] = ['location', 'job']
    payload['quasi'] = [
        {'subject': 'A', 'field': 'location', 'value': 'L1', 'levels': {'0': 'L1', '1': 'L'}},
        {'subject': 'B', 'field': 'location', 'value': 'L1', 'levels': {'0': 'L1', '1': 'L'}},
        {'subject': 'C', 'field': 'location', 'value': 'L1', 'levels': {'0': 'L1', '1': 'L'}},
        {'subject': 'A', 'field': 'job', 'value': 'J', 'levels': {'0': 'J'}},
        {'subject': 'B', 'field': 'job', 'value': 'J', 'levels': {'0': 'J'}},
    ]
    db = scan_and_build(tmp_path, payload)
    plan = body(db, 'privacy_entity', 'plan:external')
    assert plan['status'] == 'ok'
    group = plan['groups'][0]
    assert group['profile']['job'] == '__SUPPRESSED__'
    assert group['profile']['location'] == 'L1'
