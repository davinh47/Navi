import copy
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugins/navi/scripts'))
import action_evidence as actions
import change_baseline
import report
import review
import reminders
import task_state as tasks


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.data=self.root/'data';self.work=self.root/'work';self.work.mkdir()
        self.file=self.work/'part.txt';self.file.write_text('original\n');self.session='report-task'
        tasks.on_hook(self.data,{'session_id':self.session,'turn_id':'t1','hook_event_name':'UserPromptSubmit',
            'prompt':'Navi: start\nChange the title only. Keep required validation.'})
    def tearDown(self):self.temp.cleanup()
    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def save(self,s):tasks.save(tasks.paths(self.data,self.session)[1],s)
    def enable(self,names=None):actions.configure(self.data,self.session,str(self.work),names or ['part.txt'])
    def refresh(self):
        s=self.state();actions.refresh(s);self.save(s)
    def make_job(self,verdict='drift',risk='concern',labels=None):
        s=self.state();packet=review.make_packet(s,actions.export(s))
        ids=[packet['user_sources'][0]['id'],next(a['id'] for a in packet['actions'] if a['kind']=='workspace_observation')]
        result={'case_id':s['task_id'],'verdict':verdict,'scope_labels':labels or ['phase_expansion','unrelated_work'],
            'evidence_ids':ids,'reason':'Observed scope judgment.',
            'implementation_risk':{'verdict':risk,'labels':['overengineering'],'evidence_ids':ids,'reason':'Experimental risk.'}}
        job={'status':'completed','packet':packet,'action_version':actions.version(s),'context_digest':tasks.user_context_digest(s),
            'created_at':time.time(),'output':{'result':{'results':[result]},'usage':{'input_tokens':100,'output_tokens':20,'cached_input_tokens':80,'reasoning_output_tokens':7}}}
        s.setdefault('reviews',{})['review-1']=job;s['budget']['reviews_used']=1;self.save(s)
        return job
    def git(self,*args):
        return subprocess.run(['git','-C',str(self.work),'-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
            '-c','user.name=Navi Test','-c','user.email=navi-test@example.invalid',*args],check=True,capture_output=True,text=True).stdout
    def repository(self):
        self.git('init','-q');self.git('add','part.txt');self.git('commit','-qm','fixture baseline')

    def test_preexisting_git_difference_is_separate_from_later_change(self):
        self.repository();self.file.write_text('existing edit\n');self.enable()
        self.file.write_text('later edit\n');r=report.generate(self.data,self.session);f=r['files'][0]
        self.assertTrue(f['preexisting_difference'])
        self.assertIn('-original',f['preexisting_diff']);self.assertIn('+existing edit',f['preexisting_diff'])
        self.assertIn('-existing edit',f['observed_diff']);self.assertIn('+later edit',f['observed_diff'])
        self.assertIn('unknown',f['attribution']);self.assertEqual(f['dependencies'],'not_assessed')
        self.assertEqual(f['task_start_equivalence'],'unverified')

    def test_delivery_section_keeps_evidence_freshness_and_late_results(self):
        self.enable();self.make_job(labels=['implementation_substitution','unsupported_completion'])
        state=self.state();state['reviews']['review-1']['advisory_outcome']='report_only_late';self.save(state)
        r=report.generate(self.data,self.session)
        gap=r['delivery_gaps'][0]
        self.assertEqual(gap['freshness'],'current')
        self.assertEqual(gap['advisory_outcome'],'report_only_late')
        self.assertEqual({e['id'] for e in gap['evidence']},set(gap['evidence_ids']))
        rendered=report.markdown(r)
        self.assertLess(rendered.index('## 交付缺口'),rendered.index('## 任务依据'))
        self.assertIn('晚到，仅记录',rendered)
        self.assertEqual(r['report_generation_model_calls'],0)
        self.file.write_text('changed after review\n')
        later=report.generate(self.data,self.session)
        self.assertEqual(later['delivery_gaps'][0]['freshness'],'stale')
        self.assertIn('过期也不代表已修复',report.markdown(later))

    def test_no_delivery_judgment_is_not_completion_clearance(self):
        self.enable();self.make_job(verdict='within_scope',labels=['implementation_substitution'])
        r=report.generate(self.data,self.session)
        self.assertEqual(r['delivery_gaps'],[])
        self.assertIn('不代表全部需求已实现',report.markdown(r))

    def test_partial_uncertain_delivery_is_not_promoted_to_proven_gap(self):
        self.enable();self.make_job(verdict='uncertain',labels=['scope_reduction'])
        r=report.generate(self.data,self.session)
        self.assertEqual(r['delivery_gaps'][0]['verdict'],'uncertain')
        self.assertEqual(r['delivery_gaps'][0]['coverage'],'partial')
        self.assertEqual(r['summary']['current_scope_drift_judgments'],0)

    def test_git_reference_from_nested_workspace_uses_repository_relative_path(self):
        self.repository()
        nested=self.work/'sub project';nested.mkdir();(nested/'part.txt').write_text('nested original\n')
        self.git('add','sub project/part.txt');self.git('commit','-qm','nested file')
        (nested/'part.txt').write_text('nested preexisting\n')
        actions.configure(self.data,self.session,str(nested),['part.txt'])
        f=report.generate(self.data,self.session)['files'][0]
        self.assertEqual(f['reference_status'],'present')
        self.assertIn('-nested original',f['preexisting_diff'])
        self.assertIn('+nested preexisting',f['preexisting_diff'])
        self.assertNotIn('-original\n',f['preexisting_diff'])

    def test_git_reference_and_watch_baseline_survive_reenable_and_new_commit(self):
        self.repository();self.enable();before=self.state()
        self.file.write_text('later\n');self.git('add','part.txt');self.git('commit','-qm','new head')
        self.enable();after=self.state()
        self.assertEqual(before['change_baselines'],after['change_baselines'])
        self.assertEqual(before['action_evidence']['files']['part.txt']['baseline'],after['action_evidence']['files']['part.txt']['baseline'])
        self.assertNotEqual(after['change_baselines']['part.txt']['git_reference']['commit'],self.git('rev-parse','HEAD').strip())

    def test_no_git_or_late_baseline_is_unknown_not_agent_authored(self):
        self.enable()
        actions.on_hook(self.data,{'session_id':self.session,'turn_id':'t1','hook_event_name':'PreToolUse','tool_use_id':'read','tool_input':'anything'})
        (self.work/'late.txt').write_text('already here')
        self.enable(['part.txt','late.txt'])
        late=next(f for f in report.generate(self.data,self.session)['files'] if f['path']=='late.txt')
        self.assertIsNone(late['preexisting_difference'])
        self.assertGreater(late['captured_tool_events_before_baseline'],0)
        self.assertEqual(late['task_start_equivalence'],'unverified')

    def test_legacy_baseline_not_retroactively_filled(self):
        self.enable();s=self.state();s.pop('change_baselines');self.save(s)
        self.enable();self.assertNotIn('change_baselines',self.state())
        f=report.generate(self.data,self.session)['files'][0]
        self.assertEqual(f['history_coverage'],'unknown_legacy_history');self.assertIsNone(f['preexisting_difference'])

    def test_added_deleted_empty_and_returned_files(self):
        self.enable(['part.txt','empty.txt'])
        self.file.write_text('changed\n');self.refresh();self.file.write_text('original\n')
        (self.work/'empty.txt').write_text('')
        r=report.generate(self.data,self.session)
        files={f['path']:f for f in r['files']}
        self.assertEqual(files['part.txt']['kind'],'returned_to_baseline')
        self.assertEqual(files['empty.txt']['kind'],'added')
        self.assertEqual(files['empty.txt']['observed_diff'],'')
        self.file.unlink()
        self.assertEqual(next(f for f in report.generate(self.data,self.session)['files'] if f['path']=='part.txt')['kind'],'deleted')

    def test_newline_only_change_has_visible_diff(self):
        self.file.write_text('same');self.enable();self.file.write_text('same\n')
        f=report.generate(self.data,self.session)['files'][0]
        self.assertEqual(f['kind'],'modified');self.assertIn('No newline at end of file',f['observed_diff'])

    def test_unreadable_interval_does_not_invent_a_reverted_edit(self):
        self.enable();self.file.write_bytes(b'\0unknown');self.refresh()
        self.file.write_text('original\n');self.refresh()
        self.assertEqual(report.generate(self.data,self.session)['files'][0]['kind'],'no_current_difference')

    def test_binary_oversized_and_symlink_are_not_read_as_text(self):
        self.enable();(self.root/'secret').write_text('PRIVATE CONTENT')
        self.file.unlink();self.file.symlink_to(self.root/'secret')
        r=report.generate(self.data,self.session);self.assertEqual(r['files'][0]['kind'],'unknown')
        self.assertNotIn('PRIVATE CONTENT',json.dumps(r))
        self.file.unlink();self.file.write_bytes(b'\x00binary')
        self.assertIsNone(report.generate(self.data,self.session)['files'][0]['observed_diff'])
        self.file.write_text('x'*4097)
        self.assertEqual(report.generate(self.data,self.session)['files'][0]['current_status'],'oversized')

    def test_report_never_mutates_state_budget_retention_or_workspace(self):
        self.enable();self.file.write_text('new\n')
        path=tasks.paths(self.data,self.session)[1];before=path.read_bytes()
        with patch.object(review,'run_model',side_effect=AssertionError('no model')):
            r=report.generate(self.data,self.session)
        self.assertEqual(path.read_bytes(),before);self.assertEqual(self.file.read_text(),'new\n')
        self.assertEqual(r['report_generation_model_calls'],0);self.assertEqual(r['budget']['reviews_used'],0)

    def test_brief_is_identical_to_json_summary_and_uses_explicit_counting_scope(self):
        self.enable();self.make_job()
        r=report.generate(self.data,self.session)
        self.assertEqual(report.brief(r),r['user_summary'])
        self.assertIn('当前对话累计用量与保留评估，非本轮统计',r['user_summary'])
        self.assertIn('保留 1 条，完成 1、失败 0、未结束 0',r['user_summary'])
        self.assertIn('当前有效 1、过期 0',r['user_summary'])
        self.assertIn('范围偏移 1、实现风险 1',r['user_summary'])

    def test_brief_uses_current_gate_not_stale_last_status(self):
        import supervisor
        self.enable();supervisor.configure(self.data,self.session,True,profile='long-task')
        s=self.state();s['supervisor']['last_status']='review_budget_exhausted';self.save(s)
        rendered=report.generate(self.data,self.session)['user_summary']
        self.assertIn('状态：可调度',rendered)
        self.assertIn('最小间隔 180 秒',rendered)
        self.assertIn('未设累计上限',rendered)
        self.assertNotIn('预算耗尽',rendered)
        s=self.state();s['budget']['review_limit']=0;self.save(s)
        self.assertIn('累计评估预算耗尽',report.generate(self.data,self.session)['user_summary'])

    def test_brief_distinguishes_busy_lost_and_paused_supervision(self):
        import supervisor
        self.enable();supervisor.configure(self.data,self.session,True);self.make_job()
        s=self.state();s['reviews']['review-1'].update(status='running',output=None);self.save(s)
        self.assertIn('状态：评估进行中',report.generate(self.data,self.session)['user_summary'])
        s=self.state();s['reviews']['review-1']['created_at']=0;self.save(s)
        self.assertIn('执行进程失联',report.generate(self.data,self.session)['user_summary'])
        supervisor.configure(self.data,self.session,False)
        self.assertIn('自动监督未启用',report.generate(self.data,self.session)['user_summary'])

    def test_brief_keeps_stale_delivery_findings_separate_from_current_counts(self):
        self.enable();self.make_job(labels=['implementation_substitution'])
        self.file.write_text('later version\n')
        rendered=report.generate(self.data,self.session)['user_summary']
        self.assertIn('当前有效 0、过期 1',rendered)
        self.assertIn('范围偏移 0',rendered)
        self.assertIn('review-1 / 已过期 / 范围',rendered)
        self.assertIn('实现替代',rendered)

    def test_brief_reports_concrete_capture_and_usage_gaps(self):
        self.enable();self.make_job()
        s=self.state();s['action_evidence']['dropped_events']=17
        s['reviews']['review-1'].update(status='failed',output=None)
        s['review_rollup']={'reviews':8,'input_tokens':30,'output_tokens':10,'missing':2}
        self.save(s);self.file.write_text('x'*4097)
        rendered=report.generate(self.data,self.session)['user_summary']
        for expected in ('文件快照不可用 1 个','超过快照大小上限 1 个','已移出 17 条工具事件',
                         '已移出 8 条旧明细','输入＋输出 40 tokens','用量缺失或不完整 3 条'):
            self.assertIn(expected,rendered)

    def test_brief_no_records_is_not_clearance_and_delivery_attempt_is_not_receipt(self):
        self.enable();s=self.state();s['budget']['reminders_used']=2;self.save(s)
        rendered=report.generate(self.data,self.session)['user_summary']
        self.assertIn('保留 0 条',rendered)
        self.assertIn('不代表整轮任务通过',rendered)
        self.assertIn('累计尝试 2 次；保留记录中确认接收 0 次',rendered)
        unavailable=report.generate(self.data,'absent')['user_summary']
        self.assertIn('任务记录不可用',unavailable)

    def test_brief_bounds_finding_details_and_escapes_evidence(self):
        self.enable();job=self.make_job(labels=['scope_reduction'])
        s=self.state()
        for i in range(5):
            value=copy.deepcopy(job)
            value['output']['result']['results'][0]['reason']='[click](bad)\n# instruction '+'x'*200
            s['reviews']['extra-'+str(i)]=value
        self.save(s)
        rendered=report.generate(self.data,self.session)['user_summary']
        self.assertIn('另有 3 条有发现的评估',rendered)
        self.assertIn('详见完整报告',rendered)
        self.assertNotIn('\n# instruction',rendered)
        self.assertNotIn('[click](bad)',rendered)

    def test_brief_cli_and_json_produce_same_template_without_state_writes(self):
        self.enable();self.make_job();path=tasks.paths(self.data,self.session)[1];before=path.read_bytes()
        command=[sys.executable,str(Path(report.__file__)),'--data-directory',str(self.data),'--session',self.session]
        result=subprocess.run(command+['--format','brief'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr+result.stdout)
        self.assertTrue(result.stdout.startswith('### Navi 监督报告'))
        result=subprocess.run(command+['--format','json'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        value=json.loads(result.stdout)
        self.assertEqual(value['user_summary'],report.brief(value))
        self.assertEqual(path.read_bytes(),before)

    def test_paused_or_recorded_only_does_not_read_files(self):
        self.enable();self.file.write_text('unobserved')
        with patch.object(actions,'snapshot',side_effect=AssertionError('must not read')):
            report.generate(self.data,self.session,recorded_only=True)
            (self.data/'disabled').touch()
            r=report.generate(self.data,self.session)
        self.assertFalse(r['collection']['current_files_revalidated'])
        self.assertEqual(r['files'][0]['kind'],'no_current_difference')

    def test_reviews_axes_labels_counts_usage_and_citations(self):
        self.enable();self.make_job();r=report.generate(self.data,self.session)
        self.assertEqual(r['summary']['reviews_recorded'],1)
        self.assertEqual(r['summary']['current_scope_drift_judgments'],1)
        self.assertEqual(r['summary']['current_risk_concern_judgments'],1)
        self.assertEqual(r['usage']['reported_input_plus_output'],120)
        self.assertEqual(r['usage']['cached_input_tokens'],80)
        self.assertEqual(len(r['reviews'][0]['evidence']),2)
        self.assertEqual(r['files'][0]['related_review_ids'],['review-1'])
        self.assertEqual(r['summary']['overall_scope_clearance'],'not_established')

    def test_changed_files_or_sources_make_old_judgments_stale(self):
        self.enable();self.make_job();self.file.write_text('changed')
        r=report.generate(self.data,self.session)
        self.assertEqual(r['reviews'][0]['freshness'],'stale')
        self.assertEqual(r['summary']['current_scope_drift_judgments'],0)
        tasks.on_hook(self.data,{'session_id':self.session,'turn_id':'t2','hook_event_name':'UserPromptSubmit','prompt':'Continue.'})
        self.assertIn('user_sources_changed',report.generate(self.data,self.session)['reviews'][0]['freshness_reasons'])

    def test_failed_overdue_budget_and_missing_usage_not_clean(self):
        self.enable();job=self.make_job();s=self.state()
        s['reviews']['review-1'].update(status='failed',output=None,error_type='TimeoutError')
        s['reviews']['review-2']=dict(job,status='running',output=None,created_at=0)
        s['reviews']['review-3']=dict(job,status='cancelled_before_call',output=None)
        s['budget']['reviews_used']=3;self.save(s)
        r=report.generate(self.data,self.session)
        self.assertEqual(r['usage']['usage_missing_review_ids'],['review-1','review-2'])
        self.assertFalse(r['usage']['complete_for_finished_reviews'])
        self.assertEqual(r['reviews'][1]['health'],'overdue_or_worker_lost')
        self.assertEqual(r['budget']['reviews_used'],3)
        self.assertIsNone(r['reviews'][0]['scope'])

    def test_failed_validation_usage_is_preserved(self):
        self.enable();self.make_job();s=self.state()
        s['reviews']['review-1'].update(status='failed',error_type='ReviewFailure');self.save(s)
        r=report.generate(self.data,self.session)
        self.assertEqual(r['usage']['reported_input_plus_output'],120);self.assertIsNone(r['reviews'][0]['scope'])

    def test_empty_missing_expired_state_is_not_clean_or_recreated(self):
        r=report.generate(self.data,'other');self.assertEqual(r['status'],'task_unavailable')
        self.assertFalse(tasks.paths(self.data,'other')[1].exists())
        self.assertEqual(report.generate(self.data,self.session)['summary']['reviews_recorded'],0)
        s=self.state();s['expires_at']=0;tasks.atomic_write(tasks.paths(self.data,self.session)[1],s)
        self.assertEqual(report.generate(self.data,self.session)['status'],'task_unavailable')

    def test_gap_and_partial_coverage_are_visible(self):
        self.enable();self.make_job();tasks.paths(self.data,self.session)[1].with_suffix('.gap').touch()
        r=report.generate(self.data,self.session)
        self.assertTrue(r['collection']['capture_gap']);self.assertEqual(r['reviews'][0]['freshness'],'stale')
        self.assertEqual(r['reviews'][0]['coverage'],'partial')

    def test_reminder_receipt_late_and_followup_are_distinct(self):
        self.enable();reminders.configure(self.data,self.session,True)
        reminders.on_hook(self.data,{'session_id':self.session,'turn_id':'t1','hook_event_name':'UserPromptSubmit'})
        s=self.state();value={'user_context_digest':tasks.user_context_digest(s),'action_version':actions.version(s),
            'source_ids':[s['sources'][0]['id']],'requirement':'Title only','evidence':'Fixture','suggestion':'Check the task.'}
        key=reminders.queue(self.data,self.session,value)
        reminders.on_hook(self.data,{'session_id':self.session,'turn_id':'t1','tool_use_id':'tool','hook_event_name':'PreToolUse'})
        s=self.state();s['reminder_transport']['entries'][key]['followup']={'review_id':'follow','file_basis':actions.file_basis(s),
            'user_context_digest':tasks.user_context_digest(s),'judgment':{'observation':'no_longer_observed','reason':'fixture'},'observed_at':time.time()};self.save(s)
        r=report.generate(self.data,self.session);n=r['reminders'][0]
        self.assertEqual(n['status'],'emitted_unconfirmed');self.assertEqual(n['adoption'],'not_assessed')
        self.assertEqual(n['followup']['causal_adoption'],'not_established')
        self.file.write_text('later');self.assertEqual(report.generate(self.data,self.session)['reminders'][0]['followup']['freshness'],'stale')
        reminders.on_hook(self.data,{'session_id':self.session,'turn_id':'t1','hook_event_name':'Stop'})
        s=self.state();value.update(action_version=actions.version(s),evidence='late')
        late=reminders.queue(self.data,self.session,value)
        self.assertEqual(next(n for n in report.generate(self.data,self.session)['reminders'] if n['id']==late)['status'],'report_only_late')

    def test_agent_findings_are_not_verified_facts(self):
        s=self.state();s['findings']['f']={'location':'other.py','summary':'Possible issue','observations':['one']};self.save(s)
        r=report.generate(self.data,self.session)
        self.assertEqual(r['findings'][0]['verification'],'agent_reported_not_independently_verified')
        self.assertEqual(r['task']['completion'],'not_inferred_from_Stop')

    def test_markdown_escapes_untrusted_text_and_diff_fences(self):
        self.file.write_text('```\n<script>alert(1)</script>\n');self.enable();self.file.write_text('````\n')
        self.make_job();s=self.state();s['reviews']['review-1']['output']['result']['results'][0]['reason']='[click](https://example.invalid) <img src=x>'
        self.save(s);rendered=report.markdown(report.generate(self.data,self.session))
        self.assertNotIn('<img',rendered);self.assertNotIn('[click](https',rendered)
        self.assertIn('`````diff',rendered)

    def test_cli_export_does_not_overwrite_and_requires_no_model_or_git(self):
        self.enable();output=self.root/'report.json'
        command=[sys.executable,str(ROOT/'plugins/navi/scripts/report.py'),'--data-directory',str(self.data),
            '--session',self.session,'--format','json','--output',str(output)]
        result=subprocess.run(command,env=dict(os.environ,PATH=''),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        data=output.read_bytes();self.assertEqual(output.stat().st_mode & 0o777,0o600)
        second=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(second.returncode,1);self.assertEqual(output.read_bytes(),data)

    def test_git_reference_oversized_and_untracked_are_bounded(self):
        self.file.write_text('x'*4097);self.repository();self.file.write_text('small')
        self.enable(['part.txt','new.txt']);r=report.generate(self.data,self.session)
        files={f['path']:f for f in r['files']}
        self.assertEqual(files['part.txt']['reference_status'],'oversized')
        self.assertEqual(files['new.txt']['reference_status'],'absent')
        self.assertNotIn('x'*4097,json.dumps(self.state()))

    def test_report_metadata_does_not_expand_review_packets(self):
        self.enable();s=self.state();before=actions.export(s)
        s.pop('change_baselines')
        self.assertEqual(before,actions.export(s))

    def pending_review(self):
        self.enable();completed=copy.deepcopy(self.make_job())
        s=self.state();s['reviews']['review-1'].update(status='running',output=None);self.save(s)
        return completed

    def wait_with_clock(self,callback=None):
        clock=[0.0];sleeps=[]
        def sleep(seconds):
            clock[0]+=seconds;sleeps.append(seconds)
            if callback:callback(len(sleeps))
        with patch.object(report.time,'monotonic',side_effect=lambda:clock[0]),patch.object(report.time,'sleep',side_effect=sleep):
            result=report.generate(self.data,self.session,final=True)
        return result,sleeps

    def test_final_wait_progress_and_empty_queue_never_sleep(self):
        self.enable()
        with patch.object(report.time,'sleep',side_effect=AssertionError('unexpected wait')):
            r=report.generate(self.data,self.session,final=True)
            self.assertEqual(r['final_wait']['status'],'not_needed')
            self.pending_review()
            r=report.generate(self.data,self.session)
            self.assertNotIn('final_wait',r)
            self.assertIn('未结束 1',r['user_summary'])

    def test_final_wait_completion_releases_lock_and_includes_result(self):
        import concurrent.futures
        completed=self.pending_review();before=self.state()['budget']
        def finish(_):
            def publish():
                with tasks.locked(self.data,self.session) as (path,state):
                    state['reviews']['review-1']=completed;tasks.save(path,state)
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(publish).result(timeout=2)
        r,sleeps=self.wait_with_clock(finish)
        self.assertEqual(len(sleeps),1);self.assertEqual(r['final_wait']['status'],'finished')
        self.assertEqual(r['reviews'][0]['freshness'],'current')
        self.assertEqual(r['summary']['current_scope_drift_judgments'],1)
        self.assertEqual(r['budget'],before);self.assertEqual(r['usage']['reported_input_plus_output'],120)
        self.assertIn('收尾等待',report.markdown(r));self.assertEqual(r['user_summary'],report.brief(r))

    def test_final_wait_timeout_is_bounded_and_does_not_modify_or_cancel(self):
        self.pending_review();path=tasks.paths(self.data,self.session)[1];before=path.read_bytes()
        with patch.object(review,'reserve',side_effect=AssertionError('no new call')),patch.object(review,'run_model',side_effect=AssertionError('no model')):
            r,sleeps=self.wait_with_clock()
        self.assertEqual(sum(sleeps),15);self.assertEqual(r['final_wait']['status'],'timed_out')
        self.assertEqual(r['final_wait']['pending_review_ids'],['review-1'])
        self.assertIn('本报告未包含其未返回的结论',r['user_summary'])
        self.assertIn('未取消评估',report.markdown(r));self.assertEqual(path.read_bytes(),before)

    def test_final_wait_failure_is_finished_without_a_success_verdict(self):
        self.pending_review()
        def fail(_):
            s=self.state();s['reviews']['review-1'].update(status='failed',error_type='ModelError');self.save(s)
        r,sleeps=self.wait_with_clock(fail)
        self.assertEqual(len(sleeps),1);self.assertEqual(r['final_wait']['outcomes'],{'review-1':'failed'})
        self.assertEqual(r['summary']['current_scope_drift_judgments'],0)
        self.assertIn('失败 1',r['user_summary']);self.assertIn('不代表最终审查通过',r['user_summary'])

    def test_final_wait_tracks_existing_dispatch_but_not_new_reviews(self):
        self.enable();completed=copy.deepcopy(self.make_job());s=self.state();s['reviews']={}
        s['supervisor']={'worker':{'token':'old','created_at':time.time()}};self.save(s)
        def advance(n):
            s=self.state()
            if n==1:
                s['reviews']['old-job']=dict(completed,status='queued',output=None,supervision_token='old')
            else:
                s['reviews']['old-job']=dict(completed,supervision_token='old')
                s['reviews']['new-job']=dict(completed,status='running',output=None,supervision_token='new')
                s['supervisor']['worker']={'token':'new','created_at':time.time()}
            self.save(s)
        r,sleeps=self.wait_with_clock(advance)
        self.assertEqual(len(sleeps),2);self.assertEqual(r['final_wait']['review_ids'],['old-job'])
        self.assertEqual(r['final_wait']['status'],'finished')
        self.assertEqual(r['summary']['review_status_counts']['running'],1)

    def test_final_wait_refreshes_after_wait_and_does_not_clear_stale_findings(self):
        completed=self.pending_review()
        def finish(_):
            self.file.write_text('a newer edit\n');s=self.state();s['reviews']['review-1']=completed;self.save(s)
        r,_=self.wait_with_clock(finish)
        self.assertEqual(r['final_wait']['status'],'finished');self.assertEqual(r['reviews'][0]['freshness'],'stale')
        self.assertEqual(r['summary']['current_scope_drift_judgments'],0)
        self.assertIn('a newer edit',r['files'][0]['observed_diff'])

    def test_final_wait_disappearing_state_or_job_is_not_success(self):
        self.pending_review()
        def remove_job(_):
            s=self.state();s['reviews']={};self.save(s)
        r,_=self.wait_with_clock(remove_job)
        self.assertEqual(r['final_wait']['status'],'records_unavailable')
        self.pending_review()
        r,_=self.wait_with_clock(lambda _:tasks.paths(self.data,self.session)[1].unlink())
        self.assertEqual(r['status'],'task_unavailable');self.assertEqual(r['final_wait']['status'],'task_changed')

    def test_final_wait_cli_immediate_snapshot_and_all_formats(self):
        self.enable();self.make_job();path=tasks.paths(self.data,self.session)[1];before=path.read_bytes()
        command=[sys.executable,str(Path(report.__file__)),'--data-directory',str(self.data),'--session',self.session,'--final']
        for fmt in ('json','brief','markdown'):
            r=subprocess.run(command+['--format',fmt],capture_output=True,text=True,timeout=5)
            self.assertEqual(r.returncode,0,r.stderr)
            if fmt=='json':
                value=json.loads(r.stdout);self.assertEqual(value['final_wait']['status'],'not_needed')
                self.assertEqual(value['user_summary'],report.brief(value))
            else:self.assertIn('没有待等待的已有评估',r.stdout)
        self.assertEqual(path.read_bytes(),before)


if __name__=='__main__':unittest.main()
