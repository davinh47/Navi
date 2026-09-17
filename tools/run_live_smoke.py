#!/usr/bin/env python3
"""One opt-in real Codex smoke run (consumes Codex usage); bounded to 90s by default."""

import argparse
from collections import Counter
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
from urllib.request import getproxies

PROMPT = """This is a bounded integration smoke test. Do only these actions in this
fixture directory: (1) exec_command: pwd. (2) apply_patch: change fixture.txt from
before to after. (3) exec_command with yield_time_ms=1000:
python3 -c "import time; time.sleep(2); print('navi-long-done')"
Then wait with write_stdin if necessary. (4) exec_command:
python3 -c "raise SystemExit(7)"
That failure is intentional; do not fix or retry it. Finish with NAVI_SMOKE_OK.
Do not inspect other files, load skills, browse, plan, delegate, or make other changes.
"""


def read_jsonl(path):
    items = []
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            item = json.loads(line)
            if not isinstance(item, dict):
                raise ValueError("JSONL record must be an object")
            items.append(item)
    return items


def child_environment(environment, proxies):
    """Import discovered proxies only for this child; explicit env values win."""
    result = dict(environment)
    present = {key.lower() for key in environment}
    for scheme in ("http", "https", "all"):
        key = scheme + "_proxy"
        if key not in present and proxies.get(scheme):
            result[key.upper()] = proxies[scheme]
    if "no_proxy" not in present:
        result["NO_PROXY"] = proxies.get("no") or "localhost,127.0.0.1,::1"
    return result


def require_recording(data_directory):
    if (data_directory / "disabled").exists():
        raise ValueError("Navi recording is paused; resume it before a live smoke run")


def evaluate(directory, records, exit_code, timed_out):
    stream = read_jsonl(directory / "stdout.jsonl")
    thread = next((e.get("thread_id") for e in stream if e.get("type") == "thread.started"), None)
    counts = Counter(e.get("event") for e in records)
    turns = Counter(e.get("type") for e in stream)
    items = [e["item"] for e in stream if e.get("type") == "item.completed"]
    paired_pre = Counter(e.get("tool_use_id") for e in records if e.get("event") == "PreToolUse")
    paired_post = Counter(e.get("tool_use_id") for e in records if e.get("event") == "PostToolUse")
    stops = [e for e in records if e.get("event") == "Stop"]
    fixture = directory / "workspace/fixture.txt"
    messages = [e.get("text", "").strip() for e in items if e.get("type") == "agent_message"]
    checks = {
        "process_completed": exit_code == 0 and not timed_out,
        "fixture_changed": fixture.exists() and fixture.read_text() == "after\n",
        "final_marker": bool(messages) and messages[-1] == "NAVI_SMOKE_OK",
        "long_command_output": any(e.get("type") == "command_execution" and
            e.get("aggregated_output", "").strip() == "navi-long-done" and
            e.get("exit_code") == 0 for e in items),
        "intentional_exit_7": any(e.get("type") == "command_execution" and
            e.get("exit_code") == 7 for e in items),
        "expected_tool_items": Counter(e.get("type") for e in items
            if e.get("type") != "agent_message") == {"command_execution": 3, "file_change": 1},
        "single_completed_turn": turns["thread.started"] == turns["turn.started"] ==
            turns["turn.completed"] == 1 and not turns["turn.failed"] and not turns["error"],
        "hook_lifecycle": all(counts[e] == 1 for e in
            ("SessionStart", "UserPromptSubmit", "Stop", "SessionEnd")) and not counts["Interrupt"],
        "four_unique_pre_post_pairs": len(paired_pre) == 4 and None not in paired_pre and
            all(n == 1 for n in paired_pre.values()) and paired_pre == paired_post,
        "same_hook_session": bool(thread) and bool(records) and
            all(e.get("session_id") == thread for e in records),
        "no_stop_continuation_observed": len(stops) == 1 and
            stops[0].get("stop_hook_active") is False,
    }
    return {
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "passed" if all(checks.values()) else "incomplete_or_failed",
        "timed_out": timed_out, "exit_code": exit_code, "thread_id": thread,
        "checks": checks, "event_counts": dict(counts),
        "usage": [e["usage"] for e in stream if "usage" in e],
        "note": "Usage is for this smoke task, not Navi review. Cached input is a subset of input. "
                "Missing usage is unknown, not zero. Passing covers this normal CLI path only.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--model", default="gpt-5.6-luna",
                        help="Test model ID (default: gpt-5.6-luna); never inherit the global model")
    parser.add_argument("--effort", default="low", choices=("low", "medium", "high"))
    parser.add_argument("--timeout", type=int, default=90)
    parser.add_argument("--use-system-proxy", action="store_true",
                        help="Import OS proxy settings for the test child only; keep explicit environment values")
    parser.add_argument("--replay", type=Path,
                        help="Recheck saved evidence without launching Codex or consuming model usage")
    parser.add_argument("--data-directory", type=Path,
                        default=Path.home() / ".codex/plugins/data/navi-navi")
    args = parser.parse_args()
    if args.replay:
        directory = args.replay.resolve()
        prior = json.loads((directory / "report.json").read_text())
        report = evaluate(directory, read_jsonl(directory / "hook-events.jsonl"),
                          prior["exit_code"], prior["timed_out"])
        report["replay_of"] = prior["recorded_at"]
        (directory / "replay-report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))
        return 0 if report["status"] == "passed" else 1
    if not args.codex or not 1 <= args.timeout <= 300:
        parser.error("Codex must be available; timeout must be between 1 and 300 seconds")
    try:
        require_recording(args.data_directory)
    except ValueError as error:
        parser.error(str(error))
    environment = child_environment(os.environ, getproxies()) if args.use_system_proxy else dict(os.environ)
    parent = Path(__file__).resolve().parents[1] / ".navi-dev"
    parent.mkdir(exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="live-", dir=parent))
    workspace = directory / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "-q", str(workspace)], check=True)
    (workspace / "fixture.txt").write_text("before\n")
    (directory / "prompt.txt").write_text(PROMPT)
    timed_out = False
    with (directory / "stdout.jsonl").open("w") as stdout, \
         (directory / "stderr.log").open("w") as stderr:
        process = subprocess.Popen([args.codex, "exec", "--json", "--color", "never",
                                    "--model", args.model, "-c", 'model_reasoning_effort="' + args.effort + '"',
                                    "--sandbox", "workspace-write", "-C", str(workspace), "-"],
                                   stdin=subprocess.PIPE, stdout=stdout, stderr=stderr,
                                   text=True, start_new_session=True, env=environment)
        try:
            process.communicate(PROMPT, timeout=args.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.send_signal(signal.SIGINT)
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate()
    stream = read_jsonl(directory / "stdout.jsonl")
    thread = next((e.get("thread_id") for e in stream if e.get("type") == "thread.started"), None)
    records = []
    if thread:
        key = hashlib.sha256(thread.encode()).hexdigest()[:32]
        for path in (args.data_directory / "events").glob(key + ".jsonl*"):
            records.extend(read_jsonl(path))
    (directory / "hook-events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in records))
    report = evaluate(directory, records, process.returncode, timed_out)
    report["requested_model"] = args.model
    report["requested_reasoning_effort"] = args.effort
    report["proxy_mode"] = "system_discovery_with_env_precedence" if args.use_system_proxy else "inherited_env"
    (directory / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    print("Evidence:", directory)
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
