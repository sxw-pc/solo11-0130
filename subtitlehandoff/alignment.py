"""Cross-language paragraph correspondence for the same media release.

When both tracks carry editor paragraph evidence the alignment is proved on
the shared canonical segmentation (merge/split allowed, order preserved) and
on sentence-level timing evidence.  Index-based ``zip`` matching is only a
fallback for legacy tracks without paragraph evidence and can never explain
different cue counts as a correspondence.
"""
import json
from .ingest import capture
from .store import connect,save,decide
from . import paragraphs as P
from .operations import _canonical


def evaluate(path,file):
    source,payload=capture(path,'desktop_alignment_request',file)
    db=connect(path);results=[]
    try:
        with db:
            for row in payload.get('rows',[]):
                key=str(row['id']);left=db.execute('SELECT * FROM subtitle_cue WHERE id=?',(str(row['left']),)).fetchone()
                right=db.execute('SELECT * FROM subtitle_cue WHERE id=?',(str(row['right']),)).fetchone()
                shifts=[];detail={}
                if not left or not right or left['status']!='可审校' or right['status']!='可审校':
                    status,reason='待核','track_unavailable'
                else:
                    a=json.loads(left['body']);b=json.loads(right['body'])
                    if a['media_hash']!=b['media_hash'] or a['language']==b['language']:
                        status,reason='待核','incompatible_tracks';shifts=[]
                    else:
                        ev_l=db.execute('SELECT * FROM paragraph_evidence WHERE id=?',(str(row['left']),)).fetchone()
                        ev_r=db.execute('SELECT * FROM paragraph_evidence WHERE id=?',(str(row['right']),)).fetchone()
                        if ev_l and ev_r:
                            status,reason,shifts,detail=_paragraph_mode(db,a,row,ev_l,ev_r)
                        else:
                            shifts=[abs(x['start']-y['start'])+abs(x['end']-y['end']) for x,y in zip(a['cues'],b['cues'])]
                            if len(a['cues'])!=len(b['cues']):status,reason='待同步','cue_count_mismatch'
                            elif any(x>float(row['max_total_shift']) for x in shifts):status,reason='待同步','timing_drift'
                            else:status,reason='已对齐','timing_agreement'
                            detail={}
                save(db,'alignment_policy',key,source,payload['version'],row,status,reason)
                save(db,'alignment_pair',f'pair:{key}',source,payload['version'],
                     {'left':row['left'],'right':row['right'],'shifts':shifts,
                      **detail},status,reason)
                results.append(decide(db,'跨语言对齐',key,status,reason,source))
        return results
    finally:db.close()


def _paragraph_mode(db,track_body,row,ev_l,ev_r):
    """Align two evidenced tracks against the shared canonical paragraphs.

    Pair checks are deliberately local: the same paragraph IDs must exist on
    both sides and their sentence timings must agree.  Order preservation is
    decided by the shared global mapping at delivery time, so locally
    plausible timings can never be mistaken for a common ordering.
    """
    left_body=json.loads(ev_l['body']);right_body=json.loads(ev_r['body'])
    media_hash=track_body['media_hash']
    canon_ids,paragraph_shot,shots=_canonical(db,media_hash)
    if not canon_ids:
        return '待核','canonical_segmentation_missing',[],{'conflicts':[],'correspondence':[]}
    conflicts,correspondence=P.pair_conflicts(canon_ids,left_body['evidence'],
                                              right_body['evidence'],row['max_total_shift'])
    shifts=[c['shift'] for c in correspondence]
    if conflicts:
        reason=conflicts[0]['code'];status='待同步'
    else:
        reason,status='paragraph_correspondence_aligned','已对齐'
    # Track-level evidence conflicts (e.g. reversed order) are carried as
    # evidence without failing the local pair; the delivery gate enforces them.
    detail={'conflicts':conflicts,'correspondence':correspondence,'canonical_order':canon_ids,
            'track_conflicts':{str(row['left']):left_body['conflicts'],
                               str(row['right']):right_body['conflicts']}}
    return status,reason,shifts,detail
