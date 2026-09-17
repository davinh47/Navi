"""Test fixture only. Never included in the installed Navi plugin."""
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    directory = Path(sys.argv[1])
    recorder_path = Path(sys.argv[2])
    raw = sys.stdin.buffer.read()
    event = json.loads(raw)
    tool = json.dumps(event.get("tool_input", {}))
    mode = next((name for name in ("EXIT1", "UNWRITABLE", "TIMEOUT")
                 if "NAVI_FAULT_" + name in tool), "NORMAL")
    label = event["hook_event_name"]
    with (directory / "fault-trace.jsonl").open("a") as stream:
        stream.write(json.dumps({"event": label, "mode": mode,
            "session_id": event["session_id"], "tool_use_id": event.get("tool_use_id"),
            "pid": os.getpid(), "time": time.time()}) + "\n")
    if mode == "EXIT1":
        sys.stderr.write("Navi test fixture: intentional exit 1\n")
        return 1
    if mode == "TIMEOUT":
        # Verify the host also terminates descendants of a timed-out hook.
        marker = directory / (label + "-timeout-survived")
        child = subprocess.Popen([sys.executable, "-c",
            "import time; from pathlib import Path; time.sleep(8); Path(" +
            repr(str(marker)) + ").touch()"])
        (directory / (label + "-timeout-child.pid")).write_text(str(child.pid))
        child.wait()
        return 0
    os.environ["PLUGIN_DATA"] = str(directory / ("unwritable" if mode == "UNWRITABLE" else "data"))
    spec = importlib.util.spec_from_file_location("navi_recorder", recorder_path)
    sys.path.insert(0, str(recorder_path.parent))
    recorder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(recorder)
    return recorder.run(io.BytesIO(raw), sys.stdout, sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
