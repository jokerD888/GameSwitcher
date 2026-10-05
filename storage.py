"""Validated, atomic persistence in %APPDATA%/GameSwitcher."""
import hashlib
import json
import math
import os
import tempfile
import time

APP_DIR_NAME = "GameSwitcher"
BASELINE_FILE = "baseline.json"
PENDING_RESTORE_FILE = "pending_restore.json"
CONFIG_FILE = "config.json"
MODES = {"ready", "entering", "game", "partial", "restoring"}


class StorageError(ValueError):
    """An existing file could not safely be read or validated."""


def data_dir():
    directory = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), APP_DIR_NAME)
    os.makedirs(directory, exist_ok=True)
    return directory


def _path(name):
    return os.path.join(data_dir(), name)


def log_path():
    return _path("log.txt")


def _load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default
    except (OSError, ValueError, UnicodeError) as exc:
        raise StorageError(f"无法读取 {name}，已停止操作以保留原文件：{exc}") from exc


def _save(name, data):
    path = _path(name)
    fd, temporary = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def timeout_value(value, name, minimum=0.1, maximum=60):
    if (isinstance(value, bool) or not isinstance(value, (float, int))
            or not minimum <= value <= maximum or not math.isfinite(value)):
        raise StorageError(f"{name} 必须是 {minimum}–{maximum} 之间的有限数字")
    return float(value)


def validate_config(cfg):
    if not isinstance(cfg, dict):
        raise StorageError("config.json 必须是 JSON 对象")
    cfg = dict(cfg)
    excludes = cfg.setdefault("exclude_list", [])
    if not isinstance(excludes, list) or any(not isinstance(e, str) or not e.strip() for e in excludes):
        raise StorageError("config.json 的 exclude_list 必须是非空字符串列表")
    cfg["grace_timeout_sec"] = timeout_value(cfg.get("grace_timeout_sec", 5), "grace_timeout_sec")
    cfg["restore_timeout_sec"] = timeout_value(cfg.get("restore_timeout_sec", 8), "restore_timeout_sec", 1, 30)
    cfg.pop("auto_save_enabled", None)
    return cfg


def load_config():
    return validate_config(_load(CONFIG_FILE, {}))


def save_config(cfg):
    _save(CONFIG_FILE, validate_config(cfg))


def validate_apps(apps, source):
    if not isinstance(apps, list):
        raise StorageError(f"{source} 的应用记录必须是列表")
    result = []
    for original in apps:
        if not isinstance(original, dict):
            raise StorageError(f"{source} 含无效应用记录")
        app = dict(original)
        exe = app.get("exe")
        if not isinstance(exe, str) or not os.path.isabs(exe) or "\0" in exe:
            raise StorageError(f"{source} 的 exe 必须是绝对路径")
        app.setdefault("name", os.path.basename(exe))
        app.setdefault("cmdline", [])
        app.setdefault("cwd", "")
        app.setdefault("hwnds", [])
        if not isinstance(app["name"], str) or not isinstance(app["cwd"], str) or "\0" in app["cwd"]:
            raise StorageError(f"{source} 含无效名称或工作目录")
        if (not isinstance(app["cmdline"], list)
                or any(not isinstance(arg, str) or "\0" in arg for arg in app["cmdline"])):
            raise StorageError(f"{source} 的 cmdline 必须是字符串列表")
        if not isinstance(app["hwnds"], list) or any(type(h) is not int or h <= 0 for h in app["hwnds"]):
            raise StorageError(f"{source} 含无效窗口句柄")
        if "pid" in app and (type(app["pid"]) is not int or app["pid"] <= 0):
            raise StorageError(f"{source} 含无效 PID")
        if "birth" in app:
            timeout_value(app["birth"], "birth", 0, 1e12)
        for field in ("username", "id"):
            if field in app and (not isinstance(app[field], str) or not app[field]):
                raise StorageError(f"{source} 含无效 {field}")
        if "session" in app and (type(app["session"]) is not int or app["session"] < 0):
            raise StorageError(f"{source} 含无效会话")
        if "restore_attempted" in app and type(app["restore_attempted"]) is not bool:
            raise StorageError(f"{source} 含无效恢复尝试标记")
        if app.get("status", "prepared") not in {"prepared", "closed", "not_closed"}:
            raise StorageError(f"{source} 含无效应用状态")
        if "id" not in app:
            identity = [os.path.normcase(exe), app.get("pid"), app.get("birth"),
                        app["cmdline"], app["cwd"], app["hwnds"]]
            app["id"] = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode("utf-8")).hexdigest()
        result.append(app)
    if len({a["id"] for a in result}) != len(result):
        raise StorageError(f"{source} 含重复记录标识")
    return result


def baseline_exists():
    sentinel = object()
    return _load(BASELINE_FILE, sentinel) is not sentinel


def save_baseline(apps):
    valid = validate_apps(apps, BASELINE_FILE)
    paths = {os.path.normcase(a["exe"]): a["exe"] for a in valid}
    _save(BASELINE_FILE, [{"exe": exe, "name": os.path.basename(exe)} for exe in paths.values()])


def load_baseline():
    return validate_apps(_load(BASELINE_FILE, []), BASELINE_FILE)


def load_state():
    raw = _load(PENDING_RESTORE_FILE, {"version": 2, "mode": "ready", "apps": []})
    if isinstance(raw, list):
        raw = {"version": 2, "mode": "game" if raw else "ready", "apps": raw}
    if (not isinstance(raw, dict) or raw.get("version") != 2 or raw.get("mode") not in MODES):
        raise StorageError("pending_restore.json 的状态结构无效，原文件已保留")
    apps = validate_apps(raw.get("apps"), PENDING_RESTORE_FILE)
    if raw["mode"] == "ready" and apps:
        raise StorageError("就绪状态不能包含待恢复记录")
    return {"version": 2, "mode": raw["mode"], "apps": apps}


def load_pending_restore():
    return load_state()["apps"]


def save_pending_restore(apps, mode=None):
    apps = validate_apps(apps, PENDING_RESTORE_FILE)
    mode = mode or ("game" if apps else "ready")
    if mode not in MODES:
        raise StorageError("无效操作状态")
    if mode == "ready" and apps:
        raise StorageError("就绪状态不能包含待恢复记录")
    _save(PENDING_RESTORE_FILE, {"version": 2, "mode": mode, "apps": apps})


def merge_pending_restore(new_apps, mode="entering"):
    merged = {a["id"]: a for a in load_pending_restore()}
    for app in validate_apps(new_apps, PENDING_RESTORE_FILE):
        merged[app["id"]] = app
    result = list(merged.values())
    save_pending_restore(result, mode)
    return result


def clear_pending_restore():
    save_pending_restore([], "ready")
