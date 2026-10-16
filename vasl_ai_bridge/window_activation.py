"""Best-effort Windows foreground return for a window owned by this process.

Windows may refuse foreground activation. A false result lets the caller use
its ordinary taskbar alert without changing a successful authentication state.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from functools import lru_cache
import os
import sys


SW_SHOWMAXIMIZED = 3
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9


@lru_cache(maxsize=1)
def _user32():
    api = ctypes.WinDLL("user32", use_last_error=True)
    for name in ("IsWindow", "IsIconic", "IsZoomed", "SetForegroundWindow"):
        function = getattr(api, name)
        function.argtypes = [wintypes.HWND]
        function.restype = wintypes.BOOL
    api.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    api.ShowWindow.restype = wintypes.BOOL
    api.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    api.GetWindowThreadProcessId.restype = wintypes.DWORD
    api.GetForegroundWindow.argtypes = []
    api.GetForegroundWindow.restype = wintypes.HWND
    return api


def bring_window_to_front(handle: int) -> bool:
    """Restore and request focus for the supplied own HWND, then verify it.

    The caller supplies its current native window handle. No window search,
    simulated input, foreground-policy changes, or input-thread attachment is
    performed. Handles belonging to another process are never activated.
    """
    if (sys.platform != "win32" or type(handle) is not int
            or not 0 < handle < 1 << (ctypes.sizeof(ctypes.c_void_p) * 8)):
        return False
    try:
        api = _user32()
        if not api.IsWindow(handle):
            return False
        process_id = wintypes.DWORD()
        if (not api.GetWindowThreadProcessId(handle, ctypes.byref(process_id))
                or process_id.value != os.getpid()):
            return False
        minimized = bool(api.IsIconic(handle))
        maximized = bool(api.IsZoomed(handle))
        # SW_SHOW requests activation while preserving normal/maximized size
        # and position; only a minimized target needs SW_RESTORE. ShowWindow's
        # return value describes previous visibility, not activation success.
        api.ShowWindow(handle, SW_RESTORE if minimized else SW_SHOW)
        api.SetForegroundWindow(handle)
        if api.GetForegroundWindow() == handle:
            return True
        if minimized:
            return False
        # A visible window may still be refused after SW_SHOW. Perform one
        # bounded restore cycle on this own window, keeping maximized state.
        api.ShowWindow(handle, SW_MINIMIZE)
        api.ShowWindow(handle, SW_SHOWMAXIMIZED if maximized else SW_RESTORE)
        api.SetForegroundWindow(handle)
        return api.GetForegroundWindow() == handle
    except (OSError, ctypes.ArgumentError, AttributeError, TypeError, ValueError):
        return False
