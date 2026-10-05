"""Actual dependency/import/enumeration smoke check; never runs the tray or actions."""
from contextlib import ExitStack
from importlib.metadata import PackageNotFoundError, version
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with ExitStack() as stack:
    isolated = stack.enter_context(tempfile.TemporaryDirectory(prefix="gameswitcher-smoke-"))
    stack.enter_context(patch.dict(os.environ, {"APPDATA": isolated}))
    import core
    import main
    from pystray import _base
    from pystray._win32 import Icon as Win32Icon
    from pystray._util import win32

    dependencies = {}
    for package in ("pystray", "Pillow", "psutil", "pywin32", "PyInstaller"):
        try:
            dependencies[package] = version(package)
        except PackageNotFoundError:
            dependencies[package] = "not installed"
    result = {"python": sys.version.split()[0], "platform": sys.platform,
              "dependencies": dependencies}
    samples = []
    for _ in range(3):
        start = time.perf_counter()
        apps = core.visible_apps()
        samples.append({"app_count": len(apps), "elapsed_ms": round((time.perf_counter() - start) * 1000, 2)})
    result["read_only_enumeration"] = samples
    result["icon_ico_bytes"] = []
    for mode in (False, True):
        image = main.make_icon_image(mode)
        buffer = BytesIO()
        image.save(buffer, format="ICO")
        result["icon_ico_bytes"].append(len(buffer.getvalue()))
    with patch.object(core, "visible_apps", wraps=core.visible_apps) as scan:
        menu = main.build_menu()
        items = list(menu)
        result["menu_materialization"] = {"top_level_items": len(items), "process_scans": scan.call_count}

    callback_thread = []
    dummy = SimpleNamespace(update_menu=lambda: callback_thread.append(threading.get_ident()))
    callback = _base.Icon._handler(dummy, lambda _: None)
    callback()
    result["menu_refresh_runs_on_callback_thread"] = callback_thread == [threading.get_ident()]

    # Read-only native backend invocation with every native call replaced.
    dummy = SimpleNamespace(_hwnd=1, _menu_hwnd=2, _menu_handle=(3, []), update_menu=Mock())
    with patch.object(win32, "SetForegroundWindow"), patch.object(win32, "GetCursorPos"), \
            patch.object(win32, "TrackPopupMenuEx", return_value=0):
        Win32Icon._on_notify(dummy, 0, win32.WM_RBUTTONUP)
    result["right_click_refresh_calls"] = dummy.update_menu.call_count
    print(json.dumps(result, ensure_ascii=False, indent=2))
