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


if __name__=='__main__':unittest.main()
