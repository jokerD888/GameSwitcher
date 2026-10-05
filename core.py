"""Windows application close/relaunch logic. Never forcibly terminate a process."""
import ctypes
import logging
import os
import subprocess
import time

import psutil
import win32api
import win32con
import win32gui
import win32process

import storage

log = logging.getLogger("gameswitcher.core")
SELF_PID = os.getpid()
SYSTEM_EXES = {
    "explorer.exe", "applicationframehost.exe", "runtimebroker.exe",
    "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "widgets.exe", "systemsettings.exe", "taskmgr.exe",
    "ctfmon.exe", "conhost.exe", "dwm.exe", "securityhealthsystray.exe",
    "securityhealthservice.exe", "smss.exe", "csrss.exe", "wininit.exe",
    "services.exe", "lsass.exe", "svchost.exe", "fontdrvhost.exe", "winlogon.exe",
    "audiodg.exe", "gamebar.exe", "gamebarftserver.exe", "nvspcaps64.exe",
    "nvcontainer.exe", "nvidia share.exe", "radeonsoftware.exe", "amdrsserv.exe",
}
PROCESS_GONE = (psutil.NoSuchProcess, psutil.ZombieProcess)


class ScanError(RuntimeError):
    pass


def path_key(path):
    return os.path.normcase(os.path.abspath(path))


def is_excluded(app, exclusions):
    key = path_key(app["exe"])
    return any((path_key(rule) == key if os.path.isabs(rule)
                else os.path.normcase(rule) == os.path.basename(key)) for rule in exclusions)


def _session_id(pid):
    result = ctypes.c_uint()
    function = ctypes.windll.kernel32.ProcessIdToSessionId
    function.argtypes = [ctypes.c_uint, ctypes.POINTER(ctypes.c_uint)]
    function.restype = ctypes.c_int
    if not function(pid, ctypes.byref(result)):
        raise OSError("无法读取进程会话")
    return result.value


def _scope():
    try:
        own = psutil.Process(SELF_PID)
        return own.username().lower(), _session_id(SELF_PID), {SELF_PID, 0, 4, *(p.pid for p in own.parents())}
    except (psutil.Error, OSError) as exc:
        raise ScanError("无法确定当前用户、会话或祖先进程，已停止扫描") from exc


def visible_apps():
    """One entry per window-owning process, restricted to this user/session."""
    username, session, protected = _scope()
    by_pid = {}
    try:
        for proc in psutil.process_iter(["pid", "exe", "username", "create_time"]):
            info = proc.info
            exe = info.get("exe")
            if (proc.pid in protected or not exe or not info.get("username")
                    or info["username"].lower() != username or info.get("create_time") is None):
                continue
            name = os.path.basename(exe).lower()
            if name in SYSTEM_EXES or name.endswith(".tmp"):
                continue
            try:
                if _session_id(proc.pid) == session:
                    by_pid[proc.pid] = proc
            except (OSError, psutil.Error):
                log.debug("inaccessible session pid=%s", proc.pid)
    except psutil.Error as exc:
        raise ScanError("进程扫描失败") from exc

    apps = {}

    def on_window(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            proc = by_pid.get(pid)
            if not title or proc is None:
                return True
            if pid not in apps:
                try:
                    cmdline, cwd = proc.cmdline() or [], proc.cwd() or ""
                    current = psutil.Process(pid)
                    if current.create_time() != proc.info["create_time"] or path_key(current.exe()) != path_key(proc.info["exe"]):
                        return True
                except PROCESS_GONE:
                    return True
                except psutil.AccessDenied:
                    log.warning("cannot capture context pid=%s; leaving application untouched", pid)
                    return True
                apps[pid] = {"pid": pid, "birth": proc.info["create_time"], "exe": proc.info["exe"],
                             "name": os.path.basename(proc.info["exe"]), "username": username,
                             "session": session, "cmdline": cmdline, "cwd": cwd, "hwnds": []}
            apps[pid]["hwnds"].append(hwnd)
        except win32gui.error:
            return True
        return True

    win32api.SetLastError(0)
    try:
        win32gui.EnumWindows(on_window, None)
    except Exception as exc:
        raise ScanError("窗口扫描未完整完成，已停止操作") from exc
    return storage.validate_apps(list(apps.values()), "窗口扫描")


def pids_by_exes(exe_list):
    """Exact path matches only, within the current user/session."""
    username, session, protected = _scope()
    lookup = {path_key(exe): exe for exe in exe_list}
    result = {exe: [] for exe in exe_list}
    for proc in psutil.process_iter(["pid", "exe", "username"]):
        info = proc.info
        if proc.pid in protected or not info.get("exe") or (info.get("username") or "").lower() != username:
            continue
        target = lookup.get(path_key(info["exe"]))
        if target:
            try:
                if _session_id(proc.pid) == session:
                    result[target].append(proc.pid)
            except OSError:
                log.debug("cannot query session pid=%s", proc.pid)
    return result


def apps_by_exe(exe_path):
    return pids_by_exes([exe_path])[exe_path]


def _matches(app):
    """True = same live process; False = gone/replaced; None = access unknown."""
    try:
        proc = psutil.Process(app["pid"])
        return (proc.create_time() == app["birth"] and path_key(proc.exe()) == path_key(app["exe"])
                and proc.username().lower() == app["username"] and _session_id(proc.pid) == app["session"])
    except PROCESS_GONE:
        return False
    except (psutil.AccessDenied, OSError):
        return None


def _window_open(hwnd, pid):
    try:
        return (bool(win32gui.IsWindow(hwnd)) and bool(win32gui.IsWindowVisible(hwnd))
                and win32process.GetWindowThreadProcessId(hwnd)[1] == pid)
    except win32gui.error:
        return False


def _remaining_windows(app):
    matching = _matches(app)
    if matching is False:
        return []
    if matching is None:
        return None
    return [hwnd for hwnd in app["hwnds"] if _window_open(hwnd, app["pid"])]


def close_apps(target_apps, cfg, progress=None, cancel=None):
    """Request normal closure of captured windows; never chase or kill new PIDs."""
    cfg = storage.validate_config(cfg)
    apps = storage.validate_apps(target_apps, "关闭目标")
    username, session, protected = _scope()
    for app in apps:
        if not all(field in app for field in ("pid", "birth", "username", "session")):
            raise ScanError("关闭目标缺少进程身份，请重新扫描")
        if (app["pid"] in protected or app["username"] != username or app["session"] != session
                or os.path.basename(app["exe"]).lower() in SYSTEM_EXES):
            raise ScanError("关闭目标包含受保护进程，已停止操作")
    errors = set()
    for app in apps:
        if cancel and cancel.is_set():
            break
        for hwnd in app["hwnds"]:
            if cancel and cancel.is_set():
                break
            if _matches(app) is not True or not _window_open(hwnd, app["pid"]):
                continue
            try:
                win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                log.info("close requested id=%s pid=%s exe=%s", app["id"], app["pid"], app["exe"])
            except win32gui.error:
                errors.add(app["id"])
                log.warning("close message failed id=%s", app["id"], exc_info=True)
    deadline = time.monotonic() + cfg["grace_timeout_sec"]
    while time.monotonic() < deadline:
        if cancel and cancel.is_set():
            break
        if all(_remaining_windows(app) == [] for app in apps):
            break
        delay = min(0.1, max(0, deadline - time.monotonic()))
        if cancel:
            cancel.wait(delay)
        else:
            time.sleep(delay)
    result = {"closed": [], "skipped": [], "failed": []}
    for app in apps:
        remaining = _remaining_windows(app)
        category = "closed" if remaining == [] else "failed" if remaining is None or app["id"] in errors else "skipped"
        result[category].append(app)
    return result


def _signature(app):
    argv = app.get("cmdline") or [app["exe"]]
    return path_key(app["exe"]), tuple(argv[1:]), path_key(app["cwd"]) if app.get("cwd") else ""


def _window_keys(apps, exe):
    return {(a["pid"], a["birth"], h) for a in apps if path_key(a["exe"]) == path_key(exe) for h in a["hwnds"]}


def launch_app(app, timeout=8, cancel=None):
    """Preserve argv/cwd and confirm a new visible window; uncertain launches stay pending."""
    app = storage.validate_apps([app], "恢复目标")[0]
    timeout = storage.timeout_value(timeout, "restore_timeout_sec", 1, 30)
    if not os.path.isfile(app["exe"]) or (cancel and cancel.is_set()):
        return False
    cwd = app.get("cwd") or None
    if cwd and not os.path.isdir(cwd):
        log.warning("original working directory unavailable id=%s", app["id"])
        return False
    argv = [app["exe"], *(app.get("cmdline") or [app["exe"]])[1:]]
    if any(arg.startswith("--type=") or arg.startswith("--crashpad") for arg in argv[1:]):
        log.warning("unsupported internal launch context id=%s", app["id"])
        return False
    before = _window_keys(visible_apps(), app["exe"])
    child = None
    try:
        child = subprocess.Popen(argv, cwd=cwd, creationflags=subprocess.DETACHED_PROCESS)
    except OSError:
        log.info("using ShellExecute with original arguments id=%s", app["id"], exc_info=True)
        try:
            os.startfile(app["exe"], arguments=subprocess.list2cmdline(argv[1:]), cwd=cwd)
        except OSError:
            log.warning("launch failed id=%s", app["id"], exc_info=True)
            return False
    deadline = time.monotonic() + timeout
    first_seen = {}
    while time.monotonic() < deadline:
        if cancel and cancel.is_set():
            return False
        exitcode = child.poll() if child else None
        if exitcode not in (None, 0):
            log.warning("launched process failed id=%s exit=%s", app["id"], exitcode)
            return False
        current = _window_keys(visible_apps(), app["exe"]) - before
        now = time.monotonic()
        first_seen = {key: first_seen.get(key, now) for key in current}
        if any(now - birth >= 0.5 for birth in first_seen.values()):
            return True
        if cancel:
            cancel.wait(0.1)
        else:
            time.sleep(0.1)
    log.warning("launch not confirmed; keeping snapshot id=%s", app["id"])
    return False


def restore_apps(progress=None, cancel=None):
    cfg = storage.load_config()
    pending = storage.load_pending_restore()
    result = {"restored": [], "already_running": [], "missing": []}
    current = visible_apps()
    consumed = set()
    storage.save_pending_restore(pending, "restoring")
    remaining = list(pending)
    try:
        for index, app in enumerate(pending):
            if cancel and cancel.is_set():
                break
            if progress:
                progress(f"恢复 {app['name']} ({index + 1}/{len(pending)})")
            original = next((a for a in current if a["id"] not in consumed
                             and a["pid"] == app.get("pid") and a["birth"] == app.get("birth")
                             and path_key(a["exe"]) == path_key(app["exe"])
                             and set(app.get("hwnds", [])) & set(a["hwnds"])), None)
            match = original or next((a for a in current if a["id"] not in consumed and _signature(a) == _signature(app)
                                      and ("birth" not in app or a["birth"] >= app["birth"])), None)
            partial = (match is not None and app.get("birth") == match["birth"] and app.get("hwnds")
                       and set(app["hwnds"]) & set(match["hwnds"])
                       and not set(app["hwnds"]).issubset(match["hwnds"]))
            if match and not partial:
                consumed.add(match["id"])
                result["already_running"].append(app)
            else:
                if partial or app.get("restore_attempted"):
                    result["missing"].append(app)
                    continue
                # Persist BEFORE launch. If a launch is uncertain or this process
                # crashes, another Restore click cannot silently create duplicates.
                app["restore_attempted"] = True
                storage.save_pending_restore(remaining, "restoring")
                if launch_app(app, cfg["restore_timeout_sec"], cancel):
                    result["restored"].append(app)
                else:
                    result["missing"].append(app)
                    continue
            remaining = [entry for entry in remaining if entry["id"] != app["id"]]
            storage.save_pending_restore(remaining, "restoring")
    finally:
        storage.save_pending_restore(remaining, "partial" if remaining else "ready")
    return result
