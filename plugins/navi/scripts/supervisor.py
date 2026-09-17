#!/usr/bin/env python3
"""Opt-in experimental file-scope supervision. Bounded background review, never tool denial."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

import action_evidence as actions
import activity_scope
import reminders
import budget
import review
import history_context
from task_state import supervision_sources, digest, locked, paths, save, user_context_digest

COALESCE_SECONDS = 2
ADVISORY_LABELS = {'unrelated_work', 'phase_expansion', 'goal_displacement', 'rationale_as_copy'} | review.DELIVERY_LABELS


def delivery_advice(result):
    return {
        'requirement':'Preserve required capabilities; only the user can authorize their removal, deferral or replacement.',
        'suggestion':'Check this delivery gap against the actual implementation and user approval. If unresolved, clearly report the missing or substituted capability and verification limits to the user. Do not claim full completion. This is not authorization to reduce scope; no pause or acknowledgment is required.'
    } if review.DELIVERY_LABELS.intersection(result['scope_labels']) else {}


def finding_key(context, subject, result):
    # Keep delivery findings distinct from earlier expansion notices on the same evidence paths.
    delivery = sorted(review.DELIVERY_LABELS.intersection(result['scope_labels']))
    return digest([context, subject, delivery] if delivery else [context, subject])


def enabled(data, state):
    return (actions.allowed(data, state) and state.get('action_evidence', {}).get('enabled')
            and state.get('supervisor', {}).get('enabled')
            and state.get('reminder_transport', {}).get('enabled') and history_context.ready(state))


def configure(data, session, active, include_activity=False, model=None, effort=None, profile=None, max_reviews=None, unlimited_reviews=False):
    with locked(data, session) as (path, state):
        if state is None:
            raise ValueError('task absent')
        if active and (not actions.allowed(data, state) or not state.get('action_evidence', {}).get('enabled')
                       or (not state['action_evidence']['files'] and not include_activity)):
            raise ValueError('active task with explicit watched files required')
        if max_reviews is not None and (type(max_reviews) is not int or max_reviews < 0):
            raise ValueError('max_reviews must be a nonnegative integer')
        if max_reviews is not None and unlimited_reviews:
            raise ValueError('choose a ceiling or unlimited reviews')
        changed_budget = max_reviews is not None or unlimited_reviews
        if changed_budget:
            state['budget'].update(review_limit=max_reviews, limit_origin='explicit')
        supervisor = state.setdefault('supervisor', {'enabled':False, 'generation':0, 'dispatches':0,
            'last_attempt':None, 'last_dispatch_at':0, 'worker':None})
        if profile not in (None,'standard','long-task'):raise ValueError('unsupported supervision profile')
        chosen_profile=profile or supervisor.get('profile','standard')
        chosen_model = model or supervisor.get('model', review.MODEL)
        chosen_effort = effort or supervisor.get('effort', review.EFFORT)
        from task_state import text
        text(chosen_model,128)
        if chosen_effort not in ('low','medium','high'):raise ValueError('unsupported review effort')
        if not changed_budget and supervisor.get('profile','standard')==chosen_profile and supervisor.get('model',review.MODEL)==chosen_model and supervisor.get('effort',review.EFFORT)==chosen_effort and supervisor['enabled'] == active and state.get('reminder_transport',{}).get('enabled') == active and supervisor.get('include_activity',False)==include_activity:
            return
        supervisor.update(enabled=active, model=chosen_model, effort=chosen_effort, profile=chosen_profile,
            include_activity=include_activity, generation=supervisor['generation']+1, worker=None)
        if active:
            supervisor['last_attempt'] = None
        state['supervision'] = ('experimental_file_and_activity_scope' if include_activity else 'experimental_file_scope') if active else 'disabled'
        transport = state.setdefault('reminder_transport', {'active_turn':None, 'entries':{}})
        transport.update(enabled=active, mode='experimental_file_scope')
        for entry in transport['entries'].values():
            if entry['status']=='pending':
                entry.update(status='cancelled', reason='supervision_configuration_changed')
        save(path, state)


def runtime_status(data,state,path):
    """Configuration is not proof that a reviewer can run."""
    reasons=[]
    if not actions.allowed(data,state):reasons.append('recording_or_input_capture_disabled')
    if path.with_suffix('.gap').exists():reasons.append('capture_gap')
    if not state.get('action_evidence',{}).get('enabled'):reasons.append('action_capture_disabled')
    if not state.get('supervisor',{}).get('enabled'):reasons.append('supervisor_disabled')
    if not state.get('reminder_transport',{}).get('enabled'):reasons.append('reminders_disabled')
    if not history_context.ready(state):reasons.append('history_not_ready')
    if state.get('context_sync',{}).get('status')=='unavailable':reasons.append('context_sync_unavailable')
    gate=budget.review_gate(state)
    if gate:reasons.append(gate['reason'])
    jobs=state.get('reviews',{})
    old=state.get('review_rollup',{})
    return {'eligible':not reasons,'unavailable_reasons':reasons,
        'model_calls_started':old.get('started',0)+sum(j.get('status') in ('running','completed','failed') and j.get('error_type')!='SpawnError' for j in jobs.values()),
        'reviews_completed':old.get('completed',0)+sum(j.get('status')=='completed' for j in jobs.values()),
        'history_gaps':state.get('history',{}).get('gaps',[]) if 'history_not_ready' in reasons else [],
        'meaning':'eligible is not reviewed; inspect completed reviews and coverage'}


def scheduling_limits(state):
    interval,hourly,_=budget.limits(state)
    return interval,None,hourly


def candidate(state):
    basis = actions.file_basis(state)
    context = user_context_digest(state)
    for entry in state.get('reminder_transport', {}).get('entries', {}).values():
        if (entry['origin']=='independent_review' and entry.get('emitted_at')
                and not entry.get('followup_job') and entry['payload']['user_context_digest']==context
                and entry['file_basis'] != basis):
            return {'kind':'followup', 'reminder_id':entry['id'], 'basis':basis,
                    'identity':digest([context,basis,entry['id']])}
    if not any(e['baseline'] != e['current']
               and e['baseline'].get('status') in ('present','absent')
               and e['current'].get('status') in ('present','absent')
               for e in state['action_evidence']['files'].values()):
        return activity_scope.candidate(state)
    if snapshot_reviewed(state,basis):return activity_scope.candidate(state)
    return {'kind':'detect', 'basis':basis, 'identity':digest([context,basis])}


def file_evidence(state):
    # File-only packet is what makes file-basis freshness legitimate: no older tool claims.
    exported = actions.export(state)
    selected = [a for a in exported['actions'] if a['kind'] in ('coverage_manifest','workspace_observation')]
    manifest = next(a for a in selected if a['kind']=='coverage_manifest')
    manifest_value = json.loads(manifest['evidence'])
    manifest_value['review_selection'] = 'Current watched files only; all tool observations intentionally excluded.'
    manifest['evidence'] = json.dumps(manifest_value,ensure_ascii=False)
    manifest['id'] = 'm-' + digest(manifest_value)[:24]
    if not any(a['kind']=='workspace_observation' for a in selected):
        raise ValueError('no bounded current file evidence')
    return {'coverage':'partial', 'actions':selected}


def snapshot_reviewed(state, basis):
    """A followup also evaluates scope: do not reclassify its unchanged snapshot at Stop."""
    return any(not j.get('activity_basis') and j.get('file_basis')==basis and j.get('context_digest')==user_context_digest(state)
               and j.get('supervision_generation')==state['supervisor']['generation']
               for j in state.get('reviews',{}).values())


def prepare(state, path, token):
    """Called under review reservation lock after coalescing; no stale packet capture race."""
    supervisor = state.get('supervisor', {})
    pending = supervisor.get('worker')
    if not supervisor.get('enabled') or not state.get('reminder_transport', {}).get('enabled') or not pending or pending['token'] != token:
        raise ValueError('supervisor dispatch cancelled')
    if not state['action_evidence']['enabled']:
        raise ValueError('capture disabled')
    current = candidate(state)
    if current is None:
        raise ValueError('no current candidate')
    if current['kind']=='detect' and snapshot_reviewed(state,current['basis']):
        raise ValueError('current snapshot already reviewed')
    supervisor['last_attempt'] = current['identity']
    metadata = {'file_basis':current['basis'], 'supervision_generation':supervisor['generation'],
                'supervision_token':token, 'origin':'automatic_file_checkpoint',
                'trigger':pending['trigger'], 'turn_id':state['reminder_transport']['active_turn']}
    if current['kind']=='activity':
        packet=activity_scope.evidence(state,current)
        metadata.pop('file_basis')
        metadata.update(activity_scope.metadata(state,current,packet),origin='automatic_activity_checkpoint')
        return packet,metadata
    if current['kind']=='followup':
        entry = state['reminder_transport']['entries'][current['reminder_id']]
        metadata['followup'] = {'reminder_id':entry['id'], 'earlier_reason':entry['payload']['evidence'],
                               'affected_paths':entry['affected_paths'], 'scope':'specific observed issue only'}
    return file_evidence(state), metadata


def launch(data, session, token):
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'worker', '--data-directory',str(data),
        '--session',session, '--token',token], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)


def on_hook(data, payload, launcher=launch):
    if payload['hook_event_name'] not in ('PostToolUse','UserPromptSubmit','Stop','Checkpoint'):
        return
    session = payload['session_id']
    if not paths(data,session)[1].exists():
        return
    with locked(data,session) as (path,state):
        if not enabled(data,state) or path.with_suffix('.gap').exists():
            if state.get('supervisor',{}).get('enabled'):
                state['supervisor']['last_status']='unavailable:'+','.join(runtime_status(data,state,path)['unavailable_reasons'])
                save(path,state)
            return
        actions.refresh(state)
        supervisor = state['supervisor']
        current = candidate(state)
        if (not current or current['identity']==supervisor['last_attempt']
                or current['kind']=='detect' and snapshot_reviewed(state,current['basis'])):
            save(path,state)
            return
        if supervisor['worker'] is not None:
            # Never launch a second evaluator; missing workers remain visible, without automatic retries.
            save(path,state)
            return
        interval,_,hourly_limit=scheduling_limits(state)
        gate=budget.review_gate(state)
        if gate:
            supervisor['last_status']=gate['reason']
            save(path,state)
            return
        now = time.time()
        recent=[t for t in supervisor.get('dispatch_times',[]) if now-t<3600]
        if hourly_limit and len(recent)>=hourly_limit:
            supervisor['last_status']='hourly_limit; resumes_at_next_hook_after_window'
            save(path,state);return
        if now-supervisor['last_dispatch_at'] < interval:
            supervisor['last_status']='coalesced_until_next_eligible_boundary'
            save(path,state)
            return
        token = digest([state['task_id'],now,supervisor['generation'],supervisor['dispatches']])[:24]
        supervisor.update(last_attempt=current['identity'], last_dispatch_at=now, dispatches=supervisor['dispatches']+1,
            dispatch_times=(recent+[now])[-48:],
            worker={'token':token, 'created_at':now, 'trigger':payload['hook_event_name'], 'transcript_path':payload.get('transcript_path')}, last_status='dispatched')
        save(path,state)
    try:
        launcher(data,session,token)
    except OSError:
        finish_dispatch(data,session,token,'spawn_failed')


def finish_dispatch(data, session, token, status):
    with locked(data,session) as (path,state):
        if state is None:
            return
        supervisor=state.get('supervisor',{})
        if supervisor.get('worker',{} ) and supervisor['worker']['token']==token:
            supervisor.update(worker=None,last_status=status)
            if status in ('hourly_review_limit','review_budget_exhausted'):
                supervisor['last_attempt']=None
            save(path,state)


def short(value):
    raw=value.encode('utf-8')
    return value if len(raw)<=384 else raw[:378].decode('utf-8',errors='ignore')+'…'


def consume(data, session, key):
    with locked(data,session) as (path,state):
        if state is None or key not in state.get('reviews',{}):
            return
        job=state['reviews'][key]
        if job.get('supervision_processed'):
            return
        if enabled(data,state):
            actions.refresh(state)
        job['supervision_processed']=True
        if (job['status']!='completed' or job.get('stale') or review.is_stale(state,job)
                or not enabled(data,state) or path.with_suffix('.gap').exists()):
            job['advisory_outcome']='unavailable_or_stale'
            save(path,state)
            return
        result=job['output']['result']['results'][0]
        if job.get('followup'):
            entry=state['reminder_transport']['entries'][job['followup']['reminder_id']]
            observation=dict(result['followup'])
            available={a['path']:json.loads(a['evidence'])['current_status'] for a in job['packet']['actions'] if a['kind']=='workspace_observation'}
            if any(available.get(name)!='present' for name in entry['affected_paths']):
                observation.update(observation='uncertain',reason='Affected current file unavailable; no correction claim.')
            if result['verdict']=='drift' and observation['observation']=='no_longer_observed':
                observation.update(observation='uncertain',reason='Conflicting scope and followup judgments; no correction claim.')
            entry['followup']={'review_id':key, 'observed_at':time.time(), 'file_basis':job['file_basis'],
                'user_context_digest':job['context_digest'], 'judgment':observation,
                'attribution':'unknown; change after advisory is not proof of adoption or causation'}
            job['advisory_outcome']='followup_recorded_no_repeat_reminder'
            save(path,state)
            return
        if job.get('activity_basis'):
            consume_activity(state,job,result)
            save(path,state)
            return
        files={a['id']:a for a in job['packet']['actions'] if a['kind']=='workspace_observation'}
        cited=[files[i] for i in result['evidence_ids'] if i in files]
        # Scope and implementation risk are independent: risk alone never causes or vetoes a scope advisory.
        if (result['verdict']!='drift' or not result['scope_labels'] or not set(result['scope_labels'])<=ADVISORY_LABELS
                or not cited
                or any(json.loads(a['evidence'])['current_status']!='present' for a in cited)):
            job['advisory_outcome']='record_only_classification_gate'
            save(path,state)
            return
        affected=sorted({a['path'] for a in cited})
        finding=finding_key(job['context_digest'],affected,result)
        entries=state['reminder_transport']['entries'].values()
        if reminders.finding_emitted(state,finding):
            job['advisory_outcome']='merged_with_existing_finding'
            save(path,state)
            return
        sources={s['id'] for s in supervision_sources(state)}
        value={'user_context_digest':job['context_digest'], 'action_version':job['action_version'],
            'source_ids':[i for i in result['evidence_ids'] if i in sources],
            'requirement':'Keep the current user-authorized task scope; future phases and explanations do not authorize extra work.',
            'evidence':short(result['reason']),
            'suggestion':'Recheck the observed change against the user requirements. If out of scope, return to the current task and record unrelated findings. No need to pause or acknowledge this advisory.'}
        value.update(delivery_advice(result))
        metadata={'file_basis':job['file_basis'], 'supervision_generation':job['supervision_generation'],
                  'review_id':key, 'finding_key':finding, 'affected_paths':affected}
        previous=next((e for e in entries if e.get('finding_key')==finding and not e.get('emitted_at')
            and e['status'] in ('pending','stale') and e['turn_id']==job['turn_id']
            and job['turn_id']==state['reminder_transport']['active_turn']),None)
        if previous:
            previous.update(payload=value,status='pending',expires_at=time.time()+reminders.TTL_SECONDS,**metadata)
            previous.pop('reason',None)
            key_reminder=previous['id']
            outcome='merged_pending_with_latest_evidence'
        else:
            key_reminder=reminders.enqueue(state,value,'independent_review',job['turn_id'],**metadata)
            outcome=state['reminder_transport']['entries'][key_reminder]['status']
        job.update(advisory_outcome=outcome,reminder_id=key_reminder)
        save(path,state)


def consume_activity(state,job,result):
    observed={a['id'] for a in job['packet']['actions'] if a['kind']=='tool_observation'}
    cited=observed.intersection(result['evidence_ids'])
    if result['verdict']!='drift' or not cited or not result['scope_labels'] or not set(result['scope_labels'])<=ADVISORY_LABELS:
        job['advisory_outcome']='record_only_classification_gate'
        return
    finding=finding_key(job['context_digest'],'historical_activity',result)
    entries=state['reminder_transport']['entries'].values()
    if reminders.finding_emitted(state,finding) or any(e.get('finding_key')==finding and e['status']=='pending' for e in entries):
        job['advisory_outcome']='merged_with_existing_finding'
        return
    source_ids={s['id'] for s in supervision_sources(state)}
    value={'user_context_digest':job['context_digest'],'action_version':job['action_version'],
           'source_ids':[i for i in result['evidence_ids'] if i in source_ids],
           'requirement':'Keep the current user-authorized scope; plans and related future work do not expand it.',
           'evidence':short('Historical tool/plan evidence, not confirmed current behavior: '+result['reason']),
           'suggestion':'If this activity is still relevant, return to the authorized task and record unrelated findings. If already resolved, disregard this historical advisory. Do not undo unknown-author changes or pause for acknowledgment.'}
    advice=delivery_advice(result)
    if advice:
        value.update(advice)
        value['suggestion']='If already resolved, disregard this historical advisory. '+value['suggestion']
    metadata={k:job[k] for k in ('activity_basis','activity_ids','activity_cutoff','activity_plan_id','activity_files','observation_scope','supervision_generation')}
    key=reminders.enqueue(state,value,'independent_activity_review',job['turn_id'],review_id=next(k for k,v in state['reviews'].items() if v is job),finding_key=finding,**metadata)
    job.update(reminder_id=key,advisory_outcome=state['reminder_transport']['entries'][key]['status'])


def worker(data, session, token, runner=review.run_model, delay=COALESCE_SECONDS):
    # Only the detached worker sleeps/calls the model. The executing Agent never waits here.
    if delay:
        time.sleep(delay)
    outcome='no_current_review'
    try:
        from context_sync import synchronize
        with locked(data,session) as (_,state):
            pending=(state or {}).get('supervisor',{}).get('worker') or {}
            target=pending.get('transcript_path')
        if pending.get('token')!=token or not synchronize(data,session,target):
            outcome='context_sync_unavailable_no_model_call'
            return
        key,new=review.reserve(data,session,None,supervision_token=token)
        with locked(data,session) as (path,state):
            if state is None or key not in state.get('reviews',{}):
                return
            job=state['reviews'][key]
            if job.get('followup'):
                entry=state['reminder_transport']['entries'][job['followup']['reminder_id']]
                entry['followup_job']=key
                save(path,state)
        if new:
            review.worker(data,session,key,runner)
        consume(data,session,key)
        outcome='review_finished'
    except budget.ReviewDeferred as deferred:
        outcome=str(deferred)
    except (OSError,ValueError,TypeError,KeyError):
        outcome='unavailable_no_retry'
    finally:
        finish_dispatch(data,session,token,outcome)


def status(data,session):
    with locked(data,session) as (path,state):
        if state is None:
            return {'supervisor':None}
        if enabled(data,state):
            actions.refresh(state)
            save(path,state)
        supervisor=dict(state.get('supervisor',{}))
        pending=supervisor.get('worker')
        if pending and time.time()-pending['created_at']>180:
            supervisor['health']='worker_overdue_or_lost; disable/re-enable explicitly to recover'
        entries=json.loads(json.dumps(state.get('reminder_transport',{}).get('entries',{})))
        for entry in entries.values():
            entry['currently_stale']=bool(reminders.stale_reason(state,path,entry))
            if entry.get('followup'):
                check=entry['followup']
                check['currently_stale']=(check['file_basis']!=actions.file_basis(state) or check['user_context_digest']!=user_context_digest(state))
        interval,dispatch_limit,hourly_limit=scheduling_limits(state)
        return {'supervisor':supervisor,'runtime':runtime_status(data,state,path),
                'limits':{'min_interval_seconds':interval,'hourly_dispatch_limit':hourly_limit,'total_dispatch_limit':dispatch_limit,
                          'reminder_cooldown_seconds':budget.limits(state)[2]},
                'review_gate':budget.review_gate(state),'remaining_reviews':budget.remaining(state),
                'retention':{'omitted_reviews':state.get('review_rollup',{}).get('reviews',0),
                             'omitted_reminders':state.get('reminder_transport',{}).get('omitted',{})},
                'budget':state['budget'],'reminders':entries,
                'coverage':('watched files plus bounded historical local tools/update_plan; no pure-prose or bypassed-tool coverage' if supervisor.get('include_activity') else 'explicit watched files only; no tool-only investigation detection'),
                'history_supervision':history_context.status(state), 'main_task_must_wait':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('enable-experimental','disable','set-budget','checkpoint','worker','status'))
    parser.add_argument('--data-directory',type=Path,required=True)
    parser.add_argument('--session',required=True)
    parser.add_argument('--token')
    parser.add_argument('--include-activity',action='store_true')
    parser.add_argument('--model');parser.add_argument('--effort',choices=('low','medium','high'))
    parser.add_argument('--profile',choices=('standard','long-task'))
    ceiling=parser.add_mutually_exclusive_group()
    ceiling.add_argument('--max-reviews',type=int,help='Optional cumulative conversation review ceiling, including already used calls')
    ceiling.add_argument('--unlimited-reviews',action='store_true',help='Remove the cumulative ceiling; keep rate limits and spent counters')
    args=parser.parse_args()
    try:
        if args.operation in ('enable-experimental','disable'):
            configure(args.data_directory,args.session,args.operation=='enable-experimental',args.include_activity,args.model,args.effort,args.profile,args.max_reviews,args.unlimited_reviews)
        elif args.operation=='set-budget':
            if args.max_reviews is None and not args.unlimited_reviews:
                raise ValueError('set-budget requires --max-reviews or --unlimited-reviews')
            if args.max_reviews is not None and args.max_reviews < 0:
                raise ValueError('max-reviews must be nonnegative')
            with locked(args.data_directory,args.session) as (path,state):
                if state is None:raise ValueError('task absent')
                state['budget'].update(review_limit=args.max_reviews,limit_origin='explicit')
                save(path,state)
        elif args.operation=='checkpoint':
            on_hook(args.data_directory,{'session_id':args.session,'hook_event_name':'Checkpoint'})
        elif args.operation=='worker':
            worker(args.data_directory,args.session,args.token)
        print(json.dumps(status(args.data_directory,args.session),ensure_ascii=False))
        return 0
    except (OSError,ValueError,TypeError,KeyError) as error:
        print(json.dumps({'status':'unavailable','error_type':type(error).__name__,'reason':str(error),'main_task_must_wait':False}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
