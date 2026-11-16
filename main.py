"""GameSwitcher tray UI. Disk/process work stays off the native message thread."""
import functools
import logging
from logging.handlers import RotatingFileHandler
import msvcrt
import os
import queue
import sys
import threading
import winreg
import ctypes

import pystray
from PIL import Image, ImageDraw
import win32api
import win32con
import win32gui
import win32process

import core
import storage

log = logging.getLogger("gameswitcher")
MB_ICONINFO, MB_ICONWARN = 0x40, 0x30
MB_TOPMOST, MB_SETFOREGROUND, MB_YESNO = 0x40000, 0x10000, 0x04
MB_YESNOCANCEL = 0x03
IDYES, IDCANCEL = 6, 2
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VAL_NAME = "GameSwitcher"
icon = None
_action_lock = threading.Lock()
_workers_lock = threading.Lock()
_workers = set()
_shutdown = threading.Event()
_shutdown_thread = None
_busy = False
_instance_file = None
_compat_mutex = None
_notify_timer = None
_notify_generation = 0
_cache = {"state": {"mode": "ready", "apps": []}, "config": {"exclude_list": []},
          "running": [], "autostart": False, "full_baseline": False}


class TrayIcon(pystray.Icon):
    """Marshal native menu/icon changes onto pystray's Windows message thread.

    pystray's Win32 backend is pinned and covered by the read-only smoke check.
    """
    DISPATCH_MESSAGE = win32con.WM_APP + 27

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._dispatch_queue = queue.Queue()
        self._menu_open = False
        self._message_handlers[self.DISPATCH_MESSAGE] = self._on_dispatch

    def dispatch(self, callback):
        self._dispatch_queue.put(callback)
        if self._hwnd:
            win32api.PostMessage(self._hwnd, self.DISPATCH_MESSAGE, 0, 0)

    def _on_dispatch(self, *_):
        if self._menu_open:
            return 0  # TrackPopupMenu pumps messages; never destroy its active HMENU.
        while True:
            try:
                callback = self._dispatch_queue.get_nowait()
            except queue.Empty:
                break
            try:
                callback()
            except Exception:
                log.exception("UI callback failed")
        return 0

    def _on_notify(self, wparam, lparam):
        if lparam != win32con.WM_RBUTTONUP:
            return super()._on_notify(wparam, lparam)
        self._menu_open = True
        try:
            return super()._on_notify(wparam, lparam)
        finally:
            self._menu_open = False
            self._on_dispatch()

    def _mark_ready(self):
        super()._mark_ready()
        self._on_dispatch()


def setup_logging():
    try:
        handler = RotatingFileHandler(storage.log_path(), maxBytes=1024 * 1024, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
    except OSError:
        logging.basicConfig(level=logging.INFO)


def _msg_box(text, title="GameSwitcher", flags=MB_ICONWARN):
    if _shutdown.is_set():
        return IDCANCEL
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags | MB_TOPMOST | MB_SETFOREGROUND)


def _format_lines(lines, limit=18, empty="（无）"):
    items = list(lines)
    if not items:
        return empty
    if len(items) <= limit:
        return "\n".join(items)
    shown = "\n".join(items[:limit])
    return f"{shown}\n... 其余 {len(items) - limit} 项已省略（完整内容可打开配置目录查看）"


def _data_location():
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), storage.APP_DIR_NAME)


def notify(msg, title="GameSwitcher", warn=False, timeout=3):
    if warn:
        _msg_box(msg, title, MB_ICONWARN)
        return
    if not icon or _shutdown.is_set():
        return

    def show():
        global _notify_timer, _notify_generation
        _notify_generation += 1
        generation = _notify_generation
        if _notify_timer:
            _notify_timer.cancel()
        icon.notify(msg, title)
        if timeout > 0:
            def dismiss():
                def remove():
                    if generation == _notify_generation and not _shutdown.is_set():
                        icon.remove_notification()
                if not _shutdown.is_set() and icon:
                    icon.dispatch(remove)
            _notify_timer = threading.Timer(timeout, dismiss)
            _notify_timer.daemon = True
            _notify_timer.start()
    icon.dispatch(show)


def _refresh_cache():
    global _cache
    # Keep the recovery menu available even if an unrelated process scan fails.
    _cache = dict(_cache, state=storage.load_state())
    cfg = storage.load_config()
    _cache = dict(_cache, config=cfg, autostart=autostart_enabled(),
                  full_baseline=storage.full_baseline_available())
    _cache = dict(_cache, running=core.visible_apps())


def _request_update():
    if icon and not _shutdown.is_set():
        icon.dispatch(update_tray_state)


def threaded(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        global _busy
        with _workers_lock:
            if _shutdown.is_set():
                return
            if not _action_lock.acquire(blocking=False):
                if not getattr(wrapper, "_silent_busy", False):
                    notify("已有操作正在进行中。")
                return
            _busy = True

            def run():
                global _busy
                try:
                    log.info("action start: %s", fn.__name__)
                    if not _shutdown.is_set():
                        fn(*args, **kwargs)
                    log.info("action done: %s", fn.__name__)
                except Exception as exc:
                    log.exception("action failed: %s", fn.__name__)
                    _msg_box(f"操作未完成：{exc}\n\n原恢复记录会保留。配置与日志目录：\n{_data_location()}")
                finally:
                    try:
                        _refresh_cache()
                    except Exception:
                        log.exception("cache refresh failed; keeping previous display")
                    with _workers_lock:
                        _busy = False
                        _action_lock.release()
                        _workers.discard(threading.current_thread())
                    _request_update()
            worker = threading.Thread(target=run, name=f"action-{fn.__name__}", daemon=False)
            _workers.add(worker)
            try:
                worker.start()
            except Exception:
                _workers.discard(worker)
                _busy = False
                _action_lock.release()
                raise
        _request_update()
        return worker
    return wrapper


def already_running(mutex_name=r"Local\GameSwitcher_SingleInstance"):
    """A per-profile file lock also excludes instances in other login sessions."""
    global _instance_file, _compat_mutex
    stream = open(os.path.join(storage.data_dir(), ".instance.lock"), "a+b")
    if os.fstat(stream.fileno()).st_size == 0:
        stream.write(b"\0")
        stream.flush()
    stream.seek(0)
    try:
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        stream.close()
        return True
    _instance_file = stream
    # Keep the old session mutex as well, so an already running v1 cannot race
    # the migrated snapshot writer in the same desktop session.
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.restype = ctypes.c_int
    _compat_mutex = kernel.CreateMutexW(None, False, mutex_name)
    error = kernel.GetLastError()
    if not _compat_mutex:
        _release_instance()
        raise OSError("无法创建单实例互斥")
    if error == 183:
        _release_instance()
        return True
    return False


def _release_instance():
    global _instance_file, _compat_mutex
    if _compat_mutex:
        ctypes.windll.kernel32.CloseHandle(_compat_mutex)
        _compat_mutex = None
    if _instance_file:
        try:
            _instance_file.seek(0)
            msvcrt.locking(_instance_file.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            _instance_file.close()
            _instance_file = None


@threaded
def do_capture(*_):
    if storage.baseline_exists() and _msg_box("重新捕获将覆盖基线。请先关闭临时软件。\n确定继续？",
                                             flags=MB_YESNO | MB_ICONWARN) != IDYES:
        return
    apps = core.baseline_processes()
    if _shutdown.is_set():
        return
    storage.save_baseline(apps, full_processes=True)
    _msg_box(f"已捕获完整进程基线：{len({core.path_key(a['exe']) for a in apps})} 个程序路径。\n"
             "包含可读取的当前用户会话后台、托盘和有窗口程序。基线程序会保留运行。",
             flags=MB_ICONINFO)


def _suspend_ui_healthy():
    import winprocess
    return bool(icon) and winprocess.ui_responding(icon._hwnd)


def _confirm_suspend_input(seconds):
    thread_id = win32api.GetCurrentThreadId()
    def dismiss():
        def close(hwnd, _):
            try:
                if (win32gui.GetClassName(hwnd) == "#32770"
                        and win32process.GetWindowThreadProcessId(hwnd)[1] == core.SELF_PID):
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
            except win32gui.error:
                pass
            finally:
                win32api.SetLastError(0)
            return True
        win32api.SetLastError(0)
        try:
            win32gui.EnumThreadWindows(thread_id, close, None)
        except win32gui.error:
            pass
    timer = threading.Timer(seconds, dismiss)
    timer.daemon = True
    timer.start()
    try:
        return _msg_box(f"应用已暂时挂起。键盘、鼠标和桌面现在是否正常？\n"
                        f"请在 {seconds} 秒内选择“是”继续；超时或选择“否”会由独立守护自动恢复。\n"
                        "游戏中可按 Ctrl+Alt+F12 紧急恢复。",
                        flags=MB_YESNOCANCEL | MB_ICONINFO) == IDYES
    finally:
        timer.cancel()


@threaded
def do_reconcile_suspensions(*_):
    import suspension
    completed = suspension.reconcile_completed()
    if completed:
        reasons = suspension.operation_reasons(completed)
        detail = "；".join(dict.fromkeys(reasons))
        notify(f"挂起守护已自动恢复应用：{detail}" if detail else "挂起守护已自动恢复应用，请查看日志了解原因。")
do_reconcile_suspensions._silent_busy = True


def _suspension_recovered(_):
    if _shutdown.is_set():
        return
    if _busy or do_reconcile_suspensions() is None:
        if not _shutdown.is_set():
            timer = threading.Timer(1, lambda: _suspension_recovered(None))
            timer.daemon = True
            timer.start()


@threaded
def do_preview_suspend(*_):
    import suspension
    plan = suspension.preview(core.visible_apps(), enforce_limit=False)
    kept = [f"{a['app']['name']}：{a['reason']}" for a in plan["skipped"]]
    warning = ""
    if len(plan["nodes"]) > suspension.MAX_NODES:
        warning = f"⚠️ 注意：去重进程数 ({len(plan['nodes'])}) 超过安全上限 {suspension.MAX_NODES}，直接挂起将被阻止，请先扩大基线或添加排除。\n\n"
    _msg_box(warning + f"拟挂起 {len(plan['apps'])} 个应用 / {len(plan['nodes'])} 个去重后的进程：\n"
             + _format_lines([a["exe"] for a in plan["apps"]], limit=14)
             + "\n\n保留：\n" + _format_lines(kept, limit=14), flags=MB_ICONINFO)


def _enter_game_mode(action="close"):
    if action not in {"close", "suspend"}:
        raise storage.StorageError("无效游戏模式")
    cfg = storage.load_config()
    baseline = storage.load_baseline()
    storage.load_state()  # fail before any window messages if the old journal is corrupt
    if not storage.baseline_exists():
        _msg_box("请先在干净环境中捕获基线，再进入游戏模式。", flags=MB_ICONINFO)
        return
    if action == "suspend" and not storage.full_baseline_available():
        raise storage.StorageError("模式 B 需要重新捕获完整进程基线，旧基线或空基线不能用于挂起")
    baseline_exes = {core.path_key(a["exe"]) for a in baseline}
    targets = [dict(a, status="prepared", action=action) for a in core.visible_apps()
               if core.path_key(a["exe"]) not in baseline_exes
               and not core.is_excluded(a, cfg["exclude_list"])]
    if _shutdown.is_set():
        return
    if action == "suspend":
        result = core.suspend_apps(targets, cfg, cancel=_shutdown, confirm=_confirm_suspend_input,
                                   healthy=_suspend_ui_healthy, recovered=_suspension_recovered)
        if result["failed"]:
            _msg_box("挂起未完成或已回滚，恢复记录按实际结果保留：\n" + result["reason"])
        elif not result["suspended"]:
            if not storage.load_pending_restore():
                storage.clear_pending_restore()
            if result.get("skipped"):
                lines = [f"• {a['app']['name']}：{a['reason']}" for a in result["skipped"]]
                _msg_box("未执行挂起：所有候选应用均命中保护规则而被保留，仍处于就绪状态。\n\n"
                         + _format_lines(lines, limit=18), flags=MB_ICONINFO)
            else:
                notify("当前没有需要挂起的非基线应用，仍处于就绪状态。")
        return
    journal = storage.merge_pending_restore(targets, "entering", action=action)
    if not targets:
        storage.save_pending_restore(journal, "game", action=action)
        notify("当前没有需要处理的非基线应用，已进入游戏模式。")
        return

    result = core.close_apps(targets, cfg, cancel=_shutdown)
    closed = {a["id"] for a in result["closed"]}
    ids = {a["id"] for a in targets}
    for app in journal:
        if app["id"] in ids:
            app["status"] = "closed" if app["id"] in closed else "not_closed"
            app["action"] = "close"
    remaining = result["failed"] + result["skipped"]
    storage.save_pending_restore(journal, "partial" if remaining or _shutdown.is_set() else "game", action="close")
    if remaining:
        _msg_box("以下应用尚未正常关闭，已保留运行及恢复记录；请处理保存提示或手动关闭：\n"
                 + "\n".join(a["name"] for a in remaining))


@threaded
def do_enter_suspend_mode(*_):
    _enter_game_mode(action="suspend")


@threaded
def do_enter_close_mode(*_):
    _enter_game_mode(action="close")


@threaded
def do_restore(*_):
    result = core.restore_apps(cancel=_shutdown)
    if result.get("blocked_suspend"):
        _msg_box("旧版在当前 Windows 会话内的挂起记录缺少可靠的子进程身份。\n"
                 "为避免操作错误进程，记录已保留且不会盲目解冻或结束程序。\n"
                 "请使用 Windows 的「重启」，然后再选择恢复应用。\n"
                 "如果已关机再开机，可选择「确认已重启：恢复旧启动记录」。")
    elif any(a.get("suspend_id") for a in result["missing"]):
        _msg_box("部分挂起事务尚未确认恢复，完整身份记录已保留。\n"
                 "请稍后再次点击“恢复应用”；恢复未完成前不会清空这些记录。")
    elif result["missing"]:
        _msg_box("以下应用尚未确认恢复，记录已保留。可能需要手动确认启动、文档或窗口：\n"
                 + "\n".join(a["name"] for a in result["missing"])
                 + "\n\n确认程序未启动后，可选择菜单中的「重新尝试未确认启动」。")


@threaded
def do_recover_after_restart(*_):
    if _msg_box("仅在已经重启或关机再开机后使用。\n"
                "旧进程内存不能恢复，只能按启动记录尝试重新打开应用。\n"
                "原记录会备份，不会解冻或终止任何 PID。\n\n确定已经重启并继续？",
                flags=MB_YESNO | MB_ICONWARN) != IDYES or _shutdown.is_set():
        return
    core.prepare_legacy_relaunch_after_restart()
    result = core.restore_apps(cancel=_shutdown)
    if result["missing"]:
        _msg_box("部分旧应用未确认恢复，记录已保留，请检查相关应用和日志。")


@threaded
def do_retry_restore(*_):
    pending = storage.load_pending_restore()
    if _msg_box("先确认相关程序确实没有打开。重新尝试可能打开重复窗口。\n确定重新尝试？",
                flags=MB_YESNO | MB_ICONWARN) != IDYES or _shutdown.is_set():
        return
    for app in pending:
        app.pop("restore_attempted", None)
    storage.save_pending_restore(pending, "partial" if pending else "ready")
    result = core.restore_apps(cancel=_shutdown)
    if result["missing"]:
        _msg_box("仍有应用未确认恢复，记录已保留。请检查日志和相关应用。")


@threaded
def do_discard_restore(*_):
    state = storage.load_state()
    if state["apps"] and _msg_box(f"确定清空 {len(state['apps'])} 条恢复记录？\n不会结束任何程序；当前 Windows 会话的旧挂起记录不会被丢弃。",
                                  flags=MB_YESNO | MB_ICONWARN) != IDYES:
        return
    if not _shutdown.is_set():
        core.discard_apps(state["apps"])
        storage.clear_pending_restore()
        notify("恢复记录已清空，已返回就绪状态。")


@threaded
def do_show_baseline(*_):
    apps = storage.load_baseline()
    _msg_box(f"基线程序（共 {len(apps)} 项）：\n"
             + _format_lines([a["exe"] for a in apps], limit=20, empty="（空基线）"), flags=MB_ICONINFO)


@threaded
def do_refresh(*_):
    _refresh_cache()


@threaded
def do_open_data(*_):
    os.startfile(storage.data_dir())


@threaded
def _toggle_exclude(value):
    cfg = storage.load_config()
    key = os.path.normcase(value)
    matches = [e for e in cfg["exclude_list"] if os.path.normcase(e) == key]
    if matches:
        cfg["exclude_list"] = [e for e in cfg["exclude_list"] if os.path.normcase(e) != key]
    else:
        cfg["exclude_list"].append(value)
    storage.save_config(cfg)


def _exclude_submenu():
    items = [pystray.MenuItem("刷新运行应用列表", do_refresh, enabled=not _busy)]
    for value in _cache["config"]["exclude_list"]:
        items.append(pystray.MenuItem(f"✓ {value}", lambda *_, v=value: _toggle_exclude(v), enabled=not _busy))
    seen = set()
    for app in sorted(_cache["running"], key=lambda a: a["exe"].lower()):
        exe, key = app["exe"], os.path.normcase(app["exe"])
        if key not in seen and not core.is_excluded(app, _cache["config"]["exclude_list"]):
            seen.add(key)
            items.append(pystray.MenuItem(f"+ {exe}", lambda *_, v=exe: _toggle_exclude(v), enabled=not _busy))
    items.append(pystray.MenuItem("打开配置与日志目录", do_open_data, enabled=not _busy))
    return pystray.Menu(*items)


def _autostart_command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    script = os.path.abspath(__file__)
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    return f'"{pythonw if os.path.isfile(pythonw) else sys.executable}" "{script}"'


def autostart_enabled():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            command, _ = winreg.QueryValueEx(key, VAL_NAME)
        return command == _autostart_command()
    except OSError:
        return False


@threaded
def toggle_autostart(*_):
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if autostart_enabled():
            winreg.DeleteValue(key, VAL_NAME)
        else:
            winreg.SetValueEx(key, VAL_NAME, 0, winreg.REG_SZ, _autostart_command())


def _exit_app(*_):
    global _shutdown_thread
    with _workers_lock:
        if _shutdown.is_set():
            return
        _shutdown.set()
        workers = list(_workers)
    if _notify_timer:
        _notify_timer.cancel()

    def finish():
        # Close only this application's worker-owned MessageBox dialogs.
        for worker in workers:
            if worker.native_id:
                def close_dialog(hwnd, _):
                    try:
                        if (win32gui.GetClassName(hwnd) == "#32770"
                                and win32process.GetWindowThreadProcessId(hwnd)[1] == core.SELF_PID):
                            if win32gui.GetDlgItem(hwnd, 7):
                                win32gui.PostMessage(hwnd, win32con.WM_COMMAND, 7, 0)
                            else:
                                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                    except win32gui.error:
                        pass
                    finally:
                        win32api.SetLastError(0)
                    return True
                win32api.SetLastError(0)
                try:
                    win32gui.EnumThreadWindows(worker.native_id, close_dialog, None)
                except win32gui.error:
                    pass
            worker.join()
        try:
            core.resume_all_suspended(storage.load_pending_restore())
            import suspension
            suspension.reconcile_completed()
        except Exception:
            log.exception("exit recovery incomplete; durable suspension records retained")
        if icon:
            icon.stop()
        # The instance lock is released by main() only after finish completes.
    thread = threading.Thread(target=finish, name="shutdown", daemon=False)
    with _workers_lock:
        _shutdown_thread = thread
    thread.start()


def build_menu():
    state = _cache["state"]
    enabled = not _busy and not _shutdown.is_set()
    in_game = state["mode"] != "ready"
    action = state.get("action", "close")
    legacy = any(a.get("action") == "suspend" and not a.get("suspend_id") for a in state["apps"])
    action_label = ("旧版挂起记录" if legacy else "挂起保留现场") if action == "suspend" else "正常关闭"
    return pystray.Menu(
        pystray.MenuItem("模式 B：挂起保留现场" if _cache.get("full_baseline") else "模式 B：需先捕获完整进程基线",
                         do_enter_suspend_mode, enabled=enabled and not in_game and _cache.get("full_baseline", False)),
        pystray.MenuItem("预览模式 B 的挂起目标", do_preview_suspend,
                         enabled=enabled and not in_game and _cache.get("full_baseline", False)),
        pystray.MenuItem("模式 A：正常关闭并保存记录", do_enter_close_mode,
                         enabled=enabled and not in_game),
        pystray.MenuItem(f"✨ 恢复应用（{len(state['apps'])} 个已保存 · {action_label}）" if in_game else "✨ 恢复应用（无挂起记录）",
                         do_restore, enabled=enabled and in_game),
        pystray.MenuItem("确认已重启：恢复旧启动记录", do_recover_after_restart,
                         enabled=enabled and legacy),
        pystray.MenuItem("重新尝试未确认启动", do_retry_restore,
                         enabled=enabled and any(a.get("restore_attempted") for a in state["apps"])),
        pystray.MenuItem("🗑️ 放弃恢复 / 返回就绪", do_discard_restore,
                         enabled=enabled and in_game),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("捕获完整进程基线", do_capture, enabled=enabled and not in_game),
        pystray.MenuItem("查看基线", do_show_baseline, enabled=enabled),
        pystray.MenuItem("排除名单（保留运行）", _exclude_submenu()),
        pystray.MenuItem("开机自启", toggle_autostart, checked=lambda _: _cache["autostart"], enabled=enabled),
        pystray.MenuItem("退出（安全收尾）", _exit_app),
    )


def make_icon_image(in_game_mode=False):
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    color = (255, 55, 65) if in_game_mode else (100, 180, 255)
    draw.ellipse((4, 4, 60, 60), fill=(20, 26, 36), outline=color, width=4)
    draw.polygon([(26, 20), (26, 44), (48, 32)], fill=color)
    return image


def update_tray_state():
    if not icon:
        return
    state = _cache["state"]
    legacy = any(a.get("action") == "suspend" and not a.get("suspend_id") for a in state["apps"])
    action_tag = ("旧版挂起记录" if legacy else "挂起保留现场") if state.get("action") == "suspend" else "正常关闭"
    labels = {"ready": "就绪", "entering": "上次进入未完成",
              "game": f"游戏模式[{action_tag}]",
              "partial": "部分操作待处理", "restoring": "上次恢复未完成"}
    icon.icon = make_icon_image(state["mode"] != "ready")
    mode_text = labels.get(state["mode"], "就绪")
    icon.title = f"GameSwitcher - {'操作中' if _busy else mode_text}（{len(state['apps'])} 个应用记录）"
    icon.menu = build_menu()


def main():
    global icon
    setup_logging()
    try:
        if already_running():
            _msg_box("GameSwitcher 已经在运行。", flags=MB_ICONINFO)
            return
        storage.load_baseline()
        try:
            import suspension
            suspension.resume_records(storage.load_pending_restore())
            suspension.reconcile_completed()
        except Exception:
            log.exception("interrupted suspension recovery needs attention; journal retained")
        _refresh_cache()
        state = _cache["state"]
        icon = TrayIcon("GameSwitcher", make_icon_image(state["mode"] != "ready"), "GameSwitcher", build_menu())
        icon.dispatch(update_tray_state)
        icon.run(setup=lambda tray: tray.dispatch(lambda: setattr(tray, "visible", True)))
    except Exception as exc:
        log.exception("startup/main loop failed")
        _msg_box(f"GameSwitcher 无法继续运行：{exc}\n配置文件保持原样。\n{_data_location()}")
    finally:
        _exit_app()
        with _workers_lock:
            workers = list(_workers)
            shutdown_thread = _shutdown_thread
        for worker in workers:
            worker.join()
        if shutdown_thread and shutdown_thread is not threading.current_thread():
            shutdown_thread.join()
        _release_instance()


def run_self_test(report_path=None):
    """Check the packaged native UI without showing a tray or closing any app."""
    import json
    import tempfile
    from io import BytesIO
    from contextlib import ExitStack

    global icon
    previous_appdata = os.environ.get("APPDATA")
    finished = threading.Event()
    result = {"ok": False}

    def watchdog():
        if not finished.wait(15):
            os._exit(2)  # this isolated self-test process only

    threading.Thread(target=watchdog, daemon=True).start()
    try:
        with tempfile.TemporaryDirectory(prefix="gameswitcher-self-test-") as directory, ExitStack() as cleanup:
            cleanup.callback(_release_instance)
            os.environ["APPDATA"] = directory
            mutex_name = f"Local\\GameSwitcher_Smoke_{os.getpid()}"
            if already_running(mutex_name):
                raise RuntimeError("self-test instance unexpectedly locked")
            result["single_instance_excluded"] = already_running(mutex_name)
            storage.save_baseline([])
            storage.merge_pending_restore([], "game")
            _refresh_cache()
            result["enumerated_instances"] = len(_cache["running"])
            for mode in (False, True):
                make_icon_image(mode).save(BytesIO(), format="ICO")
            ui_thread = threading.get_ident()
            done = threading.Event()
            icon = TrayIcon("GameSwitcher-Smoke", make_icon_image(), "Smoke", build_menu())

            def setup(tray):
                def update():
                    update_tray_state()
                    result["dispatch_on_ui_thread"] = threading.get_ident() == ui_thread
                    done.set()
                tray.dispatch(update)
                done.wait(5)
                tray.stop()

            icon.run(setup=setup)  # supplied setup keeps the tray invisible
            result["state_after_restart"] = storage.load_state()["mode"]
            # Exercise the packaged migration/backup path only in temporary data.
            storage.save_pending_restore([{"exe": sys.executable, "pid": core.SELF_PID,
                                           "birth": 0, "username": "self-test", "session": 0,
                                           "action": "suspend", "status": "suspended"}], "game", action="suspend")
            core.prepare_legacy_relaunch_after_restart()
            result["legacy_record_converted"] = storage.load_pending_restore()[0]["action"] == "close"
            result["ok"] = (result.get("dispatch_on_ui_thread", False)
                            and result["single_instance_excluded"] and result["state_after_restart"] == "game"
                            and result["legacy_record_converted"])
            _release_instance()
    except Exception as exc:
        log.exception("self-test failed")
        result["error"] = str(exc)
    finally:
        finished.set()
        _release_instance()
        icon = None
        if previous_appdata is None:
            os.environ.pop("APPDATA", None)
        else:
            os.environ["APPDATA"] = previous_appdata
    if report_path:
        with open(report_path, "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
    if sys.stdout:
        print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--freeze-guardian":
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
        import suspension
        raise SystemExit(suspension.guardian_main(sys.argv[2]))
    elif len(sys.argv) == 4 and sys.argv[1] == "--self-test-suspend":
        import json
        import suspension_selftest
        try:
            result = suspension_selftest.run(sys.argv[2])
        except Exception as exc:
            log.exception("packaged suspension self-test failed")
            result = {"ok": False, "error": str(exc)}
        with open(sys.argv[3], "w", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
        raise SystemExit(0 if result["ok"] else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        result = run_self_test(sys.argv[2] if len(sys.argv) > 2 else None)
        raise SystemExit(0 if result["ok"] else 1)
    main()
