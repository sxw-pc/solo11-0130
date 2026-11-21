import hashlib,json
from pathlib import Path
from .store import connect,TABLES,load
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
        data['qa_coverage']=_qa_coverage(db)
        out=Path(out);out.parent.mkdir(parents=True,exist_ok=True)
        out.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
        return out
    finally:db.close()


def _qa_coverage(db):
    """Trace every high-risk target conclusion back to a legal action record."""
    graph_row=db.execute('SELECT * FROM ui_state_graph ORDER BY id LIMIT 1').fetchone()
    if not graph_row:
        return {'graph_present':False,'suites':[]}
    graph=json.loads(graph_row['body'])
    edges={tr['id']:tr for tr in graph['transitions']}
    suites=[]
    for srow in db.execute('SELECT * FROM qa_suite ORDER BY id'):
        suite=json.loads(srow['body'])
        executions=[dict(x) for x in db.execute(
            'SELECT * FROM qa_execution ORDER BY id')]
        target_trace=[]
        for tg in graph['targets']:
            tid=tg['transition'];edge=edges.get(tid,{})
            planned=tg['id'] in suite.get('covered_targets',[])
            # Collect every verified walk of the target transition, then
            # prefer evidence that comes from a fully-passing execution.
            proofs=[]
            for er in executions:
                ebody=json.loads(er['body'])
                for step in ebody.get('steps',[]):
                    if step.get('transition')==tid and step.get('verified'):
                        proofs.append({'execution':er['id'],'case':ebody.get('case'),
                               'action':tid,'window_before':step['window_before'],
                               'window_after':step['window_after'],
                               'button_text':step['button_text'],
                               'visible_evidence':step['visible_evidence'],
                               'frame_id':step['frame_id'],
                               'execution_status':er['status']})
            proof=next((p for p in proofs if p['execution_status']=='通过'),
                       proofs[0] if proofs else None)
            if proof and proof['execution_status']=='通过':
                conclusion='covered'
            elif proof:
                conclusion='evidence_replayed_but_case_returned'
            else:
                blocker=next((u for u in suite.get('uncovered_targets',[])
                              if u.get('target')==tg['id']),None)
                conclusion='uncovered' if blocker else 'not_replayed'
            target_trace.append({'target':tg['id'],'name':tg.get('name',tg['id']),
                                 'required_transition':tid,
                                 'required_modal':tg.get('required_modal'),
                                 'planned':planned,'conclusion':conclusion,
                                 'legal_action_evidence':proof,
                                 'blocker':None if conclusion!='uncovered' else blocker,
                                 'button_text':edge.get('button_text'),
                                 'disambiguated_by_window':edge.get('from_state')})
        workspaces=[]
        for wrow in db.execute('SELECT * FROM ui_workspace ORDER BY id'):
            wbody=json.loads(wrow['body'])
            workspaces.append({'id':wrow['id'],'state':wbody.get('state'),
                               'snapshot':wbody.get('snapshot'),
                               'consumed_by':wbody.get('consumed_by',[])})
        suites.append({'suite':srow['id'],'contract':suite.get('contract'),
                       'status':srow['status'],'reason':srow['reason'],
                       'limits':suite.get('limits'),
                       'total_runtime_s':suite.get('total_runtime_s'),
                       'workspace_copies_used':suite.get('workspace_copies_used'),
                       'workspace_reservations':suite.get('workspace_reservations',[]),
                       'workspaces':workspaces,
                       'target_coverage':target_trace,
                       'rejected_cases':suite.get('rejected_cases',[])})
    return {'graph_present':True,'graph_id':graph_row['id'],
            'state_count':len(graph['states']),
            'transition_count':len(graph['transitions']),
            'target_count':len(graph['targets']),'suites':suites}
