import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/navi/scripts'))
import history_context as hc
import task_state as tasks
import history
import review
import action_evidence as actions
import supervisor
import reminders
import report


class HistoryContextTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.data=self.root/'data';self.session='h-session'
        tasks.workspace_policy(self.data,str(self.root),True)
        self.prompt('Continue.','live')
        self.live=self.state()['sources'][0]
        self.old={'id':'h-old','turn_id':'old','text':'UI only. Backend deferred.','origin':'transcript_import','role':'user','authority':'direct_user_message'}
        self.agent={'id':'h-agent','turn_id':'old','text':'Proposal two: change title only.','origin':'transcript_import','role':'assistant','authority':'agent_statement_not_authorization'}
        with tasks.locked(self.data,self.session) as (path,state):
            state['history']={'status':'ready','token':'reader-one','messages':[self.old,self.agent,
                dict(self.live,role='user',authority='direct_user_message')], 'coverage':'available_text_events_only','gaps':[]}
            tasks.save(path,state)

    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def prompt(self,body,turn):
        tasks.on_hook(self.data,{'session_id':self.session,'hook_event_name':'UserPromptSubmit','turn_id':turn,'prompt':body,'cwd':str(self.root)})
    def reserve(self):
        hc.activate(self.data,self.session,launcher=lambda *a:None)
        return self.state()['history_integration']['token']
    def output(self):
        citations=[{'source_id':'h-old','quote':'UI only.'},{'source_id':self.live['id'],'quote':'Continue.'}]
        return {'result':{'contract':{'goal':'Update UI','in_scope':['UI'],'out_of_scope':['Backend'],'acceptance':[],'plan':[]},
            'uncertainties':[], 'proposal_links':[], 'intent_update':{'base_interpretation_id':None,
            'changes':[{'id':'goal','kind':'goal','text':'Update UI only','status':'active','origin':'user_requirement','citations':citations}],
            'source_effects':[dict(c,role='requirement' if i==0 else 'continuation',item_ids=['goal']) for i,c in enumerate(citations)]}},
            'usage':{'input_tokens':100,'cached_input_tokens':0,'output_tokens':40},'model':'fake','effort':'medium','tool_items':0}
    def complete(self,runner=None):
        token=self.reserve();hc.worker(self.data,self.session,token,runner or (lambda *a:self.output()))
        self.assertEqual(self.state()['history_integration']['status'],'ready')

    def evidence(self):
        return {'actions':[{'id':'a-edit','kind':'workspace_observation','path':'panel.py','evidence':'backend wired','origin':'test'}],'coverage':'partial'}

    def test_large_assistant_context_is_selected_without_truncating_users(self):
        state=self.state()
        state['history']['messages'][1:1]=[dict(self.agent,id='agent-'+str(i),text='Plan '+str(i)+'x'*4000) for i in range(30)]
        ordered,messages,packet=hc.assemble(state)
        self.assertEqual([m['text'] for m in messages if m['role']=='user'],[s['text'] for s in ordered])
        self.assertGreater(packet['assistant_context']['omitted_messages'],0)
        self.assertIn(self.agent['id'],[m['id'] for m in messages])
        self.assertLess(len(json.dumps(packet).encode()),hc.MAX_INPUT_BYTES)

    def test_recovered_snapshot_can_retry_zero_spend_preflight_failure(self):
        with tasks.locked(self.data,self.session) as (path,state):
            state['history']['status']='partial';tasks.save(path,state)
        hc.activate(self.data,self.session,launcher=lambda *a:self.fail('must not spend'))
        with tasks.locked(self.data,self.session) as (path,state):
            state['history'].update(status='ready',token='recovered');tasks.save(path,state)
        hc.activate(self.data,self.session,launcher=lambda *a:None)
        self.assertEqual(self.state()['history_integration']['status'],'queued')
        self.assertEqual(self.state()['history_budget']['calls_reserved'],1)

    def test_exact_overlap_order_and_roles(self):
        ordered,messages,_=hc.assemble(self.state())
        self.assertEqual([s['id'] for s in ordered],['h-old',self.live['id']])
        self.assertEqual([m['role'] for m in messages],['user','assistant','user'])
        self.prompt('Backend now approved.','new')
        ordered,messages,_=hc.assemble(self.state())
        self.assertEqual(ordered[-1]['text'],'Backend now approved.')
        self.assertEqual(len(ordered),3)

    def test_same_text_new_turn_is_not_deduplicated(self):
        self.prompt('Continue.','new')
        self.assertEqual(len(hc.assemble(self.state())[0]),3)

    def test_unproven_overlap_and_conflicting_turn_are_rejected(self):
        for mode in ('absent','different'):
            state=self.state()
            if mode=='absent':state['history']['messages']=state['history']['messages'][:2]
            elif mode=='different':state['history']['messages'][-1]['text']='Different'
            with self.subTest(mode=mode),self.assertRaises(ValueError):hc.assemble(state)

    def test_paused_capture_messages_recovered_as_imports_in_order(self):
        self.prompt('Continue after pause.','resumed')
        state=self.state()
        missed=dict(self.old,id='h-paused',turn_id='paused',text='Backend is now allowed.')
        state['history']['messages'] += [missed,dict(state['sources'][-1],role='user',authority='direct_user_message')]
        original_live=copy.deepcopy(state['sources'])
        ordered,messages,_=hc.assemble(state)
        self.assertEqual([s['turn_id'] for s in ordered],['old','live','paused','resumed'])
        self.assertEqual(ordered[2]['origin'],'transcript_import')
        self.assertEqual(ordered[2]['text'],'Backend is now allowed.')
        self.assertEqual(state['sources'],original_live)
        state['history']['status']='partial'
        with self.assertRaisesRegex(ValueError,'history_incomplete'):hc.assemble(state)

    def test_reversed_live_order_and_missing_intermediate_live_source_rejected(self):
        self.prompt('Second live message.','live-2')
        self.prompt('Third live message.','live-3')
        state=self.state()
        second=dict(state['sources'][1],role='user',authority='direct_user_message')
        third=dict(state['sources'][2],role='user',authority='direct_user_message')
        for suffix in ([third,second],[third]):
            changed=copy.deepcopy(state);changed['history']['messages']+=suffix
            with self.subTest(suffix=suffix),self.assertRaisesRegex(ValueError,'boundary_unproven'):hc.assemble(changed)

    def test_partial_capacity_or_overflow_never_spends_model_budget(self):
        for mode in ('partial','bytes','sources'):
            with self.subTest(mode=mode),tasks.locked(self.data,self.session) as (path,state):
                state.pop('history_integration',None)
                state['history']['status']='partial' if mode=='partial' else 'ready'
                tasks.save(path,state)
            target=patch.object(hc,'MAX_INPUT_BYTES',10) if mode=='bytes' else patch.object(tasks,'MAX_SOURCES',1 if mode=='sources' else 64)
            with target:result=hc.activate(self.data,self.session,launcher=lambda *a:self.fail('must not call'))
            self.assertEqual(result['status'],'unavailable');self.assertEqual(result['budget']['calls_reserved'],0)
            self.assertFalse(hc.ready(self.state()))

    def test_one_call_idempotence_and_separate_budget(self):
        before=self.state()['budget'];self.complete()
        hc.activate(self.data,self.session,launcher=lambda *a:self.fail('duplicate call'))
        self.assertEqual(self.state()['history_budget']['calls_reserved'],1)
        self.assertEqual(self.state()['budget'],before)
        hc.disable(self.data,self.session)
        result=hc.activate(self.data,self.session,launcher=lambda *a:self.fail('budget reset'))
        self.assertEqual(result['status'],'unavailable')

    def test_import_never_becomes_live_rollback_confirmation(self):
        self.complete();state=self.state()
        self.assertEqual(state['sources'],[self.live])
        self.assertEqual([s['id'] for s in tasks.supervision_sources(state)],['h-old',self.live['id']])
        self.assertEqual(state['contract']['source_id'],self.live['id'])

    def test_summary_is_unverified_intent_and_raw_history_reaches_review(self):
        self.complete();state=self.state();context=tasks.context_view(state)
        self.assertEqual(context['intent_memory']['verification'],'unverified')
        self.assertEqual(context['intent_memory']['freshness'],'current')
        packet=review.make_packet(state,self.evidence())
        self.assertEqual(packet['user_sources'][0]['text'],self.old['text'])
        supporting=packet['interpretation']['history_context']['messages']
        self.assertEqual(supporting[0]['authority'],'agent_statement_not_authorization')
        self.assertTrue(all(m['role']=='assistant' for m in supporting))
        self.assertNotIn(self.agent['id'],[s['id'] for s in packet['user_sources']])

    def test_invalid_or_assistant_user_citations_rejected(self):
        ordered,messages,_=hc.assemble(self.state())
        for citation in ({'source_id':'h-agent','quote':'Proposal'}, {'source_id':'h-old','quote':'Backend approved'}):
            v=self.output()['result'];v['intent_update']['changes'][0]['citations'][0]=citation
            with self.assertRaises(ValueError):hc.validate(v,ordered,messages)

    def test_proposal_reference_requires_real_later_user_quote(self):
        ordered,messages,_=hc.assemble(self.state());v=self.output()['result']
        v['proposal_links']=[{'item_id':'goal','user_approval':{'source_id':self.live['id'],'quote':'Continue.'},
            'assistant_proposal':{'source_id':'h-agent','quote':'Proposal two: change title only.'}}]
        hc.validate(v,ordered,messages)
        v['proposal_links'][0]['user_approval']={'source_id':'h-old','quote':'UI only.'}
        with self.assertRaisesRegex(ValueError,'follow'):hc.validate(v,ordered,messages)
        # This checks provenance/ordering, not whether "Continue" semantically approved proposal two.

    def test_concurrent_new_authorization_not_overwritten_or_marked_fresh(self):
        def runner(*args):
            self.prompt('Backend now approved.','new');return self.output()
        self.complete(runner);state=self.state()
        self.assertTrue(state['history_integration']['interpretation_stale_at_publish'])
        self.assertNotIn('latest_interpretation_id',state)
        packet=review.make_packet(state,self.evidence())
        self.assertEqual(packet['user_sources'][-1]['text'],'Backend now approved.')
        self.assertTrue(packet['interpretation']['history_context']['live_sources_changed_since_interpretation'])

    def test_input_while_queued_cannot_be_mistaken_for_interpreted(self):
        token=self.reserve();self.prompt('New task instead.','new')
        hc.worker(self.data,self.session,token,lambda *a:self.output())
        self.assertTrue(self.state()['history_integration']['interpretation_stale_at_publish'])

    def test_new_sources_stale_old_review_and_reminder_without_new_bootstrap(self):
        self.complete();state=self.state();old=tasks.user_context_digest(state)
        self.prompt('Backend now approved.','new');state=self.state()
        self.assertNotEqual(tasks.user_context_digest(state),old)
        self.assertTrue(review.is_stale(state,{'context_digest':old,'action_version':[0,False]}))
        self.assertEqual(state['history_budget']['calls_reserved'],1)
        self.assertEqual(tasks.context_view(state)['intent_memory']['freshness'],'stale')

    def test_pending_history_waits_but_failed_interpretation_uses_raw_authority(self):
        token=self.reserve()
        with self.assertRaisesRegex(ValueError,'history_not_ready'):review.make_packet(self.state(),self.evidence())
        def failure(*a):raise TimeoutError('model timeout')
        hc.worker(self.data,self.session,token,failure)
        self.assertEqual(self.state()['history_integration']['status'],'ready_raw_sources')
        self.prompt('Continue.','new')
        self.assertEqual(self.state()['sources'][-1]['turn_id'],'new')
        self.assertEqual(self.state()['budget']['reviews_used'],0)
        self.assertFalse(hc.status(self.state())['main_task_must_wait'])

    def test_pause_forget_cancel_and_retry_prevent_late_publish(self):
        for mode in ('pause','forget','disable','retry'):
            with self.subTest(mode=mode):
                self.setUp();token=self.reserve()
                def runner(*args):
                    if mode=='pause':(self.data/'disabled').touch()
                    elif mode=='forget':tasks.paths(self.data,self.session)[1].unlink()
                    elif mode=='disable':hc.disable(self.data,self.session)
                    else:history.request(self.data,self.session,retry=True)
                    return self.output()
                hc.worker(self.data,self.session,token,runner)
                state=self.state()
                if mode=='forget':self.assertIsNone(state)
                else:self.assertEqual(state['history_integration']['status'],'cancelled');self.assertNotIn('latest_interpretation_id',state)

    def test_existing_interpretation_and_baseline_are_preserved(self):
        target=self.root/'panel.py';target.write_text('title="Before"\n')
        actions.configure(self.data,self.session,str(self.root),['panel.py'])
        before=copy.deepcopy(self.state()['action_evidence'])
        with tasks.locked(self.data,self.session) as (path,state):
            state['interpretations']={'prior':{'user_context_digest':tasks.user_context_digest(state)}}
            state['latest_interpretation_id']='prior';tasks.save(path,state)
        self.complete();state=self.state()
        self.assertEqual(state['latest_interpretation_id'],'prior')
        self.assertEqual(state['action_evidence'],before)
        self.assertEqual(state['budget']['reviews_used'],0)

    def test_report_exposes_history_cost_without_extra_calls(self):
        self.complete();result=report.generate(self.data,self.session)
        self.assertEqual(result['history_supervision']['budget']['calls_reserved'],1)
        self.assertEqual(result['history_supervision']['output']['usage']['input_tokens'],100)
        self.assertEqual(result['report_generation_model_calls'],0)
        self.assertIn('h-old',[s['id'] for s in result['task']['user_sources']])

    def test_review_packet_limit_rejects_without_truncating_new_authorization(self):
        self.complete();self.prompt('New authority: '+('x'*15000),'new')
        with patch.object(review,'MAX_PACKET_BYTES',2000),self.assertRaisesRegex(ValueError,'never silently truncate'):
            review.make_packet(self.state(),self.evidence())

    def test_model_change_invalidates_supervision_without_resetting_budgets(self):
        (self.root/'panel.py').write_text('backend = None\n')
        actions.configure(self.data,self.session,str(self.root),['panel.py'])
        supervisor.configure(self.data,self.session,True,model='gpt-5.6-luna',effort='low')
        before=self.state();generation=before['supervisor']['generation']
        supervisor.configure(self.data,self.session,True,model='gpt-5.6-sol',effort='medium')
        after=self.state()
        self.assertEqual(after['supervisor']['generation'],generation+1)
        self.assertEqual(after['supervisor']['model'],'gpt-5.6-sol')
        self.assertEqual(after['budget'],before['budget'])
        supervisor.configure(self.data,self.session,True)
        self.assertEqual(self.state()['supervisor']['model'],'gpt-5.6-sol')

    def test_history_change_invalidates_pending_advisory(self):
        self.complete()
        (self.root/'panel.py').write_text('backend = None\n')
        actions.configure(self.data,self.session,str(self.root),['panel.py'])
        state=self.state();entry={'payload':{'user_context_digest':tasks.user_context_digest(state)}}
        history.request(self.data,self.session,retry=True)
        changed=self.state()
        self.assertFalse(hc.ready(changed))
        self.assertEqual(reminders.stale_reason(changed,tasks.paths(self.data,self.session)[1],entry),'history_not_ready')

    def test_invalid_output_failure_retains_usage_and_never_installs_memory(self):
        token=self.reserve()
        from review import ReviewFailure
        def runner(*args):
            raise ReviewFailure('invalid refs',{'usage':{'input_tokens':100,'output_tokens':20,'cached_input_tokens':0}})
        hc.worker(self.data,self.session,token,runner)
        state=self.state()
        self.assertEqual(state['history_integration']['status'],'ready_raw_sources')
        self.assertEqual(state['history_integration']['output']['usage']['input_tokens'],100)
        packet=review.make_packet(state,self.evidence())
        self.assertEqual(packet['user_sources'][0]['text'],self.old['text'])
        self.assertFalse(packet['interpretation']['history_context']['interpretation_available'])
        self.assertNotIn('latest_interpretation_id',state)
        self.assertEqual(report.generate(self.data,self.session)['history_usage']['reported_input_plus_output'],120)

    def test_raw_recovery_never_uses_rejected_summary_or_incomplete_sources(self):
        self.reserve()
        with tasks.locked(self.data,self.session) as (path,state):
            state['history_integration'].update(status='failed',output={'result':{'contract':{'goal':'FORGED'}}})
            tasks.save(path,state)
        hc.use_raw(self.data,self.session)
        packet=review.make_packet(self.state(),self.evidence())
        self.assertNotIn('FORGED',json.dumps(packet))
        self.assertEqual(self.state()['history_budget']['calls_reserved'],1)
        with tasks.locked(self.data,self.session) as (path,state):
            state['history']['status']='partial';tasks.save(path,state)
        with self.assertRaisesRegex(ValueError,'history_incomplete'):hc.use_raw(self.data,self.session)
        with self.assertRaisesRegex(ValueError,'history_not_ready'):review.make_packet(self.state(),self.evidence())

    def test_cli_invalid_task_returns_nonblocking_diagnostic(self):
        import subprocess
        command=[sys.executable,str(Path(supervisor.__file__)),'enable-experimental',
                 '--data-directory',str(self.data),'--session','absent']
        result=subprocess.run(command,capture_output=True,text=True)
        self.assertEqual(result.returncode,1)
        self.assertEqual(json.loads(result.stdout)['main_task_must_wait'],False)
        self.assertNotIn('Traceback',result.stderr)


if __name__=='__main__':unittest.main()
