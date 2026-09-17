"""First-message onboarding against canonical transcripts; no live model calls."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'plugins/navi/scripts'
sys.path.insert(0,str(SCRIPTS))
import task_state as tasks
import history
import history_context as context
import context_sync
import action_evidence as actions
import supervisor
import review
import reminders
import report


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.data=self.root/'data';self.session='first-task'
        self.file=self.root/'host.jsonl';self.work=self.root/'work';self.work.mkdir()
        self.file.write_text(json.dumps({'type':'session_meta','payload':{'id':self.session}})+'\n')
        tasks.workspace_policy(self.data,str(self.work),True)

    def append(self,text,turn='t1',role='user'):
        row={'type':'event_msg','payload':{'type':'item_completed','thread_id':self.session,
            'turn_id':turn,'item':{'id':str(self.file.stat().st_size),'type':'UserMessage' if role=='user' else 'AgentMessage',
            'content':[{'type':'text' if role=='user' else 'Text','text':text}]}}}
        with self.file.open('a') as f:f.write(json.dumps(row)+'\n')

    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def payload(self,event='PostToolUse',**kw):
        return dict(session_id=self.session,turn_id='t1',hook_event_name=event,cwd=str(self.work),
                    transcript_path=str(self.file),**kw)
    def start(self):return history.bootstrap(self.data,self.session,str(self.work))
    def dispatch(self,payload=None):
        history.on_hook(self.data,payload or self.payload(),launcher=lambda *args:None)
    def finish(self):history.worker(self.data,self.session,self.state()['history']['token'])
    def ready(self):
        self.append('Use Navi to supervise this task. Update the UI; backend wiring is deferred.')
        self.start();self.dispatch();self.finish()

    def test_first_message_becomes_raw_authority_without_resubmission_or_paid_summary(self):
        self.ready();s=self.state()
        self.assertTrue(context.ready(s));self.assertEqual(s['sources'],[])
        self.assertEqual(s['contract'],{})
        self.assertEqual(s['history_integration']['status'],'ready_raw_sources')
        sources=tasks.supervision_sources(s)
        self.assertEqual(len(sources),1);self.assertEqual(sources[0]['origin'],'transcript_import')
        self.assertIn('backend wiring is deferred',sources[0]['text'])
        self.assertEqual(s['budget']['reviews_used'],0);self.assertNotIn('history_budget',s)
        before=tasks.paths(self.data,self.session)[1].read_bytes()
        self.assertEqual(self.start()['status'],'existing_task_preserved')
        self.assertEqual(tasks.paths(self.data,self.session)[1].read_bytes(),before)

    def test_short_activation_keeps_previous_task_and_agent_proposal_roles(self):
        self.append('Only review the code and docs; do not modify files.','previous')
        self.append('I could also refactor the backend.','previous','assistant')
        self.append('用 Navi 监督这个任务。')
        self.start();self.dispatch();self.finish();s=self.state()
        self.assertEqual(len(tasks.supervision_sources(s)),2)
        self.assertEqual([m['role'] for m in s['history_integration']['messages']],['user','assistant','user'])
        self.assertNotIn('refactor',json.dumps(tasks.supervision_sources(s)))

    def test_pending_hook_is_lightweight_and_cannot_review_yet(self):
        self.append('Use Navi for this UI task.');self.start()
        self.assertFalse(context.ready(self.state()))
        with patch.object(history,'read_snapshot',side_effect=AssertionError('no read inside hook')):
            self.dispatch()
        self.assertEqual(self.state()['history']['status'],'reading')
        self.assertEqual(self.state()['budget']['reviews_used'],0)
        self.finish();self.assertTrue(context.ready(self.state()))

    def test_wrong_session_and_missing_current_turn_never_supply_authority(self):
        for wrong_session in (True,False):
            with self.subTest(wrong_session=wrong_session):
                self.file.write_text(json.dumps({'type':'session_meta','payload':{'id':'other' if wrong_session else self.session}})+'\n')
                self.append('Old scope','old')
                if self.state():
                    tasks.paths(self.data,self.session)[1].unlink()
                self.start();self.dispatch();self.finish()
                self.assertFalse(context.ready(self.state()))
                self.assertEqual(tasks.supervision_sources(self.state()),[])

    def test_no_user_or_partial_transcript_is_not_ready(self):
        self.append('I say the user approved this.',role='assistant')
        self.start();self.dispatch();self.finish()
        self.assertFalse(context.ready(self.state()))
        history.request(self.data,self.session,retry=True)
        self.append('Use Navi for this task.')
        with self.file.open('a') as f:f.write('{')
        self.dispatch();self.finish()
        self.assertFalse(context.ready(self.state()))
        self.assertIn('incomplete_boundary_line',self.state()['history']['gaps'])

    def test_other_workspace_and_subagent_hooks_do_not_bind(self):
        self.append('Use Navi for this task.');self.start()
        self.dispatch(dict(self.payload(),cwd=str(self.root)))
        self.dispatch(self.payload(agent_id='child'))
        self.assertEqual(self.state()['history']['status'],'pending')
        self.dispatch();self.finish();self.assertTrue(context.ready(self.state()))

    def test_pause_during_read_and_disabled_workspace(self):
        tasks.workspace_policy(self.data,str(self.work),False)
        with self.assertRaises(ValueError):self.start()
        self.assertIsNone(self.state())
        tasks.workspace_policy(self.data,str(self.work),True)
        self.append('Use Navi for this task.');self.start();self.dispatch()
        (self.data/'disabled').touch();self.finish()
        self.assertFalse(context.ready(self.state()));self.assertEqual(self.state()['sources'],[])

    def test_concurrent_user_update_and_later_incremental_context(self):
        self.append('Use Navi for UI only.');self.start();self.dispatch()
        tasks.on_hook(self.data,self.payload('UserPromptSubmit',prompt='Backend is now included.'))
        # Conflicting content for the same turn must not silently join.
        self.finish();self.assertFalse(context.ready(self.state()))

    def test_new_turn_during_read_is_preserved_and_incremental_sync_works(self):
        self.append('Use Navi for UI only.');self.start();self.dispatch()
        tasks.on_hook(self.data,dict(self.payload('UserPromptSubmit',prompt='Add accessibility checks.'),turn_id='t2'))
        self.append('Add accessibility checks.','t2');self.finish()
        self.assertTrue(context.ready(self.state()))
        self.assertEqual(len(tasks.supervision_sources(self.state())),2)
        self.assertTrue(context_sync.synchronize(self.data,self.session,str(self.file)))
        self.append('Checking keyboard navigation.','t2','assistant')
        self.assertTrue(context_sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(len(tasks.supervision_sources(self.state())),2)
        self.assertEqual(self.state()['budget']['reviews_used'],0)

    def test_existing_live_task_keeps_budget_baseline_and_is_not_reset(self):
        self.append('Use Navi for this task.')
        tasks.on_hook(self.data,self.payload('UserPromptSubmit',prompt='Use Navi for this task.'))
        with tasks.locked(self.data,self.session) as (p,s):
            s['budget'].update(reviews_used=7,review_limit=9,limit_origin='explicit');tasks.save(p,s)
        before=self.state();self.start();self.dispatch();self.finish();after=self.state()
        self.assertEqual(before['budget'],after['budget']);self.assertEqual(before['contract'],after['contract'])
        self.assertEqual(len(tasks.supervision_sources(after)),1);self.assertTrue(context.ready(after))

    def test_real_cli_initializes_without_task_text_argument(self):
        result=subprocess.run([sys.executable,str(SCRIPTS/'history.py'),'bootstrap','--data-directory',str(self.data),
            '--session',self.session,'--workspace',str(self.work)],capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'],'pending_native_boundary')
        self.assertEqual(tasks.supervision_sources(self.state()),[])

    def test_first_turn_flows_into_independent_review_and_advisory(self):
        self.ready();f=self.work/'ui.txt';f.write_text('UI only\n')
        actions.configure(self.data,self.session,str(self.work),['ui.txt'])
        supervisor.configure(self.data,self.session,True)
        reminders.on_hook(self.data,self.payload('PreToolUse',tool_use_id='begin'))
        f.write_text('UI and backend wiring\n')
        actions.on_hook(self.data,self.payload(tool_name='patch',tool_use_id='patch',tool_input='edit',tool_response='done'))
        launched=[];supervisor.on_hook(self.data,self.payload(),launcher=lambda d,s,t:launched.append(t))
        self.assertEqual(len(launched),1)
        def runner(packets,**kwargs):
            p=packets[0];ids=[p['user_sources'][0]['id'],next(a['id'] for a in p['actions'] if a['kind']=='workspace_observation')]
            self.assertIn('backend wiring is deferred',p['user_sources'][0]['text'])
            value={'results':[{'case_id':p['case_id'],'verdict':'drift','scope_labels':['phase_expansion'],
                'evidence_ids':ids,'reason':'Backend wiring was deferred.',
                'implementation_risk':{'verdict':'uncertain','labels':[],'evidence_ids':ids,'reason':'Partial evidence.'}}]}
            review.validate_results(value,packets)
            return {'result':value,'usage':{'input_tokens':20,'output_tokens':5,'cached_input_tokens':0}}
        supervisor.worker(self.data,self.session,launched[0],runner=runner,delay=0)
        output=reminders.on_hook(self.data,self.payload('PreToolUse',tool_use_id='next'))
        self.assertIn('additionalContext',output['hookSpecificOutput'])
        summary=report.generate(self.data,self.session)
        self.assertEqual(summary['budget']['reviews_used'],1)
        self.assertEqual(summary['usage']['reported_input_plus_output'],25)

    def test_stop_before_reader_finishes_cannot_reopen_reminder_delivery(self):
        self.append('Use Navi for UI only.');self.start();self.dispatch()
        f=self.work/'ui.txt';f.write_text('before')
        actions.configure(self.data,self.session,str(self.work),['ui.txt'])
        supervisor.configure(self.data,self.session,True)
        reminders.on_hook(self.data,self.payload('Stop'))
        self.finish()
        reminders.on_hook(self.data,self.payload('PreToolUse',tool_use_id='late'))
        self.assertIsNone(self.state()['reminder_transport']['active_turn'])
        self.assertTrue(self.state()['onboarding']['reminder_turn_bound'])

    def test_empty_contract_reports_and_later_explicit_update_work(self):
        self.start()
        self.assertEqual(report.generate(self.data,self.session)['task']['contract'],{})
        tasks.on_hook(self.data,self.payload('UserPromptSubmit',prompt='Navi: update\nUI only'))
        self.assertEqual(self.state()['contract']['revision'],1)
        self.assertEqual(self.state()['contract']['value']['goal'],'UI only')

    def test_imported_requirement_cannot_be_used_as_live_proposal_consent(self):
        self.ready();source=tasks.supervision_sources(self.state())[0]
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data,self.session,'propose',{'base_revision':0,
                'contract':{'goal':'Rollback'},'source_ids':[source['id']]})
        context.disable(self.data,self.session)
        self.assertFalse(context.ready(self.state()))

    def test_repaired_local_read_retries_without_user_resubmission(self):
        self.append('Use Navi for UI only.');self.start()
        self.dispatch(dict(self.payload(),transcript_path=None))
        self.assertEqual(self.state()['history']['status'],'unavailable')
        history.request(self.data,self.session,retry=True)
        self.dispatch();self.finish();self.assertTrue(context.ready(self.state()))

    def test_explicit_reread_preserves_original_bootstrap_anchor(self):
        self.ready();anchor=self.state()['history']['bootstrap_anchor']
        history.request(self.data,self.session,retry=True);self.dispatch();self.finish()
        self.assertEqual(self.state()['history']['bootstrap_anchor'],anchor)
        ordered,_,_=context.assemble(self.state())
        self.assertEqual(len(ordered),1)
        # A reread invalidates integration until explicitly reactivated.
        self.assertFalse(context.ready(self.state()))
