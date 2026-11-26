"""Validated suspension plans and an independent, fail-safe Windows guardian."""
import copy
import logging
import os
import re
import subprocess
import sys
import threading
import time
import uuid

import psutil
import win32api
import win32con
import win32gui
import win32process

import core
import storage

log = logging.getLogger("gameswitcher.suspension")
MAX_NODES = 256
MAX_HISTORY_FILES = 20
CONFIRM_SECONDS = 12
HEARTBEAT_SECONDS = 5
CRITICAL_EXES = {
    "gameviewer.exe", "gameviewer_service.exe", "todesk.exe", "todesk_service.exe",
    "sunloginclient.exe", "sunloginservice.exe", "anydesk.exe", "teamviewer.exe",
    "teamviewer_service.exe", "mstsc.exe", "msrdc.exe", "rdpclip.exe",
    "autohotkey.exe", "autohotkey64.exe", "autohotkey32.exe",
    "powertoys.exe", "powertoys.keyboardmanagerengine.exe",
}
LAUNCHERS = {"explorer.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe",
             "windowsterminal.exe", "python.exe", "pythonw.exe", "py.exe",
             "wscript.exe", "cscript.exe", "rundll32.exe", "conhost.exe"}
_sessions = {}
_session_lock = threading.RLock()


def identity_key(node):
    birth = node.get("birth")
    return node["pid"], round(birth, 4) if birth is not None else None, core.path_key(node["exe"])


def _windows_path(exe):
    directory = os.environ.get("SystemRoot", r"C:\Windows")
    try:
        return os.path.commonpath([core.path_key(directory), core.path_key(exe)]) == core.path_key(directory)
    except ValueError:
        return False


def _shared_root_directories():
    home = os.path.expanduser("~")
    candidates = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("ProgramData", r"C:\ProgramData"),
        os.environ.get("TEMP", ""),
        os.environ.get("TMP", ""),
        os.path.dirname(home),
        home,
        os.path.join(home, "Desktop"),
        os.path.join(home, "Downloads"),
        os.path.join(home, "Documents"),
    ]
    return {core.path_key(path) for path in candidates if path}


def _is_dedicated_app_dir(directory):
    key = core.path_key(directory)
    if os.path.dirname(key) == key or _windows_path(key):
        return False
    return key not in _shared_root_directories()


def build_plan(target_apps, inventory, baseline, cfg, scope=None, enforce_limit=True):
    """Pure plan builder. No process/window state is changed."""
    roots = storage.validate_apps(target_apps, "挂起候选")
    cfg = storage.validate_config(cfg)
    user, session, protected = scope or core._scope()
    baseline_paths = {core.path_key(a["exe"]) for a in baseline}
    critical_directories = {d for d in (os.path.dirname(core.path_key(p["exe"])) for p in [*inventory, *baseline]
                                        if p.get("exe") and os.path.basename(p["exe"]).lower() in CRITICAL_EXES)
                            if _is_dedicated_app_dir(d)}
    records = {p["pid"]: p for p in inventory}
    children = {}
    for node in inventory:
        parent = records.get(node.get("ppid", 0))
        if (parent and node.get("birth") is not None and parent.get("birth") is not None
                and node["birth"] < parent["birth"]):
            continue  # The recorded parent PID has been reused by a newer process.
        children.setdefault(node.get("ppid", 0), []).append(node["pid"])

    def descendants(pid):
        found, todo = set(), [pid]
        while todo:
            current = todo.pop()
            if current in found:
                continue
            found.add(current)
            todo.extend(children.get(current, []))
        return found

    def reason(node, ancestor=False):
        if ((not ancestor and node["pid"] in protected) or not node.get("exe") or node.get("birth") is None
                or node.get("username", "").lower() != user or node.get("session") != session):
            return "系统/自身祖先、跨用户会话或身份不可读"
        name = os.path.basename(node["exe"]).lower()
        for directory in critical_directories:
            try:
                if os.path.commonpath([core.path_key(node["exe"]), directory]) == directory:
                    return "远控/输入程序安装目录保护"
            except ValueError:
                pass
        if name in core.SYSTEM_EXES or name in CRITICAL_EXES or name in LAUNCHERS or _windows_path(node["exe"]):
            return "系统、输入、远控或命令外壳保护"
        if core.path_key(node["exe"]) in baseline_paths:
            return "完整进程基线"
        if core.is_excluded(node, cfg["exclude_list"]):
            return "排除名单"
        return None

    critical_family = set()
    for node in inventory:
        if node.get("exe") and os.path.basename(node["exe"]).lower() in CRITICAL_EXES:
            critical_family.update(descendants(node["pid"]))

    def ancestor_reason(pid):
        seen = set()
        previous = records.get(pid, {})
        parent = previous.get("ppid", 0)
        while parent and parent not in seen:
            seen.add(parent)
            node = records.get(parent)
            if not node:
                break
            if (node.get("birth") is not None and previous.get("birth") is not None
                    and node["birth"] > previous["birth"]):
                break
            name = os.path.basename(node.get("exe") or "").lower()
            # Shells/launchers start unrelated applications; CRITICAL_EXES trees
            # are already protected via critical_family across launcher hops.
            if name in LAUNCHERS:
                break
            if node.get("exe"):
                why = ("远控/输入依赖" if name in CRITICAL_EXES else
                       "完整进程基线" if core.path_key(node["exe"]) in baseline_paths else
                       "排除名单" if core.is_excluded(node, cfg["exclude_list"]) else None)
                if why:
                    return "保留应用的子进程：" + why
            previous = node
            parent = node.get("ppid", 0)
        return None

    def depth(app):
        seen, pid = set(), app["pid"]
        while pid in records and pid not in seen:
            seen.add(pid)
            current = records[pid]
            parent_pid = current.get("ppid", 0)
            parent = records.get(parent_pid)
            if (parent and current.get("birth") is not None and parent.get("birth") is not None
                    and current["birth"] < parent["birth"]):
                break
            pid = parent_pid
        return len(seen)

    selected, blocked, skipped = {}, set(), []
    for app in sorted(roots, key=depth):
        root = records.get(app["pid"])
        family = descendants(app["pid"])
        why = None
        if root is None or identity_key(root) != identity_key(app):
            why = "根进程身份已经变化"
        elif family & blocked:
            why = "所属应用树已保留"
        elif app["pid"] in critical_family:
            why = "远控/输入程序的依赖进程"
        else:
            why = ancestor_reason(app["pid"])
        if not why:
            for pid in family:
                node = records.get(pid)
                why = reason(node) if node else "后代身份不可读"
                if pid in critical_family:
                    why = "远控/输入程序的依赖进程"
                if why:
                    why = f"应用树包含保留进程 {pid}：{why}"
                    break
        if why:
            blocked.update(family)
            skipped.append({"app": app, "reason": why})
            continue
        for pid in family:
            selected[pid] = dict(records[pid], hwnds=[])
    apps = [app for app in roots if app["pid"] in selected and app["pid"] not in blocked]
    for pid in blocked:
        selected.pop(pid, None)
    if enforce_limit and len(selected) > MAX_NODES:
        raise core.ScanError(f"挂起目标超过 {MAX_NODES} 个进程，请检查基线或缩小范围")
    # Parent first stops process creation; every actual PID is captured once.
    nodes = sorted(selected.values(), key=lambda n: depth(n))
    return {"apps": apps, "nodes": nodes, "skipped": skipped}


def preview(target_apps, cfg=None, enforce_limit=True):
    if not storage.full_baseline_available():
        raise storage.StorageError("模式 B 需要重新捕获完整进程基线，旧可见窗口基线或空基线不能用于挂起")
    roots = storage.validate_apps(target_apps, "挂起候选")
    cfg = storage.validate_config(cfg or storage.load_config())
    if not roots:
        return {"apps": [], "nodes": [], "skipped": []}
    return build_plan(roots, core.process_inventory(), storage.load_baseline(),
                      cfg, enforce_limit=enforce_limit)


def _name(operation_id):
    if not isinstance(operation_id, str) or not re.fullmatch("[0-9a-f]{32}", operation_id):
        raise storage.StorageError("无效挂起事务")
    return os.path.join("suspensions", operation_id + ".json")


def read_operation(operation_id):
    data = storage._load(_name(operation_id), None)
    if (not isinstance(data, dict) or data.get("version") != 1
            or data.get("id") != operation_id or not isinstance(data.get("nodes"), list)):
        raise storage.StorageError("挂起事务日志缺失或无效，恢复记录已保留")
    return data


def _write(data):
    storage._save(_name(data["id"]), data)


def _capture_windows(nodes):
    owners = {node["pid"]: node for node in nodes}
    windows = []

    def visit(hwnd, _):
        try:
            if not win32gui.IsWindowVisible(hwnd):
                return True
            pid = win32process.GetWindowThreadProcessId(hwnd)[1]
            if pid not in owners:
                return True
            # Owned dialogs hide automatically when their owner minimizes and stay non-iconic.
            owner = win32gui.GetWindow(hwnd, win32con.GW_OWNER)
            if (isinstance(owner, int) and owner != 0 and win32gui.IsWindowVisible(owner)
                    and win32process.GetWindowThreadProcessId(owner)[1] in owners):
                return True
            rect = win32gui.GetWindowRect(hwnd)
            if isinstance(rect, tuple) and len(rect) == 4 and (rect[2] <= rect[0] or rect[3] <= rect[1]):
                return True
            title = win32gui.GetWindowText(hwnd)
            style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
            if isinstance(title, str) and not title and isinstance(style, int) and not (style & win32con.WS_MINIMIZEBOX):
                return True
            windows.append({"hwnd": hwnd, "pid": pid, "birth": owners[pid]["birth"],
                            "placement": win32gui.GetWindowPlacement(hwnd)})
        except win32gui.error:
            pass
        finally:
            win32api.SetLastError(0)
        return True

    win32api.SetLastError(0)
    try:
        win32gui.EnumWindows(visit, None)
    except win32gui.error as exc:
        raise core.ScanError("挂起前窗口扫描未完整完成，未执行挂起") from exc
    return windows


def _pending_windows(windows):
    return [w for w in windows
            if win32gui.IsWindow(w["hwnd"])
            and win32gui.IsWindowVisible(w["hwnd"])
            and not win32gui.IsIconic(w["hwnd"])]


def _restore_windows(windows, live_pids):
    for window in windows:
        hwnd = window["hwnd"]
        try:
            if (window["pid"] not in live_pids or not win32gui.IsWindow(hwnd)
                    or win32process.GetWindowThreadProcessId(hwnd)[1] != window["pid"]):
                continue
            flags, show, minimum, maximum, rect = window["placement"]
            win32gui.SetWindowPlacement(hwnd, (flags | 0x0004, show, tuple(minimum), tuple(maximum), tuple(rect)))
        except win32gui.error:
            log.warning("window placement could not be restored", exc_info=True)


def guardian_main(operation_id):
    """Own the native handles; recover even if the tray/worker stops responding."""
    import winprocess as native
    data = read_operation(operation_id)
    events = native.Events(operation_id)
    parent_handle, handles, frozen = None, {}, set()
    registered = False
    data["guardian"] = {"pid": os.getpid(), "birth": psutil.Process().create_time(),
                        "thread_id": threading.get_native_id()}
    try:
        parent = data["parent"]
        # A PyInstaller one-file guardian has an intermediate bootloader parent.
        if not any(p.pid == parent["pid"] and abs(p.create_time() - parent["birth"]) < 0.00001
                   for p in psutil.Process().parents()):
            raise core.ScanError("挂起守护的父进程身份无效")
        parent_handle = native.open_identity(parent, suspend_access=False)
        if native.signaled(parent_handle):
            raise core.ScanError("主程序已退出")
        if events.poll("resume"):
            raise core.ScanError("挂起已取消")
        fresh = preview(data["apps"], data["config"])
        if events.poll("resume") or native.signaled(parent_handle):
            raise core.ScanError("挂起已取消")
        if {identity_key(n) for n in fresh["nodes"]} != {identity_key(n) for n in data["nodes"]}:
            raise core.ScanError("挂起前进程树或保护规则发生变化，未执行挂起")
        native.register_rescue_hotkey()
        registered = True
        for node in data["nodes"]:
            if events.poll("resume") or native.signaled(parent_handle):
                raise core.ScanError("挂起已取消")
            if core._matches(node) is not True:
                raise core.ScanError("挂起前进程身份无法确认")
            proc = psutil.Process(node["pid"])
            if proc.status() == psutil.STATUS_STOPPED:
                raise core.ScanError("目标已经被其他操作挂起，未增加挂起计数")
            handles[node["pid"]] = native.open_identity(node)
            node["phase"] = "prepared"
        data["windows"] = _capture_windows(data["nodes"])
        data["phase"] = "preparing"
        _write(data)
        # Asynchronous minimization gives each GUI a chance to leave the foreground.
        for window in data["windows"]:
            if events.poll("resume") or native.signaled(parent_handle):
                raise core.ScanError("挂起已取消")
            native.minimize(window["hwnd"])
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and _pending_windows(data["windows"]):
            if events.poll("resume") or native.signaled(parent_handle):
                raise core.ScanError("挂起已取消")
            time.sleep(0.02)
        if _pending_windows(data["windows"]):
            raise core.ScanError("目标窗口未能最小化，未执行挂起")
        for node in data["nodes"]:
            if events.poll("resume") or native.signaled(parent_handle):
                raise core.ScanError("挂起已取消")
            node["phase"] = "attempting"
            _write(data)
            native.suspend(handles[node["pid"]])
            frozen.add(node["pid"])
            node["phase"] = "suspended"
            _write(data)
        data["phase"] = "awaiting_confirmation"
        _write(data)
        events.set("ready")
        deadline = time.monotonic() + CONFIRM_SECONDS
        while True:
            if events.poll("resume") or native.signaled(parent_handle) or native.rescue_pressed():
                data["reason"] = "已请求恢复、主程序退出或紧急热键"
                break
            if events.poll("confirm"):
                data["phase"] = "active"
                _write(data)
                events.set("confirmed")
                deadline = time.monotonic() + HEARTBEAT_SECONDS
                while True:
                    if events.poll("resume") or native.signaled(parent_handle) or native.rescue_pressed():
                        data["reason"] = "已请求恢复、主程序退出或紧急热键"
                        break
                    if events.poll("heartbeat"):
                        deadline = time.monotonic() + HEARTBEAT_SECONDS
                    if time.monotonic() >= deadline:
                        data["reason"] = "托盘响应超时，已自动恢复"
                        break
                    time.sleep(0.05)
                break
            if time.monotonic() >= deadline:
                data["reason"] = "输入正常确认超时，已自动恢复"
                break
            time.sleep(0.05)
    except Exception as exc:
        data["reason"] = str(exc)
        log.exception("suspension transaction interrupted")
    finally:
        data["phase"] = "resuming"
        try:
            _write(data)
        except Exception:
            log.exception("could not save rollback intent")
        errors, failed_pids = [], set()
        for node in reversed(data["nodes"]):
            pid = node["pid"]
            if pid in frozen:
                try:
                    native.resume(handles[pid])
                    node["phase"] = "resumed"
                except Exception as exc:
                    node["phase"] = "resume_failed"
                    failed_pids.add(pid)
                    errors.append(str(exc))
            elif node.get("phase") == "attempting":
                # A failed native call is not proof that no thread changed state.
                failed_pids.add(pid)
                errors.append("挂起调用结果不确定，保留身份记录以供核对")
        live = set()
        for pid, handle in handles.items():
            if pid in failed_pids:
                continue
            try:
                if not native.signaled(handle):
                    live.add(pid)
            except Exception:
                log.warning("could not check liveness for pid=%s", pid, exc_info=True)
        try:
            _restore_windows(data.get("windows", []), live)
        except Exception:
            log.exception("window restoration failed during guardian recovery")
        data["phase"] = "recovery_failed" if errors else "resumed"
        data["errors"] = errors
        try:
            _write(data)
        except Exception:
            log.exception("could not commit rollback; recovery journal retained")
        try:
            events.set("ready")
            events.set("done")
        finally:
            if registered:
                native.unregister_rescue_hotkey()
            for handle in handles.values():
                native.close(handle)
            native.close(parent_handle)
            events.close()
    return 0 if data["phase"] == "resumed" else 1


class Client:
    def __init__(self, operation_id, healthy, recovered):
        import winprocess as native
        self.id, self.native = operation_id, native
        self.events = native.Events(operation_id, create=True)
        self.healthy = healthy or (lambda: True)
        self.recovered = recovered
        self.stop_event = threading.Event()
        self.monitor = None
        command = [sys.executable]
        environment = dict(os.environ)
        if not getattr(sys, "frozen", False):
            import site
            command.append(os.path.join(os.path.dirname(__file__), "main.py"))
            environment["PYTHONPATH"] = os.pathsep.join(str(p) for p in sys.path if p)
            environment.setdefault("PYTHONUSERBASE", site.getuserbase())
        command += ["--freeze-guardian", operation_id]
        try:
            self.error_stream = open(os.path.join(storage.data_dir(), "suspensions", operation_id + ".log"), "a", encoding="utf-8")
            self.child = subprocess.Popen(command, creationflags=subprocess.CREATE_NO_WINDOW,
                                          stdout=self.error_stream, stderr=self.error_stream, env=environment)
        except Exception:
            self.events.close()
            if hasattr(self, "error_stream"):
                self.error_stream.close()
            raise

    def ready(self, cancel=None):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if cancel and cancel.is_set():
                return False
            if self.events.poll("ready", 100):
                return read_operation(self.id).get("phase") == "awaiting_confirmation"
            if self.child.poll() is not None:
                self.error_stream.flush()
                try:
                    with open(self.error_stream.name, encoding="utf-8", errors="replace") as stream:
                        log.error("guardian startup failed: %s", stream.read())
                except OSError:
                    pass
                return False
        return False

    def confirm(self):
        self.events.set("heartbeat")
        self.events.set("confirm")
        if not self.events.poll("confirmed", 2000):
            return False
        if self.events.poll("done"):
            return False
        self.monitor = threading.Thread(target=self._monitor, name="suspension-heartbeat", daemon=True)
        self.monitor.start()
        return True

    def _monitor(self):
        while not self.stop_event.wait(0.5):
            try:
                done = self.events.poll("done")
                exited = self.child.poll() is not None
            except Exception:
                log.exception("guardian event monitor failed; stopping monitor")
                return
            if done or exited:
                if exited and not done:
                    try:
                        _recover_saved(self.id)
                    except Exception:
                        log.exception("fallback recovery after guardian exit failed")
                if self.recovered:
                    try:
                        self.recovered(self.id)
                    except Exception:
                        log.exception("recovered callback failed")
                return
            try:
                if self.healthy():
                    self.events.set("heartbeat")
            except Exception:
                # Skip heartbeat on probe failure so watchdog recovers, but keep
                # monitoring for 'done' so tray state reconciles cleanly.
                log.exception("guardian heartbeat probe failed; waiting for watchdog recovery")

    def resume(self):
        self.stop_event.set()
        if self.child.poll() is not None and not self.events.poll("done"):
            _recover_saved(self.id)
            self.close()
            return True
        self.events.set("resume")
        if not self.events.poll("done", 6000):
            raise core.ScanError("守护恢复尚未完成，记录已保留")
        data = read_operation(self.id)
        if data["phase"] != "resumed":
            raise core.ScanError("部分进程尚未确认恢复，记录已保留：" + str(data.get("errors", [])))
        self.close()
        return True

    def close(self):
        self.stop_event.set()
        if self.monitor and threading.current_thread() is not self.monitor:
            self.monitor.join(timeout=1)
        self.events.close()
        try:
            self.child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            log.warning("guardian is still exiting; not terminating it")
        self.error_stream.close()


def _start_client(operation_id, healthy, recovered):
    return Client(operation_id, healthy, recovered)


def _parent_identity():
    own = psutil.Process(core.SELF_PID)
    return {"pid": own.pid, "birth": own.create_time(), "exe": own.exe()}


def suspend_apps(target_apps, cfg=None, cancel=None, confirm=None, healthy=None, recovered=None):
    """Prepare all identities, commit intent, delegate native state to a guardian."""
    cfg = storage.validate_config(cfg or storage.load_config())
    plan = preview(target_apps, cfg)
    result = {"suspended": [], "failed": [], "skipped": plan["skipped"], "reason": ""}
    if not plan["apps"]:
        return result
    if not callable(confirm):
        raise core.ScanError("模式 B 需要输入状态确认回调，未执行挂起")
    if cancel and cancel.is_set():
        result["failed"] = plan["apps"]
        return result
    operation_id = uuid.uuid4().hex
    os.makedirs(os.path.join(storage.data_dir(), "suspensions"), exist_ok=True)
    apps = [dict(app, action="suspend", status="prepared", suspend_id=operation_id) for app in plan["apps"]]
    data = {"version": 1, "id": operation_id, "phase": "planned", "parent": _parent_identity(),
            "apps": apps, "nodes": plan["nodes"], "config": cfg}
    _write(data)
    # Persist every root's reference to the guardian journal BEFORE any native call.
    before = storage.load_pending_restore()
    journal = storage.merge_pending_restore(apps, "entering", action="suspend")
    client = None
    try:
        client = _start_client(operation_id, healthy, recovered)
        with _session_lock:
            _sessions[operation_id] = client
        if not client.ready(cancel):
            raise core.ScanError(read_operation(operation_id).get("reason", "挂起准备失败或已取消，详情见事务日志"))
        if not confirm or not confirm(CONFIRM_SECONDS) or (cancel and cancel.is_set()):
            raise core.ScanError("未确认输入正常，已请求自动恢复")
        ids = {app["id"] for app in apps}
        for entry in journal:
            if entry["id"] in ids:
                entry["status"] = "suspended"
        storage.save_pending_restore(journal, "game", action="suspend")
        if not client.confirm():
            raise core.ScanError("守护已超时或无法确认，已请求恢复")
        result["suspended"] = [entry for entry in journal if entry["id"] in ids]
        return result
    except Exception as exc:
        log.exception("suspension rolled back")
        result["reason"] = str(exc)
        result["failed"] = apps
        if client is not None:
            try:
                client.resume()
                with _session_lock:
                    _sessions.pop(operation_id, None)
                storage.save_pending_restore(before, "partial" if before else "ready")
            except Exception:
                log.exception("rollback not confirmed; keeping all guardian references")
        else:
            # Process creation failed: no guardian could have executed native calls.
            storage.save_pending_restore(before, "partial" if before else "ready")
        return result


def _recover_saved(operation_id):
    with _session_lock:
        return _recover_saved_locked(operation_id)


def _recover_saved_locked(operation_id):
    """Reopen only saved identities if the guardian died; never resume bare PIDs."""
    import winprocess as native
    data = read_operation(operation_id)
    if data.get("phase") == "resumed":
        return
    guardian = data.get("guardian")
    if guardian:
        try:
            proc = psutil.Process(guardian["pid"])
            if abs(proc.create_time() - guardian["birth"]) < 0.00001:
                # Communicate through events; do not race a live native owner.
                events = native.Events(operation_id)
                try:
                    events.set("resume")
                    if not events.poll("done", 6000) or read_operation(operation_id)["phase"] != "resumed":
                        raise core.ScanError("守护进程正在恢复，请稍后重试")
                    return
                finally:
                    events.close()
        except psutil.NoSuchProcess:
            pass
    user, session, protected = core._scope()
    errors, live = [], set()
    nodes = storage.validate_apps(data["nodes"], "挂起事务进程")
    for node in reversed(nodes):
        if node.get("phase") not in {"suspended", "attempting", "resume_failed"}:
            continue
        handle = None
        try:
            match = core._matches(node)
            if match is False:
                node["phase"] = "exited"
                continue
            if (match is not True or node["pid"] in protected
                    or node["username"].lower() != user or node["session"] != session):
                raise core.ScanError("无法确认待恢复进程身份")
            handle = native.open_identity(node)
            if psutil.Process(node["pid"]).status() == psutil.STATUS_STOPPED:
                native.resume(handle)
                if psutil.Process(node["pid"]).status() == psutil.STATUS_STOPPED:
                    raise core.ScanError("进程仍处于挂起状态，未清除记录")
            live.add(node["pid"])
            node["phase"] = "resumed"
        except psutil.NoSuchProcess:
            node["phase"] = "exited"
        except Exception as exc:
            errors.append(str(exc))
        finally:
            native.close(handle)
    _restore_windows(data.get("windows", []), live)
    data["nodes"], data["errors"] = nodes, errors
    data["phase"] = "recovery_failed" if errors else "resumed"
    _write(data)
    if errors:
        raise core.ScanError("部分恢复身份或状态无法确认，记录已保留")


def resume_records(apps):
    operations = {app["suspend_id"] for app in apps if app.get("suspend_id")}
    for operation_id in operations:
        with _session_lock:
            client = _sessions.get(operation_id)
        if client:
            if client.child.poll() is not None and not client.events.poll("done"):
                client.close()
                _recover_saved(operation_id)
            else:
                client.resume()
            with _session_lock:
                _sessions.pop(operation_id, None)
        else:
            _recover_saved(operation_id)
    return operations


def operation_reasons(operation_ids):
    reasons = []
    for operation_id in sorted(operation_ids):
        try:
            reason = read_operation(operation_id).get("reason")
            if reason:
                reasons.append(reason)
        except Exception:
            pass
    return reasons


def _prune_completed_operations(active_ids):
    """Keep recent completed guardian logs for diagnostics and prune older ones."""
    directory = os.path.join(storage.data_dir(), "suspensions")
    if not os.path.isdir(directory):
        return
    entries = []
    for name in os.listdir(directory):
        if not name.endswith(".json") or len(name) != 37:
            continue
        operation_id = name[:-5]
        if operation_id in active_ids:
            continue
        with _session_lock:
            if operation_id in _sessions:
                continue
        path = os.path.join(directory, name)
        try:
            entries.append((os.path.getmtime(path), operation_id, path))
        except OSError:
            pass
    entries.sort(reverse=True)
    for _, operation_id, path in entries[MAX_HISTORY_FILES:]:
        try:
            data = read_operation(operation_id)
            if data.get("phase") != "resumed":
                continue
            os.remove(path)
            log_file = os.path.join(directory, operation_id + ".log")
            if os.path.isfile(log_file):
                os.remove(log_file)
        except Exception:
            log.debug("skip pruning guardian operation=%s", operation_id, exc_info=True)


def prune_completed_operations(active_ids):
    _prune_completed_operations(active_ids)


def reconcile_completed():
    """Remove only records whose guardian durably confirmed complete restoration."""
    pending = storage.load_pending_restore()
    completed = set()
    for operation_id in {a["suspend_id"] for a in pending if a.get("suspend_id")}:
        operation = read_operation(operation_id)
        if operation.get("phase") == "resumed":
            log.info("reconciled guardian operation=%s reason=%s", operation_id, operation.get("reason", ""))
            completed.add(operation_id)
            with _session_lock:
                client = _sessions.pop(operation_id, None)
            if client:
                client.close()
    remaining = pending
    if completed:
        remaining = []
        for app in pending:
            if app.get("suspend_id") not in completed:
                remaining.append(app)
                continue
            matching = core._matches(app)
            try:
                still_stopped = matching is True and psutil.Process(app["pid"]).status() == psutil.STATUS_STOPPED
            except psutil.Error:
                still_stopped = True
            if matching is not True or still_stopped:
                if matching is False:
                    app["action"], app["status"] = "close", "closed"
                    app.pop("suspend_id", None)
                remaining.append(app)
        storage.save_pending_restore(remaining, "partial" if remaining else "ready")
        try:
            _prune_completed_operations({a["suspend_id"] for a in remaining if a.get("suspend_id")})
        except Exception:
            log.debug("guardian history pruning failed", exc_info=True)
    return completed
