"""Offline verification of a real Codex app-server fault-injection run."""
import argparse
from collections import Counter
import json
from pathlib import Path

from run_live_smoke import read_jsonl


def evaluate(directory):
    events = read_jsonl(directory / "host-events.jsonl")
    trace = read_jsonl(directory / "fault-trace.jsonl")
    hooks = [e["params"]["run"] for e in events if e.get("method") == "hook/completed"
             and e["params"]["run"].get("source") == "sessionFlags"]
    tools = [e["params"]["item"] for e in events if e.get("method") == "item/completed"
             and e["params"]["item"].get("type") == "commandExecution"]
    turns = [e["params"]["turn"] for e in events if e.get("method") == "turn/completed"]
    pre = [h for h in hooks if h["eventName"] == "preToolUse"]
    post = [h for h in hooks if h["eventName"] == "postToolUse"]
    expected = ["failed", "failed", "failed", "completed"]
    timeouts = [h for h in pre + post if any("timed out" in x.get("text", "")
                for x in h.get("entries", []))]
    tool_trace = [e for e in trace if e["event"] in ("PreToolUse", "PostToolUse")]
    pairs = Counter((e["mode"], e["event"]) for e in tool_trace)
    counts = Counter(e.get("method") for e in events)
    thread_ids = {e.get("params", {}).get("threadId") for e in events
                  if e.get("method") in ("turn/started", "turn/completed", "hook/completed", "item/completed")}
    trace_sessions = {e.get("session_id") for e in trace}
    checks = {
        "one_completed_turn": len(turns) == counts["turn/started"] == 1 and
            turns[0]["status"] == "completed" and turns[0].get("error") is None,
        "final_marker": len(turns) == 1 and any(i.get("text") == "NAVI_HOST_FAULTS_OK"
            for i in turns[0].get("items", [])),
        "four_commands_succeeded_in_order": len(tools) == 4 and
            [i.get("aggregatedOutput") for i in tools] ==
            ["NAVI_FAULT_" + m for m in ("EXIT1", "UNWRITABLE", "TIMEOUT", "NORMAL")] and
            all(i.get("exitCode") == 0 and i.get("status") == "completed" for i in tools),
        "pre_and_post_failure_then_recovery": [h["status"] for h in pre] == expected and
            [h["status"] for h in post] == expected,
        "failure_visible_in_host_stream": sum(h["status"] == "failed" and
            any(x.get("kind") == "error" for x in h.get("entries", [])) for h in hooks) == 6,
        "timeout_bounded": len(timeouts) == 2 and
            all(2900 <= h.get("durationMs", 0) < 5000 for h in timeouts),
        "no_blocked_or_stopped_hooks": all(h["status"] not in ("blocked", "stopped") for h in hooks),
        "all_faults_actually_injected": len(tool_trace) == 8 and all(
            pairs[(mode, event)] == 1 for mode in ("EXIT1", "UNWRITABLE", "TIMEOUT", "NORMAL")
            for event in ("PreToolUse", "PostToolUse")),
        "session_ended": sum(e["event"] == "SessionEnd" for e in trace) == 1,
        "normal_stop_observed": sum(h["eventName"] == "stop" and h["status"] == "completed"
            and not h.get("entries") for h in hooks) == 1 and sum(e["event"] == "Stop" for e in trace) == 1,
        "same_test_session": len(thread_ids) == 1 and None not in thread_ids and trace_sessions == thread_ids,
        "no_delayed_child_writes": not list(directory.glob("*-timeout-survived")),
    }
    usage = [e["params"]["tokenUsage"]["total"] for e in events
             if e.get("method") == "thread/tokenUsage/updated"]
    return {"status": "passed" if all(checks.values()) else "failed", "checks": checks,
            "fault_hook_durations_ms": [h["durationMs"] for h in pre + post],
            "usage": usage[-1] if usage else None,
            "scope": "Real host and real model, test-specific synchronous hooks. Not a UI test.",
            "usage_note": "Whole test task usage; cached input is a subset. Navi reviewer calls: 0."}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    report = evaluate(args.directory)
    (args.directory / "verification.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    raise SystemExit(0 if report["status"] == "passed" else 1)
