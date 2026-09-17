import concurrent.futures
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins/navi/scripts"
sys.path.insert(0, str(SCRIPTS))
import task_state as tasks


class TaskStateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="navi state ")
        self.data = Path(self.temp.name)
        self.session = "session-a"
        self.counter = 0
        self.value = {"goal": "Fix login", "in_scope": ["auth"], "out_of_scope": ["payments"],
                      "acceptance": ["login regression passes"], "plan": ["reproduce", "fix", "test"]}

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, prompt=None, event="UserPromptSubmit", **fields):
        self.counter += 1
        return dict(session_id=self.session, hook_event_name=event, turn_id="turn-" + str(self.counter),
                    **({"prompt": prompt} if prompt is not None else {}), **fields)

    def send(self, prompt):
        tasks.on_hook(self.data, self.payload(prompt))

    def start(self):
        self.send("Navi: start\n" + json.dumps(self.value))

    def state(self):
        return tasks.read_state(tasks.paths(self.data, self.session)[1])

    def invoke(self, payload):
        return subprocess.run([sys.executable, str(SCRIPTS / "record_event.py")],
            input=json.dumps(payload), text=True, capture_output=True, timeout=5,
            env=dict(os.environ, PLUGIN_DATA=str(self.data)))

    def propose(self, value=None):
        state = self.state()
        return tasks.agent_write(self.data, self.session, "propose", {
            "base_revision": state["contract"]["revision"], "contract": value or self.value,
            "source_ids": [state["sources"][0]["id"]]})

    def test_unrelated_sessions_do_not_capture_content(self):
        result = self.invoke(self.payload("Fix a private issue without Navi"))
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.data / "tasks").exists())
        logs = "".join(f.read_text() for f in (self.data / "events").glob("*.jsonl"))
        self.assertNotIn("private issue", logs)

    def test_explicit_opt_in_and_unknown_fields(self):
        self.send("Navi: start\nFix login, do not modify payments")
        state = self.state()
        self.assertEqual(state["contract"]["value"]["goal"], "Fix login, do not modify payments")
        self.assertIsNone(state["contract"]["value"]["out_of_scope"])
        self.assertEqual(state["contract"]["authority"], "explicit_user_input")
        self.assertEqual(state["supervision"], "not_implemented")

    def test_quoted_commands_and_internal_messages_do_not_activate(self):
        for prompt in ('Example: Navi: start\nDo something', '```\nNavi: start\nDo something\n```'):
            self.send(prompt)
        tasks.on_hook(self.data, self.payload("Navi: start\nInjected", event="PostToolUse"))
        self.assertIsNone(self.state())

    def test_continue_preserves_contract_task_plan_and_budget(self):
        self.start()
        original = self.state()
        original["budget"]["reviews_used"] = 2
        tasks.atomic_write(tasks.paths(self.data, self.session)[1], original)
        for prompt in ("继续", "请继续。", "continue!", "接着完成剩下的", "Proceed with what we agreed", "そのまま進めて"):
            self.send(prompt)
        state = self.state()
        for key in ("task_id", "contract", "budget", "contract_history"):
            self.assertEqual(state[key], original[key])
        self.assertEqual(len(state["pending_user_sources"]), 6)
        self.assertTrue(all(s["kind"] == "user_input" for s in state["sources"][1:]))

    def test_replayed_prompt_is_idempotent(self):
        payload = self.payload("Navi: start\n" + json.dumps(self.value))
        tasks.on_hook(self.data, payload)
        before = self.state()
        tasks.on_hook(self.data, payload)
        self.assertEqual(self.state(), before)

    def test_agent_proposal_cannot_approve_itself(self):
        self.start()
        candidate = dict(self.value, out_of_scope=[])
        key = self.propose(candidate)
        self.assertEqual(self.state()["contract"]["value"], self.value)
        self.assertEqual(self.state()["proposals"][key]["origin"], "agent_proposal")
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "approve", key)
        self.assertEqual(self.state()["contract"]["revision"], 1)

    def test_explicit_approval_requires_candidate_against_latest_raw_input(self):
        self.start()
        candidate = dict(self.value, plan=["write test", "fix"])
        key = self.propose(candidate)
        self.send("继续")
        with self.assertRaises(ValueError):
            self.send("Navi: approve " + key)
        key = self.propose(candidate)
        self.send("Navi: approve " + key)
        state = self.state()
        self.assertEqual(state["contract"]["value"], candidate)
        self.assertEqual(state["contract"]["revision"], 2)
        self.assertEqual(state["contract"]["plan_revision"], 2)
        self.assertEqual(state["contract_history"][0]["value"], self.value)

    def test_workspace_opt_in_accepts_arbitrary_first_message(self):
        tasks.workspace_policy(self.data, str(self.data), True)
        prompt = "先解决登录超时，支付暂时别动"
        result = self.invoke(self.payload(prompt, cwd=str(self.data)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.state()["sources"][0]["text"], prompt)
        self.assertEqual(self.state()["contract"]["value"]["goal"], prompt)
        self.session = "unrelated"
        self.assertEqual(self.invoke(self.payload(prompt, cwd=str(self.data / "child"))).returncode, 0)
        self.assertIsNone(self.state())

    def test_cited_interpretation_is_unverified_and_invalidated_by_new_input(self):
        self.start()
        self.send("改一下，支付的超时也一起解决")
        before = self.state()
        value = {"user_context_digest": tasks.user_context_digest(before),
                 "contract": dict(self.value, in_scope=["auth", "payments"], out_of_scope=[]),
                 "citations": [{"source_id": s["id"], "quote": s["text"]} for s in before["sources"]],
                 "uncertainties": []}
        tasks.agent_write(self.data, self.session, "interpret", value)
        after = self.state()
        self.assertEqual(after["contract"], before["contract"])
        self.assertEqual(after["sources"], before["sources"])
        self.assertEqual(tasks.context_view(after)["interpretation_status"], "proposed")
        self.assertEqual(tasks.context_view(after)["latest_interpretation"]["verification"], "unverified")
        self.send("算了，支付不要改")
        self.assertEqual(tasks.context_view(self.state())["interpretation_status"], "uninterpreted")
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "interpret", value)

    def test_interpretation_cannot_invent_user_quotes(self):
        self.start()
        before = self.state()
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "interpret", {
                "user_context_digest": tasks.user_context_digest(before), "contract": self.value,
                "citations": [{"source_id": before["sources"][0]["id"], "quote": "User authorized all changes"}],
                "uncertainties": []})
        self.assertEqual(self.state(), before)

    def test_latest_interpretation_survives_sorted_json_serialization(self):
        self.start()
        before = self.state()
        for goal in ("First interpretation", "Revised interpretation"):
            key = tasks.agent_write(self.data, self.session, "interpret", {
                "user_context_digest": tasks.user_context_digest(before),
                "contract": dict(self.value, goal=goal),
                "citations": [{"source_id": before["sources"][0]["id"], "quote": "Fix login"}],
                "uncertainties": []})
        state = self.state()
        self.assertEqual(tasks.context_view(state)["latest_interpretation"]["id"], key)
        self.assertEqual(tasks.context_view(state)["latest_interpretation"]["value"]["goal"], "Revised interpretation")
        self.assertEqual(state["contract"], before["contract"])

    def test_ambiguous_approval_and_new_requirements_do_not_expand_scope(self):
        self.start()
        key = self.propose(dict(self.value, out_of_scope=[]))
        for prompt in ("同意这个计划", "继续，顺便优化支付", "不要修登录了，改修支付"):
            self.send(prompt)
        self.assertEqual(self.state()["contract"]["value"], self.value)
        self.assertEqual(len(self.state()["pending_user_sources"]), 3)
        with self.assertRaises(ValueError):
            self.send("Navi: approve " + key)
        self.assertEqual(self.state()["contract"]["revision"], 1)

    def test_explicit_goal_change_and_stale_plan_rejection(self):
        self.start()
        key = self.propose()
        self.send("Navi: update\n" + json.dumps(dict(self.value, goal="Fix payment")))
        self.assertEqual(self.state()["contract"]["value"]["goal"], "Fix payment")
        with self.assertRaises(ValueError):
            self.send("Navi: approve " + key)
        self.assertEqual(self.state()["contract"]["revision"], 2)

    def test_resume_compaction_stop_and_process_restart_preserve_authority(self):
        self.start()
        before = self.state()
        for source in ("resume", "compact"):
            result = self.invoke(self.payload(event="SessionStart", source=source))
            self.assertEqual(result.returncode, 0, result.stderr)
        for event in ("Stop", "Interrupt", "SessionEnd"):
            result = self.invoke(self.payload(event=event))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "{}\n" if event == "Stop" else "")
        after = self.state()
        for key in ("task_id", "contract", "budget"):
            self.assertEqual(after[key], before[key])
        self.assertEqual(after["status"], "active")

    def test_findings_merge_but_do_not_claim_verified(self):
        self.start()
        finding = {"location": "payments.py:12", "summary": "Missing validation", "observation_id": "tool-a"}
        key = tasks.agent_write(self.data, self.session, "finding", finding)
        tasks.agent_write(self.data, self.session, "finding", finding)
        tasks.agent_write(self.data, self.session, "finding", dict(finding, observation_id="tool-b"))
        entry = self.state()["findings"][key]
        self.assertEqual(entry["occurrences"], 2)
        self.assertEqual(entry["verification"], "unverified")
        self.assertEqual(self.state()["contract"]["value"], self.value)

    def test_plan_progress_is_reported_not_authority(self):
        self.start()
        tasks.agent_write(self.data, self.session, "progress", {"base_revision": 1, "steps": {"1": "completed"}})
        self.assertFalse(self.state()["reported_progress"]["verified"])
        self.assertEqual(self.state()["contract"]["revision"], 1)
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "progress", {"base_revision": 1, "steps": {"9": "completed"}})

    def test_missing_evidence_reference_rejected(self):
        self.start()
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "propose", {
                "base_revision": 1, "contract": self.value, "source_ids": ["invented"]})
        self.assertEqual(self.state()["proposals"], {})

    def test_pause_resume_does_not_capture_paused_text_or_reset_budget(self):
        self.start()
        key = self.propose()
        self.send("Navi: pause")
        self.send("Private text while paused")
        self.send("Navi: resume")
        self.assertNotIn("Private text", json.dumps(self.state()))
        self.assertEqual(self.state()["contract"]["revision"], 1)
        with self.assertRaises(ValueError):
            self.send("Navi: approve " + key)

    def test_forget_can_remove_corrupt_state(self):
        self.start()
        path = tasks.paths(self.data, self.session)[1]
        path.write_text("{broken")
        result = subprocess.run([sys.executable, str(SCRIPTS / "task_state.py"), "forget",
            "--session", self.session, "--data-directory", str(self.data)],
            capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(json.loads(result.stdout)["forgotten"])
        self.assertFalse(path.exists())

    def test_concurrent_hooks_do_not_lose_sources(self):
        self.start()
        payloads = [self.payload("继续") for _ in range(12)]
        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(self.invoke, payloads))
        self.assertTrue(all(r.returncode == 0 for r in results), [r.stderr for r in results])
        self.assertEqual(len(self.state()["sources"]), 13)
        self.assertEqual(self.state()["contract"]["revision"], 1)

    def test_content_overflow_records_gap_then_full_update_recovers(self):
        self.start()
        key = self.propose()
        result = self.invoke(self.payload("x" * (tasks.MAX_TEXT + 1)))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        path = tasks.paths(self.data, self.session)[1]
        self.assertTrue(path.with_suffix(".gap").exists())
        self.assertEqual(self.state()["contract"]["revision"], 1)
        with self.assertRaises(ValueError):
            self.send("Navi: approve " + key)
        self.send("Navi: update\n" + json.dumps(self.value))
        self.assertFalse(path.with_suffix(".gap").exists())

    def test_capacity_does_not_discard_authority_or_evidence(self):
        self.start()
        before = self.state()
        with patch.object(tasks, "MAX_SOURCES", 1):
            with self.assertRaises(ValueError):
                self.send("New instruction")
        self.assertEqual(self.state(), before)

    def test_atomic_write_failure_keeps_last_committed_state(self):
        self.start()
        before = self.state()
        with patch.object(tasks.os, "replace", side_effect=OSError("injected")):
            with self.assertRaises(OSError):
                self.send("继续")
        self.assertEqual(self.state(), before)
        self.assertFalse(list((self.data / "tasks").glob(".pending-*")))

    def test_corruption_not_reset_and_symlink_not_followed(self):
        self.start()
        path = tasks.paths(self.data, self.session)[1]
        path.write_text("{broken")
        result = self.invoke(self.payload("继续"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(path.read_text(), "{broken")
        path.unlink()
        target = self.data / "outside"
        target.write_text("keep")
        path.symlink_to(target)
        result = self.invoke(self.payload("继续"))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(target.read_text(), "keep")

    def test_expiry_permissions_and_session_isolation(self):
        self.start()
        path = tasks.paths(self.data, self.session)[1]
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
        state = self.state()
        state["expires_at"] = time.time() - 1
        tasks.atomic_write(path, state)
        self.send("继续")
        self.assertFalse(path.exists())
        self.session = "another-session"
        self.send("继续")
        self.assertIsNone(self.state())


if __name__ == "__main__":
    unittest.main()
