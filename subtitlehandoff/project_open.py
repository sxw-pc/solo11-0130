"""工程可打开核验与全工程一致的插件版本组合求解。

核验不再只比较插件名：工程可以声明版本范围、编辑器 ABI、插件依赖与
排斥关系；可选插件缺失只影响相关导出功能，不影响工程可读。求解器在
离线签发包与当前活动版本之间，为全部工程寻找一致组合，并按最少改动、
最少关闭旧工程排序；无解时给出最小冲突与隔离工作区方案。
"""
import hashlib
import itertools
import json
from .ingest import capture
from .store import connect, save, decide, active_plugins

MAX_PLUGINS = 8
MAX_VERSIONS = 3


# ---------- 版本与版本范围 ----------

def parse_version(text):
    parts = []
    for part in str(text).split('.'):
        parts.append(int(part) if part.isdigit() else part)
    return tuple(parts)


def parse_spec(spec):
    """' >=2 , <3 ' → [('>=', (2,)), ('<', (3,))]；空规格匹配任意版本。"""
    if spec is None or str(spec).strip() == '':
        return []
    clauses = []
    for clause in str(spec).split(','):
        clause = clause.strip()
        for op in ('>=', '<=', '==', '>', '<', '='):
            if clause.startswith(op):
                clauses.append(('==' if op == '=' else op,
                                parse_version(clause[len(op):].strip())))
                break
        else:
            clauses.append(('==', parse_version(clause)))
    return clauses


def _sort_key(parts):
    """数字段按数值比较，非数字段按字符串比较；类型不跨类比较。"""
    return tuple((0, p) if isinstance(p, int) else (1, str(p)) for p in parts)


def satisfies(release, spec):
    version = parse_version(release)
    for op, wanted in parse_spec(spec):
        # 数字段与非数字段混排时补齐再比较，避免 TypeError
        width = max(len(version), len(wanted))
        left = _sort_key(version + (0,) * (width - len(version)))
        right = _sort_key(wanted + (0,) * (width - len(wanted)))
        if op == '>=' and not left >= right:
            return False
        if op == '<=' and not left <= right:
            return False
        if op == '>' and not left > right:
            return False
        if op == '<' and not left < right:
            return False
        if op == '==' and left != right:
            return False
    return True


def normalize_requirements(row):
    """兼容旧版纯名称列表与新版带规格条目。"""
    out = []
    for item in row.get('required_plugins', []):
        if isinstance(item, str):
            out.append({'name': item, 'spec': None, 'optional': False, 'feature': None})
        else:
            out.append({'name': str(item['name']), 'spec': item.get('spec'),
                        'optional': bool(item.get('optional', False)),
                        'feature': item.get('feature')})
    return out


# ---------- 本地目录：活动版本 + 离线签发包 ----------

def build_catalog(db):
    """返回 {(name, release): {abi, requires, conflicts}} 与可选版本清单。"""
    catalog = {}
    for row in db.execute("SELECT * FROM plugin_inventory"):
        body = json.loads(row['body'])
        catalog[(str(body['name']), str(body.get('release', '')))] = {
            'abi': str(body.get('abi', '')) if body.get('abi') is not None else '',
            'requires': body.get('requires', {}) or {},
            'conflicts': body.get('conflicts', []) or []}
    for row in db.execute("SELECT * FROM signed_package WHERE status='已签发'"):
        body = json.loads(row['body'])
        catalog[(str(body['name']), str(body['release']))] = {
            'abi': str(body.get('abi', '')),
            'requires': body.get('requires', {}) or {},
            'conflicts': body.get('conflicts', []) or []}
    return catalog


def candidate_releases(db, station_abi, names):
    """每个插件的可选版本 = 当前活动版本 + ABI 相符的已签发本地包，去重并截断三版。"""
    result = {}
    current = active_plugins(db)
    for name in names:
        releases = []
        if name in current:
            releases.append(current[name])
        for row in db.execute("SELECT body FROM signed_package WHERE status='已签发'"):
            body = json.loads(row['body'])
            if str(body['name']) == name and str(body.get('abi')) == str(station_abi) \
                    and str(body['release']) not in releases:
                releases.append(str(body['release']))
        releases = sorted(set(releases), key=lambda r: _sort_key(parse_version(r)))[:MAX_VERSIONS]
        result[name] = releases
    return result


def dependency_closure(names, catalog):
    """沿本地目录的 requires 求传递依赖闭包（运行库等隐式插件）。"""
    closure = set(names)
    while True:
        grown = set(closure)
        for name in closure:
            for (cand_name, _cand_release), meta in catalog.items():
                if cand_name == name:
                    grown.update(meta.get('requires') or {})
        if grown == closure:
            return sorted(closure)
        closure = grown


# ---------- 单工程核验 ----------

def evaluate_project(row, current, catalog, station_abi):
    reqs = normalize_requirements(row)
    hard_missing, optional_missing, conflicts = [], [], []
    per_plugin = []
    for req in reqs:
        name, spec = req['name'], req['spec']
        release = current.get(name)
        entry = {'plugin': name, 'spec': spec, 'optional': req['optional'],
                 'feature': req['feature'], 'active_release': release}
        if release is None:
            entry.update(satisfied=False, code='plugin_name_missing',
                         detail=f'{name} 未安装')
            (optional_missing if req['optional'] else hard_missing).append(entry)
            per_plugin.append(entry)
            continue
        # 版本范围与 ABI 独立判定：规格不满足时 ABI 不符也要留痕
        meta = catalog.get((name, release), {})
        if spec and not satisfies(release, spec):
            entry.update(satisfied=False, code='plugin_version_mismatch',
                         detail=f'{name} {release} 不满足 {spec}')
            (optional_missing if req['optional'] else hard_missing).append(entry)
        elif station_abi and meta.get('abi') and meta['abi'] != str(station_abi):
            entry.update(satisfied=False, code='abi_mismatch',
                         detail=f'{name} ABI {meta["abi"]} != 编辑器 {station_abi}')
            hard_missing.append(entry)
        else:
            entry.update(satisfied=True, code='requirement_satisfied', detail='')
        per_plugin.append(entry)
    # 依赖闭包与排斥关系基于活动环境整体判断
    for name, release in current.items():
        meta = catalog.get((name, release))
        if not meta:
            continue
        for dep, dep_spec in (meta.get('requires') or {}).items():
            dep_release = current.get(dep)
            if dep_release is None or (dep_spec and not satisfies(dep_release, dep_spec)):
                conflicts.append({'code': 'dependency_conflict',
                                  'detail': f'{name} {release} 需要 {dep} {dep_spec or "任意版本"}，'
                                            f'当前为 {dep_release or "缺失"}'})
        for ban in meta.get('conflicts') or []:
            other = current.get(str(ban['name']))
            if other is not None and (not ban.get('spec') or satisfies(other, ban['spec'])):
                conflicts.append({'code': 'plugin_conflict',
                                  'detail': f'{name} {release} 排斥 {ban["name"]} {other}'})
    readable = not hard_missing
    export_complete = readable and not optional_missing and not conflicts
    return {'requirements': per_plugin, 'hard': hard_missing,
            'optional_missing': optional_missing, 'conflicts': conflicts,
            'readable': readable, 'export_complete': export_complete}


def check(path, file, station_id=None):
    source, payload = capture(path, 'desktop_project_requirements', file)
    db = connect(path)
    results = []
    try:
        with db:
            profile = None
            if station_id is not None:
                row = db.execute('SELECT * FROM station_profile WHERE id=?',
                                 (str(station_id),)).fetchone()
                profile = json.loads(row['body']) if row else None
            current = active_plugins(db)
            catalog = build_catalog(db)
            station_abi = profile['editor_abi'] if profile else None
            for row in payload.get('rows', []):
                key = str(row['id'])
                ev = evaluate_project(row, current, catalog, station_abi)
                hard = ev['hard'][0] if ev['hard'] else None
                if hard:
                    status, reason = ('待安装', 'plugin_name_missing') \
                        if hard['code'] == 'plugin_name_missing' \
                        else ('待处理', hard['code'])
                elif ev['conflicts']:
                    status, reason = '待处理', ev['conflicts'][0]['code']
                elif not ev['export_complete']:
                    status, reason = '可读导出受限', 'optional_plugin_missing'
                else:
                    status, reason = '可打开', 'plugin_requirements_satisfied'
                save(db, 'project_open', key, source, payload['version'], row, status, reason)
                # 约束表 body 只冻结工程声明的需求证据（不可变）；评估结论走
                # status/reason，环境升级后同文件重放只更新结论，不触发身份冲突
                for entry in ev['requirements']:
                    evidence = {'project': key, 'plugin': entry['plugin'],
                                'spec': entry['spec'], 'optional': entry['optional'],
                                'feature': entry['feature']}
                    save(db, 'plugin_constraint', f'{key}:{entry["plugin"]}', source,
                         payload['version'], evidence,
                         '满足' if entry['satisfied'] else '不满足', entry['code'])
                for conflict in ev['conflicts']:
                    cid = f'{key}:conflict:' \
                          f'{hashlib.sha1(conflict["detail"].encode()).hexdigest()[:8]}'
                    save(db, 'plugin_constraint', cid, source, payload['version'],
                         {'project': key, 'code': conflict['code'],
                          'detail': conflict['detail']}, '冲突', conflict['code'])
                results.append(decide(db, '工程打开核验', key, status, reason, source))
        return results
    finally:
        db.close()


# ---------- 全工程一致组合求解 ----------

def assignment_viable(assignment, projects, catalog):
    """返回 (硬约束是否全满足, 未满足的可选需求明细)。"""
    unsatisfied_optional, hard_hits = [], []
    for project in projects:
        for req in normalize_requirements(project):
            release = assignment.get(req['name'])
            ok = release is not None and (not req['spec'] or satisfies(release, req['spec']))
            if not ok:
                hit = {'project': project['id'], 'plugin': req['name'], 'spec': req['spec'],
                       'feature': req['feature'], 'release': release}
                (unsatisfied_optional if req['optional'] else hard_hits).append(hit)
    if hard_hits:
        return False, hard_hits, unsatisfied_optional
    for name, release in assignment.items():
        meta = catalog.get((name, release), {})
        for dep, dep_spec in (meta.get('requires') or {}).items():
            dep_release = assignment.get(dep)
            if dep_release is None or (dep_spec and not satisfies(dep_release, dep_spec)):
                return False, [{'project': '*', 'plugin': name, 'spec': dep_spec,
                                'feature': f'依赖 {dep}', 'release': dep_release}], unsatisfied_optional
        for ban in meta.get('conflicts') or []:
            other = assignment.get(str(ban['name']))
            if other is not None and (not ban.get('spec') or satisfies(other, ban['spec'])):
                return False, [{'project': '*', 'plugin': name, 'spec': ban.get('spec'),
                                'feature': f'排斥 {ban["name"]}', 'release': other}], unsatisfied_optional
    return True, [], unsatisfied_optional


def project_readable_under(assignment, project):
    return all(assignment.get(r['name']) is not None
               and (not r['spec'] or satisfies(assignment[r['name']], r['spec']))
               for r in normalize_requirements(project) if not r['optional'])


def _order_installs(installs, catalog):
    """被依赖者（运行库）先装，依赖者后装；失败时不会出现新库配旧插件。"""
    ordered, remaining = [], list(installs)
    while remaining:
        progressed = False
        for step in list(remaining):
            meta = catalog.get((step['plugin'], step['release']), {})
            deps = set((meta.get('requires') or {}).keys())
            if not any(d['plugin'] in deps for d in remaining if d is not step):
                ordered.append(step)
                remaining.remove(step)
                progressed = True
        if not progressed:  # 依赖环：剩余按名称确定序，保证可重放
            remaining.sort(key=lambda s: s['plugin'])
            ordered.extend(remaining)
            break
    return ordered


def solve_plan(path, station_id, project_ids=None):
    db = connect(path)
    try:
        station = db.execute('SELECT * FROM station_profile WHERE id=?', (str(station_id),)).fetchone()
        if not station:
            raise ValueError('station_not_registered')
        profile = json.loads(station['body'])
        project_rows = [dict(r) for r in db.execute('SELECT * FROM project_open ORDER BY id')]
        if project_ids is not None:
            wanted = {str(x) for x in project_ids}
            project_rows = [r for r in project_rows if r['id'] in wanted]
        projects = []
        for r in project_rows:
            body = json.loads(r['body'])
            merged = {k: r[k] for k in r.keys()}
            merged.pop('body', None)
            merged.update(body)  # 工程原始字段与表列合并，body 列不再覆盖
            projects.append(merged)
        if len(projects) > MAX_PLUGINS:
            raise ValueError('too_many_projects')
        catalog = build_catalog(db)
        explicit = sorted({req['name'] for p in projects for req in normalize_requirements(p)})
        hard_explicit = sorted({n for n in explicit if any(
            req['name'] == n and not req['optional']
            for p in projects for req in normalize_requirements(p))})
        # 组合空间 = 硬性需求（含传递依赖）；可选插件单独计数
        names = dependency_closure(hard_explicit, catalog)
        optional_names = sorted(set(explicit) - set(hard_explicit))
        candidates = candidate_releases(db, profile['editor_abi'], names)
        if len(set(names) | set(optional_names)) > MAX_PLUGINS:
            raise ValueError('too_many_plugins')        # 离线缺口 = 硬性需求里，本地可选版本（活动版本 + 已签发且 ABI 相符的包）
        # 没有任何一个能满足全部工程硬规格的插件。不可选包（未签发/篡改/ABI 不符）
        # 与无包等同，绝不触发联网补包。
        signed_rows = [json.loads(r['body']) for r in db.execute(
            "SELECT body FROM signed_package WHERE status='已签发'")]
        selectable = {str(b['name']) for b in signed_rows
                      if str(b.get('abi')) == str(profile['editor_abi'])}
        current = active_plugins(db)
        hard_specs = {}
        for p in projects:
            for req in normalize_requirements(p):
                if not req['optional']:
                    hard_specs.setdefault(req['name'], []).append(req['spec'])
        missing_pool = set()
        for name, specs in hard_specs.items():
            local_releases = set(candidates.get(name, []))
            if name in current:
                local_releases.add(current[name])
            if name not in selectable and (
                    not local_releases
                    or any(not any(satisfies(rel, spec) for rel in local_releases)
                           for spec in specs)):
                missing_pool.add(name)
        missing_pool = sorted(missing_pool)
        current_readable = {p['id']: project_readable_under(current, p) for p in projects}

        def search(scoped_projects):
            """在 scoped_projects 的硬性需求上做笛卡尔枚举（≤8 插件 × 3 版）。

            枚举空间含显式硬性需求及其传递依赖（如 OCR 依赖 runtime）；仅可选
            需求引用的插件不进入枚举：它的缺失只记入 optional_degraded，不阻断
            可行组合，体现“工程可读与完整导出能力分开”。
            """
            mandatory = dependency_closure(
                sorted({req['name'] for p in scoped_projects
                        for req in normalize_requirements(p) if not req['optional']}), catalog)
            pool = candidate_releases(db, profile['editor_abi'], mandatory)
            if any(not pool[n] for n in mandatory):
                return []
            combos = []
            for chosen in itertools.product(*(pool[n] for n in mandatory)):
                assignment = dict(zip(mandatory, chosen))
                ok, hard_hits, opt_missing = assignment_viable(assignment, scoped_projects, catalog)
                if not ok:
                    continue
                disrupted = sum(1 for p in scoped_projects
                                if current_readable.get(p['id']) and
                                not project_readable_under(assignment, p))
                changes = sum(1 for n, rel in assignment.items() if current.get(n) != rel)
                combos.append((changes, disrupted, len(opt_missing),
                               tuple(sorted(assignment.items())), assignment, opt_missing))
            combos.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
            return combos

        combos = search(projects)
        plan = {'station': str(station_id), 'projects': [p['id'] for p in projects],
                'editor_abi': profile['editor_abi']}
        if combos:
            _, _, _, _, assignment, opt_missing = combos[0]
            actions = _build_actions(assignment, current, db, catalog, profile)
            plan.update(feasible=True, target_assignment=assignment,
                        optional_degraded=opt_missing, actions=actions,
                        changes=sum(1 for n, rel in assignment.items() if current.get(n) != rel),
                        disrupted_projects=[p['id'] for p in projects
                                           if current_readable.get(p['id']) and
                                           not project_readable_under(assignment, p)],
                        workspaces=[{'workspace': 'main', 'projects': plan['projects'],
                                     'environment': assignment}])
        else:
            # 最小冲突：枚举需要隔离的最小工程子集，其余工程仍求一致组合
            isolation = _minimal_isolation(projects, search, current, current_readable)
            plan.update(feasible=False, offline_gap=sorted(missing_pool), **isolation)
        plan['offline_gap'] = sorted(missing_pool)
        plan['plan_hash'] = hashlib.sha256(
            json.dumps(plan, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        plan_id = f'plan:{station_id}:{plan["plan_hash"]}'
        frozen = profile['purpose'] == '播出'
        status = '禁止变更' if frozen else '待执行'
        reason = 'station_frozen' if frozen else ('plan_ready' if plan['feasible']
                                                  else 'minimal_conflict_with_isolation')
        with db:
            save(db, 'install_plan', plan_id, station['source_id'], station['version'],
                 plan, status, reason)
            decide(db, '安装计划', plan_id, status, reason, station['source_id'])
        plan['plan_id'] = plan_id
        plan['status'] = status
        return plan
    finally:
        db.close()


def _build_actions(assignment, current, db, catalog, profile):
    actions = []
    installs = []
    package_lookup = {}
    for row in db.execute("SELECT id,body FROM signed_package WHERE status='已签发'"):
        body = json.loads(row['body'])
        package_lookup[(str(body['name']), str(body['release']))] = row['id']
    for name, release in sorted(assignment.items()):
        if current.get(name) == release:
            actions.append({'action': '保持', 'plugin': name, 'release': release})
        else:
            package_id = package_lookup.get((name, release))
            installs.append({'action': '安装', 'plugin': name, 'release': release,
                             # 活动版本升级外的安装必须落到已签发本地包；缺失即离线缺口
                             'package_id': package_id,
                             'package_available': package_id is not None})
    for name in sorted(set(current) - set(assignment)):
        actions.insert(0, {'action': '卸载', 'plugin': name, 'release': current[name]})
    actions.extend(_order_installs(installs, catalog))
    return actions


def _minimal_isolation(projects, search, current, current_readable):
    """逐轮扩大隔离工程数，找到让剩余工程可解的最小隔离集。

    同规模隔离集之间优先选“隔离工作区沿用当前环境仍可读”的方案，避免把
    旧工程隔离后反而打开不了；其次按隔离集编号取得确定结果。
    """
    n = len(projects)
    for k in range(1, n + 1):
        candidates = []
        for isolated in itertools.combinations(projects, k):
            isolated_ids = {p['id'] for p in isolated}
            remaining = [p for p in projects if p['id'] not in isolated_ids]
            combos = search(remaining) if remaining else []
            if remaining and not combos:
                continue
            assignment = combos[0][4] if combos else {}
            isolated_readable = sum(1 for p in isolated
                                    if all(current.get(r['name']) is not None
                                           and (not r['spec'] or
                                                satisfies(current[r['name']], r['spec']))
                                           for r in normalize_requirements(p) if not r['optional']))
            # 隔离工作区沿用当前环境；主工作区扰动越小越好
            main_disrupted = combos[0][1] if combos else 0
            candidates.append((-isolated_readable, main_disrupted,
                               tuple(sorted(isolated_ids)),
                               isolated_ids, assignment,
                               combos[0][5] if combos else []))
        if candidates:
            candidates.sort(key=lambda c: (c[0], c[1], c[2]))
            _, _, _, isolated_ids, assignment, opt_missing = candidates[0]
            workspaces = [{'workspace': 'main',
                           'projects': [p['id'] for p in projects if p['id'] not in isolated_ids],
                           'environment': assignment},
                          {'workspace': 'isolation',
                           'projects': sorted(isolated_ids),
                           'environment': {n: r for n, r in current.items()}}]
            return {'isolated_projects': sorted(isolated_ids),
                    'minimal_conflict': [f'隔离 {len(isolated_ids)} 个工程后其余工程可解'],
                    'optional_degraded': opt_missing, 'workspaces': workspaces}
    return {'isolated_projects': [p['id'] for p in projects],
            'minimal_conflict': ['离线本地包无法满足任何工程组合，禁止联网补包'],
            'workspaces': [{'workspace': 'isolation', 'projects': [p['id'] for p in projects],
                            'environment': current}]}
