import copy
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'plugins/navi/scripts'))
import action_evidence as actions
import record_event
import report
import rollback
import task_state as tasks

BASE='# existing user comment\ntitle = "Before"\n\nvalidation = True\n\nbackend = None\n'
EDIT=BASE.replace('Before','After').replace('backend = None','backend = "extra"')


class RollbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.work=self.root/'workspace';self.work.mkdir();self.data=self.root/'data';self.session='rollback-test'
        self.file=self.work/'part.py';self.file.write_text(BASE);self.n=0
        self.user('Navi: start\nChange title only; keep existing comments and validation.')
        actions.configure(self.data,self.session,str(self.work),['part.py'])
        self.file.write_text(EDIT)
    def tearDown(self):self.tmp.cleanup()
    def state(self):return tasks.read_state(tasks.paths(self.data,self.session)[1])
    def user(self,content):
        self.n+=1
        tasks.on_hook(self.data,{'hook_event_name':'UserPromptSubmit','session_id':self.session,'turn_id':str(self.n),'prompt':content})
        return self.state()['sources'][-1]
    def plan(self):return rollback.prepare(self.data,self.session,'part.py',[2])
    def approval(self,plan):
        source=self.user('I reviewed preview '+plan['id']+'. Undo only these agent edits. They are independent; no other editor is writing.')
        return {'plan_id':plan['id'],'source_id':source['id'],'quote':source['text'],
                'ownership':'user_confirmed_selected_changes','dependencies':'user_confirmed_independent','exclusive_workspace':True}
    def apply(self,plan):return rollback.apply(self.data,self.session,plan['id'],self.approval(plan))
    def hook(self,event):
        out=io.StringIO();err=io.StringIO()
        with patch.dict(os.environ,PLUGIN_DATA=str(self.data)):
            code=record_event.run(io.BytesIO(json.dumps({'session_id':self.session,'turn_id':str(self.n),
                'hook_event_name':event,'tool_use_id':'tool','tool_name':'read','tool_input':'part.py'}).encode()),out,err)
        self.assertEqual(code,0,err.getvalue())
        return json.loads(out.getvalue()) if out.getvalue() else None

    def test_inspect_and_preview_do_not_touch_workspace_or_budget_or_retention(self):
        before=self.state();contents=self.file.read_bytes()
        choices=rollback.inspect(self.data,self.session,'part.py')['changes']
        self.assertEqual(len(choices),2)
        plan=self.plan();after=self.state()
        self.assertEqual(self.file.read_bytes(),contents)
        self.assertIn('-backend = "extra"',plan['diff']);self.assertIn('+backend = None',plan['diff'])
        self.assertEqual(before['expires_at'],after['expires_at']);self.assertEqual(before['budget'],after['budget'])

    def test_selected_hunk_keeps_original_user_and_unselected_task_changes(self):
        plan=self.plan();self.apply(plan)
        self.assertEqual(self.file.read_text(),BASE.replace('Before','After'))
        self.assertEqual(self.state()['rollbacks'][plan['id']]['before'],EDIT)
        self.assertEqual(self.state()['action_evidence']['files']['part.py']['current']['text'],self.file.read_text())
        self.assertEqual(self.state()['budget']['reviews_used'],0)

    def test_user_changes_in_a_separate_hunk_are_retained(self):
        self.file.write_text(EDIT.replace('existing user comment','new user comment'))
        rows=rollback.inspect(self.data,self.session,'part.py')['changes']
        last=rows[-1]['id'];p=rollback.prepare(self.data,self.session,'part.py',[last]);self.apply(p)
        self.assertIn('new user comment',self.file.read_text());self.assertIn('After',self.file.read_text())
        self.assertIn('backend = None',self.file.read_text())

    def test_same_line_changes_are_not_invented_as_independent(self):
        self.file.write_text(BASE.replace('Before','agent and user edit'))
        rows=rollback.inspect(self.data,self.session,'part.py')['changes']
        self.assertEqual(len(rows),1);self.assertEqual(rows[0]['current'],'title = "agent and user edit"\n')
        p=rollback.prepare(self.data,self.session,'part.py',[1]);a=self.approval(p);a['ownership']='unknown'
        with self.assertRaises(rollback.Refused):rollback.apply(self.data,self.session,p['id'],a)
        self.assertIn('agent and user edit',self.file.read_text())

    def test_unknown_dependency_and_unconfirmed_exclusive_editing_refuse(self):
        p=self.plan();approval=self.approval(p)
        for key,value in [('dependencies','unknown'),('ownership','unknown'),('exclusive_workspace',False)]:
            with self.subTest(key=key),self.assertRaises(rollback.Refused):
                rollback.apply(self.data,self.session,p['id'],dict(approval,**{key:value}))
        self.assertEqual(self.file.read_text(),EDIT)

    def test_no_confirmation_wrong_preview_or_fabricated_quote_refuse(self):
        p=self.plan();a=self.approval(p)
        for value in ({},dict(a,plan_id='other'),dict(a,quote='fabricated'),dict(a,source_id='fake')):
            with self.assertRaises(ValueError):rollback.apply(self.data,self.session,p['id'],value)
        self.assertEqual(self.file.read_text(),EDIT)

    def test_preexisting_source_cannot_confirm_new_preview(self):
        p=self.plan();s=self.state()['sources'][-1]
        a={'plan_id':p['id'],'source_id':s['id'],'quote':s['text'],'ownership':'user_confirmed_selected_changes',
           'dependencies':'user_confirmed_independent','exclusive_workspace':True}
        with self.assertRaises(rollback.Refused):rollback.apply(self.data,self.session,p['id'],a)

    def test_intervening_user_update_requires_new_preview(self):
        p=self.plan();self.user('Also preserve backend now.')
        with self.assertRaises(rollback.Refused):self.apply(p)
        self.assertEqual(self.file.read_text(),EDIT)

    def test_changed_file_after_preview_is_never_overwritten(self):
        p=self.plan();self.file.write_text(EDIT+'# user later\n')
        with self.assertRaises(rollback.Refused):self.apply(p)
        self.assertTrue(self.file.read_text().endswith('# user later\n'))

    def test_same_bytes_replacement_inode_and_mode_change_are_conflicts(self):
        p=self.plan();replacement=self.work/'replacement';replacement.write_text(EDIT);os.replace(replacement,self.file)
        with self.assertRaises(rollback.Refused):self.apply(p)
        q=self.plan();self.file.chmod(0o755)
        with self.assertRaises(rollback.Refused):self.apply(q)

    def test_pause_gap_disabled_capture_and_expiry_prevent_execution(self):
        for condition in ('pause','gap','capture','expiry'):
            with self.subTest(condition=condition):
                p=self.plan();a=self.approval(p);path=tasks.paths(self.data,self.session)[1];s=self.state();original=copy.deepcopy(s)
                if condition=='pause':(self.data/'disabled').touch()
                elif condition=='gap':path.with_suffix('.gap').touch()
                elif condition=='capture':s['action_evidence']['enabled']=False;tasks.atomic_write(path,s)
                else:s['expires_at']=0;tasks.atomic_write(path,s)
                with self.assertRaises(rollback.Refused):rollback.apply(self.data,self.session,p['id'],a)
                self.assertEqual(self.file.read_text(),EDIT)
                (self.data/'disabled').unlink(missing_ok=True);path.with_suffix('.gap').unlink(missing_ok=True)
                tasks.atomic_write(path,original)

    def test_capture_reenable_generation_invalidates_plan(self):
        p=self.plan();actions.configure(self.data,self.session,str(self.work),['part.py'])
        with self.assertRaises(rollback.Refused):self.apply(p)

    def test_symlink_parent_and_leaf_and_hardlink_are_refused(self):
        p=self.plan();a=self.approval(p);secret=self.root/'secret';secret.write_text(EDIT)
        self.file.unlink();self.file.symlink_to(secret)
        with self.assertRaises(OSError):rollback.apply(self.data,self.session,p['id'],a)
        self.file.unlink();os.link(secret,self.file)
        with self.assertRaises(rollback.Refused):rollback.inspect(self.data,self.session,'part.py')
        sub=self.work/'sub';sub.symlink_to(self.root,target_is_directory=True)
        with self.assertRaises(OSError):rollback.file_read(str(self.work),'sub/secret')
        self.assertEqual(secret.read_text(),EDIT)

    def test_added_deleted_binary_oversized_and_outside_paths_are_not_supported(self):
        actions.configure(self.data,self.session,str(self.work),['new.txt']);(self.work/'new.txt').write_text('new')
        with self.assertRaises(rollback.Refused):rollback.prepare(self.data,self.session,'new.txt',[1])
        for name in ('../secret','.git/config','/tmp/anything'):
            with self.assertRaises(rollback.Refused):rollback.file_read(str(self.work),name)
        for raw in (b'\0binary',b'x'*4097):
            self.file.write_bytes(raw)
            with self.assertRaises(rollback.Refused):self.plan()
        self.file.unlink()
        with self.assertRaises(OSError):self.plan()

    def test_empty_selected_duplicate_and_nonexistent_changes_refuse(self):
        for selection in ([],[99],[1,1],[True],None):
            with self.assertRaises(rollback.Refused):rollback.prepare(self.data,self.session,'part.py',selection)

    def test_insertions_deletions_and_final_newline_revert(self):
        for current in (BASE+'extra\n',BASE.replace('validation = True\n',''),BASE.rstrip('\n')):
            self.file.write_text(current)
            rows=rollback.inspect(self.data,self.session,'part.py')['changes']
            p=rollback.prepare(self.data,self.session,'part.py',[r['id'] for r in rows]);self.apply(p)
            self.assertEqual(self.file.read_text(),BASE)

    def test_recovery_preview_and_confirmed_restore_retain_user_task_change(self):
        p=self.plan();self.apply(p);rolled=self.file.read_text()
        q=rollback.prepare(self.data,self.session,restore=p['id'])
        self.assertEqual(self.file.read_text(),rolled)
        self.apply(q);self.assertEqual(self.file.read_text(),EDIT)
        self.assertEqual(self.state()['rollbacks'][q['id']]['restores'],p['id'])
        self.assertEqual([r['id'] for r in report.generate(self.data,self.session)['rollbacks']],[p['id'],q['id']])

    def test_recovery_refuses_later_modifications(self):
        p=self.plan();self.apply(p);self.file.write_text(self.file.read_text()+'# later\n')
        with self.assertRaises(rollback.Refused):rollback.prepare(self.data,self.session,restore=p['id'])

    def test_write_failure_retains_preimage_and_blocks_blind_retry(self):
        p=self.plan();a=self.approval(p)
        def fail(fd,content):os.write(fd,b'partial');raise OSError('disk failure')
        with patch.object(rollback,'write_content',side_effect=fail),self.assertRaises(OSError):
            rollback.apply(self.data,self.session,p['id'],a)
        stored=self.state()['rollbacks'][p['id']]
        self.assertEqual(stored['status'],'write_outcome_unknown');self.assertEqual(stored['before'],EDIT)
        with self.assertRaises(rollback.Refused):self.plan()
        with self.assertRaises(rollback.Refused):rollback.apply(self.data,self.session,p['id'],a)

    def test_journal_failure_before_write_preserves_file(self):
        p=self.plan();a=self.approval(p)
        with patch.object(rollback,'atomic_write',side_effect=OSError('disk full')),self.assertRaises(OSError):
            rollback.apply(self.data,self.session,p['id'],a)
        self.assertEqual(self.file.read_text(),EDIT)
        self.assertEqual(self.state()['rollbacks'][p['id']]['status'],'prepared')

    def test_final_journal_failure_keeps_durable_applying_record_and_preimage(self):
        p=self.plan();a=self.approval(p);real=rollback.atomic_write;count=[]
        def persist(path,state):
            count.append(1)
            if len(count)==2:raise OSError('disk full after file write')
            return real(path,state)
        with patch.object(rollback,'atomic_write',side_effect=persist),self.assertRaises(OSError):rollback.apply(self.data,self.session,p['id'],a)
        s=self.state()['rollbacks'][p['id']];self.assertEqual(s['status'],'applying');self.assertEqual(s['before'],EDIT)
        with self.assertRaises(rollback.Refused):self.plan()

    def test_change_during_journal_is_detected_before_write(self):
        p=self.plan();a=self.approval(p);real=rollback.atomic_write
        def concurrent(path,state):
            real(path,state)
            if state['rollbacks'][p['id']]['status']=='applying':self.file.write_text('concurrent content\n')
        with patch.object(rollback,'atomic_write',side_effect=concurrent),self.assertRaises(rollback.Refused):rollback.apply(self.data,self.session,p['id'],a)
        self.assertEqual(self.file.read_text(),'concurrent content\n')

    def test_mode_and_inode_survive(self):
        self.file.chmod(0o744);inode=self.file.stat().st_ino
        p=self.plan();self.apply(p)
        self.assertEqual(stat.S_IMODE(self.file.stat().st_mode),0o744);self.assertEqual(self.file.stat().st_ino,inode)

    def test_one_use_and_bounded_records_no_silent_eviction(self):
        p=self.plan();a=self.approval(p);rollback.apply(self.data,self.session,p['id'],a)
        with self.assertRaises(rollback.Refused):rollback.apply(self.data,self.session,p['id'],a)
        self.file.write_text(EDIT)
        for _ in range(rollback.MAX_PLANS-1):self.plan()
        with self.assertRaises(rollback.Refused):self.plan()
        self.assertIn(p['id'],self.state()['rollbacks'])

    def test_feedback_once_via_real_hook_no_stop_forced_turn_or_receipt_claim(self):
        p=self.plan();self.apply(p)
        self.assertEqual(self.hook('Stop'),{})
        out=self.hook('PreToolUse');self.assertIn(p['id'],out['hookSpecificOutput']['additionalContext'])
        self.assertNotIn('decision',out);self.assertIsNone(self.hook('PreToolUse'))
        self.assertEqual(self.state()['rollbacks'][p['id']]['notice'],'emitted_unconfirmed')
        r=report.generate(self.data,self.session);self.assertEqual(r['rollbacks'][0]['status'],'applied')
        self.assertIn(p['id'],report.markdown(r))
        self.assertEqual(self.state()['budget']['reminders_used'],0)

    def test_rollback_feedback_preserves_existing_scope_advisory(self):
        p=self.plan();self.apply(p)
        with patch('reminders.on_hook',return_value={'hookSpecificOutput':{'hookEventName':'PreToolUse','additionalContext':'existing advisory'}}):
            out=self.hook('PreToolUse')
        value=out['hookSpecificOutput']['additionalContext']
        self.assertIn('existing advisory',value);self.assertIn(p['id'],value)

    def test_cooperating_editor_lock_prevents_write(self):
        import fcntl
        p=self.plan();a=self.approval(p)
        with self.file.open('r+b') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            with self.assertRaises(OSError):rollback.apply(self.data,self.session,p['id'],a)
        self.assertEqual(self.file.read_text(),EDIT)

    def test_paused_feedback_does_not_emit_or_start_models(self):
        p=self.plan();self.apply(p);(self.data/'disabled').touch()
        self.assertIsNone(self.hook('PreToolUse'));self.assertEqual(self.state()['rollbacks'][p['id']]['notice'],'pending')

    def test_expired_status_removes_recovery_data_and_never_recreates_task(self):
        p=self.plan();s=self.state();s['expires_at']=0;tasks.atomic_write(tasks.paths(self.data,self.session)[1],s)
        self.assertEqual(rollback.status(self.data,self.session)['status'],'task_unavailable')
        self.assertFalse(tasks.paths(self.data,self.session)[1].exists());self.assertEqual(self.file.read_text(),EDIT)


if __name__=='__main__':unittest.main()
