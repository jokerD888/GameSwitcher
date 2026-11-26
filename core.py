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


def _native_process_meta():
    import winprocess
    return winprocess.snapshot_meta()


def process_inventory():
    """Snapshot identities/parentage without reading command lines or requiring windows."""
    _, own_session, _ = _scope()
    try:
        native = _native_process_meta()
    except OSError:
        native = {}
    result = []
    try:
        for proc in psutil.process_iter():
            pid = proc.pid
            meta = native.get(pid)
            if meta is not None:
                ppid, exe, birth, session = meta
                username = ""
                if session == own_session and exe:
                    try:
                        username = (proc.username() or "").lower()
                    except psutil.Error:
                        username = ""
            else:
                info = getattr(proc, "info", None) or {}
                try:
                    session = _session_id(pid)
                except (OSError, psutil.Error):
                    session = -1
                ppid = info.get("ppid", 0)
                exe = info.get("exe") or ""
                birth = info.get("create_time")
                username = (info.get("username") or "").lower()
                if not info and session == own_session:
                    try:
                        exe = proc.exe() or ""
                        birth = proc.create_time()
                        username = (proc.username() or "").lower()
                    except (OSError, psutil.Error):
                        pass
            result.append({"pid": pid, "ppid": ppid, "exe": exe, "birth": birth,
                           "username": username, "session": session})
    except psutil.Error as exc:
        raise ScanError("完整进程扫描失败，未修改基线") from exc
    return result


def baseline_processes():
    """Capture all accessible processes of this user/session, including tray/background apps."""
    username, session, _ = _scope()
    return [{"exe": p["exe"], "name": os.path.basename(p["exe"])} for p in process_inventory()
            if p["exe"] and p["username"] == username and p["session"] == session]


def visible_apps():
    """One entry per window-owning process, restricted to this user/session."""
    username, session, protected = _scope()
    by_pid = {}
    try:
        for proc in psutil.process_iter():
            pid = proc.pid
            if pid in protected:
                continue
            try:
                if _session_id(pid) != session:
                    continue
                exe = proc.exe()
                proc_user = proc.username()
                birth = proc.create_time()
            except (OSError, psutil.Error):
                continue
            if not exe or not proc_user or proc_user.lower() != username or birth is None:
                continue
            name = os.path.basename(exe).lower()
            if name in SYSTEM_EXES or name.endswith(".tmp"):
                continue
            by_pid[pid] = (proc, exe, birth)
    except psutil.Error as exc:
        raise ScanError("进程扫描失败") from exc

    apps = {}

    def on_window(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            entry = by_pid.get(pid)
            if not title or entry is None:
                return True
            proc, exe, birth = entry
            if pid not in apps:
                try:
                    cmdline, cwd = proc.cmdline() or [], proc.cwd() or ""
                    current = psutil.Process(pid)
                    if abs(current.create_time() - birth) > 0.001 or path_key(current.exe()) != path_key(exe):
                        return True
                except PROCESS_GONE:
                    return True
                except (psutil.Error, OSError):
                    log.warning("cannot capture context pid=%s; leaving application untouched", pid)
                    return True
                apps[pid] = {"pid": pid, "birth": birth, "exe": exe,
                             "name": os.path.basename(exe), "username": username,
                             "session": session, "cmdline": cmdline, "cwd": cwd, "hwnds": []}
            apps[pid]["hwnds"].append(hwnd)
        except win32gui.error:
            pass
        finally:
            win32api.SetLastError(0)
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
        return (abs(proc.create_time() - app["birth"]) < 0.001 and path_key(proc.exe()) == path_key(app["exe"])
                and proc.username().lower() == app["username"].lower() and _session_id(proc.pid) == app["session"])
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
        if (app["pid"] in protected or app["username"].lower() != username or app["session"] != session
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


class UnsafeSuspendError(ScanError):
    pass


def suspend_apps(target_apps, cfg=None, cancel=None, confirm=None, healthy=None, recovered=None):
    import suspension
    return suspension.suspend_apps(target_apps, cfg, cancel, confirm, healthy, recovered)


def _current_boot_suspensions(apps):
    """Old PID-only journals cannot safely identify suspended descendants.

    Only a full Windows boot proves that every process in an old tree is gone.
    Never resume or terminate a bare PID, including when a root has exited.
    """
    suspensions = [app for app in apps if app.get("action") == "suspend" and not app.get("suspend_id")]
    if not suspensions:
        return []
    boot = psutil.boot_time()
    return [app for app in suspensions
            if not all(field in app for field in ("pid", "birth", "username", "session"))
            or app["birth"] >= boot or _matches(app) is not False]


def discard_apps(pending_apps):
    import suspension
    suspension.resume_records(pending_apps)
    if _current_boot_suspensions(pending_apps):
        raise UnsafeSuspendError("旧版当前会话的挂起记录不能安全清空。请重启 Windows 后再恢复或放弃记录。")
    # Discarding launch records must never terminate applications.


def resume_all_suspended(pending_apps):
    """Compatibility guard: never blindly resume legacy PID-only process trees."""
    import suspension
    suspension.resume_records(pending_apps)
    if _current_boot_suspensions(pending_apps):
        raise UnsafeSuspendError("旧版挂起记录缺少子进程身份，无法安全自动解冻。请重启 Windows 后恢复应用。")


def prepare_legacy_relaunch_after_restart():
    """Explicit user-confirmed recovery for Fast Startup / uncertain boot times.

    Original processes must be gone. Preserve the journal before converting
    launch records; never operate on the recorded child PIDs.
    """
    state = storage.load_state()
    pending = state["apps"]
    changed = False
    for app in pending:
        if app.get("action") != "suspend" or app.get("suspend_id"):
            continue
        if not all(field in app for field in ("pid", "birth", "username", "session")):
            raise UnsafeSuspendError("旧记录缺少原进程身份，无法确认已经重启。记录已保留，请手动检查。")
        if _matches(app) is not False:
            raise UnsafeSuspendError("原挂起进程仍存在或无法确认身份。请先使用 Windows 的「重启」，记录已保留。")
        app["action"], app["status"] = "close", "closed"
        app.pop("tree_pids", None)
        changed = True
    if changed:
        # Backup exactly what is on disk, before changing any launch records.
        import uuid
        backup = f"pending_restore.before-restart-{uuid.uuid4().hex}.json"
        storage._save(backup, storage._load(storage.PENDING_RESTORE_FILE, None))
        storage.save_pending_restore(pending, "partial" if pending else "ready", action="close")
    return changed


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
    import suspension
    resumed, failed_operations = [], set()
    for operation_id in {a["suspend_id"] for a in pending if a.get("suspend_id")}:
        group = [a for a in pending if a.get("suspend_id") == operation_id]
        try:
            suspension.resume_records(group)
            for app in group:
                matching = _matches(app)
                if matching is True:
                    try:
                        if psutil.Process(app["pid"]).status() == psutil.STATUS_STOPPED:
                            failed_operations.add(app["id"])
                        else:
                            resumed.append(app)
                    except psutil.Error:
                        failed_operations.add(app["id"])
                elif matching is False:
                    app["action"], app["status"] = "close", "closed"
                    app.pop("suspend_id", None)
                else:
                    failed_operations.add(app["id"])
        except Exception:
            log.exception("guardian restoration not confirmed; records retained")
            failed_operations.update(a["id"] for a in group)
    resumed_ids = {a["id"] for a in resumed}
    pending = [a for a in pending if a["id"] not in resumed_ids]
    blocked_ids = {a["id"] for a in _current_boot_suspensions(pending)}
    for app in pending:
        if (app.get("action") == "suspend" and not app.get("suspend_id")
                and app["id"] not in blocked_ids and app["id"] not in failed_operations):
            # A previous Windows boot has ended; resume cannot recover its memory.
            app["action"], app["status"] = "close", "closed"
            app.pop("tree_pids", None)
    result = {"restored": resumed, "already_running": [], "missing": [], "blocked_suspend": []}
    current = visible_apps() if len(blocked_ids | failed_operations) < len(pending) else []
    consumed = set()
    storage.save_pending_restore(pending, "restoring")
    remaining = list(pending)
    try:
        for index, app in enumerate(pending):
            if cancel and cancel.is_set():
                break
            if progress:
                progress(f"恢复 {app['name']} ({index + 1}/{len(pending)})")
            if app["id"] in failed_operations:
                result["missing"].append(app)
                continue
            if app["id"] in blocked_ids:
                result["missing"].append(app)
                result["blocked_suspend"].append(app)
                log.warning("unsafe legacy suspension retained id=%s; Windows restart required", app["id"])
                continue
            original = next((a for a in current if a["id"] not in consumed
                             and a["pid"] == app.get("pid")
                             and "birth" in app and abs(a["birth"] - app["birth"]) < 0.001
                             and path_key(a["exe"]) == path_key(app["exe"])
                             and set(app.get("hwnds", [])) & set(a["hwnds"])), None)
            match = original or next((a for a in current if a["id"] not in consumed and _signature(a) == _signature(app)
                                      and ("birth" not in app or a["birth"] >= app["birth"] - 0.001)), None)
            partial = (match is not None and "birth" in app and abs(app["birth"] - match["birth"]) < 0.001
                       and app.get("hwnds")
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
        if resumed:
            try:
                suspension.prune_completed_operations({a["suspend_id"] for a in remaining if a.get("suspend_id")})
            except Exception:
                log.debug("guardian history pruning failed after restore", exc_info=True)
    return result
