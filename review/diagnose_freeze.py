"""Read-only diagnostics; never suspend, resume, trim, or terminate processes."""
import json
import os
from pathlib import Path
import sys

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import storage

state = storage.load_state()
report = {"boot_time": psutil.boot_time(), "mode": state["mode"], "action": state["action"], "apps": []}
for app in state["apps"]:
    item = {"name": app["name"], "pid": app.get("pid"), "recorded_birth": app.get("birth"),
            "action": app.get("action"), "tree_recorded": bool(app.get("tree_pids")),
            "suspend_id": app.get("suspend_id")}
    if app.get("suspend_id"):
        op_file = Path(storage.data_dir()) / "suspensions" / f"{app['suspend_id']}.json"
        if op_file.is_file():
            try:
                op = json.loads(op_file.read_text(encoding="utf-8"))
                item["guardian_phase"] = op.get("phase")
                item["guardian_reason"] = op.get("reason")
                item["guardian_errors"] = op.get("errors")
            except (OSError, ValueError) as exc:
                item["guardian_read_error"] = str(exc)
    try:
        proc = psutil.Process(app["pid"])
        item.update(current_birth=proc.create_time(), status=proc.status(),
                    identity_matches=abs(proc.create_time() - (app.get("birth") or 0)) < 0.001)
        if item["identity_matches"]:
            children = proc.children(recursive=True)
            item["children"] = len(children)
            item["stopped_children"] = sum(p.status() == psutil.STATUS_STOPPED for p in children if p.is_running())
    except psutil.Error as exc:
        item["lookup_error"] = type(exc).__name__
    report["apps"].append(item)
print(json.dumps(report, ensure_ascii=False, indent=2))
