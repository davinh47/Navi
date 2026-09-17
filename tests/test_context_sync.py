import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/navi/scripts'))
import history
import history_context as hc
import context_sync as sync
import task_state as tasks
import action_evidence as actions
import reminders
import review
import report
import supervisor


class ContextSyncTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.data=self.root/'data';self.file=self.root/'host.jsonl';self.session='sync-test'
        self.work=self.root/'work';self.work.mkdir();(self.work/'ui.py').write_text('old')
        tasks.workspace_policy(self.data,str(self.work),True)
        self.user('Repair UI only; backend deferred.','t1')
        self.file.write_text(json.dumps({'type':'session_meta','payload':{'id':self.session}})+'\n')
        self.append('Repair UI only; backend deferred.','user','t1','u1')
        self.append('Next I suggest testing the UI with a fixture.','assistant','t1','a1')
        self.initial_size=self.file.stat().st_size
        result=history.read_snapshot(str(self.file),self.session,self.snapshot())
        with tasks.locked(self.data,self.session) as (p,s):
            s['history']={**result,'token':'raw','snapshot':self.snapshot()}
            ordered,msgs,packet=hc.assemble(s)
            s['history_integration']={'enabled':True,'status':'ready_raw_sources','token':'integration',
                'history_token':'raw','ordered_sources':ordered,'messages':msgs,'packet':packet,
                'live_source_digest':tasks.digest(s['sources'])}
            s['history_budget']={'limit':1,'calls_reserved':1};tasks.save(p,s)
        actions.configure(self.data,self.session,str(self.work),['ui.py'])
        reminders.configure(self.data,self.session,True)
        reminders.on_hook(self.data,self.payload('UserPromptSubmit'))

    def payload(self,event,**kw):return dict(session_id=self.session,hook_event_name=event,cwd=str(self.work),turn_id='t1',**kw)
    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def snapshot(self):
        s=self.file.stat();return {'device':s.st_dev,'inode':s.st_ino,'size':s.st_size}
    def user(self,text,turn):tasks.on_hook(self.data,dict(session_id=self.session,hook_event_name='UserPromptSubmit',cwd=str(self.work),turn_id=turn,prompt=text))
    def append(self,text,role,turn,item,phase=None):
        row={'type':'event_msg','payload':{'type':'item_completed','thread_id':self.session,'turn_id':turn,
            'item':{'id':item,'type':'UserMessage' if role=='user' else 'AgentMessage','phase':phase,
                    'content':[{'type':'text' if role=='user' else 'Text','text':text}]}}}
        with self.file.open('a') as f:f.write(json.dumps(row)+'\n')
    def packet(self):
        return review.make_packet(self.state(),{'coverage':'partial','actions':[{'id':'a1','kind':'tool_observation','path':'ui.py','origin':'codex_hook','evidence':'Changed UI'}]})
    def notice(self):
        s=self.state();value={'user_context_digest':tasks.user_context_digest(s),'action_version':actions.version(s),
            'source_ids':[s['sources'][0]['id']],'requirement':'UI only','evidence':'fixture','suggestion':'Check scope'}
        key=reminders.queue(self.data,self.session,value)
        reminders.on_hook(self.data,self.payload('PreToolUse',tool_use_id='tool1'))
        return key

    def test_incremental_commentary_order_and_no_paid_history(self):
        before=self.state();digest=tasks.user_context_digest(before)
        self.append('Testing uses an existing browser capability.','assistant','t1','a2','commentary')
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        s=self.state();self.assertEqual(tasks.user_context_digest(s),digest)
        self.assertEqual(s['budget'],before['budget']);self.assertEqual(s['history_budget'],before['history_budget'])
        self.assertEqual(s['context_sync']['parsed_new_bytes'],self.file.stat().st_size-self.initial_size)
        p=self.packet();h=p['interpretation']['history_context']
        self.assertIn('existing browser',h['messages'][-1]['text'])
        self.assertEqual([r['role'] for r in h['conversation_order']],['user','assistant','assistant'])
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['context_sync']['parsed_new_bytes'],0)
        self.assertEqual(len(self.state()['context_sync']['reader']['messages']),3)

    def test_next_user_links_proposal_and_preserves_authority(self):
        self.user('Continue the proposed next step.','t2');self.append('Continue the proposed next step.','user','t2','u2')
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        p=self.packet();h=p['interpretation']['history_context'];order=h['conversation_order']
        self.assertEqual([m['role'] for m in order],['user','assistant','user'])
        self.assertEqual(order[-1]['id'],p['user_sources'][-1]['id'])
        self.assertEqual(len(tasks.supervision_sources(self.state())),2)
        self.assertEqual(h['messages'][0]['authority'],'agent_statement_not_authorization')

    def test_bootstrap_legacy_reader_once_without_budget_reset(self):
        with tasks.locked(self.data,self.session) as (p,s):
            s['history'].pop('cursor');s['history'].pop('snapshot_identity');tasks.save(p,s)
        self.append('New rationale','assistant','t1','a2','commentary')
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['context_sync']['parsed_new_bytes'],self.file.stat().st_size)
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['context_sync']['parsed_new_bytes'],0)
        self.assertEqual(self.state()['history_budget']['calls_reserved'],1)

    def test_rewrite_and_missing_user_provenance_preserve_previous_sources(self):
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        before=copy.deepcopy(self.state()['history_integration'])
        original=self.file.read_text();self.file.write_text(original.replace('backend deferred','backend approved'))
        self.assertFalse(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['history_integration'],before)
        with self.assertRaisesRegex(ValueError,'context_sync_unavailable'):self.packet()
        self.file.write_text(original)
        with self.file.open('a') as f:f.write(json.dumps({'type':'response_item','payload':{'type':'message','role':'user','content':[]}})+'\n')
        self.assertFalse(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertIn('user_role_without_provenance',self.state()['context_sync']['reason'])

    def test_partial_line_recovers_on_later_boundary(self):
        before=self.state()['history_integration']
        with self.file.open('a') as f:f.write('{"type":"event_msg",')
        self.assertFalse(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['history_integration'],before)
        with self.file.open('a') as f:f.write('"payload":{"type":"task_started"}}\n')
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))

    def test_recording_disabled_during_read_cannot_publish(self):
        self.append('New context','assistant','t1','a2')
        original=history.read_snapshot
        def disable(*a,**kw):
            result=original(*a,**kw);(self.data/'disabled').touch();return result
        with patch.object(history,'read_snapshot',side_effect=disable):self.assertFalse(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['context_sync']['status'],'cancelled')
        self.assertNotIn('New context',json.dumps(self.state()['history_integration']))

    def test_new_support_invalidates_review_without_losing_action_context(self):
        before=self.state();key=tasks.user_context_digest(before);support=review.support_digest(before)
        self.append('Changed verification approach','assistant','t1','a2','commentary')
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        after=self.state();self.assertEqual(key,tasks.user_context_digest(after))
        self.assertTrue(review.is_stale(after,{'support_digest':support}))

    def test_optional_response_uses_two_quotes_not_full_history(self):
        for n in range(56):self.user('Keep the same boundaries.',f't{n+2}')
        key=self.notice();before=self.state();source=before['sources'][0]
        v={'id':key,'user_context_digest':tasks.user_context_digest(before),'disposition':'disputed',
            'reason':'This is a fixture for the requested UI test.','citations':[{'source_id':source['id'],'quote':'UI only'}]}
        reminders.respond(self.data,self.session,v);reminders.respond(self.data,self.session,v)
        s=self.state();self.assertEqual(s['budget'],before['budget']);self.assertNotIn('interpretations',s)
        e=s['reminder_transport']['entries'][key]
        self.assertEqual(e['delivery'],'unconfirmed');self.assertEqual(e['status'],before['reminder_transport']['entries'][key]['status'])
        self.assertIn('fixture',json.dumps(self.packet()['interpretation']['advisory_responses']))
        self.assertIn('未独立核验',report.markdown(report.build(s)))
        bad=copy.deepcopy(v);bad['citations'][0]['quote']='Invented authorization'
        with self.assertRaises(ValueError):reminders.respond(self.data,self.session,bad)

    def test_hook_background_schedule_no_models_no_foreign_session(self):
        calls=[]
        sync.on_hook(self.data,self.payload('Stop',transcript_path=str(self.file)),launcher=lambda *a:calls.append(a))
        self.assertEqual(len(calls),1)
        sync.on_hook(self.data,self.payload('Stop',transcript_path=str(self.file),agent_id='child'),launcher=lambda *a:calls.append(a))
        self.assertEqual(len(calls),1);self.assertEqual(self.state()['budget']['reviews_used'],0)

    def test_supervisor_syncs_before_reserving_review(self):
        supervisor.configure(self.data,self.session,True)
        (self.work/'ui.py').write_text('new')
        self.append('Current test explanation','assistant','t1','a2','commentary')
        supervisor.on_hook(self.data,self.payload('PostToolUse',transcript_path=str(self.file)),launcher=lambda *a:None)
        token=self.state()['supervisor']['worker']['token'];seen=[]
        def runner(packets,**kw):
            seen.append(packets[0]);raise ValueError('fixture stops before real model')
        supervisor.worker(self.data,self.session,token,runner=runner,delay=0)
        self.assertEqual(len(seen),1);self.assertIn('Current test explanation',json.dumps(seen[0]))

    def test_compaction_cursor_carries_chain_across_increment(self):
        def compact(n,old,new):
            with self.file.open('a') as f:f.write(json.dumps({'type':'compacted','payload':{'window_number':n,'first_window_id':'w0','previous_window_id':old,'window_id':new}})+'\n')
        compact(1,'w0','w1');self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        compact(2,'w1','w2');self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['context_sync']['reader']['compaction_windows'],2)
        compact(4,'w2','w3');self.assertFalse(sync.synchronize(self.data,self.session,str(self.file)))


if __name__=='__main__':unittest.main()
