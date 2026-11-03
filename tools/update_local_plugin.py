#!/usr/bin/env python3
"""Reinstall local Navi while preserving cache paths held by active sessions.

Uses the official CLI; never edits marketplace/config or the public version prefix.
Old versions are kept intact for live sessions, not registered as new installations.
"""
import argparse
import datetime
from contextlib import contextmanager
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


@contextmanager
def preserve_cache(cache):
    with tempfile.TemporaryDirectory(prefix="navi-update-backup-") as temporary:
        backup = Path(temporary)
        versions = [p for p in cache.iterdir() if p.is_dir() and not p.is_symlink()] if cache.exists() else []
        for version in versions:
            shutil.copytree(version, backup / version.name)
        try:
            yield [p.name for p in versions]
        finally:
            for version in versions:
                if not version.exists():
                    shutil.copytree(backup / version.name, version)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "plugins/navi"
    marketplace = Path.home() / ".agents/plugins/marketplace.json"
    metadata = json.loads(marketplace.read_text())
    market_name = metadata['name']
    if not isinstance(market_name,str) or not re.fullmatch(r'[A-Za-z0-9_-]+',market_name):
        raise ValueError('invalid marketplace name')
    entry = next(p for p in metadata["plugins"] if p["name"] == "navi")
    # The default personal marketplace's relative source is rooted at the user's home.
    location = Path(entry["source"]["path"]).expanduser()
    location = location if location.is_absolute() else Path.home() / location
    if entry["source"]["source"] != "local" or location.resolve() != source.resolve():
        raise ValueError("personal Navi entry must point at this local source")
    manifest=source/'.codex-plugin/plugin.json'
    original=manifest.read_bytes();plugin=json.loads(original)
    if plugin.get('name')!=source.name or not isinstance(plugin.get('version'),str):
        raise ValueError('invalid plugin name/version')
    version=plugin['version'].split('+')[0]+'+codex.'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%d%H%M%S')
    plugin['version']=version
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    cache = codex_home / "plugins/cache" / market_name / "navi"
    try:
        manifest.write_text(json.dumps(plugin,indent=2)+'\n')
        with preserve_cache(cache) as preserved:
            result = subprocess.run(["codex", "plugin", "add", "navi@" + market_name, "--json"],
                                    check=True, capture_output=True, text=True)
    finally:
        manifest.write_bytes(original)
    installed = cache / version
    for path in source.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            expected=(json.dumps(plugin,indent=2)+'\n').encode() if path==manifest else path.read_bytes()
            if expected != (installed / path.relative_to(source)).read_bytes():
                raise ValueError("installed source mismatch: " + str(path.relative_to(source)))
    print(json.dumps({"version": version, "installed": str(installed), "preserved_versions": preserved,
                      "source_matches": True, "cli_exit": result.returncode}))


if __name__ == "__main__":
    main()
