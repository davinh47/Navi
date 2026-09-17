"""Offline checks prevent smoke false positives and unnecessary paid retries."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / "tools/run_live_smoke.py"
spec = importlib.util.spec_from_file_location("smoke", SCRIPT)
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        (self.directory / "workspace").mkdir()
        (self.directory / "workspace/fixture.txt").write_text("after\n")
        self.stream = [{"type": "thread.started", "thread_id": "test"},
                       {"type": "turn.started"}]
        for item in (
            {"type": "command_execution", "exit_code": 0, "aggregated_output": "/fixture\n"},
            {"type": "file_change"},
            {"type": "command_execution", "exit_code": 0, "aggregated_output": "navi-long-done\n"},
            {"type": "command_execution", "exit_code": 7},
            {"type": "agent_message", "text": "NAVI_SMOKE_OK"},
        ):
            self.stream.append({"type": "item.completed", "item": item})
        self.stream.append({"type": "turn.completed"})
        self.records = [{"event": e, "session_id": "test", "stop_hook_active": False}
                        for e in ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")]
        self.records.extend({"event": e, "session_id": "test", "tool_use_id": str(i)}
                            for i in range(4) for e in ("PreToolUse", "PostToolUse"))

    def tearDown(self):
        self.temp.cleanup()

    def evaluate(self):
        (self.directory / "stdout.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.stream))
        return smoke.evaluate(self.directory, self.records, 0, False)

    def test_completed_path_and_offline_replay(self):
        report = self.evaluate()
        self.assertEqual(report["status"], "passed")
        (self.directory / "report.json").write_text(json.dumps(report))
        (self.directory / "hook-events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in self.records))
        result = subprocess.run([sys.executable, str(SCRIPT), "--replay", str(self.directory),
                                 "--codex", "/does/not/exist"], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.directory / "replay-report.json").exists())

    def test_missing_duplicate_or_mismatched_hooks_fail(self):
        original = self.records[:]
        for records in (original[:-1], original + [original[-1]],
                        original[:-1] + [dict(original[-1], tool_use_id="other")],
                        [dict(e, session_id="another") for e in original]):
            with self.subTest(records=records):
                self.records = records
                self.assertEqual(self.evaluate()["status"], "incomplete_or_failed")

    def test_extra_turn_and_stop_continuation_fail(self):
        self.stream.append({"type": "turn.started"})
        self.assertFalse(self.evaluate()["checks"]["single_completed_turn"])
        self.stream.pop()
        self.records[2]["stop_hook_active"] = True
        self.assertFalse(self.evaluate()["checks"]["no_stop_continuation_observed"])

    def test_incomplete_stream_and_missing_final_fail(self):
        self.stream.pop()
        self.assertEqual(self.evaluate()["status"], "incomplete_or_failed")
        self.stream.append({"type": "turn.completed"})
        self.stream[-2]["item"]["text"] = "Could not finish"
        self.assertFalse(self.evaluate()["checks"]["final_marker"])

    def test_malformed_jsonl_is_not_silently_accepted(self):
        path = self.directory / "stdout.jsonl"
        path.write_text('{}\n{broken\n')
        with self.assertRaises(ValueError):
            smoke.read_jsonl(path)

    def test_proxy_import_preserves_explicit_environment_and_parent(self):
        env = {"http_proxy": "http://explicit", "NO_PROXY": "private.test"}
        original = env.copy()
        child = smoke.child_environment(env, {"http": "http://system", "https": "http://system"})
        self.assertEqual(child["http_proxy"], "http://explicit")
        self.assertNotIn("HTTP_PROXY", child)
        self.assertEqual(child["HTTPS_PROXY"], "http://system")
        self.assertEqual(child["NO_PROXY"], "private.test")
        self.assertEqual(env, original)

    def test_paused_recorder_exits_before_launch(self):
        (self.directory / "disabled").touch()
        result = subprocess.run([sys.executable, str(SCRIPT), "--data-directory", str(self.directory),
                                 "--codex", "/does/not/exist"], capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 2)  # Test CLI argument error, never a Hook response.
        self.assertIn(b"recording is paused", result.stderr)


if __name__ == "__main__":
    unittest.main()
