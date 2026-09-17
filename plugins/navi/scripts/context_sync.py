#!/usr/bin/env python3
"""Local incremental conversation support. No model calls or execution authority."""
import argparse
import copy
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

import history
import history_context as context
from action_evidence import allowed
from task_state import locked, paths, save


def synchronize(data, session, target=None):
    """Detached workers only: parse new records, verify the existing prefix, atomically publish.

    Snapshots without a cursor bootstrap the parser once locally. Imported user text remains
    separate from live input/rollback consent. Failed reads never replace validated sources.
    """
    with locked(data, session) as (path, state):
        if not allowed(data,state) or not context.active(state):return True
        if not context.ready(state):return False
        sync=state.setdefault('context_sync', {})
        target=target or sync.get('target')
        if not target:return True  # Older/manual packets disclose snapshot-only support.
        if not isinstance(target,str) or not Path(target).is_absolute():raise ValueError('absolute transcript required')
        now=time.time()
        if sync.get('worker') and now-sync['worker']['started_at']<60:return False
        token=uuid.uuid4().hex
        sync.update(target=target, worker={'token':token,'started_at':now}, status='reading')
        generation=state['history_integration']['token']
        history_token=state['history']['token']
        base=copy.deepcopy(state['history'])
        previous=copy.deepcopy(sync.get('reader'))
        if previous is None and base.get('cursor'):previous=base
        commentary_since=sync.get('commentary_since',base.get('read_bytes',0))
        save(path,state)
    try:
        info=os.stat(target,follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):raise ValueError('regular transcript required')
        snapshot={'device':info.st_dev,'inode':info.st_ino,'size':info.st_size}
        # A cursor bootstrap must still bind to the exact originally imported file.
        original=base.get('snapshot_identity') or base.get('snapshot')
        if original and (info.st_dev,info.st_ino)!=(original['device'],original['inode']):
            raise ValueError('transcript_replaced_or_truncated')
        result=history.read_snapshot(target,session,snapshot,previous,commentary_since)
        if result['status']!='ready':raise ValueError(','.join(result['gaps']))
        if previous is None and base.get('prefix_sha256'):
            # The bootstrap reader validated its current prefix, but the original prefix
            # must also match the previously imported snapshot, not merely its inode.
            import hashlib
            with open(target,'rb') as stream:
                hasher=hashlib.sha256();remaining=base['read_bytes']
                while remaining:
                    chunk=stream.read(min(65536,remaining))
                    if not chunk:raise ValueError('transcript_truncated')
                    hasher.update(chunk);remaining-=len(chunk)
            if hasher.hexdigest()!=base['prefix_sha256']:raise ValueError('transcript_prefix_changed')
        error=None
    except (OSError,ValueError,KeyError,TypeError,RecursionError) as exc:
        result=None;error=str(exc)[:256]
    with locked(data,session) as (path,state):
        sync=(state or {}).get('context_sync',{})
        if (sync.get('worker') or {}).get('token')!=token:return False
        sync['worker']=None
        if (not allowed(data,state) or not context.active(state)
                or state['history_integration']['token']!=generation or state['history']['token']!=history_token):
            sync.update(status='cancelled',reason='capture_or_history_changed');save(path,state);return False
        if result is not None:
            try:
                joined=copy.deepcopy(state);joined['history']=dict(result)
                if base.get('bootstrap_anchor'):
                    joined['history']['bootstrap_anchor']=base['bootstrap_anchor']
                ordered,messages,packet=context.assemble(joined)
                integration=state['history_integration']
                integration.setdefault('authority_identity_messages',integration['messages'])
                integration.update(ordered_sources=ordered,messages=messages,packet=packet)
                sync.update(status='ready',reason=None,reader=result,commentary_since=commentary_since,
                    refreshed_at=time.time(),parsed_new_bytes=result['parsed_new_bytes'],
                    synced_bytes=result['read_bytes'],model_calls=0)
            except (ValueError,KeyError,TypeError) as exc:error=str(exc)[:256]
        if error:sync.update(status='unavailable',reason=error)
        save(path,state)
        return error is None


def launch(data,session,target):
    subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'sync','--data-directory',str(data),
        '--session',session,'--transcript-path',target],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,start_new_session=True,close_fds=True)


def on_hook(data,payload,launcher=launch):
    if payload.get('agent_id') or payload.get('hook_event_name') not in ('UserPromptSubmit','Stop','SessionStart'):return
    if not paths(data,payload['session_id'])[1].exists():return
    target=payload.get('transcript_path')
    if not isinstance(target,str) or not Path(target).is_absolute():return
    with locked(data,payload['session_id']) as (path,state):
        if not allowed(data,state) or not context.active(state) or not context.ready(state):return
        sync=state.setdefault('context_sync',{})
        # The source is provided by the native hook, never a directory search.
        sync['target']=target
        if time.time()-sync.get('last_launch_at',0)<2:save(path,state);return
        sync['last_launch_at']=time.time();save(path,state)
    launcher(data,payload['session_id'],target)


def status(state):
    value=(state or {}).get('context_sync',{})
    return {k:v for k,v in value.items() if k not in ('target','reader','worker') } or {
        'status':'snapshot_only','model_calls':0,'reason':'incremental synchronization not observed'}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('operation',choices=('sync','status'))
    p.add_argument('--data-directory',required=True,type=Path);p.add_argument('--session',required=True)
    p.add_argument('--transcript-path');args=p.parse_args()
    ok=True
    if args.operation=='sync':ok=synchronize(args.data_directory,args.session,args.transcript_path)
    with locked(args.data_directory,args.session) as (_,state):print(json.dumps(status(state)))
    return 0 if ok else 1


if __name__=='__main__':raise SystemExit(main())
