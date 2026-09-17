#!/usr/bin/env python3
"""Reinstall local Navi while preserving cache paths held by active sessions.

Uses the plugin-creator helpers and official CLI; never edits marketplace/config.
Old versions are kept intact for live sessions, not registered as new installations.
"""
import argparse
from contextlib import contextmanager
import json
import os
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
    parser.add_argument("--skill-root", type=Path, default=Path.home() / ".codex/skills/.system/plugin-creator")
    args = parser.parse_args()
    source = Path(__file__).resolve().parents[1] / "plugins/navi"
    marketplace = Path.home() / ".agents/plugins/marketplace.json"
    scripts = args.skill_root / "scripts"
    def helper(name, *argv):
        return subprocess.run([sys.executable, str(scripts / name), *map(str, argv)],
                              check=True, capture_output=True, text=True).stdout.strip()
    market_name = helper("read_marketplace_name.py")
    metadata = json.loads(marketplace.read_text())
    entry = next(p for p in metadata["plugins"] if p["name"] == "navi")
    # The default personal marketplace's relative source is rooted at the user's home.
    location = Path(entry["source"]["path"]).expanduser()
    location = location if location.is_absolute() else Path.home() / location
    if entry["source"]["source"] != "local" or location.resolve() != source.resolve():
        raise ValueError("personal Navi entry must point at this local source")
    helper("validate_plugin.py", source)
    helper("update_plugin_cachebuster.py", source)
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    cache = codex_home / "plugins/cache" / market_name / "navi"
    with preserve_cache(cache) as preserved:
        result = subprocess.run(["codex", "plugin", "add", "navi@" + market_name, "--json"],
                                check=True, capture_output=True, text=True)
    version = json.loads((source / ".codex-plugin/plugin.json").read_text())["version"]
    installed = cache / version
    for path in source.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            if path.read_bytes() != (installed / path.relative_to(source)).read_bytes():
                raise ValueError("installed source mismatch: " + str(path.relative_to(source)))
    print(json.dumps({"version": version, "installed": str(installed), "preserved_versions": preserved,
                      "source_matches": True, "cli_exit": result.returncode}))


if __name__ == "__main__":
    main()
