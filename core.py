"""GameSwitcher core - enumerate, close, and relaunch user applications.

Windows-only. psutil for process info, win32gui/win32con for window messages.
UI (tray) lives in main.py; this module is UI-free and talks through callbacks.
"""
import os
import subprocess
import time
from typing import Dict, List, Set

import psutil
import win32api
import win32con
import win32gui
import win32process


SELF_PID = os.getpid()

SYSTEM_EXES = {
    # Windows 核心界面与外壳
    "explorer.exe", "applicationframehost.exe", "runtimebroker.exe",
    "shellexperiencehost.exe", "searchhost.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "widgets.exe", "systemsettings.exe", "taskmgr.exe",
    "ctfmon.exe", "conhost.exe", "dwm.exe", "securityhealthsystray.exe",
    "securityhealthservice.exe",
    # 系统底层服务
    "smss.exe", "csrss.exe", "wininit.exe", "services.exe", "lsass.exe",
    "svchost.exe", "fontdrvhost.exe", "winlogon.exe", "audiodg.exe",
    # 游戏辅助、录屏与显卡驱动覆盖层（进入游戏所需）
    "gamebar.exe", "gamebarftserver.exe", "nvspcaps64.exe", "nvcontainer.exe",
    "nvidia share.exe", "radeonsoftware.exe", "amdrsserv.exe",
}


def _is_user_app(proc: psutil.Process) -> bool:
    try:
        if proc.pid in (0, 4, SELF_PID):
            return False
        exe = proc.exe()
        name = os.path.basename(exe).lower()
        if name in SYSTEM_EXES or name.endswith(".tmp"):
            return False
        return True
    except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
        return False


def visible_apps() -> list:
    """User applications that own at least one visible top-level window.

    Returns a list of dicts: {pid, exe, name, cmdline, cwd, hwnds, titles}.
    Deduplicated by exe path (one entry per application, all its windows
    aggregated), so multi-window apps (e.g. Chrome) appear once.

    Optimized with lazy-evaluation for process cmdline & cwd.
    """
    apps = {}
    by_pid = {}
    # 极速轻量扫描：仅拉取 pid, exe, name，避免全系统进程 PEB 深度读取
    for proc in psutil.process_iter(["pid", "exe", "name"]):
        by_pid[proc.info["pid"]] = proc

    def on_window(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        try:
            title = win32gui.GetWindowText(hwnd)
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except win32gui.error:
            return True  # 窗口在枚举过程中销毁
        if not title:
            return True
        proc = by_pid.get(pid)
        if proc is None or not _is_user_app(proc):
            return True
        try:
            exe = proc.exe()
        except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
            return True
        key = os.path.normcase(exe)
        entry = apps.get(key)
        if entry is None:
            # 懒加载：仅对真正具有顶级可见窗口的目标进程提取 cmdline 与 cwd
            cmdline = []
            try:
                cmdline = proc.cmdline() or []
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            cwd = ""
            try:
                cwd = proc.cwd() or ""
            except (psutil.AccessDenied, psutil.NoSuchProcess, psutil.ZombieProcess):
                pass
            entry = {
                "pid": pid,
                "exe": exe,
                "name": os.path.basename(exe),
                "cmdline": cmdline,
                "cwd": cwd,
                "hwnds": [hwnd],
                "titles": [title],
            }
            apps[key] = entry
        else:
            entry["hwnds"].append(hwnd)
            entry["titles"].append(title)
        return True

    win32api.SetLastError(0)
    try:
        win32gui.EnumWindows(on_window, None)
    except Exception:
        pass
    return list(apps.values())



def apps_by_exe(exe_path: str) -> list:
    """PIDs whose exe matches the given path (case-insensitive). Never includes current process."""
    target = os.path.normcase(os.path.abspath(exe_path))
    out = []
    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            if proc.info["pid"] in (0, 4, SELF_PID):
                continue
            pexe = proc.info.get("exe")
            if pexe and os.path.normcase(os.path.abspath(pexe)) == target:
                out.append(proc.info["pid"])
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return out


def pids_by_exes(exe_list: List[str]) -> Dict[str, List[int]]:
    """Batch query PIDs for multiple exes in a single system process iteration (O(M) instead of O(N*M))."""
    norm_map: Dict[str, str] = {}
    out: Dict[str, List[int]] = {}
    for e in exe_list:
        n = os.path.normcase(os.path.abspath(e))
        norm_map[n] = e
        norm_map[os.path.normcase(e)] = e
        norm_map[os.path.basename(n)] = e
        out[e] = []

    for proc in psutil.process_iter(["pid", "exe"]):
        try:
            pid = proc.info["pid"]
            if pid in (0, 4, SELF_PID):
                continue
            pexe = proc.info.get("exe")
            if not pexe:
                continue
            n = os.path.normcase(os.path.abspath(pexe))
            b = os.path.basename(n)
            orig = norm_map.get(n) or norm_map.get(b)
            if orig is not None:
                out[orig].append(pid)
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return out


def _close_windows_of_pid(pid: int) -> None:
    if pid in (0, 4, SELF_PID):
        return

    def on_window(hwnd, _):
        if win32gui.IsWindowVisible(hwnd):
            _, wpid = win32process.GetWindowThreadProcessId(hwnd)
            if wpid == pid:
                try:
                    win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
                except Exception:
                    pass
        return True

    try:
        win32gui.EnumWindows(on_window, None)
    except Exception:
        pass


def _batch_taskkill(pids: List[int], force: bool = True) -> None:
    valid_pids = [p for p in set(pids) if p not in (0, 4, SELF_PID)]
    if not valid_pids:
        return
    cmd = ["taskkill"]
    if force:
        cmd.append("/F")
    cmd.append("/T")
    for p in valid_pids:
        cmd.extend(["/PID", str(p)])
    try:
        subprocess.run(cmd, capture_output=True, timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError):
        pass


def _wait_exit(pid: int, timeout: float) -> bool:
    """Wait for a process to exit, verifying creation time to guard against PID reuse."""
    try:
        proc = psutil.Process(pid)
        birth = proc.create_time()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return True

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            cur = psutil.Process(pid)
            if cur.create_time() != birth or not cur.is_running():
                return True
        except (psutil.NoSuchProcess, psutil.ZombieProcess):
            return True
        except psutil.AccessDenied:
            pass
        time.sleep(0.1)

    try:
        cur = psutil.Process(pid)
        return cur.create_time() != birth or not cur.is_running()
    except (psutil.NoSuchProcess, psutil.ZombieProcess):
        return True
    except psutil.AccessDenied:
        return False


def close_apps(target_apps: list, cfg: dict, progress=None) -> dict:
    """Close the given apps rapidly in parallel.

    Stage 1: Send graceful WM_CLOSE to all targets concurrently.
    Stage 2: Wait briefly (1.5s) for apps to save state and exit cleanly.
    Stage 3: Forcefully terminate any remaining processes via single batch taskkill.
    Zero interruption dialogs, finishes in 1-2 seconds.
    """
    result = {"closed": [], "skipped": [], "failed": []}
    if not target_apps:
        return result

    target_exes = [a["exe"] for a in target_apps]

    # 1. 批量单次扫描全系统，高速收集所有目标应用的 PID（排除自身）
    app_pids_map = pids_by_exes(target_exes)

    # 2. 第一阶段：并发广播优雅关闭信号
    for app in target_apps:
        pids = app_pids_map.get(app["exe"]) or []
        for pid in pids:
            _close_windows_of_pid(pid)

    # 3. 第二阶段：统一平滑等待缓冲（默认 1.5 秒，全退则提前结束）
    grace = float(cfg.get("grace_timeout_sec", 1.5))
    deadline = time.time() + grace
    while time.time() < deadline:
        all_done = True
        for app in target_apps:
            pids = app_pids_map.get(app["exe"]) or []
            if any(not _wait_exit(p, 0.05) for p in pids):
                all_done = False
                break
        if all_done:
            break
        time.sleep(0.1)

    # 4. 第三阶段：对未退出的残留进程执行单次批量强杀（免打扰，瞬间完成）
    remaining_map = pids_by_exes(target_exes)
    pids_to_kill = []
    for app in target_apps:
        pids_to_kill.extend(remaining_map.get(app["exe"]) or [])
    if pids_to_kill:
        _batch_taskkill(pids_to_kill, force=True)

    # 5. 快速确认关闭结果
    time.sleep(0.15)
    final_check_map = pids_by_exes(target_exes)
    for app in target_apps:
        remaining = final_check_map.get(app["exe"]) or []
        if not remaining:
            result["closed"].append(app)
        else:
            result["failed"].append(app)

    return result



def launch_app(app: dict) -> bool:
    exe = app.get("exe") or ""
    if not os.path.isfile(exe):
        return False

    raw_cwd = app.get("cwd") or ""
    cwd = raw_cwd if (raw_cwd and os.path.isdir(raw_cwd)) else None
    cmdline = app.get("cmdline") or []

    # 过滤 Chromium / Electron 等多进程应用的子进程内部参数
    # 如果参数中包含 --type= 或 crashpad，说明采集到的是内部渲染/辅助子进程，不可用于重新启动
    clean_cmdline = []
    if cmdline and isinstance(cmdline, list):
        has_internal_flag = any(
            isinstance(arg, str) and (arg.startswith("--type=") or "crashpad" in arg.lower())
            for arg in cmdline[1:]
        )
        if not has_internal_flag:
            clean_cmdline = list(cmdline)
            clean_cmdline[0] = exe  # 确保第 0 项是绝对可执行路径

    # 1. 若清洗后的参数多于 1 个，优先带参数启动
    if len(clean_cmdline) > 1:
        try:
            subprocess.Popen(
                clean_cmdline,
                cwd=cwd,
                creationflags=subprocess.DETACHED_PROCESS,
            )
            return True
        except OSError:
            pass  # 可能由于管理员权限提升要求(WinError 740)或无效参数，回退

    # 2. Windows 原生 Shell 启动（可正确触发 UAC 提权提示，并自动继承系统关联与默认环境）
    try:
        os.startfile(exe)
        return True
    except OSError:
        pass

    # 3. 最终保底：不带额外参数的纯净 Popen 启动
    try:
        subprocess.Popen(
            [exe],
            cwd=cwd,
            creationflags=subprocess.DETACHED_PROCESS,
        )
        return True
    except OSError:
        return False


def restore_apps(progress=None) -> dict:
    """Relaunch apps recorded in the pending-restore snapshot that aren't running."""
    import storage

    pending = storage.load_pending_restore()
    if not pending:
        return {"restored": [], "already_running": [], "missing": []}
    out = {"restored": [], "already_running": [], "missing": []}

    target_exes = [a["exe"] for a in pending if a.get("exe")]
    running_pids = pids_by_exes(target_exes)

    remaining = list(pending)
    try:
        for i, app in enumerate(list(pending)):
            name = app.get("name") or app.get("exe")
            if progress:
                progress(f"恢复 {name} ({i + 1}/{len(pending)}) ...")
            if running_pids.get(app["exe"]):
                out["already_running"].append(app)
                remaining = [a for a in remaining if os.path.normcase(a.get("exe", "")) != os.path.normcase(app["exe"])]
                continue
            if launch_app(app):
                out["restored"].append(app)
                remaining = [a for a in remaining if os.path.normcase(a.get("exe", "")) != os.path.normcase(app["exe"])]
            else:
                out["missing"].append(app)
            time.sleep(0.1)  # 极速平滑拉起（0.1s 间隔）
    finally:
        if remaining:
            storage.save_pending_restore(remaining)
        else:
            storage.clear_pending_restore()
    return out

