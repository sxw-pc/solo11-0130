"""Cross-language timing agreement for the same media release."""
import json
from .ingest import capture
from .store import connect,save,decide


def evaluate(path,file):
    source,payload=capture(path,'desktop_alignment_request',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);left=db.execute('SELECT * FROM subtitle_cue WHERE id=?',(str(row['left']),)).fetchone()
                right=db.execute('SELECT * FROM subtitle_cue WHERE id=?',(str(row['right']),)).fetchone()
                if not left or not right or left['status']!='可审校' or right['status']!='可审校':status,reason='待核','track_unavailable';shifts=[]
                else:
                    a=json.loads(left['body']);b=json.loads(right['body'])
                    if a['media_hash']!=b['media_hash'] or a['language']==b['language']:
                        status,reason='待核','incompatible_tracks';shifts=[]
                    else:
                        shifts=[abs(x['start']-y['start'])+abs(x['end']-y['end']) for x,y in zip(a['cues'],b['cues'])]
                        if len(a['cues'])!=len(b['cues']):status,reason='待同步','cue_count_mismatch'
                        elif any(x>float(row['max_total_shift']) for x in shifts):status,reason='待同步','timing_drift'
                        else:status,reason='已对齐','timing_agreement'
                save(db,'alignment_policy',key,source,payload['version'],row,status,reason)
                save(db,'alignment_pair',f'pair:{key}',source,payload['version'],
                     {'left':row['left'],'right':row['right'],'shifts':shifts},status,reason)
                results.append(decide(db,'跨语言对齐',key,status,reason,source))
        return results
    finally:db.close()
