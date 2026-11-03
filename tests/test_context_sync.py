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

    def test_same_turn_amendment_recovers_cursor_and_reaches_reviewer_without_reset(self):
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        before=self.state()
        amendment='Test with an isolated pointer; keep the backend deferred.'
        self.user(amendment,'t1')
        # The hook can arrive before its canonical transcript item. Preserve it once in either case.
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual([s['text'] for s in self.packet()['user_sources']],
                         ['Repair UI only; backend deferred.',amendment])
        self.append(amendment,'user','t1','u2')
        self.append('I will use the isolated pointer.','assistant','t1','a2','commentary')
        # Existing installations may have saved this failure; recovery must use the old reader.
        with tasks.locked(self.data,self.session) as (path,state):
            state['context_sync'].update(status='unavailable',reason='ambiguous_live_turn')
            tasks.save(path,state)
        old_support=review.support_digest(self.state())
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        recovered=self.state()
        self.assertEqual(recovered['context_sync']['status'],'ready')
        self.assertIsNone(recovered['context_sync']['reason'])
        self.assertEqual(recovered['history']['token'],before['history']['token'])
        self.assertEqual(recovered['history_integration']['token'],before['history_integration']['token'])
        self.assertEqual(recovered['budget'],before['budget'])
        self.assertEqual(recovered['history_budget'],before['history_budget'])
        self.assertEqual(recovered['context_sync']['parsed_new_bytes'],self.file.stat().st_size-before['context_sync']['synced_bytes'])
        self.assertTrue(review.is_stale(recovered,{'support_digest':old_support}))
        packet=self.packet()
        self.assertEqual([s['id'] for s in packet['user_sources']],[s['id'] for s in recovered['sources']])
        self.assertEqual([s['text'] for s in packet['user_sources']],
                         ['Repair UI only; backend deferred.',amendment])
        self.assertEqual([m['role'] for m in packet['interpretation']['history_context']['conversation_order']],
                         ['user','assistant','user','assistant'])
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['context_sync']['parsed_new_bytes'],0)
        supervisor.configure(self.data,self.session,True)
        (self.work/'ui.py').write_text('new')
        supervisor.on_hook(self.data,self.payload('PostToolUse',transcript_path=str(self.file)),launcher=lambda *a:None)
        token=self.state()['supervisor']['worker']['token'];seen=[]
        def runner(packets,**kw):
            seen.extend(packets);raise ValueError('fixture stops before real model')
        supervisor.worker(self.data,self.session,token,runner=runner,delay=0)
        self.assertEqual(len(seen),1)
        self.assertEqual([s['text'] for s in seen[0]['user_sources']],
                         ['Repair UI only; backend deferred.',amendment])

    def test_paginated_rotation_preserves_history_budget_and_recovers_failed_sync(self):
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        before=self.state();root_id='00000000-0000-0000-0000-000000000001'
        leaf_id='00000000-0000-0000-0000-000000000002'
        parent=self.root/('rollout-'+root_id+'.jsonl');leaf=self.root/('rollout-'+leaf_id+'.jsonl')
        rows=[json.loads(line) for line in self.file.read_text().splitlines()]
        rows[0]['payload']['history_mode']='paginated'
        for i,row in enumerate(rows):row['ordinal']=i
        parent.write_text(''.join(json.dumps(r)+'\n' for r in rows))
        header={'ordinal':len(rows),'type':'session_meta','payload':{'id':self.session,'history_mode':'paginated',
            'history_base':{'thread_id':root_id,'end_ordinal_exclusive':len(rows),'end_byte_offset':parent.stat().st_size}}}
        amendment=copy.deepcopy(rows[1]);amendment['ordinal']=len(rows)+1
        amendment['payload']['turn_id']='t2';amendment['payload']['item'].update(id='u2',content=[{'type':'text','text':'Keep the backend deferred.'}])
        leaf.write_text(json.dumps(header)+'\n'+json.dumps(amendment)+'\n')
        self.user('Keep the backend deferred.','t2')
        raw=parent.read_bytes();parent.unlink()
        self.assertFalse(sync.synchronize(self.data,self.session,str(leaf)))
        self.assertEqual(self.state()['history_integration'],before['history_integration'])
        parent.write_bytes(raw)
        self.assertTrue(sync.synchronize(self.data,self.session,str(leaf)))
        after=self.state()
        self.assertEqual(after['history_budget'],before['history_budget'])
        self.assertEqual(after['budget'],before['budget'])
        self.assertEqual(after['history_integration']['token'],before['history_integration']['token'])
        self.assertEqual([s['text'] for s in self.packet()['user_sources']],
                         ['Repair UI only; backend deferred.','Keep the backend deferred.'])
        self.assertTrue(sync.synchronize(self.data,self.session,str(leaf)))
        self.assertEqual(self.state()['context_sync']['parsed_new_bytes'],0)

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

    def test_goal_compatibility_recovers_saved_cursor_without_resetting_authority_or_budget(self):
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        before=self.state()
        host={'type':'response_item','payload':{'type':'message','role':'user',
            'content':[{'type':'input_text','text':'INTERNAL_GOAL_NOT_NEW_AUTHORITY'}],
            'internal_chat_message_metadata_passthrough':{'content_item_kinds':['goal.internal_context']}}}
        with self.file.open('a') as stream:
            for _ in range(3):stream.write(json.dumps(host)+'\n')
        with patch.object(history,'HOST_CONTEXT_KINDS',history.HOST_CONTEXT_KINDS-{'goal.internal_context'}):
            self.assertFalse(sync.synchronize(self.data,self.session,str(self.file)))
        failed=self.state()
        self.assertEqual(failed['context_sync']['reason'],'unknown_user_content_provenance')
        self.assertEqual(failed['context_sync']['reader'],before['context_sync']['reader'])
        self.assertEqual(failed['history_integration'],before['history_integration'])
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        recovered=self.state()
        self.assertEqual(recovered['context_sync']['status'],'ready')
        self.assertEqual(tasks.user_context_digest(recovered),tasks.user_context_digest(before))
        self.assertEqual(recovered['budget'],before['budget'])
        self.assertEqual(recovered['history_budget'],before['history_budget'])
        self.assertEqual(recovered['context_sync']['parsed_new_bytes'],self.file.stat().st_size-before['context_sync']['synced_bytes'])
        self.user('Backend is now allowed, but do not deploy.','t2')
        self.append('Backend is now allowed, but do not deploy.','user','t2','u2')
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        packet=self.packet()
        self.assertEqual([s['text'] for s in packet['user_sources']],
                         ['Repair UI only; backend deferred.','Backend is now allowed, but do not deploy.'])
        self.assertNotIn('INTERNAL_GOAL_NOT_NEW_AUTHORITY',json.dumps(packet))
        self.assertTrue(sync.synchronize(self.data,self.session,str(self.file)))
        self.assertEqual(self.state()['context_sync']['parsed_new_bytes'],0)

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
