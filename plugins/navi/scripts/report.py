#!/usr/bin/env python3
"""Deterministic user report. No model calls, workspace writes, attribution guesses or rollback."""
import argparse
from collections import Counter
import difflib
import html
import json
import os
from pathlib import Path
import re
import time
import budget

import action_evidence as actions
import history as conversation_history
import history_context
import reminders
import review
from task_state import supervision_sources, context_view, locked, user_context_digest


TEXT_STATES = {'present','absent'}
LABELS = {
    'historical_observations':'历史操作证据，不代表当前行为', 'prepared':'已生成预览，未执行', 'applying':'写入未完成，需检查恢复点', 'applied':'已执行并核验', 'write_outcome_unknown':'写入结果未知，需检查恢复点', 'none':'无',
    'current':'当前记录适用', 'stale':'已过期', 'not_revalidated':'未复核',
    'queued':'等待评估', 'running':'评估中', 'completed':'已完成', 'failed':'失败',
    'cancelled_before_call':'调用前取消', 'superseded_before_call':'调用前已失效',
    'within_scope':'当前证据显示范围内', 'drift':'疑似范围偏移', 'uncertain':'证据不足',
    'concern':'实现风险提示', 'none_observed':'当前证据未见风险',
    'phase_expansion':'阶段越界', 'unrelated_work':'无关工作', 'goal_displacement':'补充信息取代目标',
    'rationale_as_copy':'解释误作展示', 'overengineering':'过度工程', 'narrow_hardcoding':'过窄特判', 'unknown':'未知',
    'scope_reduction':'未授权缩减范围', 'implementation_substitution':'实现替代',
    'unsupported_completion':'完成声明与证据不符',
    'partial':'部分覆盖', 'complete_for_checkpoint':'仅覆盖指定检查点',
    'pending':'等待投递', 'emitted_unconfirmed':'已尝试投递，接收未确认', 'delivered':'有接收证据',
    'cancelled':'已取消', 'report_only_late':'晚到，仅记录', 'report_only_budget':'达到提醒上限，仅记录',
    'unconfirmed':'未确认', 'confirmed_by_external_test_observer':'外部测试观察者提供了接收证据',
    'still_observed':'此前问题仍可见', 'no_longer_observed':'此前问题在当前文件中不再可见'}


def content(snapshot):
    return (snapshot.get('status'), snapshot.get('text') if snapshot.get('status')=='present' else None)


def difference(before, after, before_name, after_name):
    if before.get('status') not in TEXT_STATES or after.get('status') not in TEXT_STATES:
        return None
    lines=difflib.unified_diff(before.get('text','').splitlines(keepends=True),
        after.get('text','').splitlines(keepends=True),fromfile=before_name,tofile=after_name,lineterm='\n')
    return ''.join(line if line.endswith('\n') else line+'\n\\ No newline at end of file\n' for line in lines)


def modification(name, entry, history, jobs):
    before,after=entry['baseline'],entry['current']
    if before['status'] not in TEXT_STATES or after['status'] not in TEXT_STATES:
        kind='unknown'
    elif content(before)==content(after):
        kind='returned_to_baseline' if history.get('ever_differed') else 'no_current_difference'
    elif before['status']=='absent':kind='added'
    elif after['status']=='absent':kind='deleted'
    else:kind='modified'
    reference=history.get('git_reference',{'status':'unavailable','reason':'legacy_baseline_no_git_reference'})
    existing=(content(reference)!=content(before) if reference['status'] in TEXT_STATES and before['status'] in TEXT_STATES else None)
    related=[]
    for job in jobs:
        if name in job['cited_paths']:
            related.append(job['id'])
    return {'path':name,'kind':kind,'baseline_at':entry.get('baseline_at'),
        'baseline_origin':history.get('origin','legacy_watch_snapshot'),
        'task_start_equivalence':history.get('task_start_equivalence','unknown'),
        'captured_tool_events_before_baseline':history.get('captured_tool_events_before_baseline'),
        'baseline_status':before['status'],'current_status':after['status'],'last_observed_at':entry.get('observed_at'),
        'baseline_sha256':before.get('sha256'),'current_sha256':after.get('sha256'),
        'preexisting_difference':existing,'reference_commit':reference.get('commit'),
        'reference_status':reference['status'],
        'preexisting_diff':difference(reference,before,'HEAD/'+name,'watch-baseline/'+name),
        'observed_diff':difference(before,after,'watch-baseline/'+name,'observed/'+name),
        'history_coverage':'since_watch_enable' if history else 'unknown_legacy_history',
        'observed_transitions':history.get('observed_transitions'),
        'attribution':'unknown_may_include_concurrent_edits',
        'related_review_ids':related,'whole_file_out_of_scope':'not_inferred_from_citations',
        'dependencies':'not_assessed','rollback':'explicit_reviewed_selection_required'}


def usage(jobs):
    return budget.usage(jobs)


def review_rows(state, gap, revalidated):
    rows=[]
    for key,job in state.get('reviews',{}).items():
        output=job.get('output') or {}
        results=(output.get('result') or {}).get('results',[]) if isinstance(output.get('result',{}),dict) else []
        result=results[0] if job['status']=='completed' and results and isinstance(results[0],dict) else None
        reasons=[]
        if job.get('stale'):reasons.append('recorded_stale')
        if job.get('context_digest')!=user_context_digest(state):reasons.append('user_sources_changed')
        if review.is_stale(state,job):reasons.append('evidence_or_supervision_version_changed')
        if gap:reasons.append('capture_gap')
        freshness='stale' if reasons else ('historical_observations' if job.get('activity_basis') else ('current' if revalidated else 'not_revalidated'))
        cited=set()
        if result:
            cited.update(result.get('evidence_ids',[]))
            cited.update(result.get('implementation_risk',{}).get('evidence_ids',[]))
            cited.update(result.get('followup',{}).get('evidence_ids',[]))
        packet=job.get('packet',{})
        evidence=[dict(s,kind='user_source') for s in packet.get('user_sources',[]) if s['id'] in cited]
        evidence += [a for a in packet.get('actions',[]) if a['id'] in cited]
        rows.append({'id':key,'status':job['status'],'purpose':'followup' if job.get('followup') else 'review',
            'freshness':freshness,'freshness_reasons':reasons,'coverage':packet.get('coverage','unknown'),
            'scope':({k:result.get(k) for k in ('verdict','scope_labels','evidence_ids','reason')} if result else None),
            'implementation_risk':result.get('implementation_risk') if result else None,
            'evidence':evidence,'cited_paths':sorted({a['path'] for a in evidence if a.get('kind')!='user_source' and a.get('path') not in (None,'task','not_inferred_from_command')}),
            'error_type':job.get('error_type'),'created_at':job.get('created_at'),'finished_at':job.get('finished_at'),
            'advisory_outcome':job.get('advisory_outcome'),
            'health':'overdue_or_worker_lost' if job['status'] in ('queued','running') and time.time()-job.get('created_at',0)>180 else 'as_recorded',
            'judgment_status':'experimental_not_verified_fact'})
    return rows


def delivery_rows(rows):
    """Project existing judgments, including uncertain/stale ones; never infer completion or resolution."""
    gaps=[]
    for row in rows:
        scope=row.get('scope') or {}
        categories=sorted(review.DELIVERY_LABELS.intersection(scope.get('scope_labels') or []))
        if not categories or scope.get('verdict') not in ('drift','uncertain'):
            continue
        gaps.append({'review_id':row['id'],'verdict':scope['verdict'],'categories':categories,
                     'reason':scope['reason'],'freshness':row['freshness'],'coverage':row['coverage'],
                     'evidence_ids':scope['evidence_ids'],
                     'evidence':[e for e in row['evidence'] if e['id'] in scope['evidence_ids']],
                     'advisory_outcome':row['advisory_outcome'],
                     'judgment_status':row['judgment_status']})
    return gaps


def build(state, gap=False, revalidated=False, recording_allowed=False):
    if state is None:
        return {'schema_version':1,'status':'task_unavailable','report_generation_model_calls':0,
                'meaning':'No retained task state; this is not a clean report.'}
    rows=review_rows(state,gap,revalidated)
    capture=state.get('action_evidence',{})
    files=[modification(n,e,state.get('change_baselines',{}).get(n,{}),rows) for n,e in capture.get('files',{}).items()]
    notices=[]
    for key,entry in state.get('reminder_transport',{}).get('entries',{}).items():
        stale=reminders.stale_reason(state,None,entry)
        check=entry.get('followup')
        followup=None
        if check:
            obsolete=(check.get('file_basis')!=actions.file_basis(state) or check.get('user_context_digest')!=user_context_digest(state))
            followup={**check,'freshness':'stale' if gap or obsolete else ('current' if revalidated else 'not_revalidated'),
                      'causal_adoption':'not_established'}
        notices.append({'id':key,'origin':entry['origin'],'status':entry['status'],
            'current_validity':'stale' if gap or stale else ('historical_observations' if entry.get('activity_basis') else ('current' if revalidated else 'not_revalidated')),
            'reason':entry.get('reason') or stale,'delivery':entry.get('delivery','unconfirmed'),
            'receipt_provenance':entry.get('receipt',{}).get('origin'),'adoption':entry.get('adoption','not_assessed'),
            'agent_response':entry.get('agent_response'),
            'scope_requirement':entry['payload']['requirement'],'judgment':entry['payload']['evidence'],
            'suggestion':entry['payload']['suggestion'],'review_id':entry.get('review_id'),'followup':followup})
    current=[r for r in rows if r['status']=='completed' and r['freshness']=='current']
    summary={'reviews_recorded':len(rows),'current_scope_drift_judgments':sum(r['scope'] is not None and r['scope']['verdict']=='drift' for r in current),
        'current_risk_concern_judgments':sum(r['implementation_risk'] is not None and r['implementation_risk']['verdict']=='concern' for r in current),
        'review_status_counts':dict(Counter(r['status'] for r in rows)),
        'review_freshness_counts':dict(Counter(r['freshness'] for r in rows)),
        'file_change_counts':dict(Counter(f['kind'] for f in files)),
        'reminder_status_counts':dict(Counter(n['status'] for n in notices)),
        'counting':'One row per review, not per label. Scope/risk axes overlap and must not be added as issue counts.',
        'overall_scope_clearance':'not_established'}
    return {'schema_version':1,'status':'snapshot_report','generated_at':time.time(),
        'task':{'id':state['task_id'],'session_id':state['session_id'],'status':state['status'],
            'created_at':state.get('created_at'),'completion':'not_inferred_from_Stop','contract':state['contract'],
            'intent':context_view(state),'user_sources':supervision_sources(state)},
        'conversation_history':conversation_history.view(state),
        'incremental_context':__import__('context_sync').status(state),
        'history_supervision':history_context.status(state),
        'history_usage':usage({'history':state['history_integration']} if state.get('history_budget',{}).get('calls_reserved') else {}),
        'collection':{'recording_allowed':bool(recording_allowed),'action_capture_enabled':bool(capture.get('enabled')),
            'current_files_revalidated':revalidated,'capture_gap':bool(gap),'coverage':'partial_files_and_local_activity' if state.get('supervisor',{}).get('include_activity') else 'partial_explicit_watch_only',
            'dropped_tool_events':capture.get('dropped_events',0),
            'truncated_tool_events':sum(e.get('input',{}).get('truncated',False) or bool(e.get('response') and e['response'].get('truncated')) for e in capture.get('events',[])),
            'watched_files':list(capture.get('files',{})),
            'unexamined':['Unobserved file contents; pure-prose plans and tools bypassing hooks','Changes before capture or between snapshots',
                'Missing caller and dependency context','Concurrent edits and authorship','Paused or missing user input']},
        'summary':summary,'budget':state['budget'],'usage':budget.total_usage(state),
        'review_gate':budget.review_gate(state),
        'retention':{'omitted_reviews':state.get('review_rollup',{}).get('reviews',0),
                     'omitted_reminders':state.get('reminder_transport',{}).get('omitted',{})},
        'supervisor_configuration':state.get('supervisor',{}),
        'files':files,'reviews':rows,'reminders':notices,
        'delivery_gaps':delivery_rows(rows),
        'findings':[dict(id=k,**v,verification='agent_reported_not_independently_verified') for k,v in state['findings'].items()],
        'rollbacks':[{k:p.get(k) for k in ('id','status','path','selected_changes','restores','created_at','finished_at','notice','error_type','diff','confirmation')} for p in sorted(state.get('rollbacks',{}).values(),key=lambda p:p['created_at'])],
        'report_generation_model_calls':0,'rollback':'explicit_reviewed_selection_required'}


def generate(data, session, recorded_only=False):
    with locked(data,session) as (path,state):
        allowed=actions.allowed(data,state)
        refreshed=bool(state and allowed and state.get('action_evidence',{}).get('enabled') and not recorded_only)
        if refreshed:
            actions.refresh(state)
        # Report generation does not persist observations, renew retention, dispatch reviews or mutate budgets.
        return build(state,path.with_suffix('.gap').exists(),refreshed,allowed)


def safe(value):
    if value is None:return '未知'
    value=html.escape(str(value),quote=False)
    return re.sub(r'([\\`*_{}\[\]()#+!|])',r'\\\1',value).replace('\n',' / ')


def label(value):
    return safe(LABELS.get(value,value))


def fence(value):
    delimiter='`'*max(3,1+max((len(m.group()) for m in re.finditer(r'`+',value)),default=0))
    return delimiter+'diff\n'+value+('' if value.endswith('\n') else '\n')+delimiter+'\n'


def when(value):
    return time.strftime('%Y-%m-%d %H:%M:%S UTC',time.gmtime(value)) if isinstance(value,(int,float)) else '未知'


def markdown(report):
    if report['status']=='task_unavailable':
        return '# Navi 任务报告\n\n没有保留的任务状态；不能据此判断任务没有偏移。\n'
    summary=report['summary'];u=report['usage'];c=report['collection'];b=report['budget']
    lines=['# Navi 任务报告','',
        '这是当前记录的报告快照；Stop 不代表整个任务完成。模型判断仍是实验性判断。','',
        '## 概览','',
        '- 任务：'+safe(report['task']['id']),
        '- 文件复核：'+('已重新读取显式观察文件' if c['current_files_revalidated'] else '仅使用已有记录，当前文件未复核'),
        '- 采集缺口：'+('有' if c['capture_gap'] else '未记录到；不代表覆盖完整'),
        '- 评估记录：'+str(summary['reviews_recorded'])+'；当前有效范围偏移判断：'+str(summary['current_scope_drift_judgments'])+'；当前有效实现风险判断：'+str(summary['current_risk_concern_judgments']),
        '- 以上是两个可重叠维度，多标签不重复计数；零条不代表已检查通过。',
        '- 评估预算已预留：'+str(b['reviews_used'])+'/'+('未设累计上限' if b.get('review_limit') is None else str(b['review_limit']))+'；提醒尝试：'+str(b['reminders_used']),
        '- 已报告独立评估 tokens：'+str(u['reported_input_plus_output'])+'（输入 '+str(u['input_tokens'])+'，输出 '+str(u['output_tokens'])+'；缓存输入 '+str(u['cached_input_tokens'])+' 已包含在输入中）。',
        '- 用量缺失/不完整：'+str(len(u['usage_missing_review_ids'])+len(u['usage_incomplete_review_ids'])+u.get('omitted_review_usage',{}).get('missing',0)+u.get('omitted_review_usage',{}).get('incomplete',0))+' 条；主 Agent 用量未采集。生成本报告没有模型调用。','',
        '## 任务依据','']
    gate=report.get('review_gate')
    if gate:
        message=('达到用户设置的累计评估预算；调整预算后才可继续评估。' if gate['reason']=='review_budget_exhausted' else
                 '滚动小时额度暂满；'+when(gate['resumes_at'])+' 后下一次符合条件的事件可恢复检查。')
        lines[-2:-2]=['- **监督评估暂停**：'+message+'执行 Agent 继续工作。']
    retention=report.get('retention',{})
    if retention.get('omitted_reviews') or retention.get('omitted_reminders',{}).get('entries'):
        lines[-2:-2]=['- 滚动保留最近明细：已移出 '+str(retention.get('omitted_reviews',0))+' 条评估、'+
                     str(retention.get('omitted_reminders',{}).get('entries',0))+' 条提醒；累计计数及已取得用量保留，旧证据不可再查看，不能当作当前通过结论。']
    sync=report.get('incremental_context',{})
    lines[-2:-2]=['- 对话增量同步：'+safe(sync.get('status','snapshot_only'))+'；最近成功同步：'+when(sync.get('refreshed_at'))+'；仅本地读取，不调用历史整理模型。',
        '- 同步缺口：'+safe(sync.get('reason') or '无已记录错误；仍受上下文选取上限约束'),'']
    delivery=['## 交付缺口与待核验项','',
        '以下来自独立评估，不是执行 Agent 的完成声明。证据不足、历史或过期判断不代表当前确认缺失；过期也不代表已修复。','']
    gaps=report.get('delivery_gaps',[])
    if not gaps:
        delivery+=['没有保留的交付缺口判断；不代表全部需求已实现或已通过交付核验。','']
    for gap in gaps:
        delivery+=['- '+safe(gap['review_id'])+'：'+'、'.join(label(v) for v in gap['categories']),
                   '  - 判断：'+label(gap['verdict'])+'；有效性：'+label(gap['freshness'])+'；覆盖：'+label(gap['coverage']),
                   '  - 差异与核验边界：'+safe(gap['reason']),
                   '  - 证据：'+safe(', '.join(gap['evidence_ids']))+'（详见独立评估与证据）。',
                   '  - 提醒状态：'+label(gap.get('advisory_outcome') or '未记录投递')]
    delivery.append('')
    lines[-2:-2]=delivery
    intent=report['task']['intent']['intent_memory']
    if intent:
        lines.append('以下是带引用、未经独立核验的任务解释：'+safe(intent['freshness']))
        for item in intent['items']:
            if item['status'] in ('active','deferred','unknown'):
                lines.append('- '+safe(item['kind'])+' / '+safe(item['status'])+'：'+safe(item['text']))
    else:lines.append('暂无可用的结构化任务解释；以保存的用户原文为准。')
    for source in report['task']['user_sources']:
        lines.append('- '+safe(source['id'])+'：'+safe(source['text'][:240])+('…（完整原文见 JSON）' if len(source['text'])>240 else ''))
    history=report.get('conversation_history',{})
    if history.get('status') not in (None,'not_requested'):
        lines+=['','### 接入前会话历史','',
            '- 读取状态：'+safe(history['status'])+'；保留消息数：'+str(len(history.get('messages',[]))),
            '- 历史监督接入：'+safe(report.get('history_supervision',{}).get('status','未开启；仅原文读取，未自动恢复任务语义'))+'；Agent 回复不代表用户授权。',
            '- 历史整理调用预留：'+str(report.get('history_supervision',{}).get('budget',{}).get('calls_reserved',0))+'/1；已报告 tokens：'+str(report.get('history_usage',{}).get('reported_input_plus_output',0))+'（与监督评估分开计数；缓存不重复相加）。',
            '- 整理状态与缺口：'+safe(report.get('history_supervision',{}).get('reason','无已记录错误；解释未经核验'))+'；新输入是否已整理：'+safe(report.get('history_supervision',{}).get('interpretation_current')),
            '- 历史整理用量缺失条数：'+str(len(report.get('history_usage',{}).get('usage_missing_review_ids',[]))),
            '- 覆盖缺口：'+safe(', '.join(history.get('gaps',[])) or '未检测到；不保证完整历史'),
            '- 完整有界原文及来源位置见 JSON 报告；读取未调用模型。']
    lines+=['','## 修改与归因','','所有文件变化的作者均未确认；工具请求或时间相邻不足以证明归因。依赖关系尚未自动评估；此处只展示 diff。','']
    labels={'added':'新增','deleted':'删除','modified':'修改','returned_to_baseline':'曾观察到变化，现已恢复观察基线',
            'no_current_difference':'当前与观察基线无差异','unknown':'无法比较'}
    if not report['files']:lines.append('没有观察文件或修改基线，无法重建修改。')
    for file in report['files']:
        lines+=['### '+safe(file['path']),'',labels[file['kind']],
            '- 基线来源：'+safe(file['baseline_origin'])+'；与任务开始时刻是否一致：未证实。',
            '- 基线时间：'+when(file['baseline_at'])+'；最近文件观察：'+when(file['last_observed_at']),
            '- Git 参考提交：'+safe(file['reference_commit'] or '不可用'),
            '- 基线之前已捕获的工具事件数：'+safe(file['captured_tool_events_before_baseline']),
            '- 关联评估：'+safe(', '.join(file['related_review_ids']) or '无；不代表范围内'),'']
        if file['preexisting_difference'] is True:
            lines+=['开始观察时相对 Git HEAD 已有差异（作者未知，不归给当前 Agent）：','',fence(file['preexisting_diff'] or '(内容为空；存在性发生变化)')]
        elif file['preexisting_difference'] is None:lines+=['缺少可比较的 Git 参考，观察前是否已有差异未知。','']
        else:lines+=['开始观察时与记录的 Git HEAD 内容一致。','']
        if file['observed_diff'] is None:lines+=['观察后 diff 不可用：'+safe(file['baseline_status'])+' → '+safe(file['current_status']), '']
        elif file['observed_diff']:lines+=['观察后累计 diff：','',fence(file['observed_diff'])]
        else:lines+=['当前无文本 diff；文件状态：'+safe(file['baseline_status'])+' → '+safe(file['current_status']), '']
    lines+=['## 独立评估与证据','']
    if not report['reviews']:lines+=['尚无独立评估；不能认定没有偏移。','']
    for job in report['reviews']:
        lines+=['### '+safe(job['id']),'','状态：'+label(job['status'])+'；有效性：'+label(job['freshness'])+'；覆盖：'+label(job['coverage'])]
        for title,axis in (('范围',job['scope']),('实现风险',job['implementation_risk'])):
            if axis:
                lines+=['- '+title+'：'+label(axis['verdict'])+'；'+'、'.join(label(v) for v in axis.get('scope_labels',axis.get('labels',[]))),
                    '  - 判断理由：'+safe(axis['reason'])]
        if job['error_type']:lines.append('- 失败类别：'+safe(job['error_type']))
        if job['freshness_reasons']:lines.append('- 过期原因：'+safe(', '.join(job['freshness_reasons'])))
        if job['health']!='as_recorded':lines.append('- 健康状态：'+safe(job['health']))
        for ev in job['evidence']:
            lines.append('- 依据 '+safe(ev['id'])+'：'+safe(ev.get('kind'))+' '+safe(ev.get('path','')))
            if ev['kind']=='user_source':lines.append('  - '+safe(ev['text'][:240])+('…（完整依据见 JSON）' if len(ev['text'])>240 else ''))
        lines.append('')
    lines+=['## 提醒及后续观察','','已尝试投递不等于已收到；后续问题不再可见不等于 Navi 导致了纠正。','']
    if not report['reminders']:lines.append('没有提醒记录。')
    for notice in report['reminders']:
        lines+=['- '+safe(notice['id'])+'：'+label(notice['status'])+'；送达证据：'+label(notice['delivery'])+'；当前有效性：'+label(notice['current_validity']),
            '  - 提醒判断：'+safe(notice['judgment'])]
        if notice.get('agent_response'):
            response=notice['agent_response']['value']
            lines.append('  - Agent 回应（未独立核验）：'+safe(response['disposition'])+'；'+safe(response['reason']))
        if notice['followup']:
            check=notice['followup'];lines.append('  - 后续观察：'+label(check['judgment']['observation'])+'；'+label(check['freshness'])+'；'+safe(check['judgment']['reason']))
    lines+=['','## 用户选择的回滚记录','']
    if not report.get('rollbacks'):lines.append('没有回滚记录；范围判断不会自动触发回滚。')
    for operation in report.get('rollbacks',[]):
        lines.append('- '+safe(operation['id'])+' / '+safe(operation['path'])+'：'+label(operation['status'])+'；Agent 通知：'+label(operation['notice']))
        if operation.get('restores'):lines.append('  - 恢复此前操作：'+safe(operation['restores']))
        if operation.get('confirmation'):lines.append('  - 确认依据：'+safe(operation['confirmation']['source_id'])+'；调用方对用户确认的解释，非自动语义核验。')
        if operation.get('diff'):lines+=['',fence(operation['diff'])]
    lines+=['','## 其他发现与未覆盖范围','']
    for finding in report['findings']:
        lines.append('- 未独立确认的发现：'+safe(finding.get('location',''))+'；'+safe(finding.get('summary','')))
    lines+=['- 文件内容限显式观察文件；开启活动检查后可检查有界工具/计划证据，不能确认未观察文件的最终修改；纯文字计划、绕过 Hook 的工具和完整依赖仍未覆盖。',
        '- 采集前、暂停期间、两次快照之间及并发修改可能缺失；旧任务可能没有历史变化轨迹。',
        '- 过期、失败、未完成和缺失评估都不是检查通过；记录不构成回滚授权。','']
    return '\n'.join(lines)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-directory',type=Path,required=True)
    parser.add_argument('--session',required=True)
    parser.add_argument('--format',choices=('json','markdown'),default='markdown')
    parser.add_argument('--recorded-only',action='store_true')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    try:
        report=generate(args.data_directory,args.session,args.recorded_only)
        rendered=json.dumps(report,ensure_ascii=False,indent=2)+'\n' if args.format=='json' else markdown(report)
        if args.output:
            # Explicit export only; never overwrite a working file or follow an existing symlink.
            with os.fdopen(os.open(args.output,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w',encoding='utf-8') as stream:
                stream.write(rendered)
        else:print(rendered,end='')
        return 0
    except (OSError,ValueError,KeyError,TypeError) as error:
        print(json.dumps({'status':'report_unavailable','error_type':type(error).__name__,'report_generation_model_calls':0}))
        return 1


if __name__=='__main__':raise SystemExit(main())
