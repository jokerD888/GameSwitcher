"""Invoke only the isolated suspension self-test of a supplied GameSwitcher EXE."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
exe = Path(sys.argv[1] if len(sys.argv) > 1 else root / "dist-fixed" / "GameSwitcher.exe").resolve()
fixture = root / "dist-fixed" / "fixtures" / "SuspensionFixture.exe"
with tempfile.TemporaryDirectory(prefix="gameswitcher-packaged-check-") as directory:
    output = Path(directory) / "result.json"
    child = subprocess.run([str(exe), "--self-test-suspend", str(fixture), str(output)],
                           creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
    if not output.is_file():
        raise SystemExit(f"No packaged self-test report; exit={child.returncode}")
    result = json.loads(output.read_text(encoding="utf-8"))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if child.returncode != 0 or not result.get("ok"):
        raise SystemExit(1)
