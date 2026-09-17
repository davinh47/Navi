#!/usr/bin/env python3
"""Bounded advisory transport; no model calls or tool permission decisions."""
import argparse
import json
from pathlib import Path
import time
import budget

from action_evidence import allowed, file_basis, refresh, version
from task_state import supervision_sources, digest, locked, paths, save, text, user_context_digest

TTL_SECONDS = 300


def configure(data, session, enabled):
    with locked(data, session) as (path, state):
        if state is None:
            raise ValueError('task absent')
        if enabled and (not allowed(data, state) or not state.get('action_evidence', {}).get('enabled')):
            raise ValueError('active task and action capture required')
        transport = state.setdefault('reminder_transport', {'enabled':False, 'mode':'controlled_test',
            'active_turn':None, 'entries':{}})
        if enabled and state.get('supervisor', {}).get('enabled'):
            raise ValueError('disable experimental supervision before controlled transport')
        transport['enabled'] = enabled
        if enabled:
            transport['mode'] = 'controlled_test'
        if not enabled:
            for entry in transport['entries'].values():
                if entry['status'] == 'pending':
                    entry.update(status='cancelled', reason='transport_disabled')
        save(path, state)


def queue(data, session, value):
    required = {'user_context_digest','action_version','source_ids','requirement','evidence','suggestion'}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError('bounded versioned advisory required')
    for field in ('requirement','evidence','suggestion'):
        text(value[field], 384)
    with locked(data, session) as (path, state):
        transport = state.get('reminder_transport') if state else None
        if (not allowed(data, state) or not transport or not transport['enabled'] or transport['mode'] != 'controlled_test'
                or not state.get('action_evidence', {}).get('enabled')):
            raise ValueError('controlled transport not enabled')
        refresh(state)
        if path.with_suffix('.gap').exists() or value['user_context_digest'] != user_context_digest(state) or value['action_version'] != version(state):
            save(path, state)
            raise ValueError('stale or incomplete evidence')
        ids = value['source_ids']
        if not isinstance(ids,list) or not ids or not all(isinstance(i,str) and i in {s['id'] for s in supervision_sources(state)} for i in ids):
            raise ValueError('real user source IDs required')
        key = enqueue(state, value, 'controlled_test', transport['active_turn'])
        save(path,state)
        return key


def finding_emitted(state, finding):
    transport = state.get('reminder_transport', {})
    recent = transport.get('seen_findings', {})
    return finding in recent or any(e.get('finding_key') == finding and e.get('emitted_at')
                                    for e in transport.get('entries', {}).values())


def enqueue(state, value, origin, turn, **metadata):
    """Internal caller holds the task lock and validates provenance/freshness first."""
    transport = state['reminder_transport']
    key = 'n-' + digest([state['task_id'], value, origin])[:20]
    if key in transport['entries']:
        return key
    budget.trim_reminders(state)
    now = time.time()
    from review import support_digest
    metadata.setdefault('support_digest',support_digest(state))
    transport['entries'][key] = {'id':key, 'origin':origin, 'payload':value,
        'status':'pending' if turn and turn == transport['active_turn'] else 'report_only_late',
        'turn_id':turn, 'created_at':now, 'expires_at':now+TTL_SECONDS,
        'delivery':'unconfirmed', 'adoption':'not_assessed', **metadata}
    return key


def rendered(entry):
    value=entry['payload']
    return ('[Navi advisory / '+entry['origin']+' / '+entry['id']+']\n'
            'Advisory only, not new user authorization; ignore if already resolved.\n'
            + value['requirement']+'\n'+value['evidence']+'\n'+value['suggestion'])


def stale_reason(state, path, entry):
    from history_context import ready
    from review import support_digest
    if entry.get('support_digest') and entry['support_digest']!=support_digest(state):
        return 'supporting_context_changed'
    if not ready(state):
        return 'history_not_ready'
    if not state.get('action_evidence', {}).get('enabled'):
        return 'action_capture_disabled'
    if path is not None and path.with_suffix('.gap').exists():
        return 'capture_gap'
    if user_context_digest(state) != entry['payload']['user_context_digest']:
        return 'user_context_changed'
    if entry.get('activity_basis'):
        from activity_scope import invalid
        supervisor=state.get('supervisor',{})
        if not supervisor.get('enabled') or supervisor.get('generation')!=entry['supervision_generation']:
            return 'supervision_disabled_or_replaced'
        if invalid(state,entry):return 'historical_activity_expired_or_superseded'
    elif 'file_basis' in entry:
        supervisor = state.get('supervisor', {})
        if not supervisor.get('enabled') or supervisor.get('generation') != entry['supervision_generation']:
            return 'supervision_disabled_or_replaced'
        if file_basis(state) != entry['file_basis']:
            return 'watched_files_changed'
    elif version(state) != entry['payload']['action_version']:
        return 'actions_changed'
    if time.time() >= entry['expires_at']:
        return 'expired'
    return None


def on_hook(data, payload):
    session = payload['session_id']
    if not paths(data, session)[1].exists():
        return None
    with locked(data, session) as (path, state):
        transport=state.get('reminder_transport') if state else None
        if not transport or not transport['enabled'] or not allowed(data,state):
            return None
        event=payload['hook_event_name']
        turn=payload.get('turn_id')
        # First activation can occur after UserPromptSubmit. Bind only a real root
        # tool boundary, once, to the transcript-verified onboarding turn.
        onboarding=state.get('onboarding',{})
        if (onboarding.get('status')=='ready' and not onboarding.get('reminder_turn_bound')
                and not payload.get('agent_id') and event in ('PreToolUse','PostToolUse')
                and turn==state.get('history',{}).get('bootstrap_anchor',{}).get('turn_id')):
            if transport['active_turn'] is None:
                transport['active_turn']=turn
            onboarding['reminder_turn_bound']=True
        if event in ('Stop','Interrupt','SessionEnd') and onboarding:
            onboarding['reminder_turn_bound']=True
        refresh(state)
        pending=[e for e in transport['entries'].values() if e['status']=='pending']
        for entry in pending:
            reason=stale_reason(state,path,entry)
            if reason:
                entry.update(status='stale',reason=reason)
        if event == 'UserPromptSubmit':
            transport['active_turn']=turn
        elif event in ('Stop','Interrupt','SessionEnd'):
            # Stop ends a turn, not the user's overall task. Never create continuation.
            if event=='SessionEnd' or turn==transport['active_turn']:
                transport['active_turn']=None
                for entry in pending:
                    if entry['status']=='pending':
                        entry.update(status='report_only_late',reason=event)
        result=None
        if event=='PreToolUse' and turn and turn==transport['active_turn']:
            for entry in pending:
                if entry['status']!='pending':
                    continue
                if entry['turn_id']!=turn:
                    entry.update(status='stale',reason='turn_changed')
                    continue
                if time.time()-transport.get('last_emitted_at',0) < budget.limits(state)[2]:
                    continue
                # Persist an attempt before stdout. A crash or rejected hook remains unconfirmed.
                entry.update(status='emitted_unconfirmed', emitted_at=time.time(),
                    tool_use_id=payload.get('tool_use_id'), event=event)
                transport['last_emitted_at']=entry['emitted_at']
                budget.remember_finding(state,entry)
                state['budget']['reminders_used']+=1
                result={'hookSpecificOutput':{'hookEventName':event,'additionalContext':rendered(entry)}}
                break
        save(path,state)
        return result


def receipt(data, session, value):
    """Explicit test-observer evidence, not a claim inferred from successful stdout."""
    if not isinstance(value,dict) or set(value)!={'id','turn_id','tool_use_id','host_run_id','host_context','agent_reply'}:
        raise ValueError('host and agent observations required')
    for key in ('host_run_id','host_context','agent_reply'):
        text(value[key], 4096)
    with locked(data,session) as (path,state):
        if state is None:
            raise ValueError('task absent')
        entry=state.get('reminder_transport',{}).get('entries',{}).get(value['id'])
        if not entry or entry['status'] not in ('emitted_unconfirmed','delivered'):
            raise ValueError('no matching emitted reminder')
        if entry['turn_id']!=value['turn_id'] or entry['tool_use_id']!=value['tool_use_id']:
            raise ValueError('receipt correlation mismatch')
        if rendered(entry) not in value['host_context'] or entry['id'] not in value['agent_reply']:
            raise ValueError('both host context and agent receipt must match')
        entry.update(status='delivered',delivery='confirmed_by_external_test_observer',
            receipt={'origin':'external_test_observer','at':time.time(),**value})
        save(path,state)


def respond(data, session, value):
    """Optional agent explanation with a few real quotes, not a full intent rewrite."""
    from task_state import validate_citations
    if not isinstance(value,dict) or set(value)!={'id','user_context_digest','disposition','reason','citations'}:
        raise ValueError('id, context digest, disposition, reason and citations required')
    if value['disposition'] not in ('accepted','disputed','obsolete','uncertain'):
        raise ValueError('invalid response disposition')
    text(value['reason'],2048)
    if len(json.dumps(value,ensure_ascii=False).encode())>4096:raise ValueError('response exceeds 4 KiB')
    if not isinstance(value['citations'],list) or len(value['citations'])>8:raise ValueError('at most eight citations')
    with locked(data,session) as (path,state):
        if not allowed(data,state):raise ValueError('recording disabled')
        entry=state.get('reminder_transport',{}).get('entries',{}).get(value['id'])
        if not entry or not entry.get('emitted_at'):raise ValueError('no emitted reminder')
        if value['user_context_digest']!=user_context_digest(state):raise ValueError('stale response context')
        if value['citations']:
            validate_citations(value['citations'],{s['id']:s['text'] for s in supervision_sources(state)})
        if entry.get('agent_response',{}).get('value')==value:return
        entry['agent_response']={'origin':'agent_statement_not_authorization','value':value,'at':time.time(),
            'receipt':'agent_reported_receipt_not_external_confirmation','adoption':'not_verified'}
        # Original judgment, external delivery status and all budgets remain unchanged.
        save(path,state)


def responses_for_review(state):
    entries=state.get('reminder_transport',{}).get('entries',{})
    values=sorted((e['agent_response'] for e in entries.values() if e.get('agent_response')),
                  key=lambda r:r['at'])
    selected=[];size=0
    for value in reversed(values):
        cost=len(json.dumps(value,ensure_ascii=False).encode())
        if size+cost>6144:break
        selected.append(value);size+=cost
        if len(selected)==4:break
    return list(reversed(selected))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('enable-test','disable','queue-test','receipt-test','respond','status'))
    parser.add_argument('--data-directory',type=Path,required=True)
    parser.add_argument('--session',required=True)
    parser.add_argument('--input',type=Path)
    args=parser.parse_args()
    try:
        if args.operation in ('enable-test','disable'):
            configure(args.data_directory,args.session,args.operation=='enable-test')
            print(json.dumps({'mode':'controlled_test','enabled':args.operation=='enable-test'}))
        elif args.operation=='status':
            with locked(args.data_directory,args.session) as (path,state):
                if state and allowed(args.data_directory,state):
                    refresh(state)
                    transport=state.get('reminder_transport',{})
                    for entry in transport.get('entries',{}).values():
                        reason=stale_reason(state,path,entry)
                        entry['currently_stale']=bool(reason)
                        if entry['status']=='pending' and reason:
                            entry.update(status='stale',reason=reason)
                    save(path,state)
                print(json.dumps({'transport':state.get('reminder_transport') if state else None,
                    'automatic_classification':'experimental_file_scope' if state and state.get('supervisor',{}).get('enabled') else 'disabled'},ensure_ascii=False))
        else:
            if args.input is None or args.input.stat().st_size>16384:
                raise ValueError('bounded input file required')
            value=json.loads(args.input.read_text())
            if args.operation=='queue-test':
                print(json.dumps({'id':queue(args.data_directory,args.session,value)}))
            elif args.operation=='respond':
                respond(args.data_directory,args.session,value)
                print(json.dumps({'recorded':True,'authority':'unverified_agent_statement','model_calls':0}))
            else:
                receipt(args.data_directory,args.session,value)
                print(json.dumps({'receipt_recorded':True,'adoption':'not_assessed'}))
        return 0
    except (OSError,ValueError,TypeError,KeyError) as error:
        print(json.dumps({'status':'unavailable','error_type':type(error).__name__,'reason':str(error)[:256]}))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
