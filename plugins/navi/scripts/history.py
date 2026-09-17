#!/usr/bin/env python3
"""Opt-in, bounded reading of this session's host transcript. No model calls."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from action_evidence import allowed
from task_state import digest, locked, paths, save, text, new_state, workspace_policy

ADAPTER = 'codex-item-completed-v3'
# Host metadata, not text matching, distinguishes these from direct user input.
# Desktop can combine multiple host context blocks into one role=user message.
HOST_CONTEXT_KINDS = frozenset({'environments.environment_context',
    'generic.turn_aborted', 'plugins.recommendations', 'agents_md.instructions'})
# Disk scan limits are independent of the much smaller model packet. Tool logs
# and compaction payloads are parsed locally, never copied into model context.
MAX_BYTES = 256 * 1024 * 1024
MAX_LINE = 16 * 1024 * 1024
MAX_MESSAGES = 256
MAX_TEXT_BYTES = 128 * 1024
MAX_MESSAGE_BYTES = 16384
MAX_ASSISTANT_BYTES = 128 * 1024
MAX_LINES = 100000
READ_SECONDS = 15


def bootstrap(data, session, workspace):
    """Opted-in same-task onboarding; the next native hook supplies the evidence boundary.

    No agent-authored task text or synthesized user event is accepted. Existing tasks
    remain intact. This command neither scans transcripts nor calls a model.
    """
    if not isinstance(workspace, str) or not Path(workspace).is_absolute():
        raise ValueError('absolute workspace required')
    workspace = str(Path(workspace).resolve())
    if (data / 'disabled').exists() or not workspace_policy(data, workspace):
        raise ValueError('workspace input capture and resumed recorder required')
    with locked(data, session) as (path, state):
        if state is not None and (state['status'] != 'active' or state.get('history')
                                  or state.get('history_integration')):
            return {'status':'existing_task_preserved', 'history':view(state)}
        if state is not None and state.get('capture_workspace',workspace) != workspace:
            raise ValueError('task workspace mismatch')
        if path.with_suffix('.gap').exists():
            raise ValueError('capture gap requires explicit recovery')
        if state is None:
            state = new_state(session)
            # Empty contract means no authority yet, not an agent-written goal.
            state['contract']={}
        state.update(capture_workspace=workspace,
                     onboarding={'status':'pending', 'origin':'same_task_transcript_bootstrap'})
        token = uuid.uuid4().hex
        state['history'] = {'status':'pending', 'token':token, 'requested_at':time.time(),
            'boundary_source_ids':[s['id'] for s in state['sources']], 'adapter':ADAPTER, 'model_calls':0,
            'use':'same_task_bootstrap', 'coverage':'not_read', 'messages':[], 'gaps':[]}
        state['history_integration'] = {'enabled':True, 'status':'preparing',
            'token':uuid.uuid4().hex, 'history_token':token, 'interpretation_available':False}
        save(path, state)
        return {'status':'pending_native_boundary', 'history':view(state)}


def request(data, session, retry=False):
    with locked(data, session) as (path, state):
        if not allowed(data, state):
            raise ValueError('active input capture and resumed recorder required')
        previous = state.get('history')
        if previous and not retry:
            return view(state)
        state['history'] = {'status':'pending', 'token':uuid.uuid4().hex,
            'requested_at':time.time(), 'boundary_source_ids':[s['id'] for s in state['sources']],
            'adapter':ADAPTER, 'model_calls':0,
            'use':'reading_only_not_integrated_into_supervision',
            'coverage':'not_read', 'messages':[], 'gaps':[]}
        if previous and previous.get('bootstrap_anchor'):
            state['history']['bootstrap_anchor']=previous['bootstrap_anchor']
        save(path, state)
        return view(state)


def view(state):
    result = dict((state or {}).get('history', {'status':'not_requested'}))
    # Internal transcript paths are not part of the ordinary interface or report.
    result.pop('target', None)
    result.pop('token', None)
    result.pop('cursor', None)
    if (state or {}).get('history_integration', {}).get('enabled'):
        result['use'] = 'history_supervision_requested; see history_supervision status'
    if result['status'] == 'reading' and time.time()-result.get('started_at',0)>30:
        result['worker_health'] = 'completion_unknown; explicit retry required'
    return result


def cancel(data, session):
    with locked(data, session) as (path, state):
        if state and state.get('history'):
            state['history'].update(status='cancelled', token=uuid.uuid4().hex)
            state['history'].pop('target', None)
            save(path, state)


def launch(data, session, token):
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'worker',
        '--data-directory', str(data), '--session', session, '--token', token],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True, close_fds=True)


def recover(data, session, target, launcher=launch):
    """Explicit local recovery of the named session, without fabricating a Hook.

    Caller supplies a known path. Identity and complete-prefix checks are still
    enforced by the reader. No directory search or other-session fallback.
    """
    if not isinstance(target,str) or not Path(target).is_absolute():
        raise ValueError('absolute session transcript path required')
    info=os.stat(target,follow_symlinks=False)
    if not stat.S_ISREG(info.st_mode):raise ValueError('regular transcript required')
    request(data,session,retry=True)
    with locked(data,session) as (path,state):
        if not allowed(data,state):raise ValueError('active input capture required')
        h=state['history']
        h.update(status='reading',started_at=time.time(),target=target,
            snapshot={'device':info.st_dev,'inode':info.st_ino,'size':info.st_size},
            request_origin='explicit_local_session_recovery; not a host hook')
        token=h['token'];save(path,state)
    try:launcher(data,session,token)
    except OSError:
        with locked(data,session) as (path,state):
            if state and state.get('history',{}).get('token')==token:
                state['history'].update(status='failed',gaps=['worker_launch_failed'])
                state['history'].pop('target',None);save(path,state)
    with locked(data,session) as (_,state):return view(state)


def on_hook(data, payload, launcher=launch):
    """Only snapshot a host-supplied path; never read the transcript in a Hook."""
    if payload.get('agent_id') or payload.get('hook_event_name') == 'SessionEnd':
        return
    if not paths(data,payload['session_id'])[1].exists():
        return
    with locked(data, payload['session_id']) as (path, state):
        history = (state or {}).get('history', {})
        if not allowed(data, state) or history.get('status') != 'pending':
            return
        onboarding = state.get('onboarding')
        if onboarding and onboarding['status'] != 'ready':
            cwd = payload.get('cwd')
            if not isinstance(cwd,str) or str(Path(cwd).resolve()) != state['capture_workspace']:
                return
            if not isinstance(payload.get('turn_id'),str) or not payload['turn_id']:
                return
        target = payload.get('transcript_path')
        if not target:
            history.update(status='unavailable', coverage='not_read', gaps=['missing_transcript_path'])
        else:
            try:
                if not isinstance(target,str) or not Path(target).is_absolute():
                    raise ValueError('absolute host path required')
                info = os.stat(target, follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    raise ValueError('regular transcript required')
                history.update(status='reading', started_at=time.time(), target=target,
                    snapshot={'device':info.st_dev, 'inode':info.st_ino, 'size':info.st_size},
                    boundary_turn_id=payload.get('turn_id'))
            except (OSError, ValueError):
                history.update(status='unavailable', coverage='not_read', gaps=['unreadable_transcript'])
        save(path, state)
        token = history['token']
        dispatch = history['status'] == 'reading'
    if dispatch:
        try:
            launcher(data, payload['session_id'], token)
        except OSError:
            with locked(data, payload['session_id']) as (path, state):
                if state and state.get('history',{}).get('token') == token:
                    state['history'].update(status='failed', coverage='not_read', gaps=['worker_launch_failed'])
                    state['history'].pop('target',None)
                    save(path,state)


def read_snapshot(target, session, snapshot, previous=None, commentary_since=None):
    """Read canonical host UserMessage/AgentMessage events, never role=user alone.

    Append-only growth is allowed after the captured boundary. Rewrites, identity
    mismatches and unsupported evidence never become successful complete imports.
    """
    import copy
    previous = previous or {}
    cursor = previous.get('cursor') or {}
    if previous and (previous.get('status') != 'ready' or not cursor):
        raise ValueError('incremental_cursor_unavailable')
    messages = copy.deepcopy(previous.get('messages', []))
    gaps = []
    seen = dict(cursor.get('seen', {}))
    mirrors = set(map(tuple, cursor.get('mirrors', [])))
    observed = set(map(tuple, cursor.get('observed', [])))
    total_text = previous.get('user_text_bytes', 0)
    user_count = previous.get('user_message_count', 0)
    assistant_bytes = sum(len(m['text'].encode()) for m in messages if m['role']=='assistant')
    omitted_assistant = previous.get('assistant_messages_omitted', 0)
    compactions = list(cursor.get('compactions', []))
    line_number = previous.get('lines_read', 0)
    offset = previous.get('read_bytes', 0)
    initial_offset, initial_line = offset, line_number
    deadline = time.monotonic()+READ_SECONDS
    fingerprint = hashlib.sha256()
    bound = min(snapshot['size'], initial_offset + MAX_BYTES)
    if snapshot['size'] - initial_offset > MAX_BYTES:
        gaps.append('byte_limit')
    fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_dev != snapshot['device']
                or before.st_ino != snapshot['inode'] or before.st_size < snapshot['size']):
            raise ValueError('transcript_replaced_or_truncated')
        if previous:
            old = previous['snapshot_identity']
            if (old['device'],old['inode']) != (snapshot['device'],snapshot['inode']) or snapshot['size']<offset:
                raise ValueError('transcript_replaced_or_truncated')
            remaining=offset
            while remaining:
                if time.monotonic()>deadline:raise ValueError('prefix_verification_timeout')
                chunk=stream.read(min(65536,remaining))
                if not chunk:raise ValueError('transcript_truncated')
                fingerprint.update(chunk);remaining-=len(chunk)
            if fingerprint.hexdigest()!=previous['prefix_sha256']:
                raise ValueError('transcript_prefix_changed')
        while offset < bound:
            if time.monotonic()>deadline or line_number-initial_line>=MAX_LINES:
                gaps.append('read_work_limit');break
            raw = stream.readline(min(bound-offset, MAX_LINE+1))
            if not raw:raise ValueError('transcript_truncated')
            start = offset
            offset += len(raw)
            fingerprint.update(raw)
            line_number += 1
            if len(raw)>MAX_LINE:
                gaps.append('line_limit');break
            if not raw.endswith(b'\n'):
                gaps.append('incomplete_boundary_line');break
            try:
                row=json.loads(raw)
            except (ValueError, UnicodeError):
                gaps.append('malformed_record');break
            if not isinstance(row,dict) or not isinstance(row.get('payload'),dict):
                gaps.append('unknown_record_format');break
            kind, value = row.get('type'), row['payload']
            if line_number==1:
                if kind!='session_meta' or value.get('id')!=session:
                    raise ValueError('session_identity_mismatch')
                if value.get('forked_from_id') or value.get('forked_from') or isinstance(value.get('source'),dict):
                    raise ValueError('fork_or_subagent_history_unsupported')
                continue
            if kind=='session_meta':raise ValueError('multiple_session_headers')
            if kind=='compacted':
                # Native append-only windows do not remove earlier canonical events.
                # A summary-only or broken chain still cannot prove continuity.
                number=value.get('window_number')
                first,previous,current=(value.get(k) for k in ('first_window_id','previous_window_id','window_id'))
                valid=(isinstance(number,int) and not isinstance(number,bool)
                    and number==len(compactions)+1 and all(isinstance(x,str) and x for x in (first,previous,current))
                    and current!=previous and bool(observed)
                    and (previous==first if not compactions else
                         first==compactions[0]['first'] and previous==compactions[-1]['current'])
                    and current not in [c['current'] for c in compactions])
                if not valid:gaps.append('compaction_original_coverage_unknown')
                compactions.append({'first':first,'current':current})
                continue
            if kind=='response_item' and value.get('type')=='message' and value.get('role')=='user':
                meta=value.get('internal_chat_message_metadata_passthrough',{})
                kinds=meta.get('content_item_kinds',[]) if isinstance(meta,dict) else []
                if kinds==['user.text']:
                    content=value.get('content',[])
                    if isinstance(content,list) and len(content)==1 and isinstance(content[0],dict) and content[0].get('type')=='input_text':
                        mirrors.add((meta.get('turn_id'),digest(content[0].get('text'))))
                    else:gaps.append('unsupported_user_content')
                elif not kinds:
                    gaps.append('user_role_without_provenance')
                elif not (isinstance(kinds,list) and all(isinstance(k,str) and k in HOST_CONTEXT_KINDS for k in kinds)):
                    gaps.append('unknown_user_content_provenance')
                # Known structured host context is not direct user input.
                continue
            if kind!='event_msg':continue
            if value.get('type') in ('user_message','agent_message'):
                gaps.append('legacy_message_format_unsupported');continue
            if value.get('type')!='item_completed':continue
            item=value.get('item',{})
            if not isinstance(item,dict):
                gaps.append('unknown_item_format');continue
            if item.get('type') not in ('UserMessage','AgentMessage'):continue
            if value.get('thread_id') != session:
                raise ValueError('message_session_mismatch')
            role='user' if item['type']=='UserMessage' else 'assistant'
            turn, item_id = value.get('turn_id'), item.get('id')
            if not isinstance(turn,str) or not turn or not isinstance(item_id,str) or not item_id:
                gaps.append('message_identity_missing');continue
            content=item.get('content')
            if not isinstance(content,list) or not content:
                gaps.append('unsupported_message_content');continue
            supported='text' if role=='user' else 'Text'
            if any(not isinstance(c,dict) or c.get('type')!=supported or not isinstance(c.get('text'),str) for c in content):
                if role=='user':gaps.append('nontext_message_omitted')
                else:omitted_assistant+=1
                continue
            body='\n'.join(c['text'] for c in content)
            identity=digest([role,turn,item_id])
            sha=digest(body)
            if identity in seen:
                if seen[identity]!=sha:raise ValueError('conflicting_duplicate_message')
                continue
            if len(seen)>=4096:
                gaps.append('message_identity_capacity');break
            seen[identity]=sha
            if role=='user':observed.add((turn,sha))
            size=len(body.encode())
            if role=='user':
                if size>MAX_MESSAGE_BYTES or total_text+size>MAX_TEXT_BYTES or user_count>=MAX_MESSAGES:
                    gaps.append('message_capacity');continue
                total_text+=size
                user_count+=1
            else:
                if (item.get('phase')=='commentary' and (commentary_since is None or start<commentary_since)) or size>MAX_MESSAGE_BYTES:
                    omitted_assistant+=1;continue
                # Keep recent complete responses; their omission is a context limit,
                # not loss of user authorization. Never truncate a user message.
                while assistant_bytes+size>MAX_ASSISTANT_BYTES:
                    old=next((m for m in messages if m['role']=='assistant'),None)
                    if old is None:break
                    assistant_bytes-=len(old['text'].encode());messages.remove(old);omitted_assistant+=1
                if assistant_bytes+size>MAX_ASSISTANT_BYTES:
                    omitted_assistant+=1;continue
                assistant_bytes+=size
            messages.append({'id':'h-'+identity[:24], 'role':role, 'turn_id':turn,
                'host_item_id':item_id, 'text':body, 'text_digest':sha,
                'phase':item.get('phase'), 'timestamp':row.get('timestamp'),
                'origin':'transcript_import', 'authority':'direct_user_message' if role=='user' else 'agent_statement_not_authorization',
                'line':line_number, 'byte_start':start, 'byte_end':offset})
        if line_number==0:raise ValueError('empty_transcript')
        # A stable bounded prefix check catches same-inode rewrites during parsing.
        stream.seek(0)
        check=hashlib.sha256()
        remaining=offset
        while remaining:
            chunk=stream.read(min(65536,remaining))
            if not chunk:raise ValueError('transcript_truncated')
            check.update(chunk);remaining-=len(chunk)
        after=os.fstat(stream.fileno())
        current=os.stat(target,follow_symlinks=False)
        if (check.digest()!=fingerprint.digest() or after.st_size<snapshot['size']
                or (current.st_dev,current.st_ino)!=(snapshot['device'],snapshot['inode'])):
            raise ValueError('transcript_changed_during_read')
    if mirrors-observed:gaps.append('user_message_event_missing')
    if not messages:gaps.append('no_supported_messages')
    return {'status':'partial' if gaps else 'ready', 'coverage':'available_text_events_only',
        'user_text_bytes':total_text, 'user_message_count':user_count,
        'assistant_messages_omitted':omitted_assistant, 'compaction_windows':len(compactions),
        'messages':messages, 'gaps':sorted(set(gaps)), 'read_bytes':offset,
        'prefix_sha256':fingerprint.hexdigest(), 'lines_read':line_number,
        'parsed_new_bytes':offset-initial_offset, 'snapshot_identity':snapshot,
        'cursor':{'seen':seen, 'observed':sorted(observed), 'mirrors':sorted(mirrors), 'compactions':compactions},
        'snapshot_bytes':snapshot['size'], 'adapter':ADAPTER,
        'limitations':['Only canonical text events retained; no completeness guarantee for all past chat content.',
            'Assistant statements are not user authorization. No semantic interpretation or supervision integration.']}


def worker(data, session, token):
    with locked(data,session) as (_,state):
        history=(state or {}).get('history',{})
        if not allowed(data,state) or history.get('token')!=token or history.get('status')!='reading':return
        target,snapshot=history['target'],history['snapshot']
    try:
        result=read_snapshot(target,session,snapshot)
    except (OSError, ValueError, TypeError, KeyError, RecursionError) as error:
        result={'status':'failed','coverage':'not_read','messages':[],
            'gaps':[str(error) if type(error) is ValueError else type(error).__name__]}
    with locked(data,session) as (path,state):
        history=(state or {}).get('history',{})
        if not state or history.get('token')!=token or history.get('status')!='reading':return
        if not allowed(data,state):
            history.update(status='cancelled',gaps=['recording_disabled_during_read'])
        else:
            # History is separate from live user sources; concurrent new turns are preserved.
            history.update(result, finished_at=time.time())
            if state.get('onboarding',{}).get('status') not in (None,'ready'):
                try:
                    if result['status'] != 'ready':
                        raise ValueError('bootstrap_history_incomplete')
                    anchors = [m for m in result['messages'] if m['role']=='user'
                               and m['turn_id']==history.get('boundary_turn_id')]
                    if len(anchors)!=1:
                        raise ValueError('bootstrap_current_user_boundary_unproven')
                    anchor=anchors[0]
                    history['bootstrap_anchor']={k:anchor[k] for k in ('id','turn_id','text_digest')}
                    from history_context import assemble
                    ordered,messages,packet=assemble(state)
                    state['history_integration'].update(enabled=True,status='ready_raw_sources',
                        history_token=history['token'], ordered_sources=ordered,messages=messages,
                        packet=packet,live_source_digest=digest(state['sources']),
                        interpretation_available=False,
                        limitation='Direct same-task transcript bootstrap; no history interpretation model call.')
                    state['context_sync']={'status':'ready','target':target,'refreshed_at':time.time(),
                        'synced_bytes':result['read_bytes'],'model_calls':0}
                    state['onboarding']['status']='ready'
                except (ValueError,KeyError,TypeError) as error:
                    state['onboarding'].update(status='unavailable',reason=str(error))
                    state['history_integration'].update(status='unavailable',reason=str(error))
        history.pop('target',None)
        save(path,state)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('bootstrap','request','retry','recover','status','cancel','worker'))
    parser.add_argument('--data-directory',type=Path,required=True)
    parser.add_argument('--session',required=True)
    parser.add_argument('--token')
    parser.add_argument('--transcript-path')
    parser.add_argument('--workspace')
    args=parser.parse_args()
    try:
        if args.operation=='bootstrap':
            result=bootstrap(args.data_directory,args.session,args.workspace)
        elif args.operation=='recover':
            result=recover(args.data_directory,args.session,args.transcript_path)
        elif args.operation in ('request','retry'):
            result=request(args.data_directory,args.session,args.operation=='retry')
        elif args.operation=='worker':
            text(args.token,128);worker(args.data_directory,args.session,args.token);result={'worker':'finished'}
        elif args.operation=='cancel':
            cancel(args.data_directory,args.session);result={'status':'cancelled'}
        else:
            with locked(args.data_directory,args.session) as (_,state):result=view(state)
        print(json.dumps(result,ensure_ascii=False));return 0
    except (OSError,ValueError,TypeError,KeyError):
        print(json.dumps({'error':'history_operation_failed','model_calls':0}));return 1


if __name__=='__main__':raise SystemExit(main())
