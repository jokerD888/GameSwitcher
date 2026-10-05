"""Safety regression tests. All OS actions are mocked; storage is temporary."""
from contextlib import ExitStack
import importlib
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import threading
import types
import unittest
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class ProcessError(Exception):
    pass


ps = types.ModuleType("psutil")
ps.Error = ProcessError
for name in ("AccessDenied", "NoSuchProcess", "ZombieProcess"):
    setattr(ps, name, type(name, (ProcessError,), {}))
ps.Process = MagicMock()
ps.process_iter = MagicMock(return_value=[])


class Item:
    def __init__(self, text, action, **options):
        self.text, self.action, self.options = text, action, options


class Menu:
    SEPARATOR = object()

    def __init__(self, *items):
        self.items = items

    def __iter__(self):
        return iter(self.items)


class Icon:
    def __init__(self, *args, **kwargs):
        self._message_handlers, self._hwnd = {}, None

    def _mark_ready(self):
        pass


tray = types.ModuleType("pystray")
tray.Icon, tray.Menu, tray.MenuItem = Icon, Menu, Item
stubs = {name: MagicMock() for name in ("win32api", "win32con", "win32gui", "win32process")}
stubs["win32con"].WM_APP, stubs["win32con"].WM_CLOSE = 32768, 16
stubs["win32gui"].error = type("WindowError", (Exception,), {})
stubs["psutil"], stubs["pystray"] = ps, tray
pil = types.ModuleType("PIL")
pil.Image, pil.ImageDraw = MagicMock(), MagicMock()
stubs["PIL"] = pil

with patch.dict(sys.modules, stubs):
    core = importlib.import_module("core")
    storage = importlib.import_module("storage")
    main = importlib.import_module("main")


def app(exe=r"C:\Apps\editor.exe", pid=1001, birth=100.0, **extra):
    data = {"pid": pid, "birth": birth, "exe": exe, "name": os.path.basename(exe),
            "username": "user", "session": 1, "cmdline": [exe, f"doc-{pid}.txt"],
            "cwd": r"C:\Work", "hwnds": [pid + 10000]}
    data.update(extra)
    return storage.validate_apps([data], "fixture")[0]


def process(entry):
    proc = MagicMock()
    proc.pid = entry["pid"]
    proc.info = {"pid": entry["pid"], "exe": entry["exe"], "username": entry["username"],
                 "create_time": entry["birth"]}
    proc.create_time.return_value = entry["birth"]
    proc.exe.return_value = entry["exe"]
    proc.username.return_value = entry["username"]
    proc.cmdline.return_value = entry["cmdline"]
    proc.cwd.return_value = entry["cwd"]
    return proc


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        tmp = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="gameswitcher-regression-"))
        self.stack.enter_context(patch.dict(os.environ, {"APPDATA": tmp}))
        self.run = self.stack.enter_context(patch.object(core.subprocess, "run"))
        self.popen = self.stack.enter_context(patch.object(core.subprocess, "Popen"))
        self.popen.return_value.poll.return_value = None
        self.startfile = self.stack.enter_context(patch.object(os, "startfile", create=True))
        self.stack.enter_context(patch.object(core.time, "sleep"))
        ticks = iter(i / 10 for i in range(100000))
        self.stack.enter_context(patch.object(core.time, "monotonic", side_effect=lambda: next(ticks)))
        self.stack.enter_context(patch.object(core, "_scope", return_value=("user", 1, {0, 4, core.SELF_PID, 2000})))
        self.stack.enter_context(patch.object(core, "_session_id", return_value=1))
        self.stack.enter_context(patch.object(main, "notify"))
        self.stack.enter_context(patch.object(main, "_msg_box", return_value=main.IDYES))
        self.stack.enter_context(patch.object(main, "_request_update"))
        ps.Process.reset_mock(return_value=True, side_effect=True)
        ps.process_iter.reset_mock(return_value=True, side_effect=True)
        self.target = app()
        self.procs = {self.target["pid"]: process(self.target)}
        ps.Process.side_effect = lambda pid: self.procs[pid] if pid in self.procs else self._gone()
        ps.process_iter.return_value = []
        core.win32gui.PostMessage.reset_mock(return_value=True, side_effect=True)
        core.win32gui.IsWindow.return_value = True
        core.win32gui.IsWindowVisible.return_value = True
        core.win32process.GetWindowThreadProcessId.side_effect = lambda h: (1, h - 10000)
        main._shutdown.clear()
        main.icon = None
        main._cache = {"state": {"mode": "ready", "apps": []}, "config": {"exclude_list": []},
                       "running": [], "autostart": False}
        main._busy = False

    def _gone(self):
        raise ps.NoSuchProcess()

    def raw(self, name, value):
        Path(storage._path(name)).write_text(value, encoding="utf-8")

    def baseline(self):
        storage.save_baseline([])

    def empty_close(self):
        return {"closed": [], "failed": [], "skipped": [self.target]}

    def test_exact_path_never_matches_other_installation(self):
        entries = [self.target, app(r"D:\Protected\editor.exe", pid=1002)]
        ps.process_iter.return_value = [process(a) for a in entries]
        self.assertEqual(core.pids_by_exes([self.target["exe"]])[self.target["exe"]], [1001])

    def test_process_queries_restrict_user_session_and_ancestors(self):
        entries = [self.target, app(pid=2000), app(pid=1002, username="other"), app(pid=1003)]
        ps.process_iter.return_value = [process(a) for a in entries]
        with patch.object(core, "_session_id", side_effect=lambda pid: 2 if pid == 1003 else 1):
            self.assertEqual(core.apps_by_exe(self.target["exe"]), [1001])

    def test_snapshots_precede_first_close(self):
        self.baseline()
        def close(*args, **kwargs):
            stored = storage.load_pending_restore()
            self.assertEqual(stored[0]["id"], self.target["id"])
            self.assertEqual(stored[0]["status"], "prepared")
            self.assertEqual(storage.load_state()["mode"], "entering")
            return {"closed": [self.target], "failed": [], "skipped": []}
        with patch.object(core, "visible_apps", return_value=[self.target]), patch.object(core, "close_apps", side_effect=close):
            main._enter_game_mode()
        self.assertEqual(storage.load_pending_restore()[0]["status"], "closed")

    def test_snapshot_failure_prevents_all_close_requests(self):
        self.baseline()
        with patch.object(core, "visible_apps", return_value=[self.target]), \
                patch.object(storage, "merge_pending_restore", side_effect=OSError("full")), \
                patch.object(core, "close_apps") as close:
            with self.assertRaises(OSError):
                main._enter_game_mode()
        close.assert_not_called()

    def test_failed_or_partially_closed_app_retains_record(self):
        self.baseline()
        with patch.object(core, "visible_apps", return_value=[self.target]), \
                patch.object(core, "close_apps", return_value=self.empty_close()):
            main._enter_game_mode()
        self.assertEqual(storage.load_pending_restore()[0]["status"], "not_closed")
        self.assertEqual(storage.load_state()["mode"], "partial")

    def test_missing_baseline_requires_capture(self):
        with patch.object(core, "close_apps") as close:
            main._enter_game_mode()
        close.assert_not_called()
        main._msg_box.assert_called_once()

    def test_corrupt_baseline_does_not_close(self):
        self.raw(storage.BASELINE_FILE, "{broken")
        with patch.object(core, "close_apps") as close:
            with self.assertRaises(storage.StorageError):
                main._enter_game_mode()
        close.assert_not_called()

    def test_corrupt_config_does_not_drop_exclusions(self):
        self.raw(storage.CONFIG_FILE, "{broken")
        with self.assertRaises(storage.StorageError):
            storage.load_config()

    def test_config_read_permission_error_is_not_empty_default(self):
        with patch("builtins.open", side_effect=PermissionError("locked")):
            with self.assertRaises(storage.StorageError):
                storage.load_config()

    def test_wrong_json_shapes_are_rejected(self):
        for filename, loader in ((storage.CONFIG_FILE, storage.load_config),
                                 (storage.BASELINE_FILE, storage.load_baseline),
                                 (storage.PENDING_RESTORE_FILE, storage.load_state)):
            for value in ("null", '"text"', "12"):
                with self.subTest(filename=filename, value=value):
                    self.raw(filename, value)
                    with self.assertRaises(storage.StorageError):
                        loader()

    def test_invalid_timeouts_rejected_before_messages(self):
        for timeout in ("bad", 0, -1, math.inf, math.nan, True, 61, 10 ** 500):
            with self.subTest(timeout=timeout):
                with self.assertRaises(storage.StorageError):
                    core.close_apps([self.target], {"grace_timeout_sec": timeout})
        core.win32gui.PostMessage.assert_not_called()

    def test_close_never_forces_or_uses_process_tree(self):
        result = core.close_apps([self.target], {"grace_timeout_sec": 0.1})
        self.assertEqual(result["skipped"][0]["id"], self.target["id"])
        self.run.assert_not_called()
        self.popen.assert_not_called()
        core.win32gui.PostMessage.assert_called_once_with(self.target["hwnds"][0], 16, 0, 0)

    def test_access_denied_is_unknown_not_exited(self):
        ps.Process.side_effect = ps.AccessDenied()
        result = core.close_apps([self.target], {"grace_timeout_sec": 0.1})
        self.assertEqual(result["failed"][0]["id"], self.target["id"])
        core.win32gui.PostMessage.assert_not_called()

    def test_pid_reuse_receives_no_close_message(self):
        self.procs[1001].create_time.return_value = 200.0
        result = core.close_apps([self.target], {"grace_timeout_sec": 0.1})
        self.assertEqual(len(result["closed"]), 1)
        core.win32gui.PostMessage.assert_not_called()

    def test_new_process_does_not_join_close_set(self):
        ps.Process.side_effect = ps.NoSuchProcess()
        core.close_apps([self.target], {"grace_timeout_sec": 0.1})
        core.win32gui.PostMessage.assert_not_called()
        ps.process_iter.assert_not_called()
        self.run.assert_not_called()

    def test_ancestor_target_aborts_entire_batch(self):
        with self.assertRaises(core.ScanError):
            core.close_apps([self.target, app(pid=2000)], {"grace_timeout_sec": 0.1})
        core.win32gui.PostMessage.assert_not_called()

    def test_cancel_prevents_messages_and_retains_alive_targets(self):
        cancel = threading.Event()
        cancel.set()
        result = core.close_apps([self.target], {"grace_timeout_sec": 0.1}, cancel=cancel)
        self.assertEqual(len(result["skipped"]), 1)
        core.win32gui.PostMessage.assert_not_called()

    def test_baseline_and_path_exclusion_remain_protected(self):
        protected = app(r"D:\Protected\editor.exe", pid=1002)
        storage.save_baseline([protected])
        storage.save_config({"exclude_list": [self.target["exe"]]})
        with patch.object(core, "visible_apps", return_value=[self.target, protected]), patch.object(core, "close_apps") as close:
            main._enter_game_mode()
        close.assert_not_called()
        self.assertEqual(storage.load_state()["mode"], "game")

    def test_no_targets_persists_game_mode_without_pending(self):
        self.baseline()
        with patch.object(core, "visible_apps", return_value=[]):
            main._enter_game_mode()
        self.assertEqual(storage.load_state()["mode"], "game")
        self.assertEqual(storage.load_pending_restore(), [])

    def test_replace_failure_preserves_old_state(self):
        storage.save_pending_restore([self.target])
        with patch.object(storage.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                storage.save_pending_restore([app(pid=1002)])
        self.assertEqual(storage.load_pending_restore()[0]["id"], self.target["id"])
        self.assertEqual(list(Path(storage.data_dir()).glob("*.tmp")), [])

    def test_fsync_failure_aborts_state_commit(self):
        storage.save_pending_restore([self.target])
        with patch.object(storage.os, "fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                storage.clear_pending_restore()
        self.assertEqual(len(storage.load_pending_restore()), 1)

    def test_clear_failure_propagates_and_retains_record(self):
        storage.save_pending_restore([self.target])
        with patch.object(storage.os, "replace", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                storage.clear_pending_restore()
        self.assertEqual(len(storage.load_pending_restore()), 1)

    def test_legacy_list_snapshot_is_read_without_overwrite(self):
        legacy = dict(self.target)
        for key in ("id", "birth", "username", "session"):
            legacy.pop(key, None)
        original = json.dumps([legacy])
        self.raw(storage.PENDING_RESTORE_FILE, original)
        self.assertEqual(storage.load_state()["mode"], "game")
        self.assertTrue(storage.load_pending_restore()[0]["id"])
        self.assertEqual(Path(storage._path(storage.PENDING_RESTORE_FILE)).read_text(), original)

    def test_same_exe_instances_keep_distinct_records(self):
        storage.merge_pending_restore([self.target, app(pid=1002)])
        self.assertEqual(len(storage.load_pending_restore()), 2)
        storage.merge_pending_restore([self.target])
        self.assertEqual(len(storage.load_pending_restore()), 2)

    def test_baseline_stores_only_required_identity(self):
        storage.save_baseline([self.target])
        raw = json.loads(Path(storage._path(storage.BASELINE_FILE)).read_text())
        self.assertEqual(set(raw[0]), {"exe", "name"})

    def test_other_path_running_does_not_suppress_restore(self):
        storage.save_pending_restore([self.target])
        with patch.object(core, "visible_apps", return_value=[app(r"D:\Other\editor.exe", pid=1002)]), \
                patch.object(core, "launch_app", return_value=False) as launch:
            result = core.restore_apps()
        launch.assert_called_once()
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(len(storage.load_pending_restore()), 1)

    def test_failed_launch_retains_record(self):
        storage.save_pending_restore([self.target])
        with patch.object(core, "visible_apps", return_value=[]), patch.object(core.os.path, "isfile", return_value=False):
            result = core.restore_apps()
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(len(storage.load_pending_restore()), 1)

    def test_immediate_nonzero_exit_is_not_success(self):
        self.popen.return_value.poll.return_value = 1
        with patch.object(core, "visible_apps", return_value=[]), \
                patch.object(core.os.path, "isfile", return_value=True), patch.object(core.os.path, "isdir", return_value=True):
            self.assertFalse(core.launch_app(self.target))
        self.popen.return_value.poll.assert_called()

    def test_no_new_window_does_not_confirm_restore(self):
        with patch.object(core, "visible_apps", return_value=[self.target]), \
                patch.object(core.os.path, "isfile", return_value=True), patch.object(core.os.path, "isdir", return_value=True):
            self.assertFalse(core.launch_app(self.target, timeout=1))

    def test_new_stable_window_confirms_launch(self):
        new = app(pid=1002, birth=200, cmdline=self.target["cmdline"])
        with patch.object(core, "visible_apps", side_effect=[[], *([new] for _ in range(20))]), \
                patch.object(core.os.path, "isfile", return_value=True), patch.object(core.os.path, "isdir", return_value=True):
            self.assertTrue(core.launch_app(self.target, timeout=3))

    def test_shell_fallback_preserves_arguments_and_cwd(self):
        self.popen.side_effect = OSError("elevation")
        with patch.object(core, "visible_apps", return_value=[]), \
                patch.object(core.os.path, "isfile", return_value=True), patch.object(core.os.path, "isdir", return_value=True):
            self.assertFalse(core.launch_app(self.target, timeout=1))
        self.startfile.assert_called_once_with(self.target["exe"], arguments="doc-1001.txt", cwd=self.target["cwd"])

    def test_missing_working_directory_does_not_change_context(self):
        with patch.object(core.os.path, "isfile", return_value=True), patch.object(core.os.path, "isdir", return_value=False):
            self.assertFalse(core.launch_app(self.target))
        self.popen.assert_not_called()
        self.startfile.assert_not_called()

    def test_running_different_document_does_not_suppress_restore(self):
        storage.save_pending_restore([self.target])
        with patch.object(core, "visible_apps", return_value=[app(pid=1002)]), \
                patch.object(core, "launch_app", return_value=False) as launch:
            core.restore_apps()
        launch.assert_called_once()
        self.assertEqual(len(storage.load_pending_restore()), 1)

    def test_running_matching_document_consumed_once(self):
        second = app(pid=1002, cmdline=self.target["cmdline"])
        current = app(pid=1003, birth=200, cmdline=self.target["cmdline"])
        storage.save_pending_restore([self.target, second])
        with patch.object(core, "visible_apps", return_value=[current]), patch.object(core, "launch_app", return_value=False):
            result = core.restore_apps()
        self.assertEqual(len(result["already_running"]), 1)
        self.assertEqual(len(storage.load_pending_restore()), 1)

    def test_partial_surviving_window_keeps_snapshot(self):
        target = app(hwnds=[11001, 11002])
        current = app(hwnds=[11001])
        storage.save_pending_restore([target])
        with patch.object(core, "visible_apps", return_value=[current]), patch.object(core, "launch_app") as launch:
            core.restore_apps()
        launch.assert_not_called()
        self.assertEqual(len(storage.load_pending_restore()), 1)

    def test_restored_record_removed_by_id_not_by_exe(self):
        second = app(pid=1002)
        storage.save_pending_restore([self.target, second])
        with patch.object(core, "visible_apps", return_value=[]), patch.object(core, "launch_app", side_effect=[True, False]):
            core.restore_apps()
        self.assertEqual([a["id"] for a in storage.load_pending_restore()], [second["id"]])

    def test_uncertain_launch_is_not_automatically_repeated(self):
        storage.save_pending_restore([self.target])
        with patch.object(core, "visible_apps", return_value=[]), patch.object(core, "launch_app", return_value=False) as launch:
            core.restore_apps()
            core.restore_apps()
        launch.assert_called_once()
        self.assertTrue(storage.load_pending_restore()[0]["restore_attempted"])

    def test_launch_intent_is_durable_before_launch(self):
        storage.save_pending_restore([self.target])
        def launch(*args):
            self.assertTrue(storage.load_pending_restore()[0]["restore_attempted"])
            return False
        with patch.object(core, "visible_apps", return_value=[]), patch.object(core, "launch_app", side_effect=launch):
            core.restore_apps()

    def test_launch_journal_failure_prevents_process_creation(self):
        storage.save_pending_restore([self.target])
        original_save = storage.save_pending_restore
        def save(apps, mode=None):
            if any(a.get("restore_attempted") for a in apps):
                raise OSError("disk full before launch")
            return original_save(apps, mode)
        with patch.object(core, "visible_apps", return_value=[]), patch.object(storage, "save_pending_restore", side_effect=save), \
                patch.object(core, "launch_app") as launch:
            with self.assertRaises(OSError):
                core.restore_apps()
        launch.assert_not_called()

    def test_ready_state_cannot_hide_pending_records(self):
        with self.assertRaises(storage.StorageError):
            storage.save_pending_restore([self.target], "ready")

    def test_original_open_window_is_recognized_after_cwd_change(self):
        storage.save_pending_restore([self.target])
        current = app(cwd=r"D:\Different")
        with patch.object(core, "visible_apps", return_value=[current]), patch.object(core, "launch_app") as launch:
            result = core.restore_apps()
        launch.assert_not_called()
        self.assertEqual(len(result["already_running"]), 1)

    def test_manual_reopen_in_same_background_process_can_confirm(self):
        storage.save_pending_restore([self.target])
        current = app(hwnds=[22001])
        with patch.object(core, "visible_apps", return_value=[current]), patch.object(core, "launch_app") as launch:
            result = core.restore_apps()
        launch.assert_not_called()
        self.assertEqual(len(result["already_running"]), 1)

    def test_scan_failure_does_not_hide_durable_recovery_state(self):
        storage.save_pending_restore([self.target], "partial")
        with patch.object(core, "visible_apps", side_effect=core.ScanError("scan failed")), \
                patch.object(main, "autostart_enabled", return_value=False):
            with self.assertRaises(core.ScanError):
                main._refresh_cache()
        self.assertEqual(main._cache["state"]["apps"][0]["id"], self.target["id"])

    def test_multi_instance_scan_retains_each_commandline(self):
        entries = [self.target, app(pid=1002)]
        self.procs = {a["pid"]: process(a) for a in entries}
        ps.process_iter.return_value = list(self.procs.values())
        def enumerate_windows(callback, arg):
            for a in entries:
                callback(a["hwnds"][0], arg)
        with patch.object(core.win32gui, "EnumWindows", side_effect=enumerate_windows), \
                patch.object(core.win32gui, "GetWindowText", return_value="Doc"):
            result = core.visible_apps()
        self.assertEqual(len(result), 2)
        self.assertEqual({tuple(a["cmdline"]) for a in result}, {tuple(a["cmdline"]) for a in entries})

    def test_incomplete_scan_raises_instead_of_partial_success(self):
        with patch.object(core.win32gui, "EnumWindows", side_effect=RuntimeError("unexpected")):
            with self.assertRaises(core.ScanError):
                core.visible_apps()

    def test_ui_menu_uses_only_cache_and_lists_all_paths(self):
        main._cache["running"] = [app(pid=1001 + i, exe=fr"C:\Apps\tool{i:02}.exe") for i in range(20)]
        with patch.object(core, "visible_apps", side_effect=AssertionError("UI scan")), \
                patch.object(storage, "load_config", side_effect=AssertionError("UI disk")):
            items = list(main._exclude_submenu())
            main.build_menu()
        self.assertEqual(len([i for i in items if i.text.startswith("+ ")]), 20)

    def test_ui_dispatch_defers_callback_until_message_thread(self):
        tray_icon = main.TrayIcon()
        callback = MagicMock()
        tray_icon.dispatch(callback)
        callback.assert_not_called()
        tray_icon._on_dispatch()
        callback.assert_called_once()

    def test_ui_updates_wait_until_popup_menu_closes(self):
        tray_icon = main.TrayIcon()
        callback = MagicMock()
        tray_icon._menu_open = True
        tray_icon.dispatch(callback)
        tray_icon._on_dispatch()
        callback.assert_not_called()
        tray_icon._menu_open = False
        tray_icon._on_dispatch()
        callback.assert_called_once()

    def test_exit_waits_for_worker_before_stopping_tray(self):
        started, release, stopped = threading.Event(), threading.Event(), threading.Event()
        def work():
            started.set()
            release.wait(3)
        worker = threading.Thread(target=work)
        worker.start()
        started.wait(1)
        with main._workers_lock:
            main._workers.add(worker)
        fake_icon = MagicMock()
        fake_icon.stop.side_effect = stopped.set
        main.icon = fake_icon
        main._exit_app()
        self.assertFalse(stopped.is_set())
        release.set()
        self.assertTrue(stopped.wait(2))
        worker.join()
        with main._workers_lock:
            main._workers.discard(worker)

    def test_actions_use_non_daemon_threads_and_serialize(self):
        started, release = threading.Event(), threading.Event()
        @main.threaded
        def work():
            started.set()
            release.wait(2)
        with patch.object(main, "_refresh_cache"):
            worker = work()
            started.wait(1)
            self.assertFalse(worker.daemon)
            self.assertIsNone(work())
            release.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertFalse(main._busy)


if __name__ == "__main__":
    print("SAFE REGRESSION: all process/window/launch APIs mocked; temporary storage.", flush=True)
    unittest.main(verbosity=2)
