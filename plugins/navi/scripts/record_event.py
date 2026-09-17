#!/usr/bin/env python3
"""Codex observation-only hook. Standard library, macOS/Linux, no model calls."""

import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import uuid

EVENTS = frozenset({
    "SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse",
    "Stop", "Interrupt", "SessionEnd",
})
MAX_INPUT_BYTES = 4 * 1024 * 1024
MAX_LOG_BYTES = 1024 * 1024
BACKUPS = 2
LOCK_TIMEOUT_SECONDS = 0.25


def short_string(payload, key, required=False):
    value = payload.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("invalid field: " + key)
    return value


def normalize(payload, size):
    if not isinstance(payload, dict):
        raise ValueError("event must be an object")
    event = short_string(payload, "hook_event_name", required=True)
    if event not in EVENTS:
        raise ValueError("unsupported hook event")
    record = {
        "schema_version": 1,
        "adapter": "codex-hooks",
        "event_id": str(uuid.uuid4()),
        "recorded_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "event": event,
        "session_id": short_string(payload, "session_id", required=True),
        "received_bytes": size,
        "content_capture": "metadata_only",
    }
    for key in ("turn_id", "tool_use_id", "agent_id", "cwd", "model",
                "permission_mode", "tool_name", "source", "reason"):
        value = short_string(payload, key)
        if value is not None:
            record[key] = value
    # Diagnostic logs never contain prompts, commands, patches, or tool output.
    # Separately opted-in task state may store direct user requirements.
    for key in ("prompt", "tool_input", "tool_response", "transcript_path",
                "last_assistant_message"):
        record["has_" + key] = payload.get(key) is not None
    if isinstance(payload.get("stop_hook_active"), bool):
        record["stop_hook_active"] = payload["stop_hook_active"]
    return record


def data_directory():
    value = os.environ.get("PLUGIN_DATA")
    if not value or not Path(value).is_absolute():
        raise ValueError("PLUGIN_DATA must be an absolute directory")
    return Path(value) / "events"


def acquire_lock(fd):
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise TimeoutError("event log busy")
            time.sleep(0.005)


def append_record(directory, record):
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    session_key = hashlib.sha256(record["session_id"].encode()).hexdigest()[:32]
    log = directory / (session_key + ".jsonl")
    lock = directory / (session_key + ".lock")
    encoded = (json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    lock_fd = os.open(lock, flags, 0o600)
    try:
        acquire_lock(lock_fd)
        if log.exists() and log.stat().st_size + len(encoded) > MAX_LOG_BYTES:
            # Rolling diagnostic history, not a durable audit ledger.
            for index in range(BACKUPS, 0, -1):
                source = log if index == 1 else Path(str(log) + "." + str(index - 1))
                target = Path(str(log) + "." + str(index))
                if source.exists():
                    os.replace(source, target)
        fd = os.open(log, flags, 0o600)
        try:
            pending = memoryview(encoded)
            while pending:
                written = os.write(fd, pending)
                if written == 0:
                    raise OSError("short write")
                pending = pending[written:]
        finally:
            os.close(fd)
    finally:
        # Closing also releases the lock after all error paths.
        os.close(lock_fd)


def run(stdin, stdout, stderr):
    try:
        raw = stdin.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("event exceeds input limit")
        record = normalize(json.loads(raw), len(raw))
        advisory = None
        directory = data_directory()
        # Local stop switch also works when a host ignores its plugin override.
        # The hook may still be launched, but it neither records nor emits context.
        if not (directory.parent / "disabled").exists():
            append_record(directory, record)
            # Task content remains opt-in; the diagnostic log stays metadata-only.
            from task_state import on_hook
            try:
                on_hook(directory.parent, json.loads(raw))
                from history import on_hook as capture_history
                capture_history(directory.parent, json.loads(raw))
                from context_sync import on_hook as sync_context
                sync_context(directory.parent, json.loads(raw))
                from action_evidence import on_hook as capture_actions
                from reminders import on_hook as transport_reminder
                # Check already-ready evidence before recording the next attempted tool.
                # No wait for evaluation and no ability to deny or rewrite the pending tool.
                if record["event"] == "PreToolUse":
                    advisory = transport_reminder(directory.parent, json.loads(raw))
                capture_actions(directory.parent, json.loads(raw))
                if record["event"] != "PreToolUse":
                    advisory = transport_reminder(directory.parent, json.loads(raw))
                from supervisor import on_hook as schedule_review
                schedule_review(directory.parent, json.loads(raw))
                from rollback import on_hook as rollback_feedback
                feedback = rollback_feedback(directory.parent, json.loads(raw))
                if feedback:
                    if advisory is None:
                        advisory = {"hookSpecificOutput":{"hookEventName":"PreToolUse","additionalContext":feedback}}
                    else:
                        advisory["hookSpecificOutput"]["additionalContext"] += "\n" + feedback
            except (OSError, ValueError, RecursionError, KeyError, TypeError) as error:
                from task_state import mark_gap
                try:
                    mark_gap(directory.parent, json.loads(raw), error)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                raise
        # Stop requires JSON. An empty object does not request continuation.
        # Only an explicitly enabled, ready advisory adds model-visible context.
        if advisory is not None:
            stdout.write(json.dumps(advisory, ensure_ascii=False) + "\n")
        elif record["event"] == "Stop":
            stdout.write("{}\n")
        return 0
    except (OSError, ValueError, RecursionError, KeyError, TypeError) as error:
        # Never exit 2 (Codex's blocking signal). Do not print payloads/paths.
        stderr.write("Navi event recording unavailable (" + type(error).__name__ +
                     "); metadata or task state may be incomplete.\n")
        return 1


if __name__ == "__main__":
    sys.exit(run(sys.stdin.buffer, sys.stdout, sys.stderr))
