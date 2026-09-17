import concurrent.futures
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/navi/scripts"))
import task_state as tasks
import review


def dual(value):
    for result in value["results"]:
        result["scope_labels"] = []
        result["implementation_risk"] = {"verdict": "none_observed", "labels": [],
            "reason": "fixture", "evidence_ids": list(result["evidence_ids"])}
    return value


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.session = "review-task"
        tasks.on_hook(self.data, {"session_id": self.session, "turn_id": "t1", "hook_event_name": "UserPromptSubmit",
                       "prompt": "Navi: start\nFix auth; leave payments alone."})
        self.evidence = {"coverage": "complete_for_checkpoint", "actions": [
            {"id": "a1", "kind": "write", "path": "auth.py", "evidence": "timeout = 5", "origin": "test_fixture"}]}

    def tearDown(self):
        self.temp.cleanup()

    def state(self):
        return tasks.read_state(tasks.paths(self.data, self.session)[1])

    def test_schema_requires_two_references_but_validator_still_checks_both_roles(self):
        result_schema = review.SCHEMA["properties"]["results"]["items"]["properties"]
        self.assertEqual(result_schema["evidence_ids"]["minItems"], 2)
        self.assertEqual(result_schema["implementation_risk"]["properties"]["evidence_ids"]["minItems"], 2)
        packet = review.make_packet(self.state(), self.evidence)
        source = packet["user_sources"][0]["id"]
        for ids in (["a1"], ["a1", "a1"], [source, source]):
            value = dual({"results": [{"case_id": packet["case_id"], "verdict": "within_scope",
                "evidence_ids": [source, "a1"], "reason": "test"}]})
            value["results"][0]["implementation_risk"]["evidence_ids"] = ids
            with self.assertRaises(ValueError):
                review.validate_results(value, [packet])

    def test_repeat_submission_and_parallel_reservation_spend_once(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            replies = list(pool.map(lambda _: review.reserve(self.data, self.session, self.evidence), range(8)))
        self.assertEqual(sum(new for _, new in replies), 1)
        self.assertEqual(len(set(key for key, _ in replies)), 1)
        self.assertEqual(self.state()["budget"]["reviews_used"], 1)

    def test_concurrent_distinct_reservations_obey_shared_hourly_limit(self):
        import copy
        def reserve(index):
            evidence=copy.deepcopy(self.evidence)
            evidence['actions'][0]['evidence']='Concurrent checkpoint '+str(index)
            try:
                return review.reserve(self.data,self.session,evidence)[1]
            except ValueError as error:
                self.assertIn('hourly_review_limit',str(error))
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results=list(pool.map(reserve,range(10)))
        self.assertEqual(sum(results),6)
        self.assertEqual(self.state()['budget']['reviews_used'],6)

    def test_budget_survives_new_user_input_and_failures(self):
        state=self.state();state["budget"].update(review_limit=3,limit_origin="explicit")
        tasks.save(tasks.paths(self.data,self.session)[1],state)
        for i in range(3):
            self.evidence["actions"][0]["evidence"] = "change " + str(i)
            key, _ = review.reserve(self.data, self.session, self.evidence)
            def fail(*args, **kwargs):
                raise TimeoutError("fixture")
            review.worker(self.data, self.session, key, fail)
            self.assertEqual(self.state()["reviews"][key]["status"], "failed")
        tasks.on_hook(self.data, {"session_id": self.session, "turn_id": "t2", "hook_event_name": "UserPromptSubmit", "prompt": "接着做"})
        with self.assertRaises(ValueError):
            review.reserve(self.data, self.session, self.evidence)
        self.assertEqual(self.state()["budget"]["reviews_used"], 3)

    def test_worker_does_not_lock_main_task_while_model_runs_and_marks_stale(self):
        key, _ = review.reserve(self.data, self.session, self.evidence)
        def concurrent_update(*args, **kwargs):
            tasks.on_hook(self.data, {"session_id": self.session, "turn_id": "t2", "hook_event_name": "UserPromptSubmit", "prompt": "改成修支付"})
            return {"result": "fixture"}
        review.worker(self.data, self.session, key, concurrent_update)
        self.assertTrue(self.state()["reviews"][key]["stale"])
        self.assertEqual(self.state()["sources"][-1]["text"], "改成修支付")

    def test_late_worker_cannot_recreate_deleted_task(self):
        key, _ = review.reserve(self.data, self.session, self.evidence)
        def deleted(*args, **kwargs):
            tasks.paths(self.data, self.session)[1].unlink()
            return {}
        review.worker(self.data, self.session, key, deleted)
        self.assertIsNone(self.state())

    def test_source_and_action_ids_required_in_model_result(self):
        packet = review.make_packet(self.state(), self.evidence)
        value = {"results": [{"case_id": packet["case_id"], "verdict": "drift", "reason": "fixture",
                              "evidence_ids": [packet["user_sources"][0]["id"], "invented"]}]}
        with self.assertRaises(ValueError):
            review.validate_results(dual(value), [packet])
        value["results"][0]["evidence_ids"][-1] = "a1"
        self.assertEqual(review.validate_results(dual(value), [packet]), value)

    def test_partial_evidence_cannot_be_reported_clean(self):
        packet = review.make_packet(self.state(), dict(self.evidence, coverage="partial"))
        value = {"results": [{"case_id": packet["case_id"], "verdict": "within_scope", "reason": "fixture",
                              "evidence_ids": [packet["user_sources"][0]["id"], "a1"]}]}
        self.assertEqual(review.validate_results(dual(value), [packet])["results"][0]["verdict"], "uncertain")

    def test_large_packet_and_gap_rejected_before_spending(self):
        self.evidence["actions"][0]["evidence"] = "x" * (review.MAX_PACKET_BYTES + 1)
        with self.assertRaises(ValueError):
            review.reserve(self.data, self.session, self.evidence)
        self.evidence["actions"][0]["evidence"] = "small"
        tasks.paths(self.data, self.session)[1].with_suffix(".gap").touch()
        with self.assertRaises(ValueError):
            review.reserve(self.data, self.session, self.evidence)
        self.assertEqual(self.state()["budget"]["reviews_used"], 0)

    def test_packets_preserve_all_raw_sources_and_label_interpretation(self):
        tasks.on_hook(self.data, {"session_id": self.session, "turn_id": "t2", "hook_event_name": "UserPromptSubmit", "prompt": "支付也改，但别重构"})
        packet = review.make_packet(self.state(), self.evidence)
        self.assertEqual(len(packet["user_sources"]), 2)
        self.assertEqual(packet["user_sources"][-1]["text"], "支付也改，但别重构")

    def test_no_active_task_or_disabled_recording_spends_nothing(self):
        (self.data / "disabled").touch()
        with self.assertRaises(ValueError):
            review.reserve(self.data, self.session, self.evidence)
        self.assertEqual(self.state()["budget"]["reviews_used"], 0)

    def test_pause_before_worker_prevents_model_call(self):
        key, _ = review.reserve(self.data, self.session, self.evidence)
        (self.data / "disabled").touch()
        def forbidden(*args, **kwargs):
            self.fail("model must not start")
        review.worker(self.data, self.session, key, forbidden)
        self.assertEqual(self.state()["reviews"][key]["status"], "cancelled_before_call")

    def test_new_input_before_worker_avoids_stale_call(self):
        key, _ = review.reserve(self.data, self.session, self.evidence)
        tasks.on_hook(self.data, {"session_id": self.session, "turn_id": "t2", "hook_event_name": "UserPromptSubmit", "prompt": "换个目标"})
        def forbidden(*args, **kwargs):
            self.fail("stale review must not start")
        review.worker(self.data, self.session, key, forbidden)
        self.assertEqual(self.state()["reviews"][key]["status"], "superseded_before_call")

    def test_workspace_disable_prevents_pending_and_new_reviews(self):
        tasks.workspace_policy(self.data, str(self.data), True)
        state = self.state()
        state["capture_workspace"] = str(self.data)
        tasks.atomic_write(tasks.paths(self.data, self.session)[1], state)
        key, _ = review.reserve(self.data, self.session, self.evidence)
        tasks.workspace_policy(self.data, str(self.data), False)
        with self.assertRaises(ValueError):
            review.reserve(self.data, self.session, self.evidence)
        review.worker(self.data, self.session, key, lambda *a, **k: self.fail("must not call model"))
        self.assertEqual(self.state()["reviews"][key]["status"], "cancelled_before_call")

    def fake_cli(self, valid=True):
        packet = review.make_packet(self.state(), self.evidence)
        result = {"results": [{"case_id": packet["case_id"], "verdict": "within_scope", "reason": "fixture",
            "evidence_ids": [packet["user_sources"][0]["id"], "a1" if valid else "invented"]}]}
        events = [{"type": "item.completed", "item": {"type": "error", "message": "skills budget diagnostic"}},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(dual(result))}},
                  {"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 3}}]
        executable = self.data / "fake-codex"
        executable.write_text("#!" + sys.executable + "\nimport sys\nsys.stdin.read()\nprint(" +
                              repr("\n".join(json.dumps(e) for e in events)) + ")\n")
        executable.chmod(0o700)
        return executable, packet

    def test_completed_cli_diagnostic_is_not_tool_use(self):
        executable, packet = self.fake_cli()
        with patch.object(review.shutil, "which", return_value=str(executable)):
            result = review.run_model([packet])
        self.assertEqual(result["tool_items"], 0)
        self.assertEqual(result["diagnostics"], ["skills budget diagnostic"])

    def test_invalid_model_references_preserve_usage_on_failure(self):
        executable, packet = self.fake_cli(valid=False)
        with patch.object(review.shutil, "which", return_value=str(executable)):
            with self.assertRaises(review.ReviewFailure) as caught:
                review.run_model([packet])
        self.assertEqual(caught.exception.diagnostics["usage"]["input_tokens"], 12)


if __name__ == "__main__":
    unittest.main()
