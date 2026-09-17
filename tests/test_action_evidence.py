import json
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/navi/scripts"))
import action_evidence as actions
import task_state as tasks
import review


class ActionEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.workspace = self.root / "workspace"
        self.workspace.mkdir()
        self.file = self.workspace / "panel.py"
        self.file.write_text('title = "old"\nbackend = None\n')
        self.session = "evidence-test"
        tasks.on_hook(self.data, {"session_id": self.session, "turn_id": "t1", "cwd": str(self.workspace),
            "hook_event_name": "UserPromptSubmit", "prompt": "Navi: start\nOnly change title. No backend wiring."})

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return tasks.read_state(tasks.paths(self.data, self.session)[1])

    def enable(self, names=None):
        actions.configure(self.data, self.session, str(self.workspace), names or ["panel.py"])

    def hook(self, event="PreToolUse", tool_id="tool1", **kwargs):
        actions.on_hook(self.data, dict(session_id=self.session, turn_id="t1", cwd=str(self.workspace),
            hook_event_name=event, tool_use_id=tool_id, tool_name="shell", tool_input={"cmd": "edit"}, **kwargs))

    def test_task_capture_needs_separate_optin(self):
        self.hook()
        self.assertNotIn("action_evidence", self.state())
        self.enable()
        self.hook()
        self.assertEqual(len(self.state()["action_evidence"]["events"]), 1)
        self.assertEqual(self.state()["budget"]["reviews_used"], 0)

    def test_same_file_cumulative_changes_and_source_provenance(self):
        self.enable()
        self.hook()
        self.file.write_text('title = "new"\nbackend = None\n')
        self.hook("PostToolUse", tool_response="done")
        self.file.write_text('title = "new"\nbackend = connect()\n')
        self.hook("PostToolUse", "tool2", tool_response="done")
        packet = actions.export(self.state())
        observed = next(a for a in packet["actions"] if a["kind"] == "workspace_observation")
        body = json.loads(observed["evidence"])
        self.assertIn('-title = "old"', body["cumulative_diff"])
        self.assertIn('+backend = connect()', body["cumulative_diff"])
        self.assertIn('unknown', body['attribution'])
        self.assertEqual(body['user_context_digest'], tasks.user_context_digest(self.state()))
        self.assertEqual(packet['coverage'], 'partial')

    def test_explicit_watch_expansion_does_not_reset_baselines_or_budget(self):
        self.enable()
        original = self.state()["action_evidence"]["files"]["panel.py"]["baseline"]
        self.file.write_text('changed\n')
        actions.configure(self.data, self.session, str(self.workspace), ['panel.py', 'new.py'])
        self.hook()
        state = self.state()
        self.assertEqual(state['action_evidence']['files']['panel.py']['baseline'], original)
        self.assertEqual(state['action_evidence']['files']['new.py']['baseline']['status'], 'absent')
        self.assertEqual(len(state['action_evidence']['files']), 2)
        self.assertEqual(state['budget']['reviews_used'], 0)

    def test_missing_binary_oversized_symlinks_and_outside_are_explicit(self):
        (self.workspace/'large').write_text('x'*(actions.MAX_FILE_BYTES+1))
        (self.workspace/'binary').write_bytes(b'x\x00y')
        (self.root/'secret').write_text('secret')
        (self.workspace/'link').symlink_to(self.root/'secret')
        (self.workspace/'directory-link').symlink_to(self.root, target_is_directory=True)
        for name, status in [('large','oversized'),('binary','binary'),('missing','absent'),
                ('link','unreadable_or_symlink'),('directory-link/secret','unreadable_or_symlink'),('../secret','unsafe_path')]:
            snap = actions.snapshot(str(self.workspace), name)
            self.assertEqual(snap['status'],status)
            self.assertNotIn('secret', json.dumps(snap))

    def test_capture_bounds_and_explicit_eviction(self):
        self.enable()
        for i in range(actions.MAX_EVENTS+3):
            self.hook('PostToolUse', str(i), tool_response='x'*5000)
        state = self.state()
        self.assertEqual(len(state['action_evidence']['events']),actions.MAX_EVENTS)
        self.assertEqual(state['action_evidence']['dropped_events'],3)
        self.assertTrue(state['action_evidence']['events'][0]['response']['truncated'])
        exported = actions.export(state)
        manifest = json.loads(exported['actions'][0]['evidence'])
        self.assertTrue(manifest['omitted_from_packet'])
        self.assertLess(len(json.dumps(exported).encode()), review.MAX_PACKET_BYTES)
        self.assertLessEqual(len(exported['actions']),32)

    def test_duplicate_hook_does_not_add_event_but_detects_external_edit(self):
        self.enable()
        self.hook()
        rev = actions.version(self.state())
        self.hook()
        self.assertEqual(actions.version(self.state()),rev)
        self.file.write_text('external edit\n')
        self.hook()
        self.assertGreater(actions.version(self.state())[0],rev[0])
        self.assertEqual(len(self.state()['action_evidence']['events']),1)

    def test_self_correction_during_review_marks_result_stale(self):
        self.enable()
        self.hook()
        self.file.write_text('backend = connect()\n')
        key, _ = review.reserve(self.data,self.session,None)
        def runner(*args, **kwargs):
            self.file.write_text('title = "old"\nbackend = None\n')
            return {'result':'fixture'}
        review.worker(self.data,self.session,key,runner)
        self.assertTrue(self.state()['reviews'][key]['stale'])

    def test_external_edit_before_worker_prevents_old_call(self):
        self.enable()
        key, _ = review.reserve(self.data,self.session,None)
        self.file.write_text('external change\n')
        review.worker(self.data,self.session,key,lambda *a, **k:self.fail('stale must not call'))
        self.assertEqual(self.state()['reviews'][key]['status'],'superseded_before_call')

    def test_new_tool_and_phase_change_invalidate_review(self):
        self.enable()
        key, _ = review.reserve(self.data,self.session,None)
        self.hook()
        self.assertTrue(review.is_stale(self.state(),self.state()['reviews'][key]))
        tasks.on_hook(self.data,{'session_id':self.session,'turn_id':'t2','hook_event_name':'UserPromptSubmit',
            'prompt':'Now wire backend.'})
        self.assertTrue(review.is_stale(self.state(),self.state()['reviews'][key]))
        self.assertEqual(self.state()['budget']['reviews_used'],1)

    def test_paused_or_disabled_capture_is_unchanged(self):
        self.enable()
        before = self.state()
        (self.data/'disabled').touch()
        self.file.write_text('not captured\n')
        self.hook()
        self.assertEqual(before,self.state())

    def test_risk_separate_from_scope_and_both_require_real_evidence(self):
        evidence = {'coverage':'complete_for_checkpoint','actions':[{'id':'a1','kind':'diff','path':'panel.py',
            'origin':'fixture','evidence':'if x == 42: return success'}]}
        packet = review.make_packet(self.state(), evidence)
        ids = [packet['user_sources'][0]['id'],'a1']
        result = {'results':[{'case_id':packet['case_id'],'verdict':'within_scope','scope_labels':[],
            'evidence_ids':ids,'reason':'in scope','implementation_risk':{'verdict':'concern',
            'labels':['narrow_hardcoding','overengineering'],'evidence_ids':ids,'reason':'risk to requirement'}}]}
        self.assertEqual(review.validate_results(result,[packet])['results'][0]['verdict'],'within_scope')
        result['results'][0]['implementation_risk']['evidence_ids'] = ['invented']
        with self.assertRaises(ValueError):
            review.validate_results(result,[packet])

    def test_gap_after_queue_prevents_call_and_late_gap_marks_stale(self):
        self.enable()
        key, _ = review.reserve(self.data, self.session, None)
        gap = tasks.paths(self.data, self.session)[1].with_suffix('.gap')
        gap.touch()
        review.worker(self.data, self.session, key, lambda *a, **k: self.fail('gap must not spend'))
        self.assertEqual(self.state()['reviews'][key]['error_type'], 'CaptureGap')
        gap.unlink()
        self.hook()
        key, _ = review.reserve(self.data, self.session, None)
        def runner(*a, **k):
            gap.touch()
            return {}
        review.worker(self.data, self.session, key, runner)
        self.assertTrue(self.state()['reviews'][key]['stale'])

    def test_manual_evidence_cache_also_tracks_new_actions(self):
        self.enable()
        evidence = actions.export(self.state())
        old, _ = review.reserve(self.data, self.session, evidence)
        self.hook()
        with self.assertRaisesRegex(review.budget.ReviewDeferred, 'review_in_progress'):
            review.reserve(self.data, self.session, evidence)
        review.worker(self.data, self.session, old, lambda *a, **k: {})
        new, launch = review.reserve(self.data, self.session, evidence)
        self.assertNotEqual(old, new)
        self.assertTrue(launch)

    def test_only_manifest_is_not_reviewable_and_spends_nothing(self):
        actions.configure(self.data, self.session, str(self.workspace), [])
        with self.assertRaises(ValueError):
            review.reserve(self.data, self.session, None)
        self.assertEqual(self.state()['budget']['reviews_used'], 0)

    def test_newline_change_is_not_lost_or_merged_into_diff_lines(self):
        self.file.write_text('old')
        self.enable()
        self.file.write_text('new\n')
        self.hook()
        entry = self.state()['action_evidence']['files']['panel.py']
        observed = actions.file_observation('panel.py', entry)
        self.assertIn('-old\n+new', observed['cumulative_diff'])
        self.assertFalse(observed['baseline_final_newline'])
        self.assertTrue(observed['current_final_newline'])

    def test_task_expiry_removes_embedded_evidence(self):
        self.enable()
        path = tasks.paths(self.data, self.session)[1]
        state = self.state()
        state['expires_at'] = 0
        tasks.atomic_write(path, state)
        with tasks.locked(self.data, self.session) as (_, expired):
            self.assertIsNone(expired)
        self.assertFalse(path.exists())

    def test_watch_capacity_rejected_without_partial_state_update(self):
        self.enable()
        before = self.state()
        with self.assertRaises(ValueError):
            actions.configure(self.data, self.session, str(self.workspace), [str(i) for i in range(actions.MAX_FILES)])
        self.assertEqual(before, self.state())

    def test_partial_coverage_cannot_clear_either_axis(self):
        self.enable()
        packet = review.make_packet(self.state(),actions.export(self.state()))
        ids = [packet['user_sources'][0]['id'],packet['actions'][1]['id']]
        result = {'results':[{'case_id':packet['case_id'],'verdict':'within_scope','scope_labels':[],
            'evidence_ids':ids,'reason':'fixture','implementation_risk':{'verdict':'none_observed',
            'labels':[],'evidence_ids':ids,'reason':'fixture'}}]}
        result = review.validate_results(result,[packet])['results'][0]
        self.assertEqual(result['verdict'],'uncertain')
        self.assertEqual(result['implementation_risk']['verdict'],'uncertain')
        result['evidence_ids'] = [ids[0],packet['actions'][0]['id']]
        with self.assertRaises(ValueError):
            review.validate_results({'results':[result]},[packet])


if __name__ == '__main__':
    unittest.main()
