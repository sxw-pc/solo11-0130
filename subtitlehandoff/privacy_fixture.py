"""Build audience-specific redacted fixtures from a scanned source.

The builder only ever *publishes* what the target audience is authorized to
see.  It reads the scan findings (which may span several adjacent subtitle
blocks), masks every resolved location — for a wrapped e-mail or a name split
across two blocks that means **both** blocks — replaces speaker codes with
pseudonyms scoped to the audience/project/vendor, applies the external
quasi-identifier plan, and writes a leak check.

Cue count, cue order and cue start/end times are never altered.  The local
ledger keeps the original fragments, alias evidence and every ladder value; the
published fixture carries no reversible mapping and no original alias.
"""
import json
from pathlib import Path

from .ingest import capture
from .store import connect, save, decide
from .privacy_scan import (SUPPRESS, _authorization_row, _pseudonym,
                           _speaker_pseudonym)


def _apply_mask(text, occurrences):
    """Mask all occurrences for one cue, right-to-left per line segment."""
    segments = text.split('\n')
    by_segment = {}
    for occurrence in occurrences:
        by_segment.setdefault(occurrence['segment'], []).append(occurrence)
    for seg_index, items in by_segment.items():
        if not (0 <= seg_index < len(segments)):
            continue
        segment = segments[seg_index]
        for occurrence in sorted(items, key=lambda o: (o['start'], o['end']), reverse=True):
            start, end = occurrence['start'], occurrence['end']
            segment = segment[:start] + '[REDACTED]' + segment[end:]
        segments[seg_index] = segment
    return '\n'.join(segments)


def _resolved(occurrences):
    return [o for o in occurrences if o['status'] == 'resolved']


def _blocked(occurrences):
    return [o for o in occurrences if o['status'] != 'resolved']


def _load_authorization(db, version, project, audience=None, vendor=None):
    row = _authorization_row(db, version, project, audience, vendor)
    if row is None:
        return None
    return json.loads(row['body'])


def _load_plan(db, project):
    for row in db.execute("SELECT * FROM privacy_entity WHERE id='plan:external'"):
        body = json.loads(row['body'])
        if body.get('project') in (None, project):
            return body
    return None


def _group_for(plan, subject):
    if not plan or plan.get('status') != 'ok':
        return None
    return next((group for group in plan['groups']
                 if subject in group['members']), None)


def _quasi_replacements(subjects, plan):
    """Per-group and global raw→safe replacements.

    A raw value can be generalized differently for different groups (for
    example ``徐汇区`` is already the safe level for one group but must become
    ``上海市`` for another).  Applying one global mapping would corrupt a cue,
    so each cue is rewritten with the mapping of the group its named subject
    belongs to.  Within a group, *every* member's raw value maps to the group's
    published value — otherwise a cue by one member could still speak another
    member's specific location.  Cues that name nobody use a global map; a raw
    value that different groups map differently is suppressed rather than
    guessed.
    """
    per_group = {}
    desired = {}
    published_all = set()
    if not plan or plan.get('status') != 'ok':
        return {}, {}
    for group in plan['groups']:
        mapping = {}
        for member in group['members']:
            entity = subjects.get(member)
            if entity is None:
                continue
            for field, published in group['profile'].items():
                raw = entity.get('quasi', {}).get(field)
                if not raw:
                    continue
                if published != SUPPRESS:
                    published_all.add(published)
                if raw != published:
                    value = '' if published == SUPPRESS else published
                    mapping[raw] = value
                    desired.setdefault(raw, set()).add(value)
        for member in group['members']:
            per_group[member] = mapping
    global_map = {raw: next(iter(values)) if len(values) == 1 else ''
                  for raw, values in desired.items()}
    # Never suppress a string that is a legitimate published value somewhere;
    # it would erase another group's safe output from subject-less cues.
    global_map = {raw: value for raw, value in global_map.items()
                  if value or raw not in published_all}
    return per_group, global_map


def _apply_quasi(text, replacements):
    for raw in sorted(replacements, key=len, reverse=True):
        text = text.replace(raw, replacements[raw])
    return text


def _forbidden_quasi(subjects, plan):
    """Raw values that identify a subject and must not appear in the export."""
    raw_values = set()
    safe_values = set()
    for subject, entity in subjects.items():
        group = _group_for(plan, subject)
        if group is None:
            continue
        for field, published in group['profile'].items():
            if published != SUPPRESS:
                safe_values.add(published)
            raw = entity.get('quasi', {}).get(field)
            if raw and raw != published:
                raw_values.add(raw)
    # A string that is a safe published value for some group may legitimately
    # appear; only raw values that are never a safe output are leaks.
    return raw_values - safe_values


def _leak_check(texts, forbidden):
    return sorted({value for value in forbidden if value and value in texts})


def _load_subjects(db):
    subjects = {}
    for row in db.execute("SELECT * FROM privacy_entity WHERE id LIKE 'subject:%'"):
        body = json.loads(row['body'])
        subjects[body['subject']] = body
    return subjects


def _describe_loss(entity, group):
    loss = []
    for field, published in (group or {}).get('profile', {}).items():
        raw = entity.get('quasi', {}).get(field)
        if raw is None or raw == published:
            continue
        if published == SUPPRESS:
            loss.append({'field': field, 'reason': 'field_suppressed'})
        else:
            loss.append({'field': field, 'reason': 'generalised'})
    return loss


def build(path, file):
    with open(file, 'r', encoding='utf-8') as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or not isinstance(payload.get('version'), str) \
            or not payload['version']:
        raise ValueError('version_required')
    audience = payload.get('audience', 'internal')
    if audience not in ('internal', 'external'):
        raise ValueError('audience_required')
    source, _ = capture(path, 'desktop_privacy_fixture', file)
    db = connect(path)
    results = []
    try:
        with db:
            authorization = _load_authorization(
                db, payload['version'], payload.get('project'), audience,
                payload.get('vendor') if audience == 'external' else None)
            entry = None
            if authorization is not None:
                entry = next((item for item in authorization['authorization']['audiences']
                              if item['audience'] == audience
                              and (audience != 'external'
                                   or item.get('vendor') == payload.get('vendor'))), None)
            if entry is None:
                # The audience was not authorized before sampling.  Nothing is
                # generated and nothing is downgraded to another audience.
                any_authorization = _load_authorization(
                    db, payload['version'], payload.get('project'))
                reason = ('authorization_missing' if any_authorization is None
                          else 'audience_not_authorized')
                for row in payload.get('rows', []):
                    key = str(row['id'])
                    save(db, 'privacy_fixture', key, source, payload['version'],
                         {'source': row.get('source'), 'audience': audience,
                          'publish': False}, '待核', reason)
                    results.append(decide(db, '脱敏样例', key, '待核', reason, source))
                return results

            project = authorization.get('project')
            vendor = entry.get('vendor') if audience == 'external' else None
            subjects = _load_subjects(db)
            plan = _load_plan(db, project) if audience == 'external' else None
            # The external sample is only publishable when the quasi-identifier
            # plan actually satisfies every group.  Masking the e-mail is not
            # enough: if generalisation still leaves one person identifiable,
            # the whole export waits for review.
            external_blocked = audience == 'external' and (
                plan is None or plan.get('status') != 'ok')
            replacements, global_replacements = _quasi_replacements(subjects, plan) \
                if audience == 'external' else ({}, {})

            published = []
            for row in payload.get('rows', []):
                key = str(row['id'])
                source_key = str(row.get('source'))
                original = db.execute('SELECT * FROM privacy_source WHERE id=?',
                                      (source_key,)).fetchone()
                if not original:
                    save(db, 'privacy_fixture', key, source, payload['version'],
                         {'source': source_key, 'audience': audience, 'publish': False},
                         '待核', 'source_missing')
                    results.append(decide(db, '脱敏样例', key, '待核', 'source_missing', source))
                    continue
                item = json.loads(original['body'])
                occurrences = item.get('occurrences', [])
                resolved = _resolved(occurrences)
                blocked = _blocked(occurrences)
                masked = _apply_mask(item['text'], resolved)
                identifiable = sorted({o['subject'] for o in resolved
                                       if o.get('subject')
                                       and not o['subject'].startswith('implicit-email:')})
                subject = identifiable[0] if len(identifiable) == 1 else None
                entity = subjects.get(subject) if subject else None
                group = _group_for(plan, subject) if entity else None
                # A cue may repeat a quasi value without naming anybody (for
                # example "they shoot at X tonight").  External generalisation
                # therefore applies to every cue, using the named subject's
                # mapping when there is one and the global safe map otherwise.
                if audience == 'external':
                    mapping = replacements.get(subject, global_replacements)
                    if len(identifiable) != 1:
                        mapping = global_replacements
                    masked = _apply_quasi(masked, mapping)

                publish = True
                loss = []
                reason = 'audience_published'
                if blocked:
                    # An identifier we could not bind is listed for review and
                    # never published with a guessed identity.  Masking still
                    # covers every location, but the cue is held back.  This
                    # takes priority over the group check: the concrete defect
                    # is the unresolved identifier, not the group size.
                    publish = False
                    reasons = {occurrence.get('source', '') for occurrence in blocked}
                    if any('out_of_scene' in source for source in reasons):
                        reason = 'short_name_other_scene'
                        loss.append({'field': 'identity',
                                     'reason': 'same_short_name_other_scene'})
                    elif any(source.startswith('alias:') for source in reasons):
                        reason = 'alias_evidence_insufficient'
                        loss.append({'field': 'identity',
                                     'reason': 'alias_evidence_insufficient'})
                    else:
                        reason = 'unresolved_identifier'
                        loss.append({'field': 'identity',
                                     'reason': 'unresolved_identifier'})
                if external_blocked and reason == 'audience_published':
                    publish = False
                    reason = 'quasi_group_not_satisfied'
                    loss.append({'field': 'quasi',
                                 'reason': 'quasi_group_not_satisfied'})
                if len(identifiable) > 1:
                    loss.append({'field': 'identity',
                                 'reason': 'multiple_subjects_in_cue'})
                if entity is not None:
                    loss.extend(_describe_loss(entity, group))
                    if audience == 'external' and group is None:
                        publish = False
                        reason = 'quasi_group_not_satisfied'
                        loss.append({'field': 'quasi',
                                     'reason': 'quasi_group_not_satisfied'})

                speaker = None
                if item.get('speaker'):
                    speaker = _speaker_pseudonym(item['speaker'], audience, project, vendor)

                body = {'source': source_key, 'audience': audience, 'vendor': vendor,
                        'publish': publish,
                        'cue_index': item.get('cue_index'), 'start': item.get('start'),
                        'end': item.get('end'), 'text': masked if publish else '',
                        'information_loss': loss}
                if speaker and publish:
                    body['speaker'] = speaker
                if entity is not None and publish:
                    # A subject label is scoped exactly like the speaker label:
                    # stable across scenes for internal review, and isolated per
                    # project and vendor for an external supplier.
                    body['pseudonym'] = _pseudonym(subject, audience, project, vendor)
                status = '待核' if not publish else '已生成'
                save(db, 'privacy_fixture', key, source, payload['version'], body,
                     status, reason)
                results.append(decide(db, '脱敏样例', key, status, reason, source))
                if publish:
                    published.append(body)

            texts = '\n'.join(body.get('text', '') for body in published)
            forbidden = set()
            for row in db.execute('SELECT * FROM privacy_source'):
                for occurrence in json.loads(row['body']).get('occurrences', []):
                    if occurrence['kind'] in ('email', 'alias', 'persona'):
                        forbidden.add(occurrence['raw'])
            if audience == 'external':
                forbidden |= _forbidden_quasi(subjects, plan)
                for row in db.execute('SELECT * FROM privacy_pseudonym'):
                    label = json.loads(row['body'])
                    if label['subject'] not in subjects:
                        continue
                    if label['audience'] == 'internal' or (
                            label['audience'] == 'external' and label['vendor'] != vendor):
                        forbidden.add(label['pseudonym'])
            leaked = _leak_check(texts, forbidden)
            check_key = f'leak:{source}:{audience}' + (f':{vendor}' if vendor else '')
            save(db, 'privacy_leak_check', check_key, source,
                 payload['version'],
                 {'audience': audience, 'project': project, 'vendor': vendor,
                  'published_cues': len(published),
                  'cues': [body.get('source') for body in published],
                  'leaked': leaked, 'clean': not leaked},
                 '通过' if not leaked else '待核',
                 'leak_clean' if not leaked else 'leak_detected')
        return results
    finally:
        db.close()


def export(path, out, audience='external', project=None, vendor=None):
    """Write the audience-facing JSON.

    Only the fields the target audience is allowed to see are emitted.  No
    reversible mapping, raw alias, original quasi value or internal pseudonym
    ever reaches this file; those remain in the local ledger for
    :func:`subtitlehandoff.report.review`.
    """
    db = connect(path)
    try:
        rows = []
        versions = {}
        for row in db.execute('SELECT * FROM privacy_fixture ORDER BY id'):
            body = json.loads(row['body'])
            if body.get('audience') != audience:
                continue
            if vendor is not None and body.get('vendor') != vendor:
                continue
            versions.setdefault(row['version'], []).append(row['id'])
            entry = {'id': row['id'], 'cue_index': body.get('cue_index'),
                     'start': body.get('start'), 'end': body.get('end'),
                     'text': body.get('text'), 'speaker': body.get('speaker'),
                     'pseudonym': body.get('pseudonym'),
                     'audience': audience, 'publish': body.get('publish'),
                     'information_loss': body.get('information_loss', [])}
            rows.append(entry)
        checks = []
        for check in db.execute('SELECT * FROM privacy_leak_check ORDER BY id'):
            check_body = json.loads(check['body'])
            if check_body.get('audience') != audience:
                continue
            if vendor is not None and check_body.get('vendor') != vendor:
                continue
            if project is not None and check_body.get('project') not in (None, project):
                continue
            checks.append(check_body)
        # An export must be able to prove it was authorized before sampling.
        authorized_versions = set()
        for row in db.execute('SELECT * FROM privacy_authorization ORDER BY id'):
            body = json.loads(row['body'])
            if project is not None and body.get('project') not in (None, project):
                continue
            entries = body['authorization']['audiences']
            entries = [item for item in entries if item['audience'] == audience]
            if vendor is not None:
                entries = [item for item in entries if item.get('vendor') == vendor]
            if entries:
                authorized_versions.add(row['version'])
        authorized = bool(versions) and set(versions).issubset(authorized_versions)
        data = {'version': '1', 'audience': audience, 'project': project,
                'vendor': vendor, 'cues': rows, 'leak_checks': checks,
                'authorized': authorized,
                'publishable': authorized
                and all(row['publish'] for row in rows)
                and all(check.get('clean') for check in checks)}
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        return target
    finally:
        db.close()


