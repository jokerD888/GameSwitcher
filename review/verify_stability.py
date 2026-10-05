"""Run the safety regressions; no real apps are closed or launched."""
from pathlib import Path
import runpy

runpy.run_path(str(Path(__file__).resolve().parents[1] / "tests" / "test_stability.py"),
               run_name="__main__")
