"""Run only the executable's isolated self-test; never enter normal app mode."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
executable = Path(sys.argv[1] if len(sys.argv) > 1 else root / "dist" / "GameSwitcher.exe").resolve()
with tempfile.TemporaryDirectory(prefix="gameswitcher-package-probe-") as directory:
    report = Path(directory) / "result.json"
    completed = subprocess.run([str(executable), "--self-test", str(report)],
                               timeout=60, creationflags=subprocess.CREATE_NO_WINDOW)
    if completed.returncode != 0 or not report.is_file():
        raise SystemExit(f"Packaged self-test failed: {completed.returncode}")
    result = json.loads(report.read_text(encoding="utf-8"))
    result["exe_bytes"] = executable.stat().st_size
    result["sha256"] = hashlib.sha256(executable.read_bytes()).hexdigest()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result.get("ok"):
        raise SystemExit(1)
