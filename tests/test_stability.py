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
ps.STATUS_STOPPED = "stopped"
for name in ("AccessDenied", "NoSuchProcess", "ZombieProcess"):
    setattr(ps, name, type(name, (ProcessError,), {}))
ps.Process = MagicMock()
ps.process_iter = MagicMock(return_value=[])
ps.boot_time = MagicMock(return_value=50.0)


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
stubs["win32con"].GW_OWNER, stubs["win32con"].GWL_STYLE, stubs["win32con"].WS_MINIMIZEBOX = 4, -16, 0x20000
stubs["win32gui"].error = type("WindowError", (Exception,), {})
stubs["psutil"], stubs["pystray"] = ps, tray
pil = types.ModuleType("PIL")
pil.Image, pil.ImageDraw = MagicMock(), MagicMock()
stubs["PIL"] = pil

with patch.dict(sys.modules, stubs):
    core = importlib.import_module("core")
    storage = importlib.import_module("storage")
    main = importlib.import_module("main")

# Lazy production modules must see the same isolated dependencies and exception
# classes, rather than importing a second real core/storage after patch.dict exits.
sys.modules.update(stubs)
sys.modules.update({"core": core, "storage": storage, "main": main})


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
    proc.status.return_value = "running"
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
        ps.boot_time.return_value = 50.0
        self.target = app()
        self.procs = {self.target["pid"]: process(self.target)}
        ps.Process.side_effect = lambda pid: self.procs[pid] if pid in self.procs else self._gone()
        ps.process_iter.return_value = []
        core.win32gui.PostMessage.reset_mock(return_value=True, side_effect=True)
        core.win32gui.IsWindow.return_value = True
        core.win32gui.IsWindowVisible.return_value = True
        core.win32process.GetWindowThreadProcessId.side_effect = lambda h: (1, h - 10000)
        main._shutdown.clear()
        main._shutdown_thread = None
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

    def test_deep_suspend_requires_full_baseline_before_scanning_or_journaling(self):
        self.baseline()
        with patch.object(core, "visible_apps") as scan, patch.object(storage, "merge_pending_restore") as save:
            with self.assertRaises(storage.StorageError):
                main._enter_game_mode("suspend")
        scan.assert_not_called()
        save.assert_not_called()

    def test_deep_suspend_without_full_baseline_has_no_os_side_effects(self):
        with self.assertRaises(storage.StorageError):
            core.suspend_apps([self.target])
        ps.Process.assert_not_called()
        core.win32gui.PostMessage.assert_not_called()
        self.run.assert_not_called()
        self.popen.assert_not_called()

    def test_default_entry_always_uses_normal_close(self):
        self.baseline()
        with patch.object(core, "visible_apps", return_value=[self.target]), \
                patch.object(core, "close_apps", return_value=self.empty_close()) as close, \
                patch.object(core, "suspend_apps", side_effect=AssertionError("must not freeze")):
            main._enter_game_mode()
        close.assert_called_once()
        self.assertEqual(storage.load_pending_restore()[0]["action"], "close")

    def test_new_empty_state_defaults_to_close(self):
        self.assertEqual(storage.load_state()["action"], "close")

    def test_old_v2_snapshot_without_action_is_normal_close(self):
        self.raw(storage.PENDING_RESTORE_FILE, json.dumps({"version": 2, "mode": "game", "apps": [self.target]}))
        state = storage.load_state()
        self.assertEqual(state["action"], "close")
        self.assertEqual(state["apps"][0]["action"], "close")

    def test_invalid_state_action_is_not_inferred_as_suspend(self):
        self.raw(storage.PENDING_RESTORE_FILE, json.dumps({"version": 2, "mode": "game", "action": "bad", "apps": []}))
        with self.assertRaises(storage.StorageError):
            storage.load_state()
        with self.assertRaises(storage.StorageError):
            storage.save_pending_restore([], action="bad")

    def test_post_reboot_suspend_record_uses_verified_launch_recovery(self):
        target = dict(self.target, action="suspend", status="suspended", tree_pids=[1001, 1002])
        storage.save_pending_restore([target], "game", action="suspend")
        ps.boot_time.return_value = 200.0
        with patch.object(core, "_matches", return_value=False), \
                patch.object(core, "visible_apps", return_value=[]), patch.object(core, "launch_app", return_value=True) as launch:
            result = core.restore_apps()
        launch.assert_called_once()
        self.assertEqual(result["blocked_suspend"], [])
        self.assertEqual(len(result["restored"]), 1)
        self.assertEqual(storage.load_pending_restore(), [])
        ps.Process.assert_not_called()

    def test_current_boot_suspend_record_is_never_blindly_resumed_or_relaunched(self):
        target = dict(self.target, action="suspend", status="suspended", tree_pids=[1001, 2000])
        storage.save_pending_restore([target], "game", action="suspend")
        with patch.object(core, "visible_apps", side_effect=AssertionError("avoid scanning frozen apps")), \
                patch.object(core, "launch_app") as launch:
            result = core.restore_apps()
        launch.assert_not_called()
        self.assertEqual(len(result["blocked_suspend"]), 1)
        self.assertEqual(storage.load_pending_restore()[0]["tree_pids"], [1001, 2000])
        ps.Process.assert_not_called()

    def test_discard_never_terminates_suspended_processes(self):
        target = dict(self.target, action="suspend", status="suspended")
        with self.assertRaises(core.UnsafeSuspendError):
            core.discard_apps([target])
        ps.Process.assert_not_called()
        self.run.assert_not_called()

    def test_discard_after_reboot_does_not_touch_reused_pids(self):
        target = dict(self.target, action="suspend", status="suspended", tree_pids=[1001, core.SELF_PID])
        ps.boot_time.return_value = 200.0
        with patch.object(core, "_matches", return_value=False):
            core.discard_apps([target])
            core.resume_all_suspended([target])
        ps.Process.assert_not_called()

    def test_boot_time_change_does_not_override_live_original_identity(self):
        target = dict(self.target, action="suspend", status="suspended")
        ps.boot_time.return_value = 200.0
        with self.assertRaises(core.UnsafeSuspendError):
            core.discard_apps([target])

    def test_unknown_suspend_identity_is_preserved(self):
        target = dict(self.target, action="suspend", status="suspended")
        del target["birth"]
        with self.assertRaises(core.UnsafeSuspendError):
            core.resume_all_suspended([target])
        ps.Process.assert_not_called()

    def test_menu_requires_full_baseline_then_enables_mode_b(self):
        items = list(main.build_menu())
        deep = [item for item in items if isinstance(item, Item) and item.text.startswith("模式 B")]
        self.assertEqual(len(deep), 1)
        self.assertTrue(callable(deep[0].action))
        self.assertFalse(deep[0].options["enabled"])
        main._cache["full_baseline"] = True
        item = next(i for i in main.build_menu() if isinstance(i, Item) and i.text.startswith("模式 B"))
        self.assertTrue(item.options["enabled"])

    def test_legacy_resume_never_runs_on_exit_message_thread(self):
        callers, done = [], threading.Event()
        main.icon = MagicMock()
        main.icon.stop.side_effect = done.set
        with patch.object(core, "resume_all_suspended", side_effect=lambda _: callers.append(threading.get_ident())):
            main._exit_app()
            self.assertTrue(done.wait(2))
        self.assertTrue(callers)
        self.assertNotEqual(callers[0], threading.get_ident())

    def test_confirmed_restart_migrates_only_gone_original_processes(self):
        target = dict(self.target, action="suspend", status="suspended", tree_pids=[1001, core.SELF_PID])
        storage.save_pending_restore([target], "game", action="suspend")
        ps.Process.side_effect = ps.NoSuchProcess()
        self.assertTrue(core.prepare_legacy_relaunch_after_restart())
        migrated = storage.load_pending_restore()[0]
        self.assertEqual(migrated["action"], "close")
        self.assertNotIn("tree_pids", migrated)
        backups = list(Path(storage.data_dir()).glob("pending_restore.before-restart-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(json.loads(backups[0].read_text())["apps"][0]["tree_pids"], [1001, core.SELF_PID])

    def test_restart_confirmation_cannot_convert_live_suspended_process(self):
        target = dict(self.target, action="suspend", status="suspended")
        storage.save_pending_restore([target], "game", action="suspend")
        with self.assertRaises(core.UnsafeSuspendError):
            core.prepare_legacy_relaunch_after_restart()
        self.assertEqual(storage.load_pending_restore()[0]["action"], "suspend")

    def test_restart_migration_backup_failure_preserves_state(self):
        target = dict(self.target, action="suspend", status="suspended")
        storage.save_pending_restore([target], "game", action="suspend")
        ps.Process.side_effect = ps.NoSuchProcess()
        with patch.object(storage, "_save", side_effect=OSError("backup full")):
            with self.assertRaises(OSError):
                core.prepare_legacy_relaunch_after_restart()
        self.assertEqual(storage.load_pending_restore()[0]["action"], "suspend")

    def freeze_node(self, pid, exe, parent=0, birth=None, **extra):
        return dict(pid=pid, exe=exe, ppid=parent, birth=float(birth if birth is not None else pid),
                    username=extra.get("username", "user"), session=extra.get("session", 1))

    def freeze_plan(self, nodes, roots=None, baseline=None, exclusions=None):
        import suspension
        roots = roots or [app(exe=nodes[0]["exe"], pid=nodes[0]["pid"], birth=nodes[0]["birth"])]
        return suspension.build_plan(roots, nodes, baseline or [], {"exclude_list": exclusions or []},
                                     scope=("user", 1, {0, 4, core.SELF_PID, 2000}))

    def test_complete_baseline_collects_hidden_processes_without_window_scan(self):
        entries = [self.target, app(exe=r"C:\Background\agent.exe", pid=1002)]
        ps.process_iter.return_value = [process(a) for a in entries]
        with patch.object(core, "visible_apps", side_effect=AssertionError("window-only capture")):
            records = core.baseline_processes()
        storage.save_baseline(records, full_processes=True)
        self.assertEqual(len(storage.load_baseline()), 2)
        self.assertTrue(storage.full_baseline_available())

    def test_empty_full_baseline_is_rejected_without_overwriting_old(self):
        storage.save_baseline([self.target])
        with self.assertRaises(storage.StorageError):
            storage.save_baseline([], full_processes=True)
        self.assertEqual(storage.load_baseline()[0]["exe"], self.target["exe"])
        self.assertFalse(storage.full_baseline_available())

    def test_mode_b_plan_checks_baseline_for_each_descendant(self):
        nodes = [self.freeze_node(1001, r"C:\Apps\editor.exe"),
                 self.freeze_node(1002, r"C:\Keep\service.exe", parent=1001)]
        plan = self.freeze_plan(nodes, baseline=[{"exe": nodes[1]["exe"]}])
        self.assertEqual(plan["nodes"], [])
        self.assertIn("基线", plan["skipped"][0]["reason"])

    def test_mode_b_plan_checks_exclusions_for_each_descendant(self):
        nodes = [self.freeze_node(1001, r"C:\Apps\editor.exe"),
                 self.freeze_node(1002, r"C:\Keep\voice.exe", parent=1001)]
        self.assertEqual(self.freeze_plan(nodes, exclusions=["voice.exe"])["nodes"], [])

    def test_mode_b_plan_preserves_remote_control_and_its_helpers(self):
        nodes = [self.freeze_node(1001, r"C:\Remote\GameViewer.exe"),
                 self.freeze_node(1002, r"C:\Remote\unknown-helper.exe", parent=1001)]
        root = app(exe=nodes[1]["exe"], pid=1002, birth=1002)
        self.assertEqual(self.freeze_plan(nodes, roots=[root])["nodes"], [])

    def test_mode_b_plan_rejects_cross_session_and_unreadable_children(self):
        for child in (self.freeze_node(1002, r"C:\Apps\worker.exe", parent=1001, session=2),
                      self.freeze_node(1002, "", parent=1001)):
            nodes = [self.freeze_node(1001, r"C:\Apps\editor.exe"), child]
            self.assertEqual(self.freeze_plan(nodes)["nodes"], [])

    def test_mode_b_preserves_new_helpers_of_a_baseline_application(self):
        nodes = [self.freeze_node(1001, r"C:\Keep\baseline.exe"),
                 self.freeze_node(1002, r"C:\Keep\new-helper.exe", parent=1001)]
        root = app(exe=nodes[1]["exe"], pid=1002, birth=1002)
        self.assertEqual(self.freeze_plan(nodes, roots=[root], baseline=[{"exe": nodes[0]["exe"]}])["nodes"], [])

    def test_baseline_explorer_does_not_protect_every_new_application(self):
        nodes = [self.freeze_node(1001, r"C:\Windows\explorer.exe"),
                 self.freeze_node(1002, r"C:\Apps\editor.exe", parent=1001)]
        root = app(exe=nodes[1]["exe"], pid=1002, birth=1002)
        self.assertEqual(len(self.freeze_plan(nodes, roots=[root], baseline=[{"exe": nodes[0]["exe"]}])["nodes"]), 1)

    def test_mode_b_deduplicates_overlapping_visible_process_trees(self):
        nodes = [self.freeze_node(1001, r"C:\Apps\editor.exe"),
                 self.freeze_node(1002, r"C:\Apps\worker.exe", parent=1001)]
        roots = [app(exe=n["exe"], pid=n["pid"], birth=n["birth"]) for n in nodes]
        plan = self.freeze_plan(nodes, roots=roots)
        self.assertEqual([n["pid"] for n in plan["nodes"]], [1001, 1002])
        self.assertEqual(len(plan["apps"]), 2)

    def test_mode_b_does_not_follow_reused_parent_pid(self):
        nodes = [self.freeze_node(1001, r"C:\Apps\editor.exe", birth=200),
                 self.freeze_node(1002, r"C:\Other\old.exe", parent=1001, birth=100)]
        plan = self.freeze_plan(nodes)
        self.assertEqual([n["pid"] for n in plan["nodes"]], [1001])

    def test_mode_b_journals_all_children_before_guardian_starts(self):
        import suspension
        child = app(exe=r"C:\Apps\worker.exe", pid=1002)
        plan = {"apps": [self.target], "nodes": [self.target, child], "skipped": []}
        client = MagicMock()
        client.ready.return_value = True
        client.confirm.return_value = True
        def start(operation_id, *args):
            pending = storage.load_pending_restore()
            self.assertEqual(pending[0]["suspend_id"], operation_id)
            self.assertEqual([n["pid"] for n in suspension.read_operation(operation_id)["nodes"]], [1001, 1002])
            return client
        with patch.object(suspension, "preview", return_value=plan), \
                patch.object(suspension, "_parent_identity", return_value=self.target), \
                patch.object(suspension, "_start_client", side_effect=start), \
                patch.object(suspension, "_sessions", {}):
            result = core.suspend_apps([self.target], confirm=lambda seconds: True)
        self.assertEqual(len(result["suspended"]), 1)
        self.assertEqual(storage.load_pending_restore()[0]["status"], "suspended")

    def test_mode_b_snapshot_failure_prevents_guardian_creation(self):
        import suspension
        plan = {"apps": [self.target], "nodes": [self.target], "skipped": []}
        with patch.object(suspension, "preview", return_value=plan), \
                patch.object(suspension, "_parent_identity", return_value=self.target), \
                patch.object(storage, "merge_pending_restore", side_effect=OSError("disk")), \
                patch.object(suspension, "_start_client") as start:
            with self.assertRaises(OSError):
                core.suspend_apps([self.target], confirm=lambda seconds: True)
        start.assert_not_called()

    def test_mode_b_cannot_start_without_input_confirmation_callback(self):
        import suspension
        plan = {"apps": [self.target], "nodes": [self.target], "skipped": []}
        with patch.object(suspension, "preview", return_value=plan), \
                patch.object(suspension, "_start_client") as start:
            with self.assertRaises(core.ScanError):
                core.suspend_apps([self.target])
        start.assert_not_called()
        self.assertEqual(storage.load_pending_restore(), [])

    def test_mode_b_unconfirmed_trial_rolls_back_and_removes_only_new_records(self):
        import suspension
        old = app(exe=r"C:\Old\old.exe", pid=1009)
        storage.save_pending_restore([old])
        plan = {"apps": [self.target], "nodes": [self.target], "skipped": []}
        client = MagicMock()
        client.ready.return_value = True
        with patch.object(suspension, "preview", return_value=plan), \
                patch.object(suspension, "_parent_identity", return_value=self.target), \
                patch.object(suspension, "_start_client", return_value=client), \
                patch.object(suspension, "_sessions", {}):
            result = core.suspend_apps([self.target], confirm=lambda seconds: False)
        client.resume.assert_called_once()
        client.confirm.assert_not_called()
        self.assertTrue(result["failed"])
        self.assertEqual([a["id"] for a in storage.load_pending_restore()], [old["id"]])

    def test_mode_b_failed_rollback_keeps_durable_references(self):
        import suspension
        plan = {"apps": [self.target], "nodes": [self.target], "skipped": []}
        client = MagicMock()
        client.ready.return_value = True
        client.resume.side_effect = OSError("resume failed")
        with patch.object(suspension, "preview", return_value=plan), \
                patch.object(suspension, "_parent_identity", return_value=self.target), \
                patch.object(suspension, "_start_client", return_value=client), \
                patch.object(suspension, "_sessions", {}):
            core.suspend_apps([self.target], confirm=lambda seconds: False)
        self.assertTrue(storage.load_pending_restore()[0]["suspend_id"])

    def test_mode_b_protects_remote_helpers_after_reparenting(self):
        nodes = [self.freeze_node(1001, r"C:\Remote\GameViewer.exe"),
                 self.freeze_node(1002, r"C:\Remote\other-helper.exe")]
        root = app(exe=nodes[1]["exe"], pid=1002, birth=1002)
        self.assertEqual(self.freeze_plan(nodes, roots=[root])["nodes"], [])

    def test_guardian_restore_groups_overlapping_apps_once(self):
        import suspension
        second = app(pid=1002)
        self.procs[1002] = process(second)
        entries = [dict(a, action="suspend", status="suspended", suspend_id="a" * 32)
                   for a in (self.target, second)]
        storage.save_pending_restore(entries)
        with patch.object(suspension, "resume_records") as resume, patch.object(core, "visible_apps", return_value=[]):
            result = core.restore_apps()
        resume.assert_called_once()
        self.assertEqual(len(result["restored"]), 2)
        self.assertEqual(storage.load_pending_restore(), [])

    def test_guardian_restore_failure_keeps_all_references(self):
        import suspension
        entry = dict(self.target, action="suspend", status="suspended", suspend_id="a" * 32)
        storage.save_pending_restore([entry])
        with patch.object(suspension, "resume_records", side_effect=OSError("still recovering")), \
                patch.object(core, "visible_apps", return_value=[]), patch.object(core, "launch_app") as launch:
            result = core.restore_apps()
        launch.assert_not_called()
        self.assertEqual(len(result["missing"]), 1)
        self.assertEqual(storage.load_pending_restore()[0]["suspend_id"], "a" * 32)

    def test_guardian_records_after_reboot_use_launch_context(self):
        import suspension
        entry = dict(self.target, action="suspend", status="suspended", suspend_id="a" * 32)
        storage.save_pending_restore([entry])
        with patch.object(suspension, "resume_records"), patch.object(core, "_matches", return_value=False), \
                patch.object(core, "visible_apps", return_value=[]), patch.object(core, "launch_app", return_value=True) as launch:
            core.restore_apps()
        self.assertNotIn("suspend_id", launch.call_args.args[0])
        self.assertEqual(launch.call_args.args[0]["action"], "close")

    def test_external_suspend_is_not_reported_as_fully_restored(self):
        import suspension
        entry = dict(self.target, action="suspend", status="suspended", suspend_id="a" * 32)
        storage.save_pending_restore([entry])
        self.procs[1001].status.return_value = "stopped"
        with patch.object(suspension, "resume_records"), patch.object(core, "visible_apps", return_value=[]):
            result = core.restore_apps()
        self.assertEqual(result["restored"], [])
        self.assertEqual(len(result["missing"]), 1)
        self.assertTrue(storage.load_pending_restore()[0]["suspend_id"])

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

    def test_visible_apps_skips_oserror_on_individual_process(self):
        broken = app(pid=1002)
        broken_proc = process(broken)
        broken_proc.cwd.side_effect = OSError("partial copy")
        entries = [self.target, broken]
        self.procs = {1001: process(self.target), 1002: broken_proc}
        ps.process_iter.return_value = list(self.procs.values())
        def enumerate_windows(callback, arg):
            for a in entries:
                callback(a["hwnds"][0], arg)
        with patch.object(core.win32gui, "EnumWindows", side_effect=enumerate_windows), \
                patch.object(core.win32gui, "GetWindowText", return_value="Doc"):
            result = core.visible_apps()
        self.assertEqual([a["pid"] for a in result], [1001])

    def test_capture_windows_skips_owned_and_zerosize_and_clears_lasterror(self):
        import suspension
        nodes = [self.freeze_node(1001, r"C:\Apps\editor.exe")]
        # 11001=main, 11002=owned by 11001, 11003=0x0 helper, 11004=titleless w/o MINIMIZEBOX
        pids = {11001: 1001, 11002: 1001, 11003: 1001, 11004: 1001}
        owners = {11001: 0, 11002: 11001, 11003: 0, 11004: 0}
        rects = {11001: (0, 0, 800, 600), 11002: (10, 10, 200, 200), 11003: (0, 0, 0, 0), 11004: (0, 0, 100, 100)}
        titles = {11001: "Editor", 11002: "Find", 11003: "", 11004: ""}
        styles = {11001: 0x20000, 11002: 0, 11003: 0, 11004: 0}
        def enumerate_windows(callback, arg):
            for h in (11001, 11002, 11003, 11004):
                callback(h, arg)
        with patch.object(suspension.win32gui, "EnumWindows", side_effect=enumerate_windows), \
                patch.object(suspension.win32process, "GetWindowThreadProcessId", side_effect=lambda h: (1, pids[h])), \
                patch.object(suspension.win32gui, "GetWindow", side_effect=lambda h, _: owners[h]), \
                patch.object(suspension.win32gui, "GetWindowRect", side_effect=lambda h: rects[h]), \
                patch.object(suspension.win32gui, "GetWindowText", side_effect=lambda h: titles[h]), \
                patch.object(suspension.win32gui, "GetWindowLong", side_effect=lambda h, _: styles[h]), \
                patch.object(suspension.win32gui, "GetWindowPlacement", return_value=(0, 1, (-1, -1), (-1, -1), (0, 0, 800, 600))), \
                patch.object(suspension.win32api, "SetLastError") as clear_err:
            captured = suspension._capture_windows(nodes)
        self.assertEqual([w["hwnd"] for w in captured], [11001])
        clear_err.assert_called()

    def test_pending_windows_accepts_hidden_or_iconic_windows(self):
        import suspension
        windows = [{"hwnd": 11001}, {"hwnd": 11002}, {"hwnd": 11003}]
        with patch.object(suspension.win32gui, "IsWindow", return_value=True), \
                patch.object(suspension.win32gui, "IsWindowVisible", side_effect=lambda h: h != 11002), \
                patch.object(suspension.win32gui, "IsIconic", side_effect=lambda h: h == 11001):
            pending = suspension._pending_windows(windows)
        self.assertEqual([w["hwnd"] for w in pending], [11003])

    def test_mode_b_all_skipped_stays_ready_with_reasons(self):
        storage.save_baseline([{"exe": r"C:\Keep\base.exe"}], full_processes=True)
        skipped_result = {"suspended": [], "failed": [],
                          "skipped": [{"app": self.target, "reason": "应用树包含保留进程"}], "reason": ""}
        with patch.object(core, "visible_apps", return_value=[self.target]), \
                patch.object(core, "suspend_apps", return_value=skipped_result):
            main._enter_game_mode("suspend")
        self.assertEqual(storage.load_state()["mode"], "ready")
        self.assertIn("命中保护规则", main._msg_box.call_args.args[0])

    def test_preview_allows_exceeding_max_nodes_and_formats_long_lists(self):
        import suspension
        nodes = [self.freeze_node(1001, r"C:\Apps\editor.exe")]
        for idx in range(suspension.MAX_NODES + 5):
            nodes.append(self.freeze_node(2001 + idx, rf"C:\Apps\worker{idx}.exe", parent=1001, birth=1002.0 + idx))
        plan = suspension.build_plan([app(exe=nodes[0]["exe"], pid=1001, birth=1001.0)],
                                     nodes, [], {"exclude_list": []},
                                     scope=("user", 1, {0, 4, core.SELF_PID}), enforce_limit=False)
        self.assertGreater(len(plan["nodes"]), suspension.MAX_NODES)
        text = main._format_lines([f"item-{i}" for i in range(25)], limit=10)
        self.assertIn("其余 15 项已省略", text)

    def test_reconcile_completed_prunes_only_old_resumed_operations(self):
        import suspension
        op_dir = Path(storage.data_dir()) / "suspensions"
        op_dir.mkdir(parents=True, exist_ok=True)
        total = suspension.MAX_HISTORY_FILES + 5
        active_id = f"{999:032x}"
        unresumed_id = f"{998:032x}"
        for i in range(total):
            op_id = f"{i:032x}"
            p = op_dir / f"{op_id}.json"
            p.write_text(json.dumps({"version": 1, "id": op_id, "nodes": [], "phase": "resumed", "reason": "ok"}))
            (op_dir / f"{op_id}.log").write_text("log")
            os.utime(p, (1000 + i, 1000 + i))
        # Unresumed old record must NEVER be deleted
        old_bad = op_dir / f"{unresumed_id}.json"
        old_bad.write_text(json.dumps({"version": 1, "id": unresumed_id, "nodes": [], "phase": "recovery_failed"}))
        os.utime(old_bad, (1, 1))
        # Active resumed record currently in pending_restore triggers reconcile_completed
        (op_dir / f"{active_id}.json").write_text(
            json.dumps({"version": 1, "id": active_id, "nodes": [], "phase": "resumed", "reason": "热键恢复"}))
        entry = dict(self.target, action="suspend", status="suspended", suspend_id=active_id)
        storage.save_pending_restore([entry])
        with patch.object(core, "_matches", return_value=True):
            completed = suspension.reconcile_completed()
        self.assertEqual(completed, {active_id})
        self.assertTrue(old_bad.exists())
        remaining_resumed = [f for f in op_dir.glob("*.json") if f.stem != unresumed_id]
        self.assertLessEqual(len(remaining_resumed), suspension.MAX_HISTORY_FILES)

    def test_suspension_recovered_reschedules_when_action_lock_held(self):
        timers = []
        class FakeTimer:
            def __init__(self, delay, fn):
                self.delay, self.fn, self.daemon = delay, fn, False
            def start(self):
                timers.append(self)
        self.assertTrue(main._action_lock.acquire(blocking=False))
        try:
            with patch.object(main.threading, "Timer", side_effect=FakeTimer):
                main._suspension_recovered(None)
        finally:
            main._action_lock.release()
        self.assertEqual(len(timers), 1)
        main.notify.assert_not_called()


if __name__ == "__main__":
    print("SAFE REGRESSION: all process/window/launch APIs mocked; temporary storage.", flush=True)
    unittest.main(verbosity=2)
