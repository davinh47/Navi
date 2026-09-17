import concurrent.futures
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/navi"
SCRIPT = PLUGIN / "scripts/record_event.py"
spec = importlib.util.spec_from_file_location("recorder", SCRIPT)
recorder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(recorder)


class RecorderTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="navi test ")
        self.data = Path(self.temp.name)
        self.env = dict(os.environ, PLUGIN_ROOT=str(PLUGIN), PLUGIN_DATA=str(self.data))

    def tearDown(self):
        self.temp.cleanup()

    def payload(self, event="PreToolUse", **fields):
        return dict(session_id="session-1", hook_event_name=event,
                    cwd=str(self.data), **fields)

    def invoke(self, payload, env=None):
        raw = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
        return subprocess.run([sys.executable, str(SCRIPT)], input=raw,
                              capture_output=True, env=env or self.env, timeout=5)

    def records(self):
        return [json.loads(line) for p in (self.data / "events").glob("*.jsonl*")
                for line in p.read_text().splitlines()]

    def test_packaged_hooks_use_actual_command_and_correct_output(self):
        config = json.loads((PLUGIN / "hooks/hooks.json").read_text())
        self.assertEqual(set(config["hooks"]), recorder.EVENTS)
        # Execute the shipped shell command, including PLUGIN_ROOT expansion and spaces.
        for event, groups in config["hooks"].items():
            handler = groups[0]["hooks"][0]
            self.assertEqual(handler["type"], "command")
            result = subprocess.run(handler["command"], shell=True,
                                    input=json.dumps(self.payload(event)).encode(),
                                    env=self.env, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, b"{}\n" if event == "Stop" else b"")
            self.assertEqual(result.stderr, b"")
        self.assertEqual(len(self.records()), len(recorder.EVENTS))

    def test_raw_content_is_not_stored(self):
        secret = "DO_NOT_STORE_测试_secret"
        result = self.invoke(self.payload("PostToolUse", prompt=secret,
            transcript_path=secret, tool_input={"command": secret},
            tool_response={"output": secret}, last_assistant_message=secret,
            future_unknown_field=secret))
        self.assertEqual(result.returncode, 0)
        stored = json.dumps(self.records(), ensure_ascii=False)
        self.assertNotIn(secret, stored)
        self.assertTrue(self.records()[0]["has_tool_input"])

    def test_packaged_launcher_missing_cache_and_recorder_failure_never_block(self):
        config = json.loads((PLUGIN / "hooks/hooks.json").read_text())
        for event in ("PreToolUse", "Stop"):
            for env, payload in ((dict(self.env, PLUGIN_ROOT=str(self.data / "removed-cache")), self.payload(event)),
                                 (self.env, b"malformed")):
                with self.subTest(event=event, missing=env != self.env):
                    raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
                    command = config["hooks"][event][0]["hooks"][0]["command"]
                    result = subprocess.run(command, shell=True, env=env, input=raw, capture_output=True, timeout=5)
                    self.assertEqual(result.returncode, 0)
                    self.assertEqual(json.loads(result.stdout), {})
                    self.assertTrue(result.stderr)  # Failure is diagnostic, never a verdict.

    def test_pre_post_correlation_and_external_ids(self):
        for event in ("PreToolUse", "PostToolUse"):
            self.assertEqual(self.invoke(self.payload(event, tool_use_id="call-1",
                turn_id="turn-1", tool_name="Bash")).returncode, 0)
        records = self.records()
        self.assertEqual({r["tool_use_id"] for r in records}, {"call-1"})
        self.assertEqual(len({r["event_id"] for r in records}), 2)

    def test_concurrent_writers_preserve_each_line(self):
        def write(index):
            return self.invoke(self.payload(tool_use_id=str(index)))
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(write, range(32)))
        self.assertTrue(all(r.returncode == 0 for r in results))
        self.assertEqual(len(self.records()), 32)
        self.assertEqual(len({r["tool_use_id"] for r in self.records()}), 32)

    def test_malformed_oversized_and_unknown_events_fail_without_block_signal(self):
        for payload in (b"not json", b"[]", b"x" * (recorder.MAX_INPUT_BYTES + 1),
                        self.payload("Unknown"), {"hook_event_name": "Stop"}):
            result = self.invoke(payload)
            self.assertEqual(result.returncode, 1)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"Navi event recording unavailable", result.stderr)
        self.assertEqual(self.records(), [])

    def test_missing_data_path_does_not_fall_back_into_repository(self):
        env = self.env.copy()
        env.pop("PLUGIN_DATA")
        result = self.invoke(self.payload(), env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.records(), [])

    def test_unwritable_destination_reports_failure(self):
        destination = self.data / "file-not-directory"
        destination.write_text("existing user content")
        result = self.invoke(self.payload(), dict(self.env, PLUGIN_DATA=str(destination)))
        self.assertEqual(result.returncode, 1)
        self.assertEqual(destination.read_text(), "existing user content")

    def test_session_id_cannot_escape_log_directory(self):
        payload = self.payload()
        payload["session_id"] = "../../outside"
        self.assertEqual(self.invoke(payload).returncode, 0)
        self.assertEqual(len(list((self.data / "events").glob("*.jsonl"))), 1)
        self.assertFalse((self.data / "outside").exists())

    def test_private_log_permissions(self):
        self.invoke(self.payload())
        directory = self.data / "events"
        self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
        for path in directory.iterdir():
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_rotation_is_bounded_and_retains_latest_record(self):
        directory = self.data / "events"
        with patch.object(recorder, "MAX_LOG_BYTES", 1300):
            for index in range(20):
                record = recorder.normalize(self.payload(tool_use_id=str(index)), 100)
                recorder.append_record(directory, record)
        self.assertLessEqual(len(list(directory.glob("*.jsonl*"))), 3)
        self.assertIn("19", {r["tool_use_id"] for r in self.records()})
        self.assertLess(len(self.records()), 20)

    def test_busy_lock_is_bounded_and_reports_degradation(self):
        directory = self.data / "events"
        directory.mkdir()
        key = hashlib.sha256(b"session-1").hexdigest()[:32]
        with (directory / (key + ".lock")).open("w") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            result = self.invoke(self.payload())
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"TimeoutError", result.stderr)

    def test_restart_appends_to_existing_session(self):
        for _ in range(2):
            self.assertEqual(self.invoke(self.payload("SessionStart", source="resume")).returncode, 0)
        self.assertEqual(len(self.records()), 2)

    def test_pause_and_resume_through_shipped_control_tool(self):
        command = [sys.executable, str(PLUGIN / "scripts/control.py")]
        subprocess.run(command + ["pause", str(self.data)], capture_output=True, check=True)
        for event in recorder.EVENTS:
            result = self.invoke(self.payload(event))
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"{}\n" if event == "Stop" else b"")
        self.assertEqual(self.records(), [])
        subprocess.run(command + ["resume", str(self.data)], capture_output=True, check=True)
        self.assertEqual(self.invoke(self.payload()).returncode, 0)
        self.assertEqual(len(self.records()), 1)

    def test_inspector_reports_invalid_tail_without_claiming_live_health(self):
        self.invoke(self.payload())
        log = next((self.data / "events").glob("*.jsonl"))
        with log.open("a") as stream:
            stream.write('{"partial":')
        result = subprocess.run([sys.executable, str(PLUGIN / "scripts/inspect_events.py"),
                                 str(self.data)], capture_output=True, timeout=5)
        report = json.loads(result.stdout)
        self.assertEqual(report["invalid_lines"], 1)
        self.assertEqual(report["event_counts"], {"PreToolUse": 1})
        self.assertEqual(report["live_hook_health"], "unknown")


if __name__ == "__main__":
    unittest.main()
