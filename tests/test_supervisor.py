import concurrent.futures
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/navi/scripts'))
import task_state as tasks
import action_evidence as actions
import review
import reminders
import supervisor
import record_event


class SupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.data=self.root/'data';self.work=self.root/'work';self.work.mkdir()
        self.file=self.work/'panel.txt';self.file.write_text('title=before\nbackend=none\n')
        self.session='supervisor-test';self.launched=[];self.calls=[]
        tasks.on_hook(self.data,self.payload('UserPromptSubmit',prompt='Navi: start\nOnly change title. Do not wire backend.'))
        actions.configure(self.data,self.session,str(self.work),['panel.txt'])
        supervisor.configure(self.data,self.session,True)
        reminders.on_hook(self.data,self.payload('UserPromptSubmit'))

    def tearDown(self):self.temp.cleanup()
    def payload(self,event,**extra):
        return dict(session_id=self.session,turn_id='t1',hook_event_name=event,cwd=str(self.work),tool_use_id='tool1',**extra)
    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def save(self,state):tasks.save(tasks.paths(self.data,self.session)[1],state)
    def launch(self,data,session,token):self.launched.append(token)
    def change(self,value='title=after\nbackend=wired\n'):
        self.file.write_text(value)
        actions.on_hook(self.data,self.payload('PostToolUse',tool_name='patch',tool_input='observed',tool_response='done'))
    def dispatch(self,event='PostToolUse'):
        supervisor.on_hook(self.data,self.payload(event),self.launch)
        return self.launched[-1] if self.launched else None
    def runner(self,packets,**kwargs):
        self.calls.append((packets,kwargs));p=packets[0]
        ids=[p['user_sources'][-1]['id'],next(a['id'] for a in p['actions'] if a['kind']=='workspace_observation')]
        result={'case_id':p['case_id'],'verdict':'drift','scope_labels':['phase_expansion'],'evidence_ids':ids,
                'reason':'The current code wires backend beyond the user UI-only phase.',
                'implementation_risk':{'verdict':'uncertain','labels':[],'evidence_ids':ids,'reason':'No full caller context.'}}
        if kwargs.get('followup'):
            result.update(verdict='uncertain',scope_labels=[])
            result['followup']={'reminder_id':kwargs['followup']['reminder_id'],'observation':'no_longer_observed',
                                'evidence_ids':ids,'reason':'Backend wiring is absent in the supplied current file.'}
        value={'results':[result]}
        if kwargs.get('followup'):review.validate_followup(value,packets,kwargs['followup'])
        else:review.validate_results(value,packets)
        return {'result':value,'usage':{'input_tokens':10,'output_tokens':4}}
    def run_worker(self,runner=None):
        supervisor.worker(self.data,self.session,self.launched[-1],runner or self.runner,delay=0)
    def entries(self):return self.state()['reminder_transport']['entries']
    def cooldown(self):
        s=self.state();s['supervisor']['last_dispatch_at']=0;self.save(s)

    def test_long_task_three_minute_boundary_ignores_old_hourly_usage(self):
        import time
        supervisor.configure(self.data,self.session,True,profile='long-task')
        now=time.time()
        with patch('time.time',return_value=now):
            self.change();self.dispatch();self.run_worker()
        state=self.state()
        state['budget']['review_times']=[now]*24
        state['supervisor']['dispatch_times']=[now]*24
        self.save(state)
        with patch('time.time',return_value=now+179):
            self.change('title=later\nbackend=wired\n');self.dispatch()
            self.assertEqual(len(self.launched),1)
        with patch('time.time',return_value=now+180):
            self.dispatch();self.run_worker()
        self.assertEqual(len(self.launched),2)
        self.assertEqual(self.state()['budget']['reviews_used'],2)
        self.assertIsNone(supervisor.status(self.data,self.session)['limits']['hourly_dispatch_limit'])

    def test_continuous_day_exceeds_old_review_dispatch_and_reminder_caps(self):
        import time
        import budget
        import report
        supervisor.configure(self.data,self.session,True,profile='long-task')
        now=time.time()
        for i in range(60):
            with patch('time.time',return_value=now+i*901):
                tasks.on_hook(self.data,self.payload('UserPromptSubmit',prompt='Only change title, do not wire backend. Continue checkpoint '+str(i)))
                reminders.on_hook(self.data,self.payload('UserPromptSubmit'))
                self.change('title='+str(i)+'\nbackend=wired\n')
                self.dispatch();self.run_worker()
                self.assertIsNotNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
                # Another boundary for the same evidence never calls again.
                self.dispatch()
        state=self.state()
        self.assertEqual(state['budget']['reviews_used'],60)
        self.assertEqual(state['budget']['reminders_used'],60)
        self.assertEqual(state['supervisor']['dispatches'],60)
        self.assertEqual(len(state['reviews']),budget.KEEP_REVIEWS)
        self.assertEqual(len(state['reminder_transport']['entries']),budget.KEEP_REMINDERS)
        result=report.generate(self.data,self.session)
        self.assertEqual(result['usage']['reported_input_plus_output'],60*14)
        self.assertEqual(result['retention']['omitted_reviews'],44)
        self.assertEqual(result['retention']['omitted_reminders']['entries'],12)
        self.assertFalse(result['usage']['complete_for_finished_reviews']) # cached usage omitted by fixture
        self.assertIn('旧证据不可再查看',report.markdown(result))
        self.assertEqual(supervisor.status(self.data,self.session)['runtime']['reviews_completed'],60)

    def test_continuous_three_minute_reviews_have_no_hourly_blackout(self):
        import time
        supervisor.configure(self.data,self.session,True,profile='long-task')
        now=time.time()
        for i in range(40):
            with patch('time.time',return_value=now+i*180):
                self.change('title='+str(i)+'\nbackend=wired\n')
                self.dispatch();self.run_worker()
                self.assertEqual(len(self.launched),i+1)
        self.assertEqual(self.state()['budget']['reviews_used'],40)
        self.assertEqual(len(self.state()['reviews']),16)
        supervisor.configure(self.data,self.session,False)
        supervisor.configure(self.data,self.session,True,profile='standard')
        self.assertEqual(self.state()['budget']['reviews_used'],40)

    def test_custom_budget_reports_pause_uses_last_slot_and_can_be_removed(self):
        import report
        supervisor.configure(self.data,self.session,True,max_reviews=1)
        self.change();self.dispatch();self.run_worker()
        self.assertEqual(self.state()['budget']['reviews_used'],1)
        self.assertEqual(supervisor.status(self.data,self.session)['remaining_reviews'],0)
        self.assertIn('监督评估暂停',report.markdown(report.generate(self.data,self.session)))
        self.change('title=next\nbackend=wired\n');self.cooldown();self.dispatch()
        self.assertEqual(len(self.launched),1)
        supervisor.configure(self.data,self.session,True,unlimited_reviews=True)
        self.dispatch();self.run_worker()
        self.assertEqual(self.state()['budget']['reviews_used'],2)
        self.assertIsNone(supervisor.status(self.data,self.session)['remaining_reviews'])

    def test_cli_budget_update_preserves_capture_model_profile_and_counters(self):
        import subprocess
        supervisor.configure(self.data,self.session,True,include_activity=True,profile='long-task',model='explicit-model',effort='medium')
        original=self.state()['supervisor'].copy()
        command=[sys.executable,str(Path(supervisor.__file__)),'set-budget','--data-directory',str(self.data),'--session',self.session]
        result=subprocess.run(command+['--max-reviews','0'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        state=self.state()
        self.assertEqual(state['supervisor'],original)
        self.assertEqual(state['budget']['review_limit'],0)
        self.assertFalse(json.loads(result.stdout)['runtime']['eligible'])
        result=subprocess.run(command+['--unlimited-reviews'],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.state()['supervisor'],original)
        self.assertIsNone(self.state()['budget']['review_limit'])
        result=subprocess.run(command+['--max-reviews','-1'],capture_output=True,text=True)
        self.assertNotEqual(result.returncode,0)
        self.assertIsNone(self.state()['budget']['review_limit'])

    def test_explicit_budget_survives_profile_and_model_changes(self):
        supervisor.configure(self.data,self.session,True,max_reviews=8)
        self.change();self.dispatch();self.run_worker()
        supervisor.configure(self.data,self.session,True,profile='long-task',model='other-model')
        self.assertEqual(self.state()['budget']['review_limit'],8)
        self.assertEqual(self.state()['budget']['reviews_used'],1)
        self.assertEqual(len(self.state()['budget']['review_times']),1)

    def test_dispatch_failures_keep_minimum_spacing_without_hourly_blackout(self):
        import time
        now=time.time()
        def fail(*args):raise OSError('fixture')
        for i in range(8):
            with patch('time.time',return_value=now+i*30):
                self.change('title='+str(i)+'\nbackend=wired\n')
                supervisor.on_hook(self.data,self.payload('PostToolUse'),fail)
                self.assertEqual(self.state()['supervisor']['dispatches'],i+1)
        with patch('time.time',return_value=now+239):
            self.change();self.dispatch();self.assertEqual(self.launched,[])
        with patch('time.time',return_value=now+240):
            self.dispatch();self.run_worker()
        self.assertEqual(self.state()['supervisor']['dispatches'],9)
        self.assertEqual(self.state()['budget']['reviews_used'],1)

    def test_manual_review_cannot_overlap_pending_automatic_dispatch(self):
        self.change();self.dispatch()
        evidence={'coverage':'partial','actions':[{'id':'manual','kind':'code','path':'x','evidence':'observed','origin':'test'}]}
        with self.assertRaisesRegex(review.budget.ReviewDeferred,'review_in_progress'):
            review.reserve(self.data,self.session,evidence)
        self.assertEqual(self.state()['budget']['reviews_used'],0)
        self.run_worker()
        self.assertEqual(self.state()['budget']['reviews_used'],1)

    def test_automatic_dispatch_waits_for_manual_review_then_uses_new_evidence(self):
        import report
        evidence={'coverage':'partial','actions':[{'id':'manual','kind':'code','path':'x','evidence':'observed','origin':'test'}]}
        key,_=review.reserve(self.data,self.session,evidence)
        self.change();self.dispatch()
        self.assertEqual(self.launched,[])
        self.assertIn('独立评估进行中',report.markdown(report.generate(self.data,self.session)))
        self.assertNotIn('监督评估暂停',report.markdown(report.generate(self.data,self.session)))
        def fail(*args,**kwargs):raise TimeoutError('fixture')
        review.worker(self.data,self.session,key,fail)
        self.change('title=newest\nbackend=wired\n');self.dispatch();self.run_worker()
        self.assertEqual(self.state()['budget']['reviews_used'],2)
        self.assertIn('newest',json.dumps(self.calls[-1][0]))

    def test_unavailable_history_is_exposed_before_any_dispatch(self):
        state=self.state();state['history_integration']={'enabled':True,'status':'unavailable','token':'history-test'}
        state['history']={'status':'partial','gaps':['byte_limit']};self.save(state)
        self.change();self.dispatch()
        value=supervisor.status(self.data,self.session)
        self.assertFalse(value['runtime']['eligible'])
        self.assertEqual(value['runtime']['history_gaps'],['byte_limit'])
        self.assertEqual(value['runtime']['model_calls_started'],0)
        self.assertIn('history_not_ready',value['supervisor']['last_status'])

    def test_oversized_files_do_not_consume_file_only_review_budget(self):
        self.change('x'*5000);self.dispatch()
        self.assertEqual(self.launched,[])
        self.assertEqual(self.state()['budget']['reviews_used'],0)

    def test_explicit_optin_and_no_change_spend_nothing(self):
        self.dispatch();self.assertEqual(self.launched,[])
        supervisor.configure(self.data,self.session,False)
        self.change();self.dispatch();self.assertEqual(self.launched,[])
        self.assertEqual(self.state()['budget']['reviews_used'],0)

    def test_file_only_packet_dedup_read_tools_dont_invalidate(self):
        self.change();self.dispatch()
        for _ in range(10):self.dispatch()
        self.assertEqual(len(self.launched),1)
        def concurrent_reads(packets,**kwargs):
            actions.on_hook(self.data,self.payload('PreToolUse',tool_input='read unrelated diagnostic'))
            return self.runner(packets,**kwargs)
        self.run_worker(concurrent_reads)
        self.assertEqual(self.state()['budget']['reviews_used'],1)
        self.assertTrue(all(a['kind']!='tool_observation' for a in self.calls[0][0][0]['actions']))
        entry=next(iter(self.entries().values()))
        self.assertEqual(entry['origin'],'independent_review')
        self.assertEqual(entry['status'],'pending')
        out=reminders.on_hook(self.data,self.payload('PreToolUse'))
        self.assertIn(entry['id'],out['hookSpecificOutput']['additionalContext'])
        self.dispatch();self.assertEqual(len(self.launched),1)

    def test_concurrent_boundaries_launch_one_worker(self):
        self.change()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda _:self.dispatch(),range(8)))
        self.assertEqual(len(self.launched),1)

    def test_burst_coalesces_latest_files_before_model_and_keeps_user_sources(self):
        self.change();self.dispatch();self.change('title=new\nbackend=wired\n')
        self.run_worker()
        p=self.calls[0][0][0]
        self.assertIn('title=new',json.dumps(p))
        self.assertEqual(p['user_sources'][0]['text'],self.state()['sources'][0]['text'])
        self.assertEqual(len(self.calls),1)

    def test_model_runs_without_task_lock_and_changed_files_suppress_result(self):
        self.change();self.dispatch()
        def correcting(packets,**kwargs):
            self.change('title=after\nbackend=none\n')
            return self.runner(packets,**kwargs)
        self.run_worker(correcting)
        job=next(iter(self.state()['reviews'].values()))
        self.assertTrue(job['stale']);self.assertEqual(self.entries(),{})

    def test_new_user_authorization_while_running_supersedes_old_judgment(self):
        self.change();self.dispatch()
        def update(packets,**kwargs):
            tasks.on_hook(self.data,dict(self.payload('UserPromptSubmit',prompt='Now wire backend.'),turn_id='t2'))
            return self.runner(packets,**kwargs)
        self.run_worker(update);self.assertEqual(self.entries(),{})
        self.assertTrue(next(iter(self.state()['reviews'].values()))['stale'])

    def test_stop_during_model_returns_report_only_without_new_turn(self):
        self.change();self.dispatch()
        def end(packets,**kwargs):
            self.assertIsNone(reminders.on_hook(self.data,self.payload('Stop')))
            return self.runner(packets,**kwargs)
        self.run_worker(end)
        self.assertEqual(next(iter(self.entries().values()))['status'],'report_only_late')
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))

    def test_stop_checkpoint_can_review_without_emitting(self):
        self.change();reminders.on_hook(self.data,self.payload('Stop'));self.dispatch('Stop');self.run_worker()
        self.assertEqual(next(iter(self.entries().values()))['status'],'report_only_late')

    def test_implementation_risk_uncertainty_and_unknown_labels_are_record_only(self):
        for condition in ('risk','uncertain','unknown'):
            with self.subTest(condition=condition):
                self.change('title='+condition+'\nbackend=wired\n');self.cooldown();self.dispatch()
                def gated(packets,**kwargs):
                    value=self.runner(packets,**kwargs);r=value['result']['results'][0]
                    if condition=='risk':
                        r.update(verdict='uncertain',scope_labels=[])
                        r['implementation_risk'].update(verdict='concern',labels=['overengineering'])
                    elif condition=='uncertain':r['verdict']='uncertain'
                    else:r['scope_labels']=['unknown']
                    return value
                self.run_worker(gated)
                self.assertEqual(self.entries(),{})
                # Each subtest is a distinct task budget, not an implementation reset.
                s=self.state();s['budget']['reviews_used']=0;s['supervisor']['last_attempt']=None;self.save(s)

    def test_risk_does_not_veto_independently_supported_scope_advisory(self):
        self.change();self.dispatch()
        def mixed(packets,**kwargs):
            value=self.runner(packets,**kwargs)
            value['result']['results'][0]['implementation_risk'].update(verdict='concern',labels=['narrow_hardcoding'],reason='Separate experimental risk.')
            return value
        self.run_worker(mixed)
        entry=next(iter(self.entries().values()))
        self.assertEqual(entry['status'],'pending')
        self.assertNotIn('Separate experimental risk',entry['payload']['evidence'])

    def test_delivery_gap_uses_existing_review_and_reaches_agent(self):
        self.change();self.dispatch()
        def delivery(packets,**kwargs):
            value=self.runner(packets,**kwargs)
            result=value['result']['results'][0]
            result.update(scope_labels=['scope_reduction','implementation_substitution','unsupported_completion'],
                          reason='Required capability replaced despite an observed completion claim.')
            review.validate_results(value['result'],packets)
            return value
        self.run_worker(delivery)
        emitted=reminders.on_hook(self.data,self.payload('PreToolUse'))
        self.assertIn('Do not claim full completion',str(emitted))
        self.assertIn('not authorization to reduce scope',str(emitted))
        self.assertEqual(self.state()['budget']['reviews_used'],1)
        self.dispatch('Stop')
        self.assertEqual(len(self.calls),1)

    def test_uncertain_delivery_gap_is_reported_without_advisory(self):
        import report
        self.change();self.dispatch()
        def uncertain(packets,**kwargs):
            value=self.runner(packets,**kwargs)
            value['result']['results'][0].update(verdict='uncertain',scope_labels=['implementation_substitution'])
            return value
        self.run_worker(uncertain)
        self.assertEqual(self.entries(),{})
        gaps=report.generate(self.data,self.session)['delivery_gaps']
        self.assertEqual(gaps[0]['verdict'],'uncertain')

    def test_delivery_findings_do_not_deduplicate_as_expansion(self):
        old={'scope_labels':['phase_expansion']}
        new={'scope_labels':['scope_reduction','implementation_substitution']}
        self.assertNotEqual(supervisor.finding_key('context',['panel.txt'],old),
                            supervisor.finding_key('context',['panel.txt'],new))
        self.assertEqual(supervisor.finding_key('context',['panel.txt'],new),
                         supervisor.finding_key('context',['panel.txt'],{'scope_labels':list(reversed(new['scope_labels']))}))

    def test_followup_shares_budget_records_observation_not_adoption_and_never_reprompts(self):
        self.change();self.dispatch();self.run_worker()
        reminders.on_hook(self.data,self.payload('PreToolUse'))
        key=next(iter(self.entries()))
        self.change('title=after\nbackend=none\n');self.cooldown();self.dispatch();self.run_worker()
        self.assertEqual(self.state()['budget']['reviews_used'],2)
        entry=self.entries()[key]
        self.assertEqual(entry['followup']['judgment']['observation'],'no_longer_observed')
        self.assertEqual(entry['adoption'],'not_assessed')
        self.assertEqual(entry['status'],'emitted_unconfirmed')
        self.assertEqual(len(self.entries()),1)
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        self.cooldown();self.dispatch('PostToolUse');self.dispatch('Stop')
        self.assertEqual(len(self.launched),2)
        self.assertEqual(self.state()['budget']['reviews_used'],2)
        self.change('title=later\nbackend=wired\n')
        self.assertTrue(supervisor.status(self.data,self.session)['reminders'][key]['followup']['currently_stale'])

    def test_missing_affected_file_cannot_claim_corrected(self):
        self.change();self.dispatch();self.run_worker();reminders.on_hook(self.data,self.payload('PreToolUse'))
        self.file.unlink();self.cooldown();self.dispatch();self.run_worker()
        entry=next(iter(self.entries().values()))
        self.assertEqual(entry['followup']['judgment']['observation'],'uncertain')

    def test_explicit_budget_has_no_reserved_slot_and_manual_reviews_share_same_limit(self):
        supervisor.configure(self.data,self.session,True,max_reviews=3)
        for n in range(2):
            self.change('title='+str(n)+'\nbackend=wired\n');self.cooldown();self.dispatch();self.run_worker()
        self.assertEqual(self.state()['budget']['reviews_used'],2)
        evidence={'coverage':'partial','actions':[{'id':'manual','kind':'code','path':'x','evidence':'x','origin':'test'}]}
        review.reserve(self.data,self.session,evidence)
        self.assertEqual(self.state()['budget']['reviews_used'],3)
        self.cooldown();self.dispatch();self.assertEqual(len(self.launched),2)

    def test_same_finding_merges_even_when_result_labels_change(self):
        self.change();self.dispatch();self.run_worker()
        self.change('title=second\nbackend=wired\n');self.cooldown();self.dispatch();self.run_worker()
        self.assertEqual(len(self.entries()),1)
        self.assertTrue(any(j['advisory_outcome']=='merged_pending_with_latest_evidence' for j in self.state()['reviews'].values()))
        self.assertEqual(next(iter(self.entries().values()))['file_basis'],actions.file_basis(self.state()))
        self.assertIsNotNone(reminders.on_hook(self.data,self.payload('PreToolUse')))

    def test_pause_gap_disable_delete_before_worker_avoid_model_calls(self):
        for mode in ('pause','gap','disable','delete'):
            self.change();self.cooldown();self.dispatch()
            path=tasks.paths(self.data,self.session)[1]
            if mode=='pause':(self.data/'disabled').touch()
            elif mode=='gap':path.with_suffix('.gap').touch()
            elif mode=='disable':supervisor.configure(self.data,self.session,False)
            else:path.unlink()
            self.run_worker(lambda *a,**k:self.fail('no model allowed'))
            (self.data/'disabled').unlink(missing_ok=True);path.with_suffix('.gap').unlink(missing_ok=True)
            if mode=='delete':self.assertIsNone(self.state());break
            if mode=='disable':supervisor.configure(self.data,self.session,True)
            s=self.state();s['supervisor']['last_attempt']=None;self.save(s)

    def test_capture_configuration_change_invalidates_file_basis(self):
        self.change();self.dispatch();self.run_worker()
        actions.configure(self.data,self.session,str(self.work),['panel.txt'],enabled=False)
        actions.configure(self.data,self.session,str(self.work),['panel.txt'],enabled=True)
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        self.assertEqual(next(iter(self.entries().values()))['reason'],'watched_files_changed')

    def test_spawn_failure_and_model_failure_never_loop_or_claim_success(self):
        self.change()
        def fail_launch(*a):raise OSError('fixture')
        supervisor.on_hook(self.data,self.payload('PostToolUse'),fail_launch)
        self.dispatch();self.assertEqual(self.launched,[])
        self.assertEqual(self.state()['supervisor']['last_status'],'spawn_failed')
        self.change('title=two\nbackend=wired\n');self.cooldown();self.dispatch()
        def fail_model(*a,**k):raise TimeoutError('fixture')
        self.run_worker(fail_model);self.assertEqual(self.entries(),{})
        self.assertEqual(self.state()['budget']['reviews_used'],1)
        self.dispatch();self.assertEqual(len(self.launched),1)

    def test_cooldown_and_lost_worker_health(self):
        self.change();self.dispatch();self.run_worker()
        self.change('title=next\nbackend=wired\n');self.dispatch()
        self.assertEqual(len(self.launched),1)
        self.cooldown();self.dispatch()
        s=self.state();s['supervisor']['worker']['created_at']=0;self.save(s)
        self.assertIn('worker_overdue',supervisor.status(self.data,self.session)['supervisor']['health'])
        self.dispatch();self.assertEqual(len(self.launched),2)

    def test_explicit_recovery_can_redispatch_without_resetting_budget(self):
        self.change();self.dispatch()
        supervisor.configure(self.data,self.session,False)
        supervisor.configure(self.data,self.session,True)
        self.cooldown();self.dispatch()
        self.assertEqual(len(self.launched),2)
        self.assertEqual(self.state()['supervisor']['dispatches'],2)
        self.run_worker()
        used=self.state()['budget']['reviews_used']
        reminders.configure(self.data,self.session,False)
        supervisor.configure(self.data,self.session,True)
        self.assertTrue(self.state()['reminder_transport']['enabled'])
        self.assertEqual(self.state()['budget']['reviews_used'],used)

    def test_transport_disable_before_reserved_review_avoids_call(self):
        self.change();token=self.dispatch()
        key,_=review.reserve(self.data,self.session,None,supervision_token=token)
        reminders.configure(self.data,self.session,False)
        review.worker(self.data,self.session,key,lambda *a,**k:self.fail('no model allowed'))
        self.assertEqual(self.state()['reviews'][key]['status'],'superseded_before_call')

    def test_hook_dispatches_without_model_wait_or_tool_decision(self):
        self.change();out=io.StringIO();err=io.StringIO()
        with patch.dict(os.environ,PLUGIN_DATA=str(self.data)), patch.object(supervisor,'on_hook') as schedule:
            code=record_event.run(io.BytesIO(json.dumps(self.payload('PostToolUse')).encode()),out,err)
        self.assertEqual(code,0);schedule.assert_called_once();self.assertEqual(out.getvalue(),'')

    def test_followup_schema_citations_and_partial_scope_remain_separate(self):
        self.change();self.dispatch();self.run_worker();reminders.on_hook(self.data,self.payload('PreToolUse'))
        self.change('title=after\nbackend=none\n');self.cooldown();self.dispatch();self.run_worker()
        packets,kwargs=self.calls[-1];value=self.runner(packets,**kwargs)['result']
        value['results'][0]['verdict']='within_scope'
        review.validate_followup(value,packets,kwargs['followup'])
        self.assertEqual(value['results'][0]['verdict'],'uncertain')
        value['results'][0]['followup']['evidence_ids']=['fabricated']
        with self.assertRaises(ValueError):review.validate_followup(value,packets,kwargs['followup'])

    def test_malformed_followup_is_a_validation_failure(self):
        for value in (None,[], 'invalid'):
            with self.assertRaises(ValueError):review.validate_followup(value,[],{})


if __name__=='__main__':unittest.main()
