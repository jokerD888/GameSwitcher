"""Windows native tray smoke: hidden icon, temporary state, no application actions."""
from contextlib import ExitStack
import ctypes
from io import BytesIO
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

with ExitStack() as stack:
    temporary = stack.enter_context(tempfile.TemporaryDirectory(prefix="gameswitcher-native-"))
    stack.enter_context(patch.dict(os.environ, {"APPDATA": temporary}))
    import core
    import main
    import storage

    main._refresh_cache()
    storage.save_baseline([])
    storage.merge_pending_restore([], "game")
    main._refresh_cache()
    main_thread = threading.get_ident()
    done = threading.Event()
    result = {"enumerated_instances": len(main._cache["running"]), "dispatch_on_ui_thread": False}
    for mode in (False, True):
        image = main.make_icon_image(mode)
        image.save(BytesIO(), format="ICO")
    tray = main.TrayIcon("GameSwitcher-Smoke", main.make_icon_image(), "Smoke", main.build_menu())
    main.icon = tray

    def setup(icon):
        def update():
            main.update_tray_state()
            result["dispatch_on_ui_thread"] = threading.get_ident() == main_thread
            done.set()
        icon.dispatch(update)
        if not done.wait(5):
            result["error"] = "native dispatch timed out"
        icon.stop()

    def watchdog():
        # Stop only this test process if the native event loop becomes stuck.
        if not finished.wait(15):
            print("native smoke hung", flush=True)
            os._exit(2)

    finished = threading.Event()
    threading.Thread(target=watchdog, daemon=True).start()
    tray.run(setup=setup)  # supplied setup deliberately keeps the icon invisible
    finished.set()
    main.icon = None
    result["state_after_restart"] = storage.load_state()["mode"]
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if not result["dispatch_on_ui_thread"] or result["state_after_restart"] != "game":
        raise SystemExit(1)
