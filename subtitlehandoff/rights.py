"""Client language rights, embargo windows and post-delivery withdrawal."""
import json
from datetime import datetime
from .ingest import capture
from .store import connect,save,decide


def register(path,file):
    source,payload=capture(path,'desktop_rights_export',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);start=datetime.fromisoformat(row['start']);end=datetime.fromisoformat(row['end'])
                if start>=end or not row.get('languages'):status,reason='拒绝','invalid_rights_scope'
                else:status,reason='已授权','rights_frozen'
                save(db,'client_contract',key,source,payload['version'],row,status,reason)
                save(db,'rights_window',f'window:{key}',source,payload['version'],
                     {'start':row['start'],'end':row['end'],'languages':row['languages']},status,reason)
                results.append(decide(db,'客户授权',key,status,reason,source))
        return results
    finally:db.close()


def check(db,client,languages,media_hash,at):
    contract=db.execute('SELECT * FROM client_contract WHERE id=?',(str(client),)).fetchone()
    if not contract or contract['status']!='已授权':return '待授权','contract_missing_or_revoked'
    body=json.loads(contract['body'])
    if body['media_hash']!=media_hash or not set(languages).issubset(set(body['languages'])):return '待授权','rights_scope_mismatch'
    instant=datetime.fromisoformat(at)
    if instant<datetime.fromisoformat(body['start']):return '待授权','embargo_active'
    if instant>datetime.fromisoformat(body['end']):return '待授权','rights_expired'
    return '已授权','rights_valid'


def revoke(path,file):
    source,payload=capture(path,'desktop_rights_revocation',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);contract=db.execute('SELECT * FROM client_contract WHERE id=?',(str(row['client']),)).fetchone()
                old=db.execute('SELECT * FROM rights_revocation WHERE id=?',(key,)).fetchone()
                if old:
                    if old['body']!=json.dumps(row,sort_keys=True,ensure_ascii=False):raise ValueError('revocation_identity_conflict')
                    results.append(decide(db,'客户授权',key,old['status'],old['reason'],source));continue
                if not contract or contract['status']!='已授权':status,reason='待核','contract_not_active'
                elif not row.get('approver') or not row.get('reason'):status,reason='待核','approval_missing'
                else:
                    status,reason='已撤销','approved_revocation'
                    db.execute("UPDATE client_contract SET status='已撤销',reason='approved_revocation' WHERE id=?",(str(row['client']),))
                    for package in db.execute("SELECT * FROM delivery_package WHERE status='已交付'"):
                        body=json.loads(package['body'])
                        if body.get('client')==row['client']:
                            db.execute("UPDATE delivery_package SET status='已撤回',reason='rights_revoked' WHERE id=?",(package['id'],))
                            save(db,'delivery_recall',f"recall:{package['id']}",source,payload['version'],
                                 {'package':package['id'],'revocation':key},'待回收','rights_revoked')
                save(db,'rights_revocation',key,source,payload['version'],row,status,reason)
                results.append(decide(db,'客户授权',key,status,reason,source))
        return results
    finally:db.close()
