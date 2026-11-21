"""Privacy scan for synthetic subtitle samples.

The scan understands the block order produced by the local subtitle editor
(consecutive cues, scene membership and speaker codes).  Direct identifiers are
resolved across adjacent blocks, aliases are only joined when the locally
maintained alias table carries sufficient authorization evidence, and the
quasi-identifier plan for the external audience is computed with a joint
(never per-field greedy) optimisation.

Everything that is needed for an authorized reviewer stays in the local
ledger.  :mod:`subtitlehandoff.privacy_fixture` only publishes the fields that
the target audience is allowed to see.
"""
import hashlib
import itertools
import json
import re
from datetime import datetime

from .ingest import capture
from .store import connect, save, decide

EMAIL = re.compile(r'[\w.+-]+@[\w.-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}')
SUPPRESS = '__SUPPRESSED__'
AUDIENCES = ('internal', 'external')
EVIDENCE_KEYS = ('kind', 'ref', 'authorized_by', 'at')


# ---------------------------------------------------------------------------
# small deterministic helpers
# ---------------------------------------------------------------------------

def _digest(*parts):
    return hashlib.sha256('\x1f'.join(str(p) for p in parts).encode('utf-8')).hexdigest()


def _segments(text):
    return text.split('\n')


def _scene_key(cue, index):
    scene = cue.get('scene')
    # A cue without an explicit scene is its own scene: it can never be
    # silently joined to a neighbouring scene.
    return str(scene) if scene not in (None, '') else f'__cue_{index}'


def _scene_stream(cues):
    """Concatenate cue line text without separators and keep a char map.

    Removing the line break but retaining a per-character map is what lets a
    name or an e-mail that was wrapped across two subtitle blocks be recovered
    and, just as important, masked in *both* blocks.
    """
    parts = []
    mapping = []
    for cue in cues:
        for seg_index, segment in enumerate(_segments(cue['text'])):
            for offset, char in enumerate(segment):
                parts.append(char)
                mapping.append((cue['id'], seg_index, offset))
    return ''.join(parts), mapping


def _find_all(haystack, needle):
    found = []
    start = 0
    while needle:
        at = haystack.find(needle, start)
        if at < 0:
            break
        found.append((at, at + len(needle)))
        start = at + 1
    return found


def _split_to_blocks(mapping, start, end):
    """Split a stream span into runs that live inside one cue line."""
    runs = []
    current = None
    for position in range(start, end):
        cue_id, seg_index, offset = mapping[position]
        if (current and current['cue'] == cue_id
                and current['segment'] == seg_index
                and current['end'] == offset):
            current['end'] = offset + 1
            continue
        if current:
            runs.append(current)
        current = {'cue': cue_id, 'segment': seg_index, 'start': offset, 'end': offset + 1}
    if current:
        runs.append(current)
    return runs


def _normalise_levels(levels):
    if not isinstance(levels, dict):
        return None
    out = {}
    for key, value in levels.items():
        try:
            level = int(key)
        except (TypeError, ValueError):
            return None
        if isinstance(value, str) and value:
            out[level] = value
    if 0 not in out:
        return None
    return dict(sorted(out.items()))


def _default_levels(field, raw):
    """Finite, documented fallback ladder when no explicit ladder is given."""
    values = [raw]
    if field == 'date':
        match = re.match(r'^(\d{4})-(\d{2})(?:-(\d{2}))?', raw)
        if match:
            year, month, _ = match.group(1), match.group(2), match.group(3)
            values.append(f'{year}-{month}')
            values.append(year)
            values.append(f'{year[:3]}0s')
    elif field == 'location':
        if len(raw) > 2:
            values.append(raw[:2])
        values.append('地区')
    elif field == 'job':
        values.append('从业者')
    else:
        if len(raw) > 2:
            values.append(raw[:2])
        values.append('其他')
    result = {}
    for value in values:
        if value not in result.values():
            result[len(result)] = value
    return result


def _value_at(levels, raw, level):
    if not levels:
        return raw
    if level in levels:
        return levels[level]
    lower = [key for key in levels if key <= level]
    if not lower:
        return raw
    return levels[max(lower)]


def _max_level(levels):
    return max(levels) if levels else 0


# ---------------------------------------------------------------------------
# authorization, alias evidence and payload validation
# ---------------------------------------------------------------------------

def _evidence_ok(evidence):
    if not isinstance(evidence, dict):
        return False, 'evidence_missing'
    for key in EVIDENCE_KEYS:
        if not evidence.get(key):
            return False, f'evidence_{key}_missing'
    try:
        datetime.fromisoformat(str(evidence['at']))
    except (TypeError, ValueError):
        return False, 'evidence_at_invalid'
    return True, 'evidence_authorized'


def _authorization_row(db, version, project=None, audience=None, vendor=None):
    """Return the authorization row for a project/audience/vendor.

    A ledger may hold authorizations for several vendors.  For an external
    build the requested vendor must be matched explicitly; silently falling
    back to another vendor's row would let one supplier's identifiers leak into
    another supplier's sample.
    """
    rows = [dict(row) for row in db.execute(
        'SELECT * FROM privacy_authorization WHERE version=? ORDER BY id', (version,))]
    candidates = []
    for row in rows:
        body = json.loads(row['body'])
        if project is not None and body.get('project') not in (None, project):
            continue
        if audience is not None:
            entries = [item for item in
                       body['authorization']['audiences']
                       if item['audience'] == audience]
            if not entries:
                continue
            if audience == 'external' and vendor is not None:
                entries = [item for item in entries if item.get('vendor') == vendor]
                if not entries:
                    continue
        candidates.append(row)
    return candidates[0] if candidates else None


def _normalise_authorization(payload):
    """Read the pre-scan audience authorization.

    A legacy payload without an authorization block is accepted as
    *internal-only* with an explicit reason.  It is never silently upgraded to
    the external audience when generation later fails; external output simply
    reports ``authorization_missing``.
    """
    raw = payload.get('authorization')
    if raw is None:
        return ({'audiences': [{'audience': 'internal', 'purpose': 'legacy internal review',
                                'authorized_by': 'legacy_default',
                                'at': '1970-01-01T00:00:00'}],
                 'legacy': True}, 'legacy_internal_default')
    if not isinstance(raw, dict) or not isinstance(raw.get('audiences'), list) or not raw['audiences']:
        raise ValueError('authorization_invalid')
    audiences = []
    seen = set()
    for item in raw['audiences']:
        if not isinstance(item, dict):
            raise ValueError('authorization_invalid')
        audience = item.get('audience')
        if audience not in AUDIENCES:
            raise ValueError('authorization_audience_invalid')
        if audience in seen:
            raise ValueError('authorization_audience_duplicate')
        seen.add(audience)
        for key in ('purpose', 'authorized_by', 'at'):
            if not item.get(key):
                raise ValueError('authorization_field_missing')
        try:
            datetime.fromisoformat(str(item['at']))
        except (TypeError, ValueError):
            raise ValueError('authorization_at_invalid')
        entry = {'audience': audience, 'purpose': item['purpose'],
                 'authorized_by': item['authorized_by'], 'at': item['at']}
        if audience == 'external':
            if not item.get('vendor'):
                raise ValueError('authorization_vendor_missing')
            entry['vendor'] = str(item['vendor'])
        audiences.append(entry)
    return {'audiences': audiences, 'legacy': False}, 'audiences_authorized'


def _normalise_payload(payload):
    """Validate the pre-scan shape before anything is written to the ledger."""
    if not isinstance(payload, dict):
        raise ValueError('payload_invalid')
    if not isinstance(payload.get('version'), str) or not payload['version']:
        raise ValueError('version_required')
    rows = payload.get('rows')
    if not isinstance(rows, list):
        raise ValueError('rows_required')
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or row.get('id') in (None, ''):
            raise ValueError('cue_id_required')
        if str(row['id']) in seen:
            raise ValueError('cue_id_duplicate')
        seen.add(str(row['id']))
        if not isinstance(row.get('text'), str):
            raise ValueError('cue_text_required')
        for key in ('start', 'end'):
            if key in row and not isinstance(row[key], (int, float)):
                raise ValueError('cue_time_invalid')
    subjects = payload.get('subjects', [])
    if not isinstance(subjects, list):
        raise ValueError('subjects_invalid')
    subject_ids = set()
    for item in subjects:
        if not isinstance(item, dict) or item.get('id') in (None, ''):
            raise ValueError('subject_id_required')
        if str(item['id']) in subject_ids:
            raise ValueError('subject_id_duplicate')
        subject_ids.add(str(item['id']))
        names = item.get('persona_names', [])
        if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
            raise ValueError('persona_names_invalid')
        scenes = item.get('scenes')
        if scenes is not None and (not isinstance(scenes, list)
                                   or not all(isinstance(s, str) for s in scenes)):
            raise ValueError('subject_scenes_invalid')
    aliases = payload.get('aliases', [])
    if not isinstance(aliases, list):
        raise ValueError('aliases_invalid')
    alias_ids = set()
    for item in aliases:
        if not isinstance(item, dict) or item.get('id') in (None, ''):
            raise ValueError('alias_id_required')
        if str(item['id']) in alias_ids:
            raise ValueError('alias_id_duplicate')
        alias_ids.add(str(item['id']))
        if not isinstance(item.get('alias'), str) or not item['alias']:
            raise ValueError('alias_value_required')
        if item.get('scene') in (None, ''):
            raise ValueError('alias_scene_required')
        if item.get('subject') is not None and str(item['subject']) not in subject_ids:
            raise ValueError('alias_subject_unknown')
    quasi = payload.get('quasi', [])
    if not isinstance(quasi, list):
        raise ValueError('quasi_invalid')
    quasi_keys = set()
    for item in quasi:
        if not isinstance(item, dict) or not item.get('subject') or not item.get('field'):
            raise ValueError('quasi_entry_invalid')
        if str(item['subject']) not in subject_ids:
            raise ValueError('quasi_subject_unknown')
        if not isinstance(item.get('value'), str) or not item['value']:
            raise ValueError('quasi_value_invalid')
        key = (str(item['subject']), item['field'])
        if key in quasi_keys:
            raise ValueError('quasi_field_duplicate')
        quasi_keys.add(key)
    declared = payload.get('quasi_fields')
    if declared is not None:
        if not isinstance(declared, list) or not all(isinstance(f, str) and f for f in declared):
            raise ValueError('quasi_fields_invalid')
        for item in quasi:
            if item['field'] not in declared:
                raise ValueError('quasi_field_not_declared')
    return payload


# ---------------------------------------------------------------------------
# identity literals and cross-block resolution
# ---------------------------------------------------------------------------

def _collect_identity_strings(payload):
    """Return the literal identity strings; subject binding happens per scene."""
    strings = []
    for subject in payload.get('subjects', []):
        for name in subject.get('persona_names', []):
            strings.append({'raw': name, 'subject': str(subject['id']),
                            'kind': 'email' if EMAIL.fullmatch(name) else 'persona',
                            'scenes': set(subject.get('scenes') or []),
                            'source': f'persona:{subject["id"]}'})
    for alias in payload.get('aliases', []):
        ok, reason = _evidence_ok(alias.get('evidence'))
        strings.append({'raw': alias['alias'],
                        'subject': str(alias['subject']) if alias.get('subject') and ok else None,
                        'kind': 'alias',
                        'scenes': {str(alias['scene'])},
                        'source': f'alias:{alias["id"]}',
                        'alias_id': str(alias['id']),
                        'evidence': alias.get('evidence'),
                        'evidence_ok': ok,
                        'evidence_reason': reason})
    return strings


def _occurrence(cue_id, segment, start, end, raw, kind, subject, status,
                source, evidence=None, alias_id=None):
    return {'cue': cue_id, 'segment': segment, 'start': start, 'end': end,
            'raw': raw, 'kind': kind, 'subject': subject, 'status': status,
            'source': source,
            **({'evidence': evidence} if evidence is not None else {}),
            **({'alias_id': alias_id} if alias_id is not None else {})}


class _Literal:
    """A finditer-compatible match for literal identity strings."""

    def __init__(self, raw, start, end):
        self._raw, self._start, self._end = raw, start, end

    def start(self):
        return self._start

    def end(self):
        return self._end

    def group(self):
        return self._raw


def _scan_scene(items, identity_strings):
    """Resolve identity occurrences inside one scene (or one lone cue).

    Text is de-wrapped across adjacent blocks before matching, and a match that
    spans blocks is split back into one occurrence per affected block line so
    every location can be masked.  A literal is only bound to a subject inside
    that subject's declared scenes and only through an authorized alias, so two
    different people who happen to share a short name in different scenes stay
    two people.
    """
    # Blocks are de-wrapped in the editor's export order, not by start time:
    # the wrapped fragment follows the block order the editor produced.
    ordered = sorted(items, key=lambda pair: pair[0])
    cues = [cue for _, cue in ordered]
    scene_key = _scene_key(ordered[0][1], ordered[0][0])

    applicable = []
    owners = {}
    out_of_scene = set()
    for item in identity_strings:
        if item['kind'] == 'alias':
            # An alias is explicitly scoped to the scene it was maintained for.
            if item['scenes'] == {scene_key}:
                applicable.append(item)
                if item['subject'] is not None:
                    owners.setdefault(item['raw'], set()).add(item['subject'])
        else:
            applicable.append(item)
            if item['scenes'] and scene_key not in item['scenes']:
                out_of_scene.add(item['raw'])
                continue
            if item['subject'] is not None:
                owners.setdefault(item['raw'], set()).add(item['subject'])

    stream, mapping = _scene_stream(cues)
    occurrences = []
    seen = set()

    def add(raw, kind, subject, status, source, match, evidence=None, alias_id=None):
        for run in _split_to_blocks(mapping, match.start(), match.end()):
            key = (run['cue'], run['segment'], run['start'], run['end'], raw)
            if key in seen:
                continue
            seen.add(key)
            occurrences.append(_occurrence(run['cue'], run['segment'], run['start'],
                                           run['end'], raw, kind, subject, status,
                                           source, evidence, alias_id))

    def bind(raw, matches, kind):
        found = owners.get(raw, set())
        if len(found) > 1:
            status, subject = 'ambiguous', None
        elif len(found) == 1:
            status, subject = 'resolved', next(iter(found))
        elif raw in out_of_scene:
            # The literal belongs to another scene.  It is a different person,
            # never "the same short name", so it is listed for review and the
            # fixture will not publish this cue externally.
            status, subject = 'pending', None
        elif any(item['kind'] == 'alias' for item in applicable if item['raw'] == raw):
            status, subject = 'pending', None  # alias whose evidence did not bind
        else:
            status, subject = 'pending', None
        for match in matches:
            if subject is not None:
                chosen = next(item for item in applicable
                              if item['raw'] == raw and item['subject'] == subject)
                source = chosen['source']
                evidence = chosen.get('evidence')
                alias_id = chosen.get('alias_id')
            else:
                source = (f'{kind}:ambiguous' if status == 'ambiguous'
                          else f'{kind}:out_of_scene' if raw in out_of_scene
                          else f'{kind}:pending')
                candidates = [item for item in applicable if item['raw'] == raw]
                evidence = next((item.get('evidence') for item in candidates
                                 if item.get('evidence')), None)
                alias_id = next((item.get('alias_id') for item in candidates
                                 if item.get('alias_id')), None)
            add(raw, kind, subject, status, source, match, evidence, alias_id)

    for match in EMAIL.finditer(stream):
        raw = match.group()
        if raw in owners or raw in out_of_scene:
            bind(raw, [match], 'email')
        else:
            # An unlisted contact still has to be masked and forms its own
            # subject; it simply has no quasi-identifier profile.
            add(raw, 'email', f'implicit-email:{_digest(raw)[:12]}', 'resolved',
                'email:implicit', match)

    literal_raws = []
    for item in applicable:
        if item['kind'] == 'email' or item['raw'] in literal_raws:
            continue
        literal_raws.append(item['raw'])
    for raw in literal_raws:
        matches = [_Literal(raw, start, end) for start, end in _find_all(stream, raw)]
        if not matches:
            continue
        kind = 'alias' if all(item['kind'] == 'alias' for item in applicable
                              if item['raw'] == raw) else 'persona'
        bind(raw, matches, kind)
    return occurrences


def _scene_runs(items):
    """Split a scene's blocks into runs of *adjacent* export blocks.

    A wrapped identifier may only be recovered across blocks that are adjacent
    in the editor export.  Matching the whole scene as one string would join
    fragments from blocks that are separated by another scene's cues, inventing
    an identifier nobody spoke.
    """
    ordered = sorted(items, key=lambda pair: pair[0])
    runs = []
    current = []
    previous = None
    for index, cue in ordered:
        if previous is not None and index != previous + 1:
            runs.append(current)
            current = []
        current.append((index, cue))
        previous = index
    if current:
        runs.append(current)
    return runs


def _scene_groups(payload):
    groups = {}
    for index, cue in enumerate(payload['rows']):
        groups.setdefault(_scene_key(cue, index), []).append((index, cue))
    return groups


def _pseudonym(subject, audience, project, vendor):
    if audience == 'internal':
        return '角色' + _digest('internal', project or '', subject)[:4].upper()
    return '主体' + _digest('external', project or '', vendor or '', subject)[:6].upper()


def _speaker_pseudonym(speaker, audience, project, vendor):
    salt = 'internal' if audience == 'internal' else f'external|{vendor}'
    return '说话人' + _digest(salt, project or '', speaker)[:5].upper()


# ---------------------------------------------------------------------------
# quasi-identifier grouping (external audience)
# ---------------------------------------------------------------------------

def _field_option_sets(members, fields, profiles, levels, suppress_cost):
    """Per-field publish options for one candidate group.

    Returns ``None`` when the members cannot be made equal on every declared
    field.  Each option is ``(kind, published, cost, suppressed)``.
    """
    options = []
    for field in fields:
        values = [profiles[member].get(field) for member in members]
        if all(value is None for value in values):
            options.append([('absent', None, 0, False)])
            continue
        if any(value is None for value in values):
            # Some members have no value for this field and others do.  The
            # field cannot be made equal, and inventing a value for the missing
            # members is not acceptable, so it is suppressed for the members
            # that do have one.  Publishing it unchanged would leave those
            # members distinguishable inside their own three-person group.
            present_count = sum(1 for value in values if value is not None)
            options.append([('suppress', SUPPRESS,
                             suppress_cost * present_count, True)])
            continue
        top = max(_max_level(levels.get((member, field))) for member in members)
        field_options = []
        seen_published = set()
        for level in range(top + 1):
            published = []
            cost = 0
            for member, value in zip(members, values):
                out = _value_at(levels.get((member, field)), value, level)
                published.append(out)
                if out != value:
                    cost += level
            if len(set(published)) == 1 and published[0] not in seen_published:
                seen_published.add(published[0])
                field_options.append(('generalise', published[0], cost, False))
        field_options.append(('suppress', SUPPRESS, suppress_cost * len(members), True))
        options.append(field_options)
    return options


def _group_plan(members, fields, profiles, levels, readability):
    """Cheapest readable publish plan for a candidate group, or ``None``."""
    suppress_cost = readability['suppress_cost']
    options = _field_option_sets(members, fields, profiles, levels, suppress_cost)
    if options is None:
        return None
    present = [field for field in fields
               if any(profiles[member].get(field) is not None for member in members)]
    best = None
    for combination in itertools.product(*options):
        profile = {}
        cost = 0
        suppressed = 0
        for field, option in zip(fields, combination):
            kind, published, field_cost, is_suppressed = option
            cost += field_cost
            if kind == 'absent':
                continue
            profile[field] = published
            if is_suppressed:
                suppressed += 1
        if suppressed > readability['max_suppressed_fields']:
            continue
        if present and readability['require_retained_field'] and suppressed == len(present):
            continue
        signature = tuple(sorted(profile.items()))
        candidate = (cost, suppressed, signature, profile)
        if best is None or candidate[:3] < best[:3]:
            best = candidate
    if best is None:
        return None
    return {'members': sorted(members), 'profile': best[3], 'cost': best[0],
            'suppressed_fields': best[1]}


def _partition(subjects, fields, profiles, levels, readability, allow_skip=False):
    """Exact minimum-loss partition of subjects into publishable groups.

    This is a set-partition dynamic programme over all subjects, not a
    field-by-field greedy pass: a field is never fixed just because it happens
    to put three subjects together while leaving a cross-field unique
    combination behind.
    """
    count = len(subjects)
    full = (1 << count) - 1
    skip_cost = 10 ** 6
    cache = {}

    def plan_for(mask):
        if mask not in cache:
            members = [subjects[i] for i in range(count) if mask & (1 << i)]
            cache[mask] = (_group_plan(members, fields, profiles, levels, readability)
                           if len(members) >= 3 else None)
        return cache[mask]

    infinity = 10 ** 12
    dp = [infinity] * (1 << count)
    back = [None] * (1 << count)
    dp[0] = 0
    for mask in range(1 << count):
        if dp[mask] >= infinity:
            continue
        remaining = full ^ mask
        if not remaining:
            continue
        low = remaining & -remaining
        if allow_skip:
            skipped_mask = mask | low
            if dp[skipped_mask] > dp[mask] + skip_cost:
                dp[skipped_mask] = dp[mask] + skip_cost
                back[skipped_mask] = ('skip', low)
        sub = remaining
        while sub:
            if sub & low and sub.bit_count() >= 3:
                plan = plan_for(sub)
                if plan is not None and dp[mask | sub] > dp[mask] + plan['cost']:
                    dp[mask | sub] = dp[mask] + plan['cost']
                    back[mask | sub] = ('group', sub)
            sub = (sub - 1) & remaining
    groups = []
    skipped = []
    mask = full
    while mask:
        step = back[mask]
        if step is None:
            return None
        kind, sub = step
        if kind == 'group':
            groups.append(plan_for(sub))
        else:
            skipped.append(subjects[sub.bit_length() - 1])
        mask ^= sub
    return {'groups': sorted(groups, key=lambda g: g['members']),
            'skipped': sorted(skipped)}


def _minimal_conflict_fields(subject, pool, fields, profiles):
    present = [field for field in fields if profiles[subject].get(field) is not None]
    for size in range(1, len(present) + 1):
        for combination in itertools.combinations(present, size):
            unique = True
            for other in pool:
                if other == subject:
                    continue
                if all(profiles[other].get(field) == profiles[subject].get(field)
                       for field in combination):
                    unique = False
                    break
            if unique:
                return list(combination)
    return []


def _external_plan(subjects, quasi_rows, fields, readability):
    profiles = {subject: {} for subject in subjects}
    levels = {}
    for row in quasi_rows:
        subject = row['subject']
        if subject not in profiles:
            continue
        profiles[subject][row['field']] = row['raw']
        levels[(subject, row['field'])] = row['levels']
    for subject in subjects:
        for field in fields:
            profiles[subject].setdefault(field, None)
            levels.setdefault((subject, field),
                              _default_levels(field, profiles[subject][field] or ''))
    result = _partition(subjects, fields, profiles, levels, readability)
    if result is not None:
        return {'status': 'ok', 'groups': result['groups'], 'skipped': {},
                'conflict_fields': {}, 'reason': 'groups_satisfied'}
    fallback = _partition(subjects, fields, profiles, levels, readability, allow_skip=True)
    conflict = {}
    if fallback is not None:
        for subject in fallback['skipped']:
            conflict[subject] = _minimal_conflict_fields(subject, subjects, fields, profiles)
    reason = 'group_size_unsatisfiable' if subjects else 'no_subjects'
    return {'status': 'unsatisfiable',
            'groups': fallback['groups'] if fallback else [],
            'skipped': {s: conflict.get(s, []) for s in (fallback['skipped'] if fallback else [])},
            'conflict_fields': conflict, 'reason': reason}


# ---------------------------------------------------------------------------
# public scan entry point
# ---------------------------------------------------------------------------

def scan(path, file):
    with open(file, 'r', encoding='utf-8') as handle:
        raw_payload = json.load(handle)
    payload = _normalise_payload(raw_payload)
    authorization, auth_reason = _normalise_authorization(payload)
    project = payload.get('project')
    declared = payload.get('quasi_fields')
    quasi_fields = list(declared) if declared is not None else sorted(
        {item['field'] for item in payload.get('quasi', [])})
    readability = {'max_suppressed_fields': 2, 'suppress_cost': 4,
                   'require_retained_field': True}
    readability.update(payload.get('readability') or {})
    if not isinstance(readability['max_suppressed_fields'], int) or \
            readability['max_suppressed_fields'] < 0:
        raise ValueError('readability_invalid')

    source, _ = capture(path, 'desktop_privacy_source', file)
    db = connect(path)
    results = []
    try:
        with db:
            save(db, 'privacy_authorization', f'authorization:{source}', source,
                 payload['version'], {'authorization': authorization, 'project': project,
                                      'quasi_fields': quasi_fields,
                                      'readability': readability},
                 '已授权' if not authorization.get('legacy') else '待核', auth_reason)

            identity_strings = _collect_identity_strings(payload)
            scenes = _scene_groups(payload)
            all_occurrences = []
            for scene_key in sorted(scenes):
                for run in _scene_runs(scenes[scene_key]):
                    all_occurrences.extend(_scan_scene(run, identity_strings))

            per_cue = {}
            for occurrence in all_occurrences:
                per_cue.setdefault(occurrence['cue'], []).append(occurrence)

            subject_cues = {}
            subject_kinds = {}
            pending = {}
            for occurrence in all_occurrences:
                subject = occurrence['subject']
                if subject:
                    subject_cues.setdefault(subject, set()).add(occurrence['cue'])
                    subject_kinds.setdefault(subject, set()).add(occurrence['kind'])
                if occurrence['status'] != 'resolved':
                    owner = occurrence.get('alias_id') or occurrence['raw']
                    pending.setdefault(owner, set()).add(occurrence['cue'])

            for index, row in enumerate(payload['rows']):
                key = str(row['id'])
                occurrences = sorted(per_cue.get(key, []), key=lambda o: (
                    o['segment'], o['start'], o['end'], o['raw']))
                # Each finding is expanded with every block location where that
                # literal appears, so a fragment wrapped over C1+C2 is fully
                # traceable from either row.
                entity_spans = {}
                for occurrence in all_occurrences:
                    if occurrence['raw'] in {o['raw'] for o in occurrences}:
                        entity_spans.setdefault(occurrence['raw'], []).append(
                            {'cue': occurrence['cue'], 'segment': occurrence['segment'],
                             'start': occurrence['start'], 'end': occurrence['end']})
                emails = sorted({o['raw'] for o in occurrences if o['kind'] == 'email'})
                unresolved = any(o['status'] != 'resolved' for o in occurrences)
                status = '待遮盖' if occurrences else '无发现'
                if unresolved:
                    reason = 'identifier_pending'
                elif emails:
                    reason = 'email_entity'
                elif occurrences:
                    reason = 'identity_entity'
                else:
                    reason = 'no_direct_identifier'
                body = dict(row, findings=emails, occurrences=occurrences,
                            entity_spans=entity_spans,
                            cue_index=index, scene=row.get('scene'),
                            speaker=row.get('speaker'))
                save(db, 'privacy_source', key, source, payload['version'], body, status, reason)
                results.append(decide(db, '隐私扫描', key, status, reason, source))

            # Alias evidence is persisted separately: an insufficient entry is
            # listed for review and is never used to guess an identity.
            for item in identity_strings:
                if item['kind'] != 'alias':
                    continue
                alias_id = item['alias_id']
                occurrence_cues = sorted({o['cue'] for o in all_occurrences
                                          if o.get('alias_id') == alias_id})
                ok = item['evidence_ok']
                body = {'alias': item['raw'], 'subject': item['subject'],
                        'scene': sorted(item['scenes'])[0], 'evidence': item.get('evidence'),
                        'authorized': ok, 'occurrence_cues': occurrence_cues}
                if not ok:
                    body['occurrence_cues'] = sorted({
                        o['cue'] for o in all_occurrences if o['raw'] == item['raw']})
                save(db, 'privacy_alias', f'alias:{alias_id}', source, payload['version'],
                     body, '已核验' if ok else '待核',
                     'evidence_authorized' if ok else item['evidence_reason'])

            # Quasi identifiers, with the raw value and every ladder level.
            quasi_rows = []
            for item in payload.get('quasi', []):
                levels = _normalise_levels(item.get('levels'))
                if levels is None:
                    levels = _default_levels(item['field'], item['value'])
                row = {'subject': str(item['subject']), 'field': item['field'],
                       'raw': item['value'], 'levels': levels}
                quasi_rows.append(row)
                save(db, 'privacy_quasi', f'quasi:{item["subject"]}:{item["field"]}',
                     source, payload['version'], row, '已登记', 'quasi_declared')

            # Every subject that actually appears in the sample.  Subjects
            # without an occurrence are not counted towards a group; that would
            # be padding the group with people who are not in the release.
            # Contact-only subjects carry no quasi profile and are excluded.
            appearing = sorted(subject for subject in subject_cues
                               if not subject.startswith('implicit-email:'))
            for subject in appearing:
                aliases = [{'alias': item['raw'], 'alias_id': item['alias_id'],
                            'evidence': item.get('evidence'),
                            'authorized': item['evidence_ok']}
                           for item in identity_strings
                           if item['kind'] == 'alias' and item['subject'] == subject]
                body = {'subject': subject, 'resolved': True,
                        'kinds': sorted(subject_kinds.get(subject, set())),
                        'cues': sorted(subject_cues[subject]),
                        'occurrence_count': sum(1 for o in all_occurrences
                                                if o['subject'] == subject),
                        'aliases': aliases,
                        'quasi': {row['field']: row['raw'] for row in quasi_rows
                                  if row['subject'] == subject}}
                save(db, 'privacy_entity', f'subject:{subject}', source, payload['version'],
                     body, '已识别', 'subject_resolved')
            alias_ids = {item['alias_id']: item for item in identity_strings
                         if item['kind'] == 'alias'}
            for owner in sorted(pending):
                cue_ids = sorted(pending[owner])
                owner_occurrences = [o for o in all_occurrences
                                     if (o.get('alias_id') or o['raw']) == owner]
                if owner in alias_ids:
                    if identity_strings_ambiguous(alias_ids[owner]['raw'],
                                                  identity_strings, payload):
                        reason = 'alias_ambiguous'
                    elif not alias_ids[owner]['evidence_ok']:
                        reason = 'alias_evidence_insufficient'
                    elif any(o['source'] == 'alias:out_of_scene'
                             for o in owner_occurrences):
                        reason = 'alias_scene_out_of_scope'
                    else:
                        reason = 'alias_evidence_insufficient'
                elif any(o['source'].endswith(':out_of_scene')
                         for o in owner_occurrences):
                    reason = 'short_name_other_scene'
                else:
                    reason = 'identity_ambiguous'
                body = {'subject': None, 'resolved': False, 'pending': owner,
                        'cues': cue_ids, 'reason': reason}
                save(db, 'privacy_entity', f'pending:{owner}', source, payload['version'],
                     body, '待核', reason)

            # Pseudonyms are materialised per audience.  The internal label is
            # stable across scenes; the external label is isolated per project
            # and per vendor, so two suppliers cannot link their samples.
            for subject in appearing:
                for entry in authorization['audiences']:
                    audience = entry['audience']
                    vendor = entry.get('vendor') if audience == 'external' else None
                    label = _pseudonym(subject, audience, project, vendor)
                    save(db, 'privacy_pseudonym',
                         f'pseudonym:{subject}:{audience}:{vendor or "-"}', source,
                         payload['version'],
                         {'subject': subject, 'audience': audience, 'vendor': vendor,
                          'pseudonym': label}, '已生成', 'audience_scoped_pseudonym')

            plan = _external_plan(appearing, quasi_rows, quasi_fields, readability)
            plan_body = {'audience': 'external', 'project': project,
                         'quasi_fields': quasi_fields, 'readability': readability, **plan}
            plan_status = '已生成' if plan['status'] == 'ok' else '待核'
            save(db, 'privacy_entity', 'plan:external', source, payload['version'],
                 plan_body, plan_status, plan['reason'])

            unresolved_any = any(o['status'] != 'resolved' for o in all_occurrences)
            summary_status = '待核' if (
                authorization.get('legacy') or plan['status'] != 'ok'
                or unresolved_any) else '已扫描'
            results.append(decide(db, '隐私扫描', f'scan:{source}', summary_status,
                                  auth_reason, source))
        return results
    finally:
        db.close()


def identity_strings_ambiguous(raw, identity_strings, payload):
    owners = {item['subject'] for item in identity_strings
              if item['raw'] == raw and item['subject'] is not None}
    return len(owners) > 1
