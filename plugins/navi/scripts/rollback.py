#!/usr/bin/env python3
"""Explicit single-file text rollback. No automatic decisions, model calls or Git writes."""
import argparse
from contextlib import contextmanager
import difflib
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import time
import uuid

import action_evidence as actions
from task_state import atomic_write, locked, paths, validate_citations

MAX_PLANS = 8


class Refused(ValueError):
    pass


def persist(path,state):
    atomic_write(path,state)
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    try:os.fsync(directory)
    finally:os.close(directory)


def require(data, path, state):
    if not actions.allowed(data,state) or not state.get('action_evidence',{}).get('enabled'):
        raise Refused('active_action_capture_required')
    if path.with_suffix('.gap').exists():
        raise Refused('capture_gap')
    return state['action_evidence']


@contextmanager
def opened(workspace, name, writable=False):
    relative=Path(name)
    if relative.is_absolute() or not relative.parts or '..' in relative.parts or '.git' in relative.parts:
        raise Refused('unsafe_path')
    parent=leaf=None
    try:
        parent=os.open(workspace,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        for part in relative.parts[:-1]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            os.close(parent);parent=child
        leaf=os.open(relative.parts[-1],(os.O_RDWR if writable else os.O_RDONLY)|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=parent)
        info=os.fstat(leaf)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid():
            raise Refused('ordinary_owned_single_link_file_required')
        if writable:
            fcntl.flock(leaf,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield parent,leaf,relative.parts[-1]
    finally:
        if leaf is not None:os.close(leaf)
        if parent is not None:os.close(parent)


def identity(info):
    return [info.st_dev,info.st_ino,info.st_size,info.st_mtime_ns,info.st_ctime_ns,stat.S_IMODE(info.st_mode)]


def read_file(fd):
    before=os.fstat(fd);os.lseek(fd,0,os.SEEK_SET)
    raw=os.read(fd,actions.MAX_FILE_BYTES+1)
    if identity(before)!=identity(os.fstat(fd)):
        raise Refused('changed_during_read')
    if len(raw)>actions.MAX_FILE_BYTES or b'\0' in raw:
        raise Refused('bounded_text_required')
    return raw.decode('utf-8'),identity(before)


def file_read(workspace,name):
    with opened(workspace,name) as (_,fd,__):return read_file(fd)


def diff(before,after,name):
    # Imported lazily to avoid coupling hook receipt to the report renderer.
    from report import difference
    return difference({'status':'present','text':before},{'status':'present','text':after},'current/'+name,'proposed/'+name)


def choices(baseline,current):
    left=baseline.splitlines(keepends=True);right=current.splitlines(keepends=True)
    rows=[]
    for tag,a,b,c,d in difflib.SequenceMatcher(None,left,right,autojunk=False).get_opcodes():
        if tag!='equal':rows.append({'id':len(rows)+1,'baseline_lines':[a,b],'current_lines':[c,d],
                                    'before':''.join(left[a:b]),'current':''.join(right[c:d])})
    return rows


def inspect(data,session,name):
    with locked(data,session) as (path,state):
        capture=require(data,path,state)
        entry=capture['files'].get(name)
        if not entry or entry['baseline']['status']!='present':
            raise Refused('watched_existing_text_baseline_required')
        current,_=file_read(capture['workspace'],name)
        return {'path':name,'changes':choices(entry['baseline']['text'],current),
                'attribution':'unknown_requires_user_review','dependencies':'unknown_requires_user_review',
                'model_calls':0,'note':'Adjacent edits may form one inseparable change. Select only independently reviewed changes.'}


def prepare(data,session,name=None,selected=None,restore=None):
    with locked(data,session) as (path,state):
        capture=require(data,path,state)
        plans=state.setdefault('rollbacks',{})
        if len(plans)>=MAX_PLANS:raise Refused('rollback_record_capacity')
        if any(p['status'] in ('applying','write_outcome_unknown') for p in plans.values()):raise Refused('unfinished_write_requires_inspection')
        if restore:
            original=plans.get(restore)
            if not original or original['status']!='applied':raise Refused('applied_recovery_point_required')
            name=original['path'];current,version=file_read(capture['workspace'],name)
            if current!=original['after']:raise Refused('recovery_conflicts_with_later_edits')
            after=original['before'];selected=[]
        else:
            entry=capture['files'].get(name)
            if not entry or entry['baseline']['status']!='present':raise Refused('watched_existing_text_baseline_required')
            current,version=file_read(capture['workspace'],name)
            items=choices(entry['baseline']['text'],current)
            valid={r['id']:r for r in items}
            if not isinstance(selected,list) or not selected or any(type(i)!=int or i not in valid for i in selected) or len(set(selected))!=len(selected):
                raise Refused('explicit_valid_change_selection_required')
            lines=current.splitlines(keepends=True)
            for index in sorted(selected,reverse=True):
                item=valid[index];start,end=item['current_lines']
                lines[start:end]=item['before'].splitlines(keepends=True)
            after=''.join(lines)
        if after==current:raise Refused('no_change')
        if len(after.encode())>actions.MAX_FILE_BYTES:raise Refused('result_exceeds_snapshot_limit')
        key='rb-'+uuid.uuid4().hex[:20]
        plan={'id':key,'status':'prepared','path':name,'workspace':capture['workspace'],
              'generation':capture.get('generation'), 'created_at':time.time(),
              'source_ids':[s['id'] for s in state['sources']],
              'selected_changes':selected,'restores':restore,'before':current,'after':after,'file_identity':version,
              'diff':diff(current,after,name),'attribution':'not_independently_verified',
              'dependencies':'not_independently_verified','notice':'none'}
        plans[key]=plan
        persist(path,state)  # Do not extend task retention or reset any budget.
        return plan


def confirm(state,plan,value):
    expected={'plan_id','source_id','quote','ownership','dependencies','exclusive_workspace'}
    if not isinstance(value,dict) or set(value)!=expected or value['plan_id']!=plan['id']:
        raise Refused('confirmation_must_bind_exact_preview')
    if value['ownership']!='user_confirmed_selected_changes' or value['dependencies']!='user_confirmed_independent' or value['exclusive_workspace'] is not True:
        raise Refused('ownership_dependencies_and_exclusive_editing_must_be_confirmed')
    sources=state['sources'];old=plan['source_ids']
    if [s['id'] for s in sources[:len(old)]]!=old or len(sources)!=len(old)+1:
        raise Refused('preview_user_context_changed_reprepare')
    source=sources[-1]
    if source['id']!=value['source_id'] or source['origin']!='user_prompt_hook' or source['recorded_at']<plan['created_at']:
        raise Refused('new_direct_user_confirmation_required')
    validate_citations([{'source_id':value['source_id'],'quote':value['quote']}],{source['id']:source['text']})
    # Provenance/structure only. The calling Agent must faithfully map actual informed user consent.
    return {**value,'semantic_verification':'caller_interpreted_user_confirmation','recorded_at':time.time()}


def write_content(fd,content):
    raw=content.encode('utf-8');os.lseek(fd,0,os.SEEK_SET)
    view=memoryview(raw)
    while view:
        written=os.write(fd,view)
        if not written:raise OSError('short write')
        view=view[written:]
    os.ftruncate(fd,len(raw));os.fsync(fd)


def apply(data,session,key,confirmation):
    with locked(data,session) as (path,state):
        capture=require(data,path,state)
        plan=state.get('rollbacks',{}).get(key)
        if not plan or plan['status']!='prepared':raise Refused('unused_prepared_plan_required')
        if any(p['status'] in ('applying','write_outcome_unknown') for p in state['rollbacks'].values()):raise Refused('unfinished_write_requires_inspection')
        if capture['workspace']!=plan['workspace'] or capture.get('generation')!=plan['generation']:
            raise Refused('capture_configuration_changed')
        approval=confirm(state,plan,confirmation)
        with opened(plan['workspace'],plan['path'],True) as (parent,fd,leaf):
            current,version=read_file(fd)
            if current!=plan['before'] or version!=plan['file_identity']:
                raise Refused('file_changed_since_preview')
            # Persist pre-write bytes and authorization BEFORE touching the workspace.
            plan.update(status='applying',confirmation=approval,started_at=time.time())
            persist(path,state)
            try:
                # Check path and bytes again after durable journal I/O; do not apply to an old inode.
                latest,latest_id=file_read(plan['workspace'],plan['path'])
                if latest!=current or latest_id!=version or identity(os.stat(leaf,dir_fd=parent,follow_symlinks=False))!=version:
                    raise Refused('file_changed_before_write')
                write_content(fd,plan['after'])
                actual,_=file_read(plan['workspace'],plan['path'])
                if actual!=plan['after']:raise Refused('post_write_verification_failed')
            except (OSError,ValueError) as error:
                plan.update(status='write_outcome_unknown',error_type=type(error).__name__,notice='pending')
                # Preserve before/after; never blindly retry or automatically undo a possibly concurrent change.
                persist(path,state)
                raise
            plan.update(status='applied',finished_at=time.time(),notice='pending')
            actions.refresh(state)
            persist(path,state)
            return {'status':'applied','id':key,'path':plan['path'],'recovery_point':key,
                    'feedback':'Return this result to the executing Agent; no task continuation is forced.','model_calls':0}


def status(data,session):
    with locked(data,session) as (_,state):
        return {'status':'available' if state else 'task_unavailable','rollbacks':state.get('rollbacks',{}) if state else {},'model_calls':0}


def on_hook(data,payload):
    if payload['hook_event_name']!='PreToolUse':return None
    # No unrelated task creation, model dispatch, forced continuation or tool veto.
    if not paths(data,payload['session_id'])[1].exists():return None
    with locked(data,payload['session_id']) as (path,state):
        if not actions.allowed(data,state):return None
        entries=[p for p in state.get('rollbacks',{}).values() if p.get('notice')=='pending']
        if not entries:return None
        entry=entries[0];entry['notice']='emitted_unconfirmed'
        persist(path,state)
        return '[Navi rollback record / '+entry['id']+'] '+json.dumps({'path':entry['path'],'outcome':entry['status']},ensure_ascii=False)+'. Historical operation result; inspect current state before further edits. No acknowledgment or forced continuation required.'


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation',choices=('inspect','prepare','apply','status'))
    parser.add_argument('--data-directory',type=Path,required=True);parser.add_argument('--session',required=True)
    parser.add_argument('--path');parser.add_argument('--select',type=int,nargs='+');parser.add_argument('--restore')
    parser.add_argument('--plan');parser.add_argument('--confirmation',type=Path)
    args=parser.parse_args()
    try:
        if args.operation=='inspect':result=inspect(args.data_directory,args.session,args.path)
        elif args.operation=='prepare':result=prepare(args.data_directory,args.session,args.path,args.select,args.restore)
        elif args.operation=='status':result=status(args.data_directory,args.session)
        else:
            if not args.confirmation:raise Refused('confirmation_required')
            with args.confirmation.open('rb') as stream:raw=stream.read(16385)
            if len(raw)>16384:raise Refused('confirmation_too_large')
            result=apply(args.data_directory,args.session,args.plan,json.loads(raw))
        print(json.dumps(result,ensure_ascii=False,indent=2));return 0
    except (OSError,ValueError,TypeError,KeyError) as error:
        print(json.dumps({'status':'rollback_refused_or_incomplete','reason':str(error) if isinstance(error,Refused) else type(error).__name__,'plan_id':args.plan,'recovery':'inspect stored before/after with status if a write may have started; do not blindly retry','model_calls':0}));return 1


if __name__=='__main__':sys.exit(main())
