#!/usr/bin/env python3
"""Explicit task opt-in, bounded host observations and workspace snapshots. No model calls."""
import argparse
import difflib
import json
import os
from pathlib import Path
import stat
import time

from task_state import digest, locked, paths, save, user_context_digest, workspace_policy

MAX_FILES = 12
MAX_FILE_BYTES = 4096
MAX_EVENTS = 48
MAX_INPUT_BYTES = 2048
MAX_OUTPUT_BYTES = 1024
MAX_EXPORT_BYTES = 22000


def clipped(value, limit):
    raw = json.dumps(value, ensure_ascii=False).encode()
    return {"text": raw[:limit].decode("utf-8", errors="ignore"), "truncated": len(raw) > limit,
            "sha256": digest(value)}


def snapshot(workspace, name):
    """No symlink traversal, devices, outside paths, recursive scans or shell execution."""
    relative = Path(name)
    if relative.is_absolute() or not relative.parts or any(p == ".." for p in relative.parts):
        return {"status": "unsafe_path"}
    fd = None
    try:
        fd = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        for part in relative.parts[:-1]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        leaf = os.open(relative.parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        with os.fdopen(leaf, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return {"status": "not_regular"}
            raw = stream.read(MAX_FILE_BYTES + 1)
            after = os.fstat(stream.fileno())
        if (before.st_mtime_ns, before.st_size) != (after.st_mtime_ns, after.st_size):
            return {"status": "changed_during_read"}
        if len(raw) > MAX_FILE_BYTES:
            return {"status": "oversized", "size": after.st_size, "mtime_ns": after.st_mtime_ns}
        if b"\x00" in raw:
            return {"status": "binary"}
        return {"status": "present", "text": raw.decode("utf-8"), "sha256": digest(raw.hex())}
    except FileNotFoundError:
        return {"status": "absent"}
    except (OSError, UnicodeError):
        return {"status": "unreadable_or_symlink"}
    finally:
        if fd is not None:
            os.close(fd)


def allowed(data, state):
    return (state is not None and state["status"] == "active" and not (data / "disabled").exists()
            and (not state.get("capture_workspace") or workspace_policy(data, state["capture_workspace"])))


def version(state):
    evidence = state.get("action_evidence", {})
    return [evidence.get("revision", 0), evidence.get("enabled", False)]


def file_basis(state):
    """Only for file-only reviews: tool traffic alone cannot supersede current code."""
    capture = state.get("action_evidence", {})
    return digest([capture.get("enabled"), capture.get("generation", 0), capture.get("workspace"),
                   {name: [entry['baseline'], entry['current']] for name, entry in capture.get('files', {}).items()}])


def refresh(state):
    """Refresh explicitly watched files at hook/checkpoint boundaries, never whole-repo."""
    capture = state.get("action_evidence")
    if not capture or not capture["enabled"]:
        return
    for name, entry in capture["files"].items():
        current = snapshot(capture["workspace"], name)
        if current != entry["current"]:
            capture["revision"] += 1
            history = state.get('change_baselines', {}).get(name)
            if history is not None:
                history['observed_transitions'] += 1
                comparable = current['status'] in ('present','absent') and entry['baseline']['status'] in ('present','absent')
                history['ever_differed'] = history['ever_differed'] or (comparable and current != entry['baseline'])
                history['last_transition_at'] = time.time()
            entry.update(current=current, observed_at=time.time(), revision=capture["revision"],
                         user_context_digest=user_context_digest(state), attribution="unknown_may_include_concurrent_edits")


def configure(data, session, workspace, names, enabled=True):
    if not isinstance(names, list) or len(names) > MAX_FILES or not all(isinstance(n, str) and len(n) <= 512 for n in names):
        raise ValueError("bounded relative watch paths required")
    root = Path(workspace)
    if not root.is_absolute() or not root.is_dir() or root.is_symlink():
        raise ValueError("real absolute workspace required")
    root = root.resolve()
    # Git inspection is explicit opt-in work, never done in a hook or while holding the state lock.
    with locked(data, session) as (_, state):
        if not allowed(data, state):
            raise ValueError('active opted-in task required')
        if state.get('capture_workspace') and root != Path(state['capture_workspace']):
            raise ValueError('evidence workspace must match task')
        if state.get('action_evidence',{}).get('workspace',str(root)) != str(root):
            raise ValueError('cannot replace evidence workspace')
        known = state.get('action_evidence', {}).get('files', {})
        new_names = [str(Path(n)) for n in names if str(Path(n)) not in known]
        safe_names = [n for n in new_names if not Path(n).is_absolute() and '..' not in Path(n).parts
                      and snapshot(str(root), n)['status'] in ('present','absent')]
    from change_baseline import references
    git_refs = references(root, safe_names, MAX_FILE_BYTES) if enabled else {}
    with locked(data, session) as (path, state):
        if not allowed(data, state):
            raise ValueError("active opted-in task required")
        if state.get("capture_workspace") and root != Path(state["capture_workspace"]):
            raise ValueError("evidence workspace must match task")
        capture = state.setdefault("action_evidence", {"enabled": False, "workspace": str(root),
            "revision": 0, "events": [], "files": {}, "dropped_events": 0, "started_at": time.time()})
        if capture["workspace"] != str(root):
            raise ValueError("cannot replace evidence workspace")
        if len(set(names) | set(capture["files"])) > MAX_FILES:
            raise ValueError("watch capacity reached")
        for name in names:
            name = str(Path(name))
            if name not in capture["files"]:
                current = snapshot(str(root), name)
                if current["status"] == "unsafe_path":
                    raise ValueError("watch paths must remain in workspace")
                capture["files"][name] = {"baseline": current, "current": current, "observed_at": time.time(),
                    "baseline_at": time.time(), "revision": capture["revision"],
                    "baseline_user_context_digest": user_context_digest(state),
                    "user_context_digest": user_context_digest(state), "attribution": "unknown_may_include_concurrent_edits"}
                state.setdefault('change_baselines', {})[name] = {
                    'origin':'watch_enable_snapshot', 'task_start_equivalence':'unverified',
                    'captured_tool_events_before_baseline':len(capture['events'])+capture['dropped_events'],
                    'git_reference':git_refs.get(name, {'status':'unavailable','reason':'not_captured'}),
                    'observed_transitions':0, 'ever_differed':False}
        capture["enabled"] = enabled
        capture["generation"] = capture.get("generation", 0) + 1
        capture["revision"] += 1
        save(path, state)


def on_hook(data, payload):
    session = payload["session_id"]
    if not paths(data, session)[1].exists():
        return
    with locked(data, session) as (path, state):
        capture = state.get("action_evidence") if state else None
        if not allowed(data, state) or not capture or not capture["enabled"]:
            return
        event = payload["hook_event_name"]
        if event in ("PreToolUse", "PostToolUse"):
            identity = digest([event, payload.get("turn_id"), payload.get("tool_use_id"),
                               payload.get("tool_name"), payload.get("tool_input"), payload.get("tool_response")])
            if any(e["id"] == "a-" + identity[:24] for e in capture["events"]):
                refresh(state)
                save(path, state)
                return
            capture["revision"] += 1
            record = {"id": "a-" + identity[:24], "event": event, "tool": payload.get("tool_name"),
                "tool_use_id": payload.get("tool_use_id"), "turn_id": payload.get("turn_id"),
                "at": time.time(), "revision": capture["revision"], "user_context_digest": user_context_digest(state),
                "source_ids": [s["id"] for s in state["sources"]],
                "input": clipped(payload.get("tool_input"), MAX_INPUT_BYTES),
                "response": clipped(payload.get("tool_response"), MAX_OUTPUT_BYTES) if event == "PostToolUse" else None,
                "meaning": "host_observed_tool_attempt_or_response; not proof of successful edit"}
            capture["events"].append(record)
            if len(capture["events"]) > MAX_EVENTS:
                capture["events"].pop(0)
                capture["dropped_events"] += 1
        refresh(state)
        save(path, state)


def file_observation(name, entry):
    before, after = entry["baseline"], entry["current"]
    value = {k: v for k, v in entry.items() if k not in ("baseline", "current")}
    value.update(baseline_status=before["status"], current_status=after["status"],
                 baseline_sha256=before.get("sha256"), current_sha256=after.get("sha256"))
    if before["status"] in ("present", "absent") and after["status"] in ("present", "absent"):
        value["cumulative_diff"] = "\n".join(difflib.unified_diff(before.get("text", "").splitlines(),
            after.get("text", "").splitlines(), fromfile="baseline/" + name, tofile="current/" + name, n=3, lineterm=""))
        value["baseline_final_newline"] = before.get("text", "").endswith("\n")
        value["current_final_newline"] = after.get("text", "").endswith("\n")
        value["current_context"] = after.get("text", "")
    return value


def export(state):
    capture = state.get("action_evidence")
    if not capture or not capture["enabled"]:
        raise ValueError("action evidence not enabled")
    candidates = [{"id": e["id"], "kind": "tool_observation", "path": "not_inferred_from_command",
        "evidence": json.dumps(e, ensure_ascii=False), "origin": "codex_hook"} for e in capture["events"]]
    files = [{"id": "f-" + digest([name, entry])[:24], "kind": "workspace_observation", "path": name,
        "evidence": json.dumps(file_observation(name, entry), ensure_ascii=False),
        "origin": "local_snapshot; attribution_unknown"} for name, entry in capture["files"].items()]
    selected, omitted = [], []
    # Prefer current code; then recent actions. Older raw observations remain local up to MAX_EVENTS.
    size = 0
    for action in files + list(reversed(candidates)):
        cost = len(json.dumps(action, ensure_ascii=False).encode())
        if size + cost > MAX_EXPORT_BYTES or len(selected) >= 30:
            omitted.append(action["id"])
        else:
            size += cost
            selected.append(action)
    manifest = {"revision": capture["revision"], "started_at": capture["started_at"],
        "watched_files": list(capture["files"]), "dropped_events": capture["dropped_events"],
        "omitted_from_packet": omitted, "coverage_gaps": ["Only explicit watched files have snapshots.",
        "Earlier actions, unwatched files, background work and external edits may be missing.",
        "Truncated tool text and absent caller/input contracts cannot establish implementation necessity.",
        "File diffs are cumulative since each baseline; authorship is unknown, even during a tool interval."],
        "sources": [{"id": s["id"], "digest": digest(s["text"])} for s in state["sources"]]}
    return {"coverage": "partial", "actions": [{"id": "m-" + digest(manifest)[:24], "kind": "coverage_manifest",
        "path": "task", "origin": "navi_local_recorder", "evidence": json.dumps(manifest, ensure_ascii=False)}] + selected}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("enable", "disable", "export"))
    parser.add_argument("--data-directory", type=Path, required=True)
    parser.add_argument("--session", required=True)
    parser.add_argument("--workspace")
    parser.add_argument("--watch", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.operation == "export":
            with locked(args.data_directory, args.session) as (path, state):
                if not allowed(args.data_directory, state):
                    raise ValueError("recording paused")
                refresh(state)
                save(path, state)
                print(json.dumps(export(state), ensure_ascii=False))
        else:
            configure(args.data_directory, args.session, args.workspace, args.watch, args.operation == "enable")
            print(json.dumps({"enabled": args.operation == "enable", "model_calls": 0}))
        return 0
    except (OSError, ValueError, TypeError, KeyError) as error:
        print(json.dumps({"status": "unavailable", "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
