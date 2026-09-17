#!/usr/bin/env python3
"""Inspect local event metadata without invoking a model or Codex."""

import argparse
from collections import Counter
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_directory", type=Path, help="Codex-provided PLUGIN_DATA directory")
    args = parser.parse_args()
    counts = Counter()
    sessions = set()
    invalid_lines = 0
    last_event = None
    directory = args.data_directory / "events"
    for path in sorted(directory.glob("*.jsonl*")):
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    item = json.loads(line)
                    event, session, timestamp = item["event"], item["session_id"], item["recorded_at"]
                    if not all(isinstance(value, str) for value in (event, session, timestamp)):
                        raise ValueError("invalid record")
                    counts[event] += 1
                    sessions.add(session)
                    last_event = max(last_event or timestamp, timestamp)
                except (ValueError, KeyError, TypeError):
                    invalid_lines += 1
    print(json.dumps({
        "mode": "metadata_recorder_only",
        "event_counts": dict(counts), "sessions": len(sessions),
        "last_recorded_at": last_event, "invalid_lines": invalid_lines,
        "live_hook_health": "unknown",
        "note": "Historical records do not prove the hook is currently enabled or healthy.",
    }, indent=2))


if __name__ == "__main__":
    main()
