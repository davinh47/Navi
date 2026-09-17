import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS=Path(__file__).resolve().parents[1]/'plugins/navi/scripts'
sys.path.insert(0,str(SCRIPTS))
import history
import task_state as tasks
import report


class HistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.data=self.root/'data';self.session='session-a'
        self.file=self.root/'transcript.jsonl'
        tasks.workspace_policy(self.data,str(self.root),True)
        self.prompt('enable-history','t-current')

    def prompt(self,body,turn):
        tasks.on_hook(self.data,{'session_id':self.session,'hook_event_name':'UserPromptSubmit',
            'turn_id':turn,'prompt':body,'cwd':str(self.root)})

    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])

    def header(self,**kw):return {'type':'session_meta','payload':{'id':self.session,**kw}}

    def message(self,body='UI only; backend deferred.',role='user',turn='t-old',item='m-old',**kw):
        return {'type':'event_msg','payload':{'type':'item_completed','thread_id':self.session,
            'turn_id':turn,'item':{'id':item,'type':'UserMessage' if role=='user' else 'AgentMessage',
                'content':[{'type':'text' if role=='user' else 'Text','text':body}]},**kw}}

    def write(self,*rows,tail=b''):
        self.file.write_bytes((''.join(json.dumps(x)+'\n' for x in (self.header(),)+rows)).encode()+tail)
        st=self.file.stat();return {'device':st.st_dev,'inode':st.st_ino,'size':st.st_size}

    def read(self,*rows,tail=b''):
        return history.read_snapshot(str(self.file),self.session,self.write(*rows,tail=tail))

    def start(self):
        history.request(self.data,self.session)
        history.on_hook(self.data,{'session_id':self.session,'hook_event_name':'PreToolUse',
            'turn_id':'t-current','transcript_path':str(self.file)},launcher=lambda *args:None)
        return self.state()['history']['token']

    def test_roles_order_mirrors_and_no_tools_or_reasoning(self):
        result=self.read(self.message(),self.message('I suggest backend work','assistant',item='m-agent'),
            {'type':'response_item','payload':{'type':'message','role':'user',
                'content':[{'type':'input_text','text':'SYSTEM_INJECTED'}],
                'internal_chat_message_metadata_passthrough':{'content_item_kinds':['environments.environment_context']}}},
            {'type':'response_item','payload':{'type':'reasoning','text':'PRIVATE_REASONING'}},
            {'type':'event_msg','payload':{'type':'item_completed','item':{'type':'CommandExecution','output':'TOOL_SECRET'}}})
        self.assertEqual(result['status'],'ready')
        self.assertEqual([m['role'] for m in result['messages']],['user','assistant'])
        self.assertIn('not_authorization',result['messages'][1]['authority'])
        self.assertNotIn('PRIVATE_REASONING',json.dumps(result));self.assertNotIn('TOOL_SECRET',json.dumps(result))
        self.assertNotIn('SYSTEM_INJECTED',json.dumps(result))

    def test_long_scan_excludes_tool_logs_but_keeps_late_user(self):
        tool={'type':'response_item','payload':{'type':'function_call_output','output':'x'*900000}}
        result=self.read(self.message(),*([tool]*20),self.message('Backend now allowed',turn='new',item='new'))
        self.assertEqual(result['status'],'ready')
        self.assertGreater(result['read_bytes'],16*1024*1024)
        self.assertEqual(result['messages'][-1]['text'],'Backend now allowed')
        self.assertLess(len(json.dumps(result)),5000)

    def test_native_compaction_chain_keeps_original_authority(self):
        def compact(n,old,new):
            return {'type':'compacted','payload':{'window_number':n,'first_window_id':'w0',
                'previous_window_id':old,'window_id':new,'message':'SUMMARY_NOT_AUTHORITY'}}
        result=self.read(self.message(),compact(1,'w0','w1'),compact(2,'w1','w2'),
            self.message('New scope',turn='new',item='new'))
        self.assertEqual(result['status'],'ready');self.assertEqual(result['compaction_windows'],2)
        self.assertNotIn('SUMMARY_NOT_AUTHORITY',json.dumps(result))
        result=self.read(self.message(),compact(1,'w0','w1'),compact(3,'w1','w3'))
        self.assertIn('compaction_original_coverage_unknown',result['gaps'])
        result=self.read(compact(1,'w0','w1'),self.message())
        self.assertEqual(result['status'],'partial')

    def test_assistant_overflow_never_evicts_user_authority(self):
        rows=[self.message('progress','assistant',item='a'+str(i)) for i in range(20)]
        with patch.object(history,'MAX_ASSISTANT_BYTES',16):
            result=self.read(self.message(),*rows,self.message('Newest user',turn='new',item='new'))
        self.assertEqual(result['status'],'ready')
        self.assertEqual([m['text'] for m in result['messages'] if m['role']=='user'],
            ['UI only; backend deferred.','Newest user'])
        self.assertGreater(result['assistant_messages_omitted'],0)

    def test_commentary_and_agents_context_are_not_authority(self):
        row=self.message('Working','assistant');row['payload']['item']['phase']='commentary'
        context={'type':'response_item','payload':{'type':'message','role':'user',
            'internal_chat_message_metadata_passthrough':{'content_item_kinds':
                ['plugins.recommendations','agents_md.instructions','environments.environment_context']}}}
        result=self.read(context,self.message(),row)
        self.assertEqual(result['status'],'ready');self.assertEqual(len(result['messages']),1)

    def test_explicit_recovery_checks_session_without_forging_hook(self):
        self.write(self.message())
        result=history.recover(self.data,self.session,str(self.file),launcher=lambda *a:None)
        self.assertIn('not a host hook',result['request_origin'])
        history.worker(self.data,self.session,self.state()['history']['token'])
        self.assertEqual(self.state()['history']['status'],'ready')
        self.file.write_text(json.dumps(self.header(id='other'))+'\n')
        history.recover(self.data,self.session,str(self.file),launcher=lambda *a:None)
        history.worker(self.data,self.session,self.state()['history']['token'])
        self.assertEqual(self.state()['history']['status'],'failed')

    def test_same_identity_dedup_but_distinct_turns_retained(self):
        row=self.message('继续')
        result=self.read(row,row,self.message('继续',turn='t-next',item='m-next'))
        self.assertEqual(len(result['messages']),2)

    def test_conflicting_duplicate_fails(self):
        with self.assertRaisesRegex(ValueError,'conflicting_duplicate'):
            self.read(self.message(),self.message('Now allowed'))

    def test_different_session_and_fork_rejected(self):
        snap=self.write(self.message())
        with self.assertRaisesRegex(ValueError,'identity_mismatch'):
            history.read_snapshot(str(self.file),'other',snap)
        self.file.write_text(json.dumps(self.header(forked_from_id='parent'))+'\n')
        st=self.file.stat()
        with self.assertRaisesRegex(ValueError,'fork_or_subagent'):
            history.read_snapshot(str(self.file),self.session,dict(device=st.st_dev,inode=st.st_ino,size=st.st_size))

    def test_foreign_message_rejected(self):
        with self.assertRaisesRegex(ValueError,'message_session_mismatch'):
            self.read(self.message(thread_id='other'))

    def test_unsupported_role_user_never_promoted(self):
        result=self.read({'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'unverified'}]}})
        self.assertEqual(result['status'],'partial');self.assertEqual(result['messages'],[])

    def test_missing_canonical_user_event_visible(self):
        mirror={'type':'response_item','payload':{'type':'message','role':'user','content':[{'type':'input_text','text':'hello'}],
            'internal_chat_message_metadata_passthrough':{'turn_id':'old','content_item_kinds':['user.text']}}}
        self.assertIn('user_message_event_missing',self.read(mirror)['gaps'])
        self.assertEqual(self.read(mirror,self.message('hello',turn='old'))['status'],'ready')

    def test_desktop_combined_host_context_is_never_user_authority(self):
        for kinds in (['plugins.recommendations'],
                ['plugins.recommendations','environments.environment_context'],
                ['environments.environment_context','plugins.recommendations']):
            with self.subTest(kinds=kinds):
                host={'type':'response_item','payload':{'type':'message','role':'user',
                    'content':[{'type':'input_text','text':'HOST_TEXT_MUST_NOT_AUTHORIZE'}],
                    'internal_chat_message_metadata_passthrough':{'content_item_kinds':kinds}}}
                result=self.read(host,self.message())
                self.assertEqual(result['status'],'ready')
                self.assertEqual(len(result['messages']),1)
                self.assertNotIn('HOST_TEXT_MUST_NOT_AUTHORIZE',json.dumps(result))

    def test_mixed_unknown_or_user_context_still_marks_gap(self):
        for kinds in (['plugins.recommendations','user.text'],
                ['plugins.recommendations','future.unknown'],
                'plugins.recommendations', [{'kind':'plugins.recommendations'}]):
            with self.subTest(kinds=kinds):
                row={'type':'response_item','payload':{'type':'message','role':'user',
                    'content':[{'type':'input_text','text':'UNKNOWN'}],
                    'internal_chat_message_metadata_passthrough':{'content_item_kinds':kinds}}}
                result=self.read(row,self.message())
                self.assertEqual(result['status'],'partial')
                self.assertIn('unknown_user_content_provenance',result['gaps'])
                self.assertEqual(len(result['messages']),1)

    def test_partial_tail_compaction_and_malformed_visible(self):
        self.assertIn('incomplete_boundary_line',self.read(self.message(),tail=b'{partial')['gaps'])
        self.assertIn('malformed_record',self.read(self.message(),tail=b'{bad}\n')['gaps'])
        self.assertIn('compaction_original_coverage_unknown',self.read(self.message(),{'type':'compacted','payload':{'message':'summary'}})['gaps'])

    def test_limits_do_not_claim_complete_or_drop_later_authority_silently(self):
        with patch.object(history,'MAX_MESSAGES',1):
            result=self.read(self.message(),self.message('Backend now allowed',turn='next',item='next'))
        self.assertEqual(result['status'],'partial');self.assertIn('message_capacity',result['gaps'])
        with patch.object(history,'MAX_BYTES',100):
            self.assertEqual(self.read(self.message())['status'],'partial')
        with patch.object(history,'READ_SECONDS',-1):
            with self.assertRaisesRegex(ValueError,'empty_transcript'):self.read(self.message())

    def test_nontext_messages_visible(self):
        row=self.message();row['payload']['item']['content'].append({'type':'image','url':'private'})
        result=self.read(row)
        self.assertEqual(result['messages'],[]);self.assertIn('nontext_message_omitted',result['gaps'])

    def test_append_allowed_snapshot_excludes_new_messages(self):
        snap=self.write(self.message())
        with self.file.open('a') as f:f.write(json.dumps(self.message('New',turn='new',item='new'))+'\n')
        result=history.read_snapshot(str(self.file),self.session,snap)
        self.assertEqual(len(result['messages']),1);self.assertEqual(result['read_bytes'],snap['size'])

    def test_truncate_replace_and_symlink_rejected(self):
        snap=self.write(self.message());self.file.write_bytes(b'')
        with self.assertRaises(ValueError):history.read_snapshot(str(self.file),self.session,snap)
        snap=self.write(self.message());other=self.root/'other';self.file.rename(other);self.file.write_text(other.read_text())
        with self.assertRaises(ValueError):history.read_snapshot(str(self.file),self.session,snap)
        self.file.unlink();self.file.symlink_to(other)
        with self.assertRaises(OSError):history.read_snapshot(str(self.file),self.session,snap)

    def test_idempotent_request_and_one_background_dispatch(self):
        self.write(self.message());first=history.request(self.data,self.session)
        self.assertEqual(first,history.request(self.data,self.session))
        payload={'session_id':self.session,'hook_event_name':'PreToolUse','transcript_path':str(self.file)}
        with patch.object(history,'read_snapshot',side_effect=AssertionError('Hook must not read')),patch.object(history,'launch') as launch:
            history.on_hook(self.data,payload,launcher=launch);history.on_hook(self.data,payload,launcher=launch)
        self.assertEqual(launch.call_count,1)

    def test_no_optin_and_subagent_do_not_read(self):
        self.write(self.message())
        payload={'session_id':self.session,'hook_event_name':'PreToolUse','transcript_path':str(self.file)}
        with patch.object(history,'launch') as launch:
            history.on_hook(self.data,payload,launcher=launch)
            history.request(self.data,self.session)
            history.on_hook(self.data,dict(payload,agent_id='child'),launcher=launch)
        self.assertEqual(launch.call_count,0);self.assertEqual(self.state()['history']['status'],'pending')
        history.on_hook(self.data,dict(payload,session_id='unrelated'))
        self.assertFalse(tasks.paths(self.data,'unrelated')[2].exists())

    def test_worker_keeps_live_sources_budget_and_action_baseline_unchanged(self):
        self.write(self.message());before=self.state();token=self.start()
        self.prompt('backend now allowed','t-new')
        history.worker(self.data,self.session,token)
        after=self.state()
        self.assertEqual(after['history']['status'],'ready');self.assertEqual(after['sources'][-1]['text'],'backend now allowed')
        self.assertEqual(after['budget'],before['budget']);self.assertEqual(after['contract'],before['contract'])
        self.assertNotIn('action_evidence',after)
        self.assertNotIn('target',after['history'])
        self.assertEqual(after['history']['model_calls'],0)

    def test_forget_cancel_and_new_generation_prevent_stale_publish(self):
        self.write(self.message());token=self.start()
        history.cancel(self.data,self.session);history.worker(self.data,self.session,token)
        self.assertEqual(self.state()['history']['status'],'cancelled')
        history.request(self.data,self.session,retry=True);history.worker(self.data,self.session,token)
        self.assertEqual(self.state()['history']['status'],'pending')
        token=self.start();tasks.paths(self.data,self.session)[1].unlink()
        history.worker(self.data,self.session,token);self.assertIsNone(self.state())

    def test_pause_during_read_cancels_publication(self):
        self.write(self.message());token=self.start()
        original=history.read_snapshot
        def paused(*args):
            result=original(*args);(self.data/'disabled').touch();return result
        with patch.object(history,'read_snapshot',side_effect=paused):history.worker(self.data,self.session,token)
        self.assertEqual(self.state()['history']['status'],'cancelled')
        self.assertEqual(self.state()['history']['messages'],[])

    def test_missing_path_launch_error_and_bad_file_visible(self):
        history.request(self.data,self.session)
        history.on_hook(self.data,{'session_id':self.session,'hook_event_name':'PreToolUse'})
        self.assertEqual(self.state()['history']['status'],'unavailable')
        self.write(self.message());history.request(self.data,self.session,retry=True)
        with patch.object(history,'launch',side_effect=OSError()):
            history.on_hook(self.data,{'session_id':self.session,'hook_event_name':'PreToolUse','transcript_path':str(self.file)},launcher=history.launch)
        self.assertIn('worker_launch_failed',self.state()['history']['gaps'])

    def test_report_separates_history_from_current_task_authority(self):
        self.write(self.message(),self.message('My plan','assistant',item='agent'));token=self.start()
        history.worker(self.data,self.session,token)
        result=report.generate(self.data,self.session)
        self.assertEqual(len(result['task']['user_sources']),1)
        self.assertEqual(len(result['conversation_history']['messages']),2)
        self.assertIn('未自动恢复任务语义',report.markdown(result))


if __name__=='__main__':unittest.main()
