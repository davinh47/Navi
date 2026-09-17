import concurrent.futures
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'plugins/navi/scripts'))
import task_state as tasks
import action_evidence as actions
import reminders
import record_event


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.data=self.root/'data'
        self.work=self.root/'work';self.work.mkdir()
        self.file=self.work/'ui.txt';self.file.write_text('before\n')
        self.session='reminder-test'
        tasks.on_hook(self.data,self.payload('UserPromptSubmit',prompt='Navi: start\nOnly update title; do not wire backend.'))
        actions.configure(self.data,self.session,str(self.work),['ui.txt'])
        reminders.configure(self.data,self.session,True)
        reminders.on_hook(self.data,self.payload('UserPromptSubmit'))

    def tearDown(self):self.temp.cleanup()
    def payload(self,event,**extra):
        return dict(session_id=self.session,turn_id='t1',hook_event_name=event,cwd=str(self.work),tool_use_id='tool1',**extra)
    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def value(self):
        s=self.state()
        return {'user_context_digest':tasks.user_context_digest(s),'action_version':actions.version(s),
            'source_ids':[s['sources'][0]['id']],'requirement':'Only update title.',
            'evidence':'Controlled fixture: draft proposes backend wiring.','suggestion':'Keep the change to title only.'}
    def queued(self):return reminders.queue(self.data,self.session,self.value())
    def entry(self,key):return self.state()['reminder_transport']['entries'][key]

    def test_explicit_controlled_optin_and_no_extra_review_calls(self):
        reminders.configure(self.data,self.session,False)
        with self.assertRaises(ValueError):self.queued()
        self.assertEqual(self.state()['budget']['reviews_used'],0)

    def test_capture_revocation_prevents_queue_and_delivery_and_allows_disable(self):
        key=self.queued()
        actions.configure(self.data,self.session,str(self.work),['ui.txt'],enabled=False)
        with self.assertRaises(ValueError):self.queued()
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        self.assertEqual(self.entry(key)['reason'],'action_capture_disabled')
        (self.data/'disabled').touch()
        reminders.configure(self.data,self.session,False)
        self.assertFalse(self.state()['reminder_transport']['enabled'])
        with self.assertRaises(ValueError):reminders.configure(self.data,self.session,True)

    def test_disable_cancels_pending_without_requiring_active_task(self):
        key=self.queued()
        path=tasks.paths(self.data,self.session)[1]
        state=self.state();state['status']='paused';tasks.save(path,state)
        reminders.configure(self.data,self.session,False)
        self.assertEqual(self.entry(key)['status'],'cancelled')

    def test_pre_tool_emits_only_context_once_without_permission_fields(self):
        key=self.queued()
        output=reminders.on_hook(self.data,self.payload('PreToolUse'))
        self.assertEqual(set(output),{'hookSpecificOutput'})
        self.assertEqual(set(output['hookSpecificOutput']),{'hookEventName','additionalContext'})
        self.assertIn(key,output['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.entry(key)['status'],'emitted_unconfirmed')
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        self.assertEqual(self.state()['budget']['reminders_used'],1)

    def test_concurrent_drain_is_at_most_once(self):
        key=self.queued()
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(lambda _:reminders.on_hook(self.data,self.payload('PreToolUse')),range(4)))
        self.assertEqual(sum(r is not None for r in results),1)
        self.assertEqual(self.entry(key)['delivery'],'unconfirmed')

    def test_cooldown_replaces_cumulative_reminder_budget(self):
        self.assertEqual(self.queued(),self.queued())
        self.assertIsNotNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        with patch('time.time',return_value=__import__('time').time()+61):
            value=self.value();value['suggestion']='Another finding'
            key=reminders.queue(self.data,self.session,value)
            self.assertIsNotNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
            value['suggestion']='Next finding'
            later=reminders.queue(self.data,self.session,value)
            self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
            self.assertEqual(self.entry(later)['status'],'pending')
        self.assertEqual(self.state()['budget']['reminders_used'],2)
        self.assertEqual(self.state()['budget']['reviews_used'],0)


    def test_new_user_input_prevents_old_advisory(self):
        key=self.queued()
        payload=dict(self.payload('UserPromptSubmit',prompt='Now wire backend.'),turn_id='t2')
        tasks.on_hook(self.data,payload)
        reminders.on_hook(self.data,payload)
        self.assertIsNone(reminders.on_hook(self.data,dict(self.payload('PreToolUse'),turn_id='t2')))
        self.assertEqual(self.entry(key)['reason'],'user_context_changed')

    def test_file_self_correction_or_new_action_prevents_emission(self):
        key=self.queued();self.file.write_text('corrected\n')
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        self.assertEqual(self.entry(key)['reason'],'actions_changed')
        key=self.queued()
        actions.on_hook(self.data,self.payload('PostToolUse',tool_name='Bash',tool_response='done'))
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        self.assertEqual(self.entry(key)['reason'],'actions_changed')

    def test_cooldown_releases_pending_at_next_hook_without_new_model_call(self):
        import time
        now=time.time()
        with patch('time.time',return_value=now):
            first=self.queued()
            self.assertIsNotNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
            value=self.value();value['evidence']='New evidence'
            second=reminders.queue(self.data,self.session,value)
            self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        with patch('time.time',return_value=now+60):
            output=reminders.on_hook(self.data,self.payload('PreToolUse'))
            self.assertIn(second,output['hookSpecificOutput']['additionalContext'])
        self.assertEqual(self.state()['budget']['reviews_used'],0)

    def test_record_rollover_keeps_emitted_finding_deduplicated(self):
        import budget
        key=self.queued();state=self.state()
        state['reminder_transport']['entries'][key]['finding_key']='same-scope-finding'
        tasks.save(tasks.paths(self.data,self.session)[1],state)
        reminders.on_hook(self.data,self.payload('PreToolUse'))
        for i in range(budget.KEEP_REMINDERS):
            value=self.value();value['evidence']='Different observation '+str(i)
            reminders.queue(self.data,self.session,value)
        state=self.state()
        self.assertNotIn(key,state['reminder_transport']['entries'])
        self.assertTrue(reminders.finding_emitted(state,'same-scope-finding'))
        self.assertEqual(state['budget']['reminders_used'],1)

    def test_expired_gap_disabled_and_wrong_turn_do_not_deliver(self):
        for condition in ('expired','gap','disabled','wrong_turn'):
            with self.subTest(condition=condition):
                key=self.queued()
                path=tasks.paths(self.data,self.session)[1]
                if condition=='expired':
                    s=self.state();s['reminder_transport']['entries'][key]['expires_at']=0;tasks.save(path,s)
                elif condition=='gap':path.with_suffix('.gap').touch()
                elif condition=='disabled':(self.data/'disabled').touch()
                payload=self.payload('PreToolUse')
                if condition=='wrong_turn':payload['turn_id']='other'
                self.assertIsNone(reminders.on_hook(self.data,payload))
                path.with_suffix('.gap').unlink(missing_ok=True);(self.data/'disabled').unlink(missing_ok=True)
                # Fresh fixture ID for the next condition without resetting budget.
                s=self.state();s['reminder_transport']['entries'].clear();tasks.save(path,s)

    def test_stop_and_interrupt_never_force_continuation_or_roll_into_next_turn(self):
        for event in ('Stop','Interrupt','SessionEnd'):
            reminders.on_hook(self.data,self.payload('UserPromptSubmit'))
            value=self.value();value['evidence']=event
            key=reminders.queue(self.data,self.session,value)
            self.assertIsNone(reminders.on_hook(self.data,self.payload(event)))
            self.assertEqual(self.entry(key)['status'],'report_only_late')
            self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))

    def test_queue_after_stop_is_report_only(self):
        reminders.on_hook(self.data,self.payload('Stop'))
        key=self.queued()
        self.assertEqual(self.entry(key)['status'],'report_only_late')

    def test_external_receipt_requires_matching_host_and_model_observations(self):
        key=self.queued();out=reminders.on_hook(self.data,self.payload('PreToolUse'))
        value={'id':key,'turn_id':'t1','tool_use_id':'tool1','host_run_id':'host-run',
            'host_context':out['hookSpecificOutput']['additionalContext'],'agent_reply':'Received '+key}
        with self.assertRaises(ValueError):reminders.receipt(self.data,self.session,dict(value,agent_reply='DONE'))
        with self.assertRaises(ValueError):reminders.receipt(self.data,self.session,dict(value,tool_use_id='other'))
        reminders.receipt(self.data,self.session,value)
        self.assertEqual(self.entry(key)['status'],'delivered')
        self.assertEqual(self.entry(key)['adoption'],'not_assessed')

    def test_real_recorder_checks_ready_packet_before_next_action_capture(self):
        key=self.queued();out=io.StringIO();err=io.StringIO()
        with patch.dict(os.environ,PLUGIN_DATA=str(self.data)):
            code=record_event.run(io.BytesIO(json.dumps(self.payload('PreToolUse',tool_name='Bash',tool_input={'command':'pwd'})).encode()),out,err)
        self.assertEqual(code,0,err.getvalue())
        self.assertIn(key,json.loads(out.getvalue())['hookSpecificOutput']['additionalContext'])
        self.assertTrue(self.state()['action_evidence']['events'])
        self.assertEqual(self.entry(key)['status'],'emitted_unconfirmed')

    def test_bounds_and_deleted_task_do_not_emit_or_resurrect(self):
        value=self.value();value['suggestion']='x'*385
        with self.assertRaises(ValueError):reminders.queue(self.data,self.session,value)
        self.queued();path=tasks.paths(self.data,self.session)[1];path.unlink()
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))
        self.assertFalse(path.exists())

    def test_failure_after_attempt_does_not_claim_delivery_or_resend(self):
        key=self.queued();out=io.StringIO();err=io.StringIO()
        with patch.dict(os.environ,PLUGIN_DATA=str(self.data)), patch.object(actions,'on_hook',side_effect=OSError):
            code=record_event.run(io.BytesIO(json.dumps(self.payload('PreToolUse')).encode()),out,err)
        self.assertEqual(code,1)
        self.assertEqual(out.getvalue(),'')
        self.assertEqual(self.entry(key)['status'],'emitted_unconfirmed')
        self.assertEqual(self.entry(key)['delivery'],'unconfirmed')
        self.assertIsNone(reminders.on_hook(self.data,self.payload('PreToolUse')))


if __name__=='__main__':unittest.main()
