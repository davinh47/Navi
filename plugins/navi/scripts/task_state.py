#!/usr/bin/env python3
"""Opt-in task contracts. Local state only; no model calls or blocking decisions."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import time
import uuid

MAX_STATE = 4 * 1024 * 1024
MAX_TEXT = 16384
MAX_SOURCES = 256
MAX_PROPOSALS = 32
MAX_FINDINGS = 128
RETENTION_SECONDS = 7 * 86400
CONTRACT_FIELDS = {"goal", "in_scope", "out_of_scope", "acceptance", "plan"}
# Output vocabulary only. No user-text classification is performed by these enums.
INTENT_KINDS = {"goal", "acceptance", "phase", "deliverable", "operation", "constraint",
                "plan", "context", "presentation"}
INTENT_STATUSES = {"active", "deferred", "paused", "superseded", "cancelled", "unknown"}
SOURCE_ROLES = {"requirement", "fact", "clarification", "addition", "replacement", "cancellation",
                "pause", "resume", "continuation", "rationale", "example", "presentation", "uncertain"}
MAX_INTENT_ITEMS = 64
MAX_SOURCE_EFFECTS = 512


def workspace_policy(data, cwd, enabled=None):
    """Explicit exact-workspace opt-in; unrelated tasks remain metadata-only."""
    if not isinstance(cwd, str) or not Path(cwd).is_absolute():
        if enabled is None:
            return False
        raise ValueError("absolute workspace required")
    workspace = str(Path(cwd).resolve())
    directory = data / "capture-workspaces"
    path = directory / (hashlib.sha256(workspace.encode()).hexdigest() + ".json")
    if enabled is not None:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if enabled:
            atomic_write(path, {"workspace": workspace, "enabled": True})
        else:
            path.unlink(missing_ok=True)
    if not path.exists():
        return False
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "rb") as stream:
        value = json.loads(stream.read(MAX_TEXT))
    return value == {"workspace": workspace, "enabled": True}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def text(value, limit=MAX_TEXT):
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > limit:
        raise ValueError("invalid or oversized text")
    return value


def contract(value):
    if isinstance(value, str):
        value = {"goal": text(value)}
    if not isinstance(value, dict) or set(value) - CONTRACT_FIELDS or "goal" not in value:
        raise ValueError("invalid contract fields")
    result = {"goal": text(value["goal"])}
    for key in sorted(CONTRACT_FIELDS - {"goal"}):
        items = value.get(key)
        if items is not None:
            if not isinstance(items, list) or len(items) > 32:
                raise ValueError("invalid contract list")
            items = [text(item, 2048) for item in items]
        # Unknown is distinct from an explicit empty list.
        result[key] = items
    return result


def parse_contract(body):
    text(body)
    return contract(json.loads(body) if body.lstrip().startswith("{") else body)


def paths(data, session):
    if not data.is_absolute():
        raise ValueError("data directory must be absolute")
    text(session, 4096)
    directory = data / "tasks"
    key = hashlib.sha256(session.encode()).hexdigest()[:32]
    return directory, directory / (key + ".json"), directory / (key + ".lock")


def read_state(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, "rb") as stream:
        raw = stream.read(MAX_STATE + 1)
    if len(raw) > MAX_STATE:
        raise ValueError("state exceeds size limit")
    state = json.loads(raw)
    if not isinstance(state, dict) or state.get("schema_version") != 1:
        raise ValueError("invalid task state")
    if not isinstance(state.get("expires_at"), (float, int)) or not math.isfinite(state["expires_at"]):
        raise ValueError("invalid expiry")
    for field, kind in {"task_id": str, "session_id": str, "status": str, "contract": dict,
                        "sources": list, "contract_history": list, "pending_user_sources": list,
                        "proposals": dict, "findings": dict, "budget": dict,
                        "lifecycle": dict, "seen_lifecycle": list}.items():
        if not isinstance(state.get(field), kind):
            raise ValueError("incomplete task state")
    from budget import migrate
    migrate(state)
    return state


def atomic_write(path, state):
    encoded = (json.dumps(state, ensure_ascii=False, sort_keys=True) + "\n").encode()
    if len(encoded) > MAX_STATE:
        raise ValueError("task state capacity reached")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(data, session, load=True):
    directory, path, lock = paths(data, session)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        deadline = time.monotonic() + 0.25
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("task state busy")
                time.sleep(0.005)
        state = read_state(path) if load else None
        if state is not None:
            if state.get("session_id") != session:
                raise ValueError("session identity mismatch")
            if state["expires_at"] <= time.time():
                path.unlink()
                path.with_suffix(".gap").unlink(missing_ok=True)
                state = None
        yield path, state
    finally:
        os.close(fd)


def save(path, state):
    state["updated_at"] = time.time()
    state["expires_at"] = state["updated_at"] + RETENTION_SECONDS
    atomic_write(path, state)


def source_id(turn, prompt):
    return "u-" + digest([text(turn, 4096), prompt])[:24]


def supervision_sources(state):
    from history_context import sources
    return sources(state)


def user_context_digest(state):
    from history_context import identity
    history = identity(state)
    return digest([supervision_sources(state), history]) if history else digest(state["sources"])


def validate_citations(citations, sources):
    if not isinstance(citations, list) or not 1 <= len(citations) <= MAX_SOURCES:
        raise ValueError("bounded source citations required")
    for citation in citations:
        if not isinstance(citation, dict) or set(citation) != {"source_id", "quote"}:
            raise ValueError("invalid citation")
        source = text(citation["source_id"], 128)
        if text(citation["quote"]) not in sources.get(source, ""):
            raise ValueError("citation must quote recorded user text")


def intent_update(state, value, uncertainties):
    """Merge an unverified, cited delta. Omission never deletes previous intent.

    A matching parent protects same-user-version concurrent interpretations as well as
    cross-turn ones. Every new source needs explicit interpretation (possibly unknown).
    Valid references prove provenance, not semantic truth or user approval.
    """
    if not isinstance(value, dict) or set(value) != {"base_interpretation_id", "changes", "source_effects"}:
        raise ValueError("intent update requires parent, changes and source effects")
    parent_id = state.get("latest_interpretation_id")
    parent = state.get("interpretations", {}).get(parent_id, {})
    previous = parent.get("intent", {"items": [], "source_effects": []})
    expected_parent = parent_id if "intent" in parent else None
    if value["base_interpretation_id"] != expected_parent:
        raise ValueError("stale intent parent; reread task state")
    changes, effects = value["changes"], value["source_effects"]
    if not isinstance(changes, list) or len(changes) > MAX_INTENT_ITEMS:
        raise ValueError("bounded intent changes required")
    if not isinstance(effects, list) or len(effects) > MAX_SOURCE_EFFECTS:
        raise ValueError("bounded source effects required")
    sources = {s["id"]: s["text"] for s in supervision_sources(state)}
    items = {item["id"]: item for item in previous["items"]}
    changed_ids = set()
    for item in changes:
        if not isinstance(item, dict) or set(item) != {"id", "kind", "text", "status", "origin", "citations"}:
            raise ValueError("invalid intent item")
        key = text(item["id"], 128)
        if key in changed_ids:
            raise ValueError("duplicate intent change")
        changed_ids.add(key)
        if item["kind"] not in INTENT_KINDS or item["status"] not in INTENT_STATUSES:
            raise ValueError("invalid intent kind or status")
        if item["origin"] not in {"user_requirement", "agent_proposal"}:
            raise ValueError("invalid intent origin")
        text(item["text"], 2048)
        validate_citations(item["citations"], sources)
        if key in items and item["kind"] != items[key]["kind"]:
            raise ValueError("item kind is stable; supersede and create a new item")
        items[key] = item
    if len(items) > MAX_INTENT_ITEMS:
        raise ValueError("intent capacity reached; nothing is silently dropped")
    if sum(i["kind"] == "phase" and i["status"] == "active" for i in items.values()) > 1:
        raise ValueError("multiple current phases; resolve or mark unknown")
    covered = set()
    references = set()
    for effect in effects:
        if not isinstance(effect, dict) or set(effect) != {"source_id", "quote", "role", "item_ids"}:
            raise ValueError("invalid source effect")
        validate_citations([{k: effect[k] for k in ("source_id", "quote")}], sources)
        if effect["role"] not in SOURCE_ROLES:
            raise ValueError("invalid source role")
        ids = effect["item_ids"]
        if (not isinstance(ids, list) or len(ids) > MAX_INTENT_ITEMS
                or not all(isinstance(i, str) and i in items for i in ids) or len(set(ids)) != len(ids)):
            raise ValueError("invalid intent references")
        if (not ids or effect["role"] == "uncertain") and not uncertainties:
            raise ValueError("unresolved source effects require an uncertainty")
        covered.add(effect["source_id"])
        references.update((effect["source_id"], i) for i in ids)
    old_covered = {e["source_id"] for e in previous["source_effects"]}
    if set(sources) - old_covered - covered:
        raise ValueError("every unprocessed user source needs an effect or uncertainty")
    for item in changes:
        if not all((c["source_id"], item["id"]) in references for c in item["citations"]):
            raise ValueError("changed items need matching source effects")
    # Revise only touched source/item relations. Unmentioned relations survive, just
    # like unmentioned intent items; reinterpreting a quote cannot erase other goals.
    merged_effects = []
    for effect in previous["source_effects"]:
        remaining = [i for i in effect["item_ids"] if (effect["source_id"], i) not in references]
        if remaining or (not effect["item_ids"] and effect["source_id"] not in covered):
            merged_effects.append(dict(effect, item_ids=remaining))
    merged_effects += effects
    if len(merged_effects) > MAX_SOURCE_EFFECTS:
        raise ValueError("source effect capacity reached")
    if any(e["role"] == "uncertain" or not e["item_ids"] for e in merged_effects) and not uncertainties:
        raise ValueError("unresolved prior effects require retained uncertainties")
    return {"items": [items[k] for k in sorted(items)], "source_effects": merged_effects}


def context_view(state):
    """Raw user evidence stays authoritative; interpretations never replace it."""
    if state is None:
        return None
    key = user_context_digest(state)
    candidates = [dict(id=k, **v) for k, v in state.get("interpretations", {}).items()
                  if v["user_context_digest"] == key]
    latest = next((v for v in candidates if v["id"] == state.get("latest_interpretation_id")), None)
    if latest is None and len(candidates) == 1:
        latest = candidates[0]
    parent_id = state.get("latest_interpretation_id")
    parent = state.get("interpretations", {}).get(parent_id, {})
    memory = None
    if "intent" in parent:
        memory = {"interpretation_id": parent_id, "verification": "unverified",
                  "freshness": "current" if parent["user_context_digest"] == key else "stale",
                  "user_context_digest": parent["user_context_digest"], **parent["intent"]}
        memory["current_phase_id"] = next((i["id"] for i in memory["items"]
            if i["kind"] == "phase" and i["status"] == "active"), None)
    processed = {e["source_id"] for e in memory["source_effects"]} if memory else set()
    return {"user_context_digest": key, "source_revision": len(supervision_sources(state)),
            "interpretation_status": "proposed" if latest else "uninterpreted",
            "latest_interpretation": latest,
            "intent_memory": memory,
            "unprocessed_source_ids": [s["id"] for s in supervision_sources(state) if s["id"] not in processed],
            "authority": "ordered_direct_user_sources",
            "supervision_sources": supervision_sources(state)}


def mark_gap(data, payload, error):
    directory, path, _ = paths(data, payload["session_id"])
    if not path.exists() and not str(payload.get("prompt", "")).startswith("Navi: start") and not workspace_policy(data, payload.get("cwd")):
        return
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    atomic_write(path.with_suffix(".gap"), {"error": type(error).__name__,
                 "event": payload["hook_event_name"], "recorded_at": time.time()})


def append_source(state, turn, prompt, kind):
    key = source_id(turn, prompt)
    if any(s["id"] == key for s in state["sources"]):
        return None
    if len(state["sources"]) >= MAX_SOURCES:
        raise ValueError("source capacity reached; retained contract unchanged")
    state["sources"].append({"id": key, "turn_id": turn, "origin": "user_prompt_hook",
                             "kind": kind, "text": text(prompt), "recorded_at": time.time()})
    return key


def set_contract(state, value, source, proposal_id=None):
    previous = state["contract"]
    revision = 1 if previous is None else previous["revision"] + 1
    plan_revision = 1 if previous is None else previous["plan_revision"] + (previous["value"]["plan"] != value["plan"])
    entry = {"revision": revision, "plan_revision": plan_revision, "value": value,
             "authority": "explicit_user_input", "source_id": source, "proposal_id": proposal_id}
    state["contract"] = entry
    state["contract_history"].append(entry)
    state["pending_user_sources"] = []
    state["reported_progress"] = {}


def new_state(session):
    return {"schema_version": 1, "task_id": str(uuid.uuid4()), "session_id": session,
            "status": "active", "contract": None, "contract_history": [], "sources": [],
            "pending_user_sources": [], "proposals": {}, "findings": {},
            "reported_progress": {}, "lifecycle": {}, "seen_lifecycle": [],
            "budget": {"policy_version": 2, "review_limit": None, "limit_origin": "default",
                       "review_times": [], "reviews_used": 0, "reminders_used": 0, "reminder_limit": None},
            "supervision": "not_implemented", "created_at": time.time()}


def on_hook(data, payload):
    """Only direct UserPromptSubmit can establish/approve authoritative state."""
    event = payload["hook_event_name"]
    session = payload["session_id"]
    _, path, _ = paths(data, session)
    prompt = payload.get("prompt")
    command = None
    if event == "UserPromptSubmit" and isinstance(prompt, str):
        command = re.fullmatch(r"Navi: (start|update|approve|pause|resume)(?:[ \n](.*))?", prompt.strip(), re.S)
    # No directory creation or content capture in unrelated sessions.
    opted_in = workspace_policy(data, payload.get("cwd")) if event == "UserPromptSubmit" else False
    if not path.exists() and not (command and command[1] == "start") and not opted_in:
        return
    if event not in {"UserPromptSubmit", "SessionStart", "Stop", "Interrupt", "SessionEnd"}:
        return
    with locked(data, session) as (path, state):
        if state and state.get("capture_workspace") and not workspace_policy(data, state["capture_workspace"]):
            # Disabling workspace capture also covers existing workspace-opted tasks.
            return
        if event != "UserPromptSubmit":
            if state is None or state["status"] != "active":
                return
            identity = digest([event, payload.get("turn_id"), payload.get("source"), payload.get("reason")])
            if identity in state["seen_lifecycle"]:
                return
            state["seen_lifecycle"] = (state["seen_lifecycle"] + [identity])[-256:]
            state["lifecycle"][event] = state["lifecycle"].get(event, 0) + 1
            save(path, state)
            return
        turn = text(payload.get("turn_id"), 4096)
        text(prompt)
        if state is not None and any(s["id"] == source_id(turn, prompt) for s in state["sources"]):
            return
        verb, body = (command[1], (command[2] or "").strip()) if command else (None, None)
        if state is None:
            if verb != "start" and not opted_in:
                return
            value = parse_contract(body) if verb == "start" else contract(prompt)
            state = new_state(session)
            if opted_in and verb != "start":
                state["capture_workspace"] = str(Path(payload["cwd"]).resolve())
            source = append_source(state, turn, prompt, "start" if verb == "start" else "user_input")
            set_contract(state, value, source)
        elif state["status"] == "paused" and verb != "resume":
            mark_gap(data, payload, ValueError("user input skipped while paused"))
            return
        elif verb == "start":
            raise ValueError("task already exists; start never resets task or budget")
        elif verb in ("pause", "resume"):
            if body:
                raise ValueError("unexpected command body")
            append_source(state, turn, prompt, verb)
            state["status"] = "paused" if verb == "pause" else "active"
        elif verb == "update":
            value = parse_contract(body)
            source = append_source(state, turn, prompt, "update")
            set_contract(state, value, source)
        elif verb == "approve":
            if path.with_suffix(".gap").exists():
                raise ValueError("capture gap requires explicit full update")
            proposal = state["proposals"].get(body)
            if proposal is None or proposal["base_revision"] != state["contract"]["revision"]:
                raise ValueError("missing or stale proposal")
            if proposal["user_context_digest"] != user_context_digest(state):
                raise ValueError("user input changed since proposal; re-propose against latest evidence")
            source = append_source(state, turn, prompt, "approve")
            set_contract(state, proposal["value"], source, body)
        else:
            # No language/keyword classification. Meaning is a cited model proposal.
            source = append_source(state, turn, prompt, "user_input")
            state["pending_user_sources"].append(source)
        save(path, state)
        if verb in ("start", "update"):
            path.with_suffix(".gap").unlink(missing_ok=True)


def agent_write(data, session, operation, value):
    with locked(data, session) as (path, state):
        if state is None or state["status"] != "active":
            raise ValueError("no active opted-in task")
        if operation == "interpret":
            required = {"user_context_digest", "contract", "citations", "uncertainties"}
            if not isinstance(value, dict) or not required <= set(value) or set(value) - required - {"intent_update"}:
                raise ValueError("interpretation requires digest, contract, citations and uncertainties")
            if path.with_suffix(".gap").exists():
                raise ValueError("capture gap; interpretation unavailable until full update")
            if value["user_context_digest"] != user_context_digest(state):
                raise ValueError("stale interpretation")
            citations = value["citations"]
            sources = {s["id"]: s["text"] for s in supervision_sources(state)}
            validate_citations(citations, sources)
            uncertainty = value["uncertainties"]
            if not isinstance(uncertainty, list) or len(uncertainty) > 32:
                raise ValueError("invalid uncertainties")
            proposal = {"origin": "agent_interpretation", "verification": "unverified",
                "user_context_digest": value["user_context_digest"], "value": contract(value["contract"]),
                "citations": citations, "uncertainties": [text(s, 2048) for s in uncertainty]}
            if "intent_update" in value:
                previous = state.get("interpretations", {}).get(state.get("latest_interpretation_id"), {})
                if (previous.get("intent_update") == value["intent_update"]
                        and all(previous.get(k) == v for k, v in proposal.items())):
                    return state["latest_interpretation_id"]  # Retry of the latest committed delta.
                proposal["intent"] = intent_update(state, value["intent_update"], uncertainty)
                proposal["intent_update"] = value["intent_update"]
            elif "intent" in state.get("interpretations", {}).get(state.get("latest_interpretation_id"), {}):
                raise ValueError("intent tracking requires an incremental update; cannot discard memory")
            key = "i-" + digest(proposal)[:24]
            entries = state.setdefault("interpretations", {})
            if key not in entries and len(entries) >= MAX_PROPOSALS:
                raise ValueError("interpretation capacity reached")
            entries[key] = proposal
            state["latest_interpretation_id"] = key
        elif operation == "propose":
            if not isinstance(value, dict) or set(value) != {"base_revision", "contract", "source_ids"}:
                raise ValueError("proposal requires base_revision, contract and source_ids")
            if value["base_revision"] != state["contract"]["revision"]:
                raise ValueError("stale proposal base")
            ids = value["source_ids"]
            available = {s["id"] for s in state["sources"]}
            if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in available for i in ids):
                raise ValueError("invalid evidence references")
            proposal = {"origin": "agent_proposal", "base_revision": value["base_revision"],
                        "value": contract(value["contract"]), "source_ids": ids,
                        "user_context_digest": user_context_digest(state)}
            key = "p-" + digest(proposal)[:24]
            if key not in state["proposals"] and len(state["proposals"]) >= MAX_PROPOSALS:
                raise ValueError("proposal capacity reached")
            state["proposals"][key] = proposal
        elif operation == "finding":
            if not isinstance(value, dict) or set(value) != {"location", "summary", "observation_id"}:
                raise ValueError("finding requires location, summary and observation_id")
            location = text(value["location"], 2048)
            summary = text(value["summary"], 4096)
            observation = text(value["observation_id"], 256)
            key = "f-" + digest([" ".join(location.split()), " ".join(summary.split()).casefold()])[:24]
            if key not in state["findings"]:
                if len(state["findings"]) >= MAX_FINDINGS:
                    raise ValueError("finding capacity reached")
                state["findings"][key] = {"location": location, "summary": summary,
                    "origin": "agent_report", "verification": "unverified", "observation_ids": []}
            entry = state["findings"][key]
            if observation not in entry["observation_ids"]:
                if len(entry["observation_ids"]) >= 128:
                    raise ValueError("finding observation capacity reached")
                entry["observation_ids"].append(observation)
            entry["occurrences"] = len(entry["observation_ids"])
        elif operation == "progress":
            if not isinstance(value, dict) or set(value) != {"base_revision", "steps"}:
                raise ValueError("progress requires base_revision and steps")
            current = state["contract"]
            if value["base_revision"] != current["revision"] or current["value"]["plan"] is None:
                raise ValueError("no matching approved plan")
            steps = value["steps"]
            if not isinstance(steps, dict) or any(k not in {str(i + 1) for i in range(len(current["value"]["plan"]))}
                    or v not in ("pending", "in_progress", "completed") for k, v in steps.items()):
                raise ValueError("invalid progress step")
            state["reported_progress"] = {"origin": "agent_report", "verified": False,
                                           "base_revision": current["revision"], "steps": steps}
            key = "reported_progress"
        else:
            raise ValueError("agent operation cannot approve or change task authority")
        save(path, state)
        return key


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("show", "interpret", "propose", "finding", "progress", "forget", "prune", "enable-workspace", "disable-workspace"))
    parser.add_argument("--data-directory", type=Path, default=Path(os.environ["PLUGIN_DATA"]) if os.environ.get("PLUGIN_DATA") else None)
    parser.add_argument("--session")
    parser.add_argument("--workspace", help="Exact workspace to opt in; does not include children")
    parser.add_argument("--input", type=Path, help="JSON file for agent-authored proposals or reports")
    args = parser.parse_args()
    if args.data_directory is None or not args.data_directory.is_absolute():
        parser.error("explicit absolute plugin data directory required")
    try:
        if args.operation in ("enable-workspace", "disable-workspace"):
            print(json.dumps({"capture_enabled": workspace_policy(args.data_directory, args.workspace,
                              args.operation == "enable-workspace")}))
        elif args.operation == "prune":
            removed = 0
            for path in (args.data_directory / "tasks").glob("*.json"):
                state = read_state(path)
                if state and state["expires_at"] <= time.time():
                    with locked(args.data_directory, state["session_id"]) as (_, remaining):
                        removed += remaining is None
            print(json.dumps({"expired_tasks_removed": removed}))
        else:
            text(args.session, 4096)
            if args.operation in ("show", "forget"):
                with locked(args.data_directory, args.session, load=args.operation != "forget") as (path, state):
                    existed = path.exists() or path.is_symlink()
                    if args.operation == "forget":
                        path.unlink(missing_ok=True)
                    gap = path.with_suffix(".gap")
                    if args.operation == "forget":
                        gap.unlink(missing_ok=True)
                    print(json.dumps({"task": state, "context": context_view(state), "capture_gap": gap.exists()} if args.operation == "show"
                                     else {"forgotten": existed}, ensure_ascii=False))
            else:
                if args.input is None or args.input.stat().st_size > MAX_TEXT * 4:
                    raise ValueError("bounded JSON input file required")
                key = agent_write(args.data_directory, args.session, args.operation, json.loads(args.input.read_text()))
                print(json.dumps({"id": key, "authority": "agent_proposal_or_report"}))
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"error": type(error).__name__, "message": "Task state operation failed; no approval was granted."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
