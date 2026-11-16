"""Small Windows handle layer for the isolated suspension guardian.

No working-set trimming, tree traversal, or bare-PID resume is performed here.
"""
import ctypes
from ctypes import wintypes
import os

import core

kernel = ctypes.WinDLL("kernel32", use_last_error=True)
ntdll = ctypes.WinDLL("ntdll")
user = ctypes.WinDLL("user32", use_last_error=True)
HANDLE = wintypes.HANDLE
DWORD = wintypes.DWORD
BOOL = wintypes.BOOL
WAIT_OBJECT_0, WAIT_TIMEOUT = 0, 258
SYNCHRONIZE, MODIFY_EVENT = 0x100000, 0x0002
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_SUSPEND_RESUME = 0x0800


def bind(library, name, args, result):
    function = getattr(library, name)
    function.argtypes, function.restype = args, result
    return function


_open = bind(kernel, "OpenProcess", [DWORD, BOOL, DWORD], HANDLE)
_close = bind(kernel, "CloseHandle", [HANDLE], BOOL)
_wait = bind(kernel, "WaitForSingleObject", [HANDLE, DWORD], DWORD)
_times = bind(kernel, "GetProcessTimes", [HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4, BOOL)
_image = bind(kernel, "QueryFullProcessImageNameW", [HANDLE, DWORD, wintypes.LPWSTR, ctypes.POINTER(DWORD)], BOOL)
_suspend = bind(ntdll, "NtSuspendProcess", [HANDLE], ctypes.c_long)
_resume = bind(ntdll, "NtResumeProcess", [HANDLE], ctypes.c_long)
_create_event = bind(kernel, "CreateEventW", [ctypes.c_void_p, BOOL, BOOL, wintypes.LPCWSTR], HANDLE)
_open_event = bind(kernel, "OpenEventW", [DWORD, BOOL, wintypes.LPCWSTR], HANDLE)
_set_event = bind(kernel, "SetEvent", [HANDLE], BOOL)
_reset_event = bind(kernel, "ResetEvent", [HANDLE], BOOL)
_register_hotkey = bind(user, "RegisterHotKey", [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT], BOOL)
_unregister_hotkey = bind(user, "UnregisterHotKey", [wintypes.HWND, ctypes.c_int], BOOL)
_peek = bind(user, "PeekMessageW", [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT], BOOL)
_send_timeout = bind(user, "SendMessageTimeoutW",
                     [wintypes.HWND, wintypes.UINT, ctypes.c_size_t, ctypes.c_ssize_t,
                      wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)], ctypes.c_ssize_t)
_show_async = bind(user, "ShowWindowAsync", [wintypes.HWND, ctypes.c_int], BOOL)


def minimize(hwnd):
    return _show_async(hwnd, 6)


def close(handle):
    if handle:
        _close(handle)


def signaled(handle, timeout_ms=0):
    code = _wait(handle, timeout_ms)
    if code not in (WAIT_OBJECT_0, WAIT_TIMEOUT):
        raise ctypes.WinError(ctypes.get_last_error())
    return code == WAIT_OBJECT_0


def open_identity(identity, suspend_access=True):
    """Pin the process object and check its creation time and path through this handle."""
    rights = PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE
    if suspend_access:
        rights |= PROCESS_SUSPEND_RESUME
    handle = _open(rights, False, identity["pid"])
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        times = [wintypes.FILETIME() for _ in range(4)]
        if not _times(handle, *(ctypes.byref(value) for value in times)):
            raise ctypes.WinError(ctypes.get_last_error())
        ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        birth = (ticks - 116444736000000000) / 10000000
        path, size = ctypes.create_unicode_buffer(32768), DWORD(32768)
        if not _image(handle, 0, path, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        if abs(birth - identity["birth"]) > 0.00001 or core.path_key(path.value) != core.path_key(identity["exe"]):
            raise core.ScanError("进程身份已变化，拒绝操作")
        return handle
    except Exception:
        close(handle)
        raise


def suspend(handle):
    status = _suspend(handle)
    if status < 0:
        raise OSError(f"NtSuspendProcess failed: 0x{status & 0xffffffff:08x}")


def resume(handle):
    if signaled(handle):
        return
    status = _resume(handle)
    if status < 0:
        raise OSError(f"NtResumeProcess failed: 0x{status & 0xffffffff:08x}")


EVENT_NAMES = ("ready", "confirm", "confirmed", "heartbeat", "resume", "done")


class Events:
    def __init__(self, operation_id, create=False):
        self.handles = {}
        try:
            for event in EVENT_NAMES:
                name = f"Local\\GameSwitcher_{operation_id}_{event}"
                handle = (_create_event(None, event != "heartbeat", False, name) if create
                          else _open_event(SYNCHRONIZE | MODIFY_EVENT, False, name))
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                if create and ctypes.get_last_error() == 183:
                    close(handle)
                    raise core.ScanError("挂起事务事件已经存在")
                self.handles[event] = handle
        except Exception:
            self.close()
            raise

    def set(self, event):
        if not _set_event(self.handles[event]):
            raise ctypes.WinError(ctypes.get_last_error())

    def poll(self, event, milliseconds=0):
        return signaled(self.handles[event], milliseconds)

    def close(self):
        for handle in self.handles.values():
            close(handle)
        self.handles.clear()


def register_rescue_hotkey():
    # Ctrl+Alt+F12 belongs to the independent guardian, not a frozen application.
    message = wintypes.MSG()
    _peek(ctypes.byref(message), None, 0, 0, 0)
    if not _register_hotkey(None, 1, 0x0002 | 0x0001 | 0x4000, 0x7B):
        raise OSError("Ctrl+Alt+F12 已被占用，未执行挂起")


def rescue_pressed():
    message = wintypes.MSG()
    pressed = False
    while _peek(ctypes.byref(message), None, 0, 0, 1):
        pressed |= message.message == 0x0312
    return pressed


def unregister_rescue_hotkey():
    _unregister_hotkey(None, 1)


def ui_responding(hwnd):
    if not hwnd:
        return False
    result = ctypes.c_size_t()
    return bool(_send_timeout(hwnd, 0, 0, 0, 0x0002, 300, ctypes.byref(result)))
