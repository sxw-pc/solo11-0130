import hashlib,json
from pathlib import Path
from .store import connect,TABLES,PLUGIN_TABLES,active_plugins
def build(path,out):
    db=connect(path)
    try:
        sources=[]
        for item in db.execute('SELECT * FROM export_file ORDER BY id'):
            x=dict(item);x.pop('body')
            x['verified']=hashlib.sha256(item['body']).hexdigest()==item['hash']
            sources.append(x)
        data={'sources':sources,'entities':{},'issues':[dict(x) for x in db.execute('SELECT * FROM issue')],
              'decisions':[dict(x) for x in db.execute('SELECT * FROM decision_log')]}
        for table in TABLES:
            data['entities'][table]=[dict(x) for x in db.execute(f'SELECT * FROM {table} ORDER BY id')]
        data['plugin_environment']=_plugin_environment(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()


def _plugin_environment(db):
    """派生视图：工程可读与完整导出能力分开报告，并附冲突依据与计划/回滚轨迹。"""
    stations=[dict(r) for r in db.execute('SELECT * FROM station_profile ORDER BY id')]
    projects=[]
    for row in db.execute('SELECT * FROM project_open ORDER BY id'):
        pid=row['id']
        constraints=[dict(c) for c in db.execute(
            'SELECT * FROM plugin_constraint WHERE id LIKE ? ORDER BY id',(f'{pid}:%',))]
        conflict_basis=[{'code':c['reason'],'detail':json.loads(c['body']).get('detail'),
                         'constraint_id':c['id']}
                        for c in constraints if c['status']=='冲突']
        hard=[json.loads(c['body']) for c in constraints if c['status']=='不满足'
              and not json.loads(c['body']).get('optional')]
        optional=[json.loads(c['body']) for c in constraints if c['status']=='不满足'
                  and json.loads(c['body']).get('optional')]
        projects.append({'project_id':pid,'status':row['status'],'reason':row['reason'],
                         'requirements_version':row['version'],
                         'readable': row['status'] in ('可打开','可读导出受限'),
                         'export_complete': row['status']=='可打开',
                         'unsatisfied_required':hard,'missing_optional':optional,
                         'conflict_basis':conflict_basis})
    return {
        'active_plugins':active_plugins(db),
        'stations':[{'station_id':r['id'],'purpose':json.loads(r['body']).get('purpose'),
                     'editor_abi':json.loads(r['body']).get('editor_abi'),
                     'status':r['status']} for r in stations],
        'packages':[{'package_id':r['id'],'status':r['status'],'reason':r['reason'],
                     'name':json.loads(r['body']).get('name'),
                     'release':json.loads(r['body']).get('release'),
                     'abi':json.loads(r['body']).get('abi')}
                    for r in db.execute('SELECT * FROM signed_package ORDER BY id')],
        'projects':projects,
        'snapshots':[{'snapshot_id':r['id'],'status':r['status'],'reason':r['reason'],
                      'inventory_versions':json.loads(r['body']).get('inventory_versions'),
                      'frozen_active':json.loads(r['body']).get('active')}
                     for r in db.execute('SELECT * FROM environment_snapshot ORDER BY id')],
        'plans':[{'plan_id':r['id'],'status':r['status'],'reason':r['reason'],
                  'feasible':json.loads(r['body']).get('feasible'),
                  'stations':json.loads(r['body']).get('station'),
                  'projects':json.loads(r['body']).get('projects'),
                  'actions':json.loads(r['body']).get('actions'),
                  'workspaces':json.loads(r['body']).get('workspaces'),
                  'offline_gap':json.loads(r['body']).get('offline_gap')}
                 for r in db.execute('SELECT * FROM install_plan ORDER BY id')],
        'runs':[dict(r) for r in db.execute('SELECT * FROM install_run ORDER BY id')],
        'rollbacks':[dict(r) for r in db.execute('SELECT * FROM rollback_record ORDER BY id')],
        'plugin_tables':PLUGIN_TABLES,
    }
