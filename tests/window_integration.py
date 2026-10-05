"""Real WM_CLOSE test against two spawned fixtures; no desktop apps are targeted."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import win32con
import win32gui
import win32process

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import core

results = []
with tempfile.TemporaryDirectory(prefix="gameswitcher-window-integration-") as directory:
    for accept in (False, True):
        ready = Path(directory) / f"ready-{accept}.json"
        command = [sys.executable, str(root / "tests" / "window_fixture.py"), "--ready", str(ready)]
        if accept:
            command.append("--accept-close")
        child = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW)
        metadata = None
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("window fixture exited before readiness")
                time.sleep(0.05)
            metadata = json.loads(ready.read_text(encoding="utf-8"))
            assert metadata["pid"] == child.pid
            target = next(a for a in core.visible_apps() if a["pid"] == child.pid)
            # Only the exact process we spawned is passed to the real close API.
            result = core.close_apps([target], {"grace_timeout_sec": 0.3})
            if accept:
                assert len(result["closed"]) == 1, result
                child.wait(timeout=5)
            else:
                assert len(result["skipped"]) == 1, result
                assert child.poll() is None, "close cancellation must leave fixture alive"
            results.append({"accept_close": accept, "result": "closed" if accept else "preserved"})
        finally:
            if child.poll() is None:
                if metadata and win32gui.IsWindow(metadata["hwnd"]):
                    if win32process.GetWindowThreadProcessId(metadata["hwnd"])[1] == child.pid:
                        win32gui.PostMessage(metadata["hwnd"], win32con.WM_APP + 55, 0, 0)
                try:
                    child.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    child.kill()  # only this test's child handle, never a desktop process
                    child.wait(timeout=3)
print(json.dumps(results))
