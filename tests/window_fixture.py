"""An offscreen synthetic window owned solely by the integration test."""
import argparse
import json
import os
from pathlib import Path
import win32api
import win32con
import win32gui

parser = argparse.ArgumentParser()
parser.add_argument("--ready", required=True)
parser.add_argument("--accept-close", action="store_true")
args = parser.parse_args()
STOP = win32con.WM_APP + 55


def wndproc(hwnd, message, wparam, lparam):
    if message == STOP or (message == win32con.WM_CLOSE and args.accept_close):
        win32gui.DestroyWindow(hwnd)
        return 0
    if message == win32con.WM_CLOSE:
        return 0  # behaves like a user cancelling a save/close prompt
    if message == win32con.WM_DESTROY:
        win32gui.PostQuitMessage(0)
        return 0
    return win32gui.DefWindowProc(hwnd, message, wparam, lparam)


instance = win32api.GetModuleHandle(None)
window_class = win32gui.WNDCLASS()
window_class.hInstance = instance
window_class.lpszClassName = f"GameSwitcher-Fixture-{os.getpid()}"
window_class.lpfnWndProc = wndproc
atom = win32gui.RegisterClass(window_class)
hwnd = win32gui.CreateWindowEx(win32con.WS_EX_TOOLWINDOW, atom, "GameSwitcher test fixture",
                              win32con.WS_OVERLAPPEDWINDOW, -32000, -32000, 100, 100,
                              0, 0, instance, None)
win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
ready = Path(args.ready)
temporary = ready.with_suffix(".tmp")
temporary.write_text(json.dumps({"pid": os.getpid(), "hwnd": hwnd}), encoding="utf-8")
os.replace(temporary, ready)
win32gui.PumpMessages()
