"""Packaged Mode-B self-check. Targets only an offscreen fixture spawned here."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

import psutil
import win32con
import win32gui
import win32process

import core
import storage
import suspension


def run(fixture_path):
    fixture = Path(fixture_path).resolve()
    if fixture.name != "SuspensionFixture.exe" or not fixture.is_file():
        raise ValueError("Only the dedicated SuspensionFixture.exe is accepted")
    with ExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="gameswitcher-packaged-freeze-"))
        previous = os.environ.get("APPDATA")
        def restore_environment():
            if previous is None:
                os.environ.pop("APPDATA", None)
            else:
                os.environ["APPDATA"] = previous
        stack.callback(restore_environment)
        os.environ["APPDATA"] = directory
        # Only a deliberately selected child fixture is passed to the production API.
        storage.save_baseline([{"exe": r"C:\SelfTestBaseline\keep.exe"}], full_processes=True)
        ready = Path(directory) / "ready.json"
        child = subprocess.Popen([str(fixture), "--ready", str(ready)], creationflags=subprocess.CREATE_NO_WINDOW)
        data, target = None, None
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                if child.poll() is not None:
                    raise RuntimeError("fixture startup failed")
                time.sleep(0.05)
            data = json.loads(ready.read_text(encoding="utf-8"))
            family = {child.pid, *(p.pid for p in psutil.Process(child.pid).children(recursive=True))}
            if data["pid"] not in family or core.path_key(psutil.Process(data["pid"]).exe()) != core.path_key(str(fixture)):
                raise RuntimeError("fixture ownership mismatch")
            target = next(a for a in core.visible_apps() if a["pid"] == data["pid"])
            result = core.suspend_apps([target], confirm=lambda _: True)
            if not result["suspended"]:
                raise RuntimeError("fixture suspension failed: " + result["reason"])
            stopped = psutil.Process(data["pid"]).status() == psutil.STATUS_STOPPED
            saved = storage.load_pending_restore()
            node_count = len(suspension.read_operation(saved[0]["suspend_id"])["nodes"])
            result = core.restore_apps()
            deadline = time.monotonic() + 2
            while win32gui.IsIconic(data["hwnd"]) and time.monotonic() < deadline:
                time.sleep(0.05)
            restored = psutil.Process(data["pid"]).status() != psutil.STATUS_STOPPED
            return {"ok": stopped and restored and bool(result["restored"]) and not storage.load_pending_restore(),
                    "suspended": stopped, "resumed": restored, "saved_identity_count": node_count,
                    "fixture_only": True}
        finally:
            core.resume_all_suspended(storage.load_pending_restore())
            if data and child.poll() is None and win32gui.IsWindow(data["hwnd"]):
                if win32process.GetWindowThreadProcessId(data["hwnd"])[1] == data["pid"]:
                    win32gui.PostMessage(data["hwnd"], win32con.WM_APP + 55, 0, 0)
            try:
                child.wait(timeout=4)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=4)
