"""Bounded historical tool/plan evidence for opt-in automatic scope review."""
import json
import time

import action_evidence as actions
from task_state import digest, user_context_digest

MIN_COMPLETED = 4
MAX_OBSERVATIONS = 8
MAX_AGE_SECONDS = 120


def latest_plan(state):
    # Official tool protocol identifier, not a natural-language scope rule.
    values=[e['id'] for e in state.get('action_evidence',{}).get('events',[])
            if e['tool']=='update_plan' and e['event']=='PostToolUse']
    return values[-1] if values else None


def candidate(state):
    if not state.get('supervisor',{}).get('include_activity'):return None
    context=user_context_digest(state)
    jobs=[j for j in state.get('reviews',{}).values() if j.get('activity_basis') and j.get('context_digest')==context]
    cutoff=max((j['activity_cutoff'] for j in jobs),default=0)
    events=[e for e in state['action_evidence']['events'] if e['at']>cutoff and e['user_context_digest']==context]
    complete=[e for e in events if e['event']=='PostToolUse']
    if len(complete)<MIN_COMPLETED and not any(e['tool']=='update_plan' for e in complete):return None
    # Retain input/response pairs where available; each observation remains a separate immutable source.
    chosen=events[-MAX_OBSERVATIONS:]
    if not complete or not chosen:return None
    basis=digest([context,[e['id'] for e in chosen],latest_plan(state)])
    return {'kind':'activity','basis':basis,'identity':basis,'events':chosen,'cutoff':max(e['at'] for e in chosen)}


def evidence(state,current):
    manifest={'coverage':'partial','review_selection':'Historical local tool attempts/responses and observed update_plan; no proof of success, current activity or authorship.',
              'uncovered':['Tools bypassing hooks','Pure prose plans','Truncated/evicted operations','Actual file changes not observed'],
              'dropped_events':state['action_evidence']['dropped_events']}
    rows=[{'id':'m-'+digest(manifest)[:24],'kind':'coverage_manifest','path':'task','origin':'navi_local_recorder','evidence':json.dumps(manifest)}]
    rows += [{'id':e['id'],'kind':'tool_observation','path':'not_inferred_from_command','origin':'codex_hook',
              'evidence':json.dumps(e,ensure_ascii=False)} for e in current['events']]
    # Same hard input bound as existing action exporter; omit oldest observations explicitly.
    while len(json.dumps(rows,ensure_ascii=False).encode())>actions.MAX_EXPORT_BYTES and len(rows)>2:rows.pop(1)
    return {'coverage':'partial','actions':rows}


def metadata(state,current,packet):
    ids=[a['id'] for a in packet['actions'] if a['kind']=='tool_observation']
    return {'activity_basis':current['basis'],'activity_ids':ids,'activity_cutoff':current['cutoff'],
            'activity_plan_id':latest_plan(state),'activity_files':actions.file_basis(state),
            'observation_scope':'historical_attempts_not_current_behavior'}


def invalid(state,value):
    events={e['id']:e for e in state.get('action_evidence',{}).get('events',[])}
    return (not value.get('activity_ids') or any(i not in events for i in value['activity_ids'])
            or latest_plan(state)!=value.get('activity_plan_id')
            or actions.file_basis(state)!=value.get('activity_files')
            or time.time()-value.get('activity_cutoff',0)>MAX_AGE_SECONDS)
