#!/usr/bin/env python3
"""Pause/resume only Navi's recorder. Does not modify Codex permissions or trust."""

import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["pause", "resume", "status"])
    parser.add_argument("data_directory", type=Path, help="Codex-provided PLUGIN_DATA directory")
    args = parser.parse_args()
    directory = args.data_directory.expanduser()
    if not directory.is_absolute():
        parser.error("data_directory must be absolute")
    marker = directory / "disabled"
    if args.action == "pause":
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        marker.touch(mode=0o600, exist_ok=True)
    elif args.action == "resume":
        if marker.exists():
            marker.unlink()
    print(json.dumps({"recording_requested": not marker.exists(),
                      "live_hook_health": "unknown",
                      "scope": "local Navi event and task-state recording only"}, indent=2))


if __name__ == "__main__":
    main()
