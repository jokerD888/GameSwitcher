"""Native Mode-B integration against an explicitly spawned, offscreen fixture only."""
from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import psutil
import win32api
import win32con
import win32gui
import win32process

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import core
import storage
import suspension

FIXTURE = ROOT / "dist-fixed" / "fixtures" / "SuspensionFixture.exe"


def spawn_fixture(directory, extra_args=()):
    ready = Path(directory) / "fixture-ready.json"
    ready.unlink(missing_ok=True)
    child = subprocess.Popen([str(FIXTURE), "--ready", str(ready), *extra_args], creationflags=subprocess.CREATE_NO_WINDOW)
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        if child.poll() is not None:
            raise RuntimeError("fixture failed to start")
        time.sleep(0.05)
    data = json.loads(ready.read_text(encoding="utf-8"))
    assert data["pid"] in {child.pid, *(p.pid for p in psutil.Process(child.pid).children(recursive=True))}
    assert core.path_key(psutil.Process(data["pid"]).exe()) == core.path_key(str(FIXTURE))
    target = next(a for a in core.visible_apps() if a["pid"] == data["pid"])
    return child, data, target


def cleanup(child, data):
    if child.poll() is None:
        hwnd = data["hwnd"]
        if win32gui.IsWindow(hwnd) and win32process.GetWindowThreadProcessId(hwnd)[1] == data["pid"]:
            win32gui.PostMessage(hwnd, win32con.WM_APP + 55, 0, 0)
        try:
            child.wait(timeout=4)
        except subprocess.TimeoutExpired:
            child.kill()  # only our fixture's Popen handle
            child.wait(timeout=4)


def wait_restored(pid, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if psutil.Process(pid).status() != psutil.STATUS_STOPPED:
            return
        time.sleep(0.05)
    raise AssertionError("fixture remained suspended")


def wait_guardian_resumed(operation_id, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if suspension.read_operation(operation_id).get("phase") == "resumed":
            return
        time.sleep(0.05)
    raise AssertionError("guardian did not commit resumed phase")


def parent_fixture(directory):
    # Used by the outer test to simulate loss of the tray process.
    os.environ["APPDATA"] = directory
    storage.save_baseline([{"exe": r"C:\IntegrationBaseline\keep.exe"}], full_processes=True)
    child, data, target = spawn_fixture(directory)
    result = core.suspend_apps([target], confirm=lambda _: True)
    if not result["suspended"]:
        raise RuntimeError("suspension failed: " + result["reason"])
    data["operation"] = storage.load_pending_restore()[0]["suspend_id"]
    (Path(directory) / "parent-ready.json").write_text(json.dumps(data), encoding="utf-8")
    while True:
        time.sleep(0.1)


if len(sys.argv) == 3 and sys.argv[1] == "--parent-fixture":
    parent_fixture(sys.argv[2])
    raise SystemExit(0)

assert FIXTURE.is_file(), "Build tests/window_fixture.py as SuspensionFixture first"
results = []
for mode in ("normal", "owned_dialog", "confirmation_timeout", "heartbeat_timeout", "emergency_hotkey"):
    with ExitStack() as stack:
        directory = stack.enter_context(tempfile.TemporaryDirectory(prefix="gameswitcher-suspend-test-"))
        stack.enter_context(patch.dict(os.environ, {"APPDATA": directory}))
        storage.save_baseline([{"exe": r"C:\IntegrationBaseline\keep.exe"}], full_processes=True)
        child, data, target = spawn_fixture(directory, ("--with-owned",) if mode == "owned_dialog" else ())
        try:
            original_placement = win32gui.GetWindowPlacement(data["hwnd"])
            if mode == "confirmation_timeout":
                def confirm(_):
                    # The guardian must recover without any cooperation from this callback.
                    time.sleep(suspension.CONFIRM_SECONDS + 0.5)
                    return False
                result = core.suspend_apps([target], confirm=confirm)
                assert result["failed"] and not storage.load_pending_restore(), result
            else:
                result = core.suspend_apps([target], confirm=lambda _: True,
                                          healthy=(lambda: False) if mode == "heartbeat_timeout" else (lambda: True))
                if not result["suspended"] and result["failed"]:
                    saved = storage.load_pending_restore()
                    if saved and saved[0].get("suspend_id"):
                        print(json.dumps(suspension.read_operation(saved[0]["suspend_id"]), ensure_ascii=False), flush=True)
                        log_path = Path(storage.data_dir()) / "suspensions" / (saved[0]["suspend_id"] + ".log")
                        print(log_path.read_text(encoding="utf-8", errors="replace"), flush=True)
                assert result["suspended"], result
                assert psutil.Process(data["pid"]).status() == psutil.STATUS_STOPPED
                saved = storage.load_pending_restore()
                operation = suspension.read_operation(saved[0]["suspend_id"])
                assert operation["nodes"][0]["pid"] == data["pid"]
                assert operation["nodes"][0]["birth"] == target["birth"]
                if mode in ("normal", "owned_dialog"):
                    restored = core.restore_apps()
                    assert restored["restored"] and not storage.load_pending_restore()
                elif mode == "emergency_hotkey":
                    win32api.PostThreadMessage(operation["guardian"]["thread_id"], win32con.WM_HOTKEY, 1, 0)
                    wait_restored(data["pid"])
                    wait_guardian_resumed(saved[0]["suspend_id"])
                    suspension.reconcile_completed()
                    assert not storage.load_pending_restore()
                else:
                    wait_restored(data["pid"])
                    wait_guardian_resumed(saved[0]["suspend_id"])
                    suspension.reconcile_completed()
                    assert not storage.load_pending_restore()
            wait_restored(data["pid"])
            deadline = time.monotonic() + 2
            while win32gui.IsIconic(data["hwnd"]) and time.monotonic() < deadline:
                time.sleep(0.05)
            assert not win32gui.IsIconic(data["hwnd"])
            assert win32gui.GetWindowPlacement(data["hwnd"])[4] == original_placement[4]
            results.append({"case": mode, "result": "restored"})
        finally:
            try:
                core.resume_all_suspended(storage.load_pending_restore())
            finally:
                cleanup(child, data)

with tempfile.TemporaryDirectory(prefix="gameswitcher-parent-crash-") as directory:
    parent = subprocess.Popen([sys.executable, str(Path(__file__)), "--parent-fixture", directory],
                              creationflags=subprocess.CREATE_NO_WINDOW)
    ready = Path(directory) / "parent-ready.json"
    data = None
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and time.monotonic() < deadline:
            if parent.poll() is not None:
                raise AssertionError("parent fixture failed")
            time.sleep(0.05)
        data = json.loads(ready.read_text(encoding="utf-8"))
        assert psutil.Process(data["pid"]).status() == psutil.STATUS_STOPPED
        parent.kill()  # only the dedicated parent fixture, to test guardian independence
        parent.wait(timeout=4)
        wait_restored(data["pid"])
        results.append({"case": "parent_crash", "result": "restored"})
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait(timeout=4)
        if data and win32gui.IsWindow(data["hwnd"]):
            win32gui.PostMessage(data["hwnd"], win32con.WM_APP + 55, 0, 0)
        time.sleep(0.5)

print(json.dumps(results, ensure_ascii=False))
