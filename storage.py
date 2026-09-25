"""GameSwitcher - config and snapshot persistence.

All files live in %APPDATA%\\GameSwitcher\\.
"""
import json
import os
import tempfile
import time

APP_DIR_NAME = "GameSwitcher"

BASELINE_FILE = "baseline.json"          # apps that may keep running in game mode
PENDING_RESTORE_FILE = "pending_restore.json"  # snapshot of apps closed for gaming
CONFIG_FILE = "config.json"


def data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, APP_DIR_NAME)
    os.makedirs(d, exist_ok=True)
    return d


def log_path() -> str:
    return os.path.join(data_dir(), "log.txt")


def _path(name: str) -> str:
    return os.path.join(data_dir(), name)


def _load(name: str, default):
    try:
        with open(_path(name), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default


def _save(name: str, data) -> None:
    # write to temp file, flush and fsync, then atomic replace
    path = _path(name)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def load_config() -> dict:
    cfg = _load(CONFIG_FILE, {})
    cfg.setdefault("exclude_list", [])          # exe names never closed
    cfg.setdefault("auto_save_enabled", False)  # experimental: try clicking "Save"
    cfg.setdefault("grace_timeout_sec", 5)
    return cfg


def save_config(cfg: dict) -> None:
    _save(CONFIG_FILE, cfg)


def save_baseline(apps: list) -> None:
    _save(BASELINE_FILE, apps)


def load_baseline() -> list:
    return _load(BASELINE_FILE, [])


def save_pending_restore(apps: list) -> None:
    _save(PENDING_RESTORE_FILE, apps)


def load_pending_restore() -> list:
    return _load(PENDING_RESTORE_FILE, [])


def merge_pending_restore(new_apps: list) -> list:
    """Merge newly closed apps into existing pending_restore snapshot without losing prior apps."""
    existing = load_pending_restore()
    merged = {os.path.normcase(a["exe"]): a for a in existing if a.get("exe")}
    for app in new_apps:
        if app.get("exe"):
            merged[os.path.normcase(app["exe"])] = app
    res = list(merged.values())
    save_pending_restore(res)
    return res


def clear_pending_restore() -> None:
    try:
        os.remove(_path(PENDING_RESTORE_FILE))
    except OSError:
        pass
