"""Paragraph-level cross-language correspondence.

The editor's canonical translation paragraphs (paragraph IDs such as U1/U2)
are the one segmentation shared by every delivered language.  A subtitle cue
may merge several consecutive paragraphs or share one paragraph across
adjacent cues, but the film order must always be preserved: the same text
repeated twice can never be paired across the paragraphs in between, and one
cue cannot point at two contradictory paragraph groups.

The functions here are pure: they take frozen evidence dictionaries and
return correspondence structures plus conflict records.  Persistence and
status wording live in :mod:`subtitlehandoff.alignment` and
:mod:`subtitlehandoff.operations`.
"""

# conflict / rejection codes
PARAGRAPH_UNKNOWN='paragraph_unknown'
EVIDENCE_INCOMPLETE='paragraph_evidence_incomplete'
PARAGRAPH_ORDER_CONFLICT='paragraph_order_conflict'
PARAGRAPH_MISSING='paragraph_missing'
SENTENCE_EVIDENCE_DRIFT='sentence_evidence_drift'
CROSS_SHOT_MERGE_GAP='cross_shot_merge_gap'
PARAGRAPH_TIMING_DRIFT='paragraph_timing_drift'
COMMON_MAPPING_CONFLICT='common_mapping_conflict'
SERVICE_POLICY_MISSING='service_policy_missing'
SENTENCE_CUE_REQUIRED='sentence_cue_required'


def canonical(paragraphs):
    """Return (ordered paragraph ids, id -> shot id) from the project export."""
    ordered=[str(p['id']) for p in paragraphs or []]
    if len(set(ordered))!=len(ordered):
        raise ValueError('duplicate_paragraph_id')
    return ordered,{str(p['id']):p.get('shot') for p in paragraphs or []}


def shot_map(shots):
    return {str(s['id']):(float(s['start']),float(s['end'])) for s in shots or []}


def _intervals_overlap_cut(start,end,shots):
    """Yield every shot boundary strictly inside an open cue interval."""
    for sid,(a,b) in shots.items():
        if start<a<end or start<b<end:
            yield sid,a,b


def validate_evidence(canon_ids,paragraph_shot,shots,cues,evidence_cues):
    """Validate one language track's paragraph evidence.

    ``cues`` are the timeline cues ordered by start; ``evidence_cues`` are the
    frozen ``{"cue_index", "paragraphs"}`` rows.  Returns a list of conflict
    dictionaries; an empty list means the track is a valid ordered walk over
    the canonical segmentation.
    """
    conflicts=[];index_of={pid:i for i,pid in enumerate(canon_ids)}
    by_index={int(e['cue_index']):e for e in evidence_cues}
    if set(by_index)!=set(range(len(cues))):
        conflicts.append({'code':EVIDENCE_INCOMPLETE,'paragraph':None,'cue_index':None,
                          'detail':'every cue must carry paragraph evidence'})
    last_pos=-1;covered=set()
    for i,cue in enumerate(cues):
        entry=by_index.get(i)
        if not entry or not entry.get('paragraphs'):
            conflicts.append({'code':EVIDENCE_INCOMPLETE,'paragraph':None,'cue_index':i,
                              'detail':'cue without paragraph evidence'})
            continue
        cue_start=float(cue['start']);cue_end=float(cue['end'])
        crossing=list(_intervals_overlap_cut(cue_start,cue_end,shots))
        previous_pid=None
        for p in entry['paragraphs']:
            pid=str(p['id'])
            if pid not in index_of:
                conflicts.append({'code':PARAGRAPH_UNKNOWN,'paragraph':pid,'cue_index':i,
                                  'detail':'paragraph id absent from project segmentation'})
                continue
            pos=index_of[pid]
            # Ordered walk over the canonical film segmentation: the first new
            # paragraph must be the first canonical one and each later new
            # paragraph must be exactly last_pos+1.  Re-announcing the current
            # paragraph (one paragraph shared by adjacent cues) is allowed.
            if pos<last_pos:
                conflicts.append({'code':PARAGRAPH_ORDER_CONFLICT,'paragraph':pid,'cue_index':i,
                                  'detail':'paragraph order reversed'})
            elif pos>last_pos:
                if last_pos==-1 and pos!=0:
                    conflicts.append({'code':PARAGRAPH_ORDER_CONFLICT,'paragraph':pid,'cue_index':i,
                                      'detail':f'paragraph at canonical position {pos} appears first'})
                elif last_pos!=-1 and pos!=last_pos+1 and previous_pid!=pid:
                    conflicts.append({'code':PARAGRAPH_ORDER_CONFLICT,'paragraph':pid,'cue_index':i,
                                      'detail':f'jumps from position {last_pos} to {pos}'})
                last_pos=pos
            previous_pid=pid;covered.add(pid)
            previous_pid=pid;covered.add(pid)
            try:
                p_start=float(p['start']);p_end=float(p['end'])
            except (KeyError,TypeError,ValueError):
                conflicts.append({'code':SENTENCE_EVIDENCE_DRIFT,'paragraph':pid,'cue_index':i,
                                  'detail':'sentence timing evidence missing'})
                continue
            if not (cue_start-1e-9<=p_start and p_end<=cue_end+1e-9 and p_start<p_end):
                conflicts.append({'code':SENTENCE_EVIDENCE_DRIFT,'paragraph':pid,'cue_index':i,
                                  'detail':'sentence evidence outside its cue interval'})
            # A cue crossing a shot cut must carry explicit in-shot sentence
            # evidence for every covered paragraph, otherwise the merge would
            # hide a missing sentence in the next shot.
            sid=paragraph_shot.get(pid)
            if crossing and sid in shots:
                a,b=shots[sid]
                if not (a-1e-9<=p_start and p_end<=b+1e-9):
                    conflicts.append({'code':CROSS_SHOT_MERGE_GAP,'paragraph':pid,'cue_index':i,
                                      'detail':f'cue crosses shot cut but paragraph {pid} has no in-shot evidence'})
    missing=[pid for pid in canon_ids if pid not in covered]
    for pid in missing:
        conflicts.append({'code':PARAGRAPH_MISSING,'paragraph':pid,'cue_index':None,
                          'detail':'canonical paragraph not covered by any cue'})
    return conflicts


def paragraph_hulls(evidence_cues):
    """Map paragraph id -> (min start, max end) and covering cue indices."""
    hull={}
    for entry in evidence_cues:
        i=int(entry['cue_index'])
        for p in entry.get('paragraphs',[]):
            pid=str(p['id']);lo=float(p['start']);hi=float(p['end'])
            if pid not in hull:hull[pid]=[lo,hi,[i]]
            else:
                hull[pid][0]=min(hull[pid][0],lo);hull[pid][1]=max(hull[pid][1],hi)
                if i not in hull[pid][2]:hull[pid][2].append(i)
    return hull


def pair_conflicts(canon_ids,left_view,right_view,max_total_shift):
    """Compare two evidenced tracks paragraph by paragraph.

    Returns ``(conflicts, correspondence)`` where correspondence is one
    reviewable row per canonical paragraph with the cue indices and sentence
    intervals on both sides.  Order violations are intentionally not checked
    here: they belong to the shared global mapping, so locally plausible
    timings cannot alone prove a common ordering.
    """
    conflicts=[];correspondence=[]
    left_hull=paragraph_hulls(left_view);right_hull=paragraph_hulls(right_view)
    for pid in canon_ids:
        if pid not in left_hull or pid not in right_hull:
            conflicts.append({'code':PARAGRAPH_MISSING,'paragraph':pid,'cue_index':None,
                              'detail':'paragraph missing on one side of the pair'})
            continue
        la,lb,lc=left_hull[pid];ra,rb,rc=right_hull[pid]
        shift=abs(la-ra)+abs(lb-rb)
        if shift>float(max_total_shift)+1e-9:
            conflicts.append({'code':PARAGRAPH_TIMING_DRIFT,'paragraph':pid,'cue_index':None,
                              'detail':f'shift {shift:.3f} exceeds {max_total_shift}'})
        correspondence.append({'paragraph':pid,'left_cues':lc,'right_cues':rc,
                               'left_interval':[la,lb],'right_interval':[ra,rb],'shift':shift})
    return conflicts,correspondence


def global_mapping(canon_ids,paragraph_shot,track_views,track_conflicts):
    """Build the shared segmentation mapping or aggregate conflicts.

    ``track_views`` maps track id -> evidence cue list; ``track_conflicts``
    maps track id -> validation conflict list.  Returns
    ``(mapping, conflicts)``; mapping is ``None`` when conflicts exist.
    """
    conflicts=[]
    for track_id,rows in track_conflicts.items():
        for row in rows:
            item=dict(row);item['track']=track_id;conflicts.append(item)
    if conflicts:
        return None,conflicts
    coverage={pid:{} for pid in canon_ids}
    for track_id,evidence_cues in track_views.items():
        hull=paragraph_hulls(evidence_cues)
        for pid in canon_ids:
            if pid in hull:coverage[pid][track_id]=hull[pid][2]
    mapping=[{'paragraph':pid,'shot':paragraph_shot.get(pid),
              'coverage':{track:indices for track,indices in per_track.items()}}
             for pid,per_track in coverage.items()]
    return mapping,[]


def service_gate(service_body,track_views,canon_ids):
    """Apply the pre-agreed service policy to the shared mapping."""
    if not service_body:
        return [{'code':SERVICE_POLICY_MISSING,'paragraph':None,'cue_index':None,
                 'detail':'no service policy registered before delivery'}]
    conflicts=[]
    if service_body.get('sentence_cue_required'):
        # Accessibility service: every paragraph needs at least one cue whose
        # only paragraph is that one, i.e. an independent synchronised prompt.
        for track_id,evidence_cues in track_views.items():
            single={pid for entry in evidence_cues for pid in
                    [str(p['id']) for p in entry.get('paragraphs',[])]
                    if len(entry.get('paragraphs',[]))==1}
            for pid in canon_ids:
                if pid not in single:
                    conflicts.append({'code':SENTENCE_CUE_REQUIRED,'paragraph':pid,'cue_index':None,
                                      'detail':f'track {track_id} merges paragraph {pid}, rework required'})
    return conflicts
