"""GameSwitcher - tray application.

Close all non-baseline apps with one click before gaming, restore them after.
Run after boot, "捕获基线" once, then use 托盘菜单 to switch modes.
"""
import ctypes
import functools
import logging
import os
import sys
import threading
import winreg

import pystray
from PIL import Image, ImageDraw

import core
import storage

log = logging.getLogger("gameswitcher")


def setup_logging() -> None:
    try:
        logging.basicConfig(
            filename=storage.log_path(), encoding="utf-8",
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    except OSError:
        logging.basicConfig(level=logging.INFO)


MB_ICONINFO = 0x40
MB_ICONWARN = 0x30
MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000
MB_OKCANCEL = 0x01
MB_YESNO = 0x04
MB_YESNOCANCEL = 0x03
IDOK = 1
IDCANCEL = 2
IDYES = 6
IDNO = 7

icon = None  # pystray.Icon, set in main()

# pystray runs menu actions inline on its message-loop thread; native dialogs
# shown from that stack freeze. Every action runs on its own thread instead.
# This lock also keeps game mode / restore from overlapping.
_action_lock = threading.Lock()


def threaded(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        def run():
            try:
                log.info("action start: %s", fn.__name__)
                fn(*args, **kwargs)
                log.info("action done: %s", fn.__name__)
            except Exception:
                log.exception("action failed: %s", fn.__name__)
                try:
                    _msg_box(f"操作「{fn.__name__}」出错，请把日志发给开发者：\n"
                             f"{storage.log_path()}", "GameSwitcher",
                             MB_ICONWARN)
                except Exception:
                    pass
        threading.Thread(target=run, daemon=True).start()
    return wrapper


def acquire_lock() -> bool:
    return _action_lock.acquire(blocking=False)


def release_lock() -> None:
    _action_lock.release()


_mutex_handle = None
MUTEX_NAME = "Local\\GameSwitcher_SingleInstance"
ERROR_ALREADY_EXISTS = 183


def already_running() -> bool:
    """True if another GameSwitcher instance holds the startup mutex."""
    global _mutex_handle
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _mutex_handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
    return kernel32.GetLastError() == ERROR_ALREADY_EXISTS


_notify_timer = None
_notify_timer_lock = threading.Lock()


def notify(msg: str, title: str = "GameSwitcher", warn: bool = False, timeout: float = 3.0) -> None:
    """Display toast notification (auto-dismiss after timeout seconds) or warn msgbox."""
    global _notify_timer
    if icon:
        icon.notify(msg, title)
        if timeout > 0:
            with _notify_timer_lock:
                if _notify_timer is not None:
                    _notify_timer.cancel()

                def _dismiss():
                    try:
                        if icon:
                            icon.remove_notification()
                    except Exception:
                        pass

                _notify_timer = threading.Timer(timeout, _dismiss)
                _notify_timer.daemon = True
                _notify_timer.start()
    if warn:
        ctypes.windll.user32.MessageBoxW(0, msg, title, MB_ICONWARN | MB_TOPMOST | MB_SETFOREGROUND)



def _msg_box(text: str, title: str, flags: int) -> int:
    return ctypes.windll.user32.MessageBoxW(0, text, title, flags | MB_TOPMOST | MB_SETFOREGROUND)


@threaded
def do_capture(icon_=None, item=None):
    if not acquire_lock():
        notify("已有操作正在进行中，请等它完成。", warn=True)
        return
    try:
        old_baseline = storage.load_baseline()
        if old_baseline:
            r = _msg_box(
                f"当前已存在基线（共 {len(old_baseline)} 个应用）。\n\n"
                "⚠️ 重新捕获将覆盖现有基线配置。\n"
                "请确保当前处于刚开机的干净环境（无额外临时打开的软件）。\n\n"
                "确定要重新捕获吗？",
                "GameSwitcher - 确认捕获基线",
                MB_YESNO | MB_ICONWARN
            )
            if r != IDYES:
                return
        apps = core.visible_apps()
        storage.save_baseline(apps)
        if apps:
            names = "\n- " + "\n- ".join(a["name"] for a in apps)
            msg = f"已成功捕获基线，共 {len(apps)} 个应用：{names}"
        else:
            msg = "已成功捕获基线（当前为 0 个应用，进入游戏模式将关闭所有非排除应用）。"
        _msg_box(msg, "GameSwitcher", MB_ICONINFO)
    finally:
        release_lock()


@threaded
def do_enter_game_mode(icon_=None, item=None):
    if not acquire_lock():
        notify("已有操作正在进行中，请等它完成。", warn=True)
        return
    try:
        _enter_game_mode()
    finally:
        release_lock()


def _enter_game_mode():
    cfg = storage.load_config()
    baseline_exes = {os.path.normcase(a["exe"]) for a in storage.load_baseline()}
    exclude = {os.path.normcase(e) for e in cfg["exclude_list"]}

    def to_close(app):
        key = os.path.normcase(app["exe"])
        name_key = os.path.basename(key)
        return (key not in baseline_exes
                and name_key not in exclude
                and key not in exclude)

    targets = [a for a in core.visible_apps() if to_close(a)]
    if not targets:
        notify("当前没有需要关闭的非基线应用，已在游戏模式。")
        update_tray_state()
        return

    # 并发极速关闭：WM_CLOSE -> 1.5s -> 批量自动强杀，0 弹窗打扰
    res = core.close_apps(targets, cfg)
    if res["closed"]:
        storage.merge_pending_restore(res["closed"])

    update_tray_state()

    # 静默成功哲学：若无失败，完全不弹窗！仅当有应用未能关闭时才弹窗告警
    if res["failed"]:
        failed_names = ", ".join(a["name"] for a in res["failed"])
        _msg_box(f"部分应用未能关闭（可能具有管理员权限或进程假死）：\n{failed_names}", "GameSwitcher - 游戏模式", MB_ICONWARN)


@threaded
def do_restore(icon_=None, item=None):
    if not acquire_lock():
        notify("已有操作正在进行中，请等它完成。", warn=True)
        return
    try:
        pending = storage.load_pending_restore()
        if not pending:
            notify("当前没有待恢复的应用。")
            update_tray_state()
            return

        res = core.restore_apps(progress=None)
        update_tray_state()

        # 静默成功哲学：若全部唤回成功，完全不弹窗！用户直接回到工作状态
        # 仅当有应用启动失败时才弹窗提醒用户检查
        if res["missing"]:
            missing_names = ", ".join(a.get("name") or a.get("exe") for a in res["missing"])
            _msg_box(f"部分应用启动失败（文件可能被移动或删除）：\n{missing_names}", "GameSwitcher - 恢复提示", MB_ICONWARN)
    finally:
        release_lock()


@threaded
def do_discard_restore(icon_=None, item=None):
    if not acquire_lock():
        notify("已有操作正在进行中，请等它完成。", warn=True)
        return
    try:
        pending = storage.load_pending_restore()
        if not pending:
            return
        r = _msg_box(
            f"当前快照记录了 {len(pending)} 个已挂起应用。\n\n"
            "⚠️ 放弃恢复将清空该挂起快照，不重新启动这些应用，并将状态重置为普通就绪状态。\n\n"
            "确定要放弃本次恢复吗？",
            "GameSwitcher - 确认放弃恢复",
            MB_YESNO | MB_ICONWARN
        )
        if r == IDYES:
            storage.clear_pending_restore()
            update_tray_state()
            notify("已清空挂起快照，重置为就绪状态。")
    finally:
        release_lock()



@threaded
def do_show_baseline(icon_=None, item=None):
    apps = storage.load_baseline()
    if not apps:
        _msg_box("基线应用（游戏模式下保留运行）：\n(无，进入游戏模式将关闭所有非排除应用)", "GameSwitcher", MB_ICONINFO)
        return
    _msg_box("基线应用（游戏模式下保留运行）：\n- " +
             "\n- ".join(a["name"] for a in apps), "GameSwitcher", MB_ICONINFO)


# ---------- exclude list ----------

def _add_exclude(name: str):
    cfg = storage.load_config()
    target_norm = os.path.normcase(name)
    if not any(os.path.normcase(x) == target_norm for x in cfg["exclude_list"]):
        cfg["exclude_list"].append(name)
        storage.save_config(cfg)
    update_tray_state()


def _toggle_exclude(exe_name: str):
    cfg = storage.load_config()
    target_norm = os.path.normcase(exe_name)
    existing_idx = next(
        (i for i, item in enumerate(cfg["exclude_list"]) if os.path.normcase(item) == target_norm),
        -1
    )
    if existing_idx != -1:
        cfg["exclude_list"].pop(existing_idx)
    else:
        cfg["exclude_list"].append(exe_name)
    storage.save_config(cfg)
    update_tray_state()


def _exclude_submenu():
    cfg = storage.load_config()
    items = []

    # 1. 顶部：已排除的应用列表（带 ✓，点击取消）
    if cfg["exclude_list"]:
        for name in cfg["exclude_list"]:
            items.append(pystray.MenuItem(
                f"✓ {name} (点击取消)", lambda *_, n=name: _toggle_exclude(n)))
        items.append(pystray.Menu.SEPARATOR)

    # 2. 底部：当前正在运行但未排除的应用（点击直接加入排除名单，免敲键盘）
    running = core.visible_apps()
    existing_norms = {os.path.normcase(e) for e in cfg["exclude_list"]}
    candidates = []
    seen = set()
    for a in running:
        norm = os.path.normcase(a["name"])
        if norm not in existing_norms and norm not in seen:
            seen.add(norm)
            candidates.append(a["name"])

    if candidates:
        for name in sorted(candidates)[:15]:
            items.append(pystray.MenuItem(
                f"+ 排除当前运行: {name}", lambda *_, n=name: _add_exclude(n)))
    else:
        if not cfg["exclude_list"]:
            items.append(pystray.MenuItem("  (无已排除应用)", None, enabled=False))

    items.append(pystray.Menu.SEPARATOR)
    items.append(pystray.MenuItem("📁 打开配置与日志目录", lambda *_: os.startfile(storage.data_dir())))

    return items


# ---------- autostart ----------

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VAL_NAME = "GameSwitcher"


def autostart_enabled() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VAL_NAME)
            return True
    except OSError:
        return False


@threaded
def toggle_autostart(icon_=None, item=None):
    script = os.path.abspath(sys.argv[0])
    if script.lower().endswith(".py"):
        python_dir = os.path.dirname(sys.executable)
        pythonw = os.path.join(python_dir, "pythonw.exe")
        if not os.path.isfile(pythonw):
            pythonw = sys.executable
        cmd = f'"{pythonw}" "{script}"'
    else:
        cmd = f'"{script}"'
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if autostart_enabled():
                winreg.DeleteValue(k, VAL_NAME)
            else:
                winreg.SetValueEx(k, VAL_NAME, 0, winreg.REG_SZ, cmd)
    except OSError as e:
        notify(f"设置开机自启失败: {e}", warn=True)
    update_tray_state()


def _exit_app(*_):
    global _mutex_handle, _notify_timer
    with _notify_timer_lock:
        if _notify_timer is not None:
            _notify_timer.cancel()
            _notify_timer = None
    if _mutex_handle:
        try:
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
            _mutex_handle = None
        except Exception:
            pass
    if icon:
        icon.stop()


def build_menu():
    pending = storage.load_pending_restore()
    in_game_mode = len(pending) > 0
    restore_text = f"✨ 恢复应用 ({len(pending)} 个已保存)" if in_game_mode else "恢复应用 (无挂起应用)"

    menu_items = [
        pystray.MenuItem("🎮 进入游戏模式（关闭并保存快照）", do_enter_game_mode),
        pystray.MenuItem(restore_text, do_restore, enabled=in_game_mode),
    ]
    if in_game_mode:
        menu_items.append(pystray.MenuItem("🗑️ 放弃恢复（清空挂起快照）", do_discard_restore))

    menu_items.extend([
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("捕获基线（以此为准保留应用）", do_capture),
        pystray.MenuItem("查看基线", do_show_baseline),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("排除名单（永不关闭）", pystray.Menu(_exclude_submenu)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("开机自启", toggle_autostart, checked=lambda item: autostart_enabled()),
        pystray.MenuItem("退出", _exit_app),
    ])
    menu = pystray.Menu(*menu_items)
    if icon:
        icon.menu = menu
    return menu



def make_icon_image(in_game_mode: bool = False) -> Image.Image:
    """Generate dynamic tray icon based on mode."""
    # RGBA with transparent background gives a modern circular badge on any taskbar color
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    if in_game_mode:
        # 游戏模式：醒目电竞炽红圆环与红色三角形
        d.ellipse((4, 4, 60, 60), fill=(45, 12, 16), outline=(255, 55, 65), width=5)
        d.polygon([(26, 20), (26, 44), (48, 32)], fill=(255, 55, 65))
    else:
        # 普通模式：优雅天蓝色圆环与天蓝色三角形
        d.ellipse((4, 4, 60, 60), fill=(20, 26, 36), outline=(100, 180, 255), width=4)
        d.polygon([(26, 20), (26, 44), (48, 32)], fill=(100, 180, 255))
    return img



def update_tray_state():
    """Sync tray visual icon, tooltip title and menu to current mode."""
    if not icon:
        return
    pending = storage.load_pending_restore()
    in_game = len(pending) > 0
    icon.icon = make_icon_image(in_game_mode=in_game)
    if in_game:
        icon.title = f"GameSwitcher - 🎮 游戏模式中 ({len(pending)} 个应用已挂起)"
    else:
        icon.title = "GameSwitcher - 就绪 (普通模式)"
    build_menu()


def main():
    global icon
    setup_logging()
    log.info("GameSwitcher starting")
    if already_running():
        log.info("another instance is running, exiting")
        _msg_box("GameSwitcher 已经在运行了（看屏幕右下角托盘区，圆形图标）。", "GameSwitcher", MB_ICONINFO)
        return

    pending = storage.load_pending_restore()
    in_game = len(pending) > 0
    initial_title = f"GameSwitcher - 🎮 游戏模式中 ({len(pending)} 个应用已挂起)" if in_game else "GameSwitcher - 就绪 (普通模式)"

    icon = pystray.Icon("GameSwitcher", make_icon_image(in_game_mode=in_game),
                        initial_title, build_menu())
    icon.run()


if __name__ == "__main__":
    main()
