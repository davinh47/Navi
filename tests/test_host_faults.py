import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
from verify_host_faults import evaluate


class HostFaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.events = [{"method": "turn/started", "params": {"threadId": "test"}}]
        self.trace = [{"event": event, "session_id": "test"} for event in ("Stop", "SessionEnd")]
        for mode in ("EXIT1", "UNWRITABLE", "TIMEOUT", "NORMAL"):
            self.events.append({"method": "item/completed", "params": {"threadId": "test", "item": {
                "type": "commandExecution", "aggregatedOutput": "NAVI_FAULT_" + mode,
                "status": "completed", "exitCode": 0}}})
            for event, name in (("PreToolUse", "preToolUse"), ("PostToolUse", "postToolUse")):
                self.trace.append({"event": event, "mode": mode, "session_id": "test"})
                self.events.append({"method": "hook/completed", "params": {"threadId": "test", "run": {
                    "eventName": name, "source": "sessionFlags",
                    "status": "completed" if mode == "NORMAL" else "failed",
                    "durationMs": 3002 if mode == "TIMEOUT" else 25,
                    "entries": [] if mode == "NORMAL" else [{"kind": "error",
                        "text": "hook timed out after 3s" if mode == "TIMEOUT" else "hook exited with code 1"}]}}})
        self.events.append({"method": "hook/completed", "params": {"threadId": "test", "run": {
            "eventName": "stop", "source": "sessionFlags", "status": "completed", "entries": []}}})
        self.events.append({"method": "turn/completed", "params": {"threadId": "test", "turn": {
            "status": "completed", "error": None, "items": [{"text": "NAVI_HOST_FAULTS_OK"}]}}})

    def tearDown(self):
        self.temp.cleanup()

    def check(self):
        for name, records in (("host-events.jsonl", self.events), ("fault-trace.jsonl", self.trace)):
            (self.directory / name).write_text("".join(json.dumps(e) + "\n" for e in records))
        return evaluate(self.directory)

    def test_success_requires_visible_faults_and_real_tool_results(self):
        self.assertEqual(self.check()["status"], "passed")
        self.events[1]["params"]["item"]["exitCode"] = 7
        self.assertEqual(self.check()["status"], "failed")

    def test_missing_fault_or_mixed_session_cannot_pass(self):
        original = copy.deepcopy(self.events)
        self.events.pop(2)
        self.assertEqual(self.check()["status"], "failed")
        self.events = original
        self.trace[0]["session_id"] = "other-session"
        self.assertFalse(self.check()["checks"]["same_test_session"])

    def test_surviving_timeout_descendant_fails(self):
        (self.directory / "PreToolUse-timeout-survived").touch()
        self.assertFalse(self.check()["checks"]["no_delayed_child_writes"])

    def test_fixture_invokes_recorder_and_real_permission_failure(self):
        denied = self.directory / "unwritable"
        denied.mkdir(mode=0o500)
        try:
            for mode, expected in (("NORMAL", 0), ("EXIT1", 1), ("UNWRITABLE", 1)):
                event = {"hook_event_name": "PreToolUse", "session_id": "fixture-test",
                         "tool_input": {"cmd": "printf NAVI_FAULT_" + mode}}
                result = subprocess.run([sys.executable, str(ROOT / "tools/host_fault_hook.py"),
                    str(self.directory), str(ROOT / "plugins/navi/scripts/record_event.py")],
                    input=json.dumps(event), text=True, capture_output=True, timeout=5)
                self.assertEqual(result.returncode, expected, result.stderr)
                self.assertEqual(result.stdout, "")
                if mode == "UNWRITABLE":
                    self.assertIn("PermissionError", result.stderr)
            self.assertTrue(list((self.directory / "data/events").glob("*.jsonl")))
        finally:
            denied.chmod(0o700)


if __name__ == "__main__":
    unittest.main()
