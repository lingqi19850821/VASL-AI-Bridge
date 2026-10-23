"""Bundled worker commands and isolated Windows external-process launching.

Only the console helper resets DLL lookup. The desktop process keeps its
PyInstaller lookup intact, so concurrent Qt activity cannot observe a reset.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
from typing import Sequence

from . import APP_NAME

SPEECH_HELPER_NAME = "VASL-AI-Bridge-Speech.exe"
_JOB_HANDLES: list[int] = []


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def speech_helper_path() -> Path:
    """Resolve only the helper installed beside the desktop executable."""
    helper = Path(sys.executable).resolve().with_name(SPEECH_HELPER_NAME)
    if not helper.is_file():
        raise RuntimeError(f"The installed background helper is missing. Reinstall {APP_NAME}.")
    return helper


def external_command(command: Sequence[str]) -> list[str]:
    """Wrap external argv in frozen builds; preserve direct source launches."""
    if not command or any(not isinstance(value, str) for value in command):
        raise ValueError("A local program command is required.")
    if not is_frozen():
        return list(command)
    return [str(speech_helper_path()), "--external", *command]


def _bundle_path(value: str, root: Path) -> bool:
    try:
        Path(value).resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return bool(value)


def external_environment(environment: dict[str, str] | None = None) -> dict[str, str]:
    """Remove only bundle-owned search paths from a copied child environment."""
    result = dict(os.environ if environment is None else environment)
    bundle = getattr(sys, "_MEIPASS", None)
    if not is_frozen() or not bundle:
        return result
    root = Path(bundle).resolve()
    for key in list(result):
        if key.upper() in {"PATH", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH",
                           "QML2_IMPORT_PATH", "QML_IMPORT_PATH"}:
            entries = result[key].split(os.pathsep)
            kept = [value for value in entries if not _bundle_path(value, root)]
            if kept:
                result[key] = os.pathsep.join(kept)
            else:
                del result[key]
    return result


def reset_helper_library_search() -> None:
    """Reset only a helper's inherited frozen DLL lookup, never the GUI's.

    Call after importing the helper's bundled native Python modules. Windows
    children inherit SetDllDirectory; PyInstaller documents this required reset:
    https://pyinstaller.org/en/stable/common-issues-and-pitfalls.html#launching-external-programs-from-the-frozen-application
    """
    if is_frozen() and sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        reset = kernel32.SetDllDirectoryW
        reset.argtypes = [wintypes.LPCWSTR]
        reset.restype = wintypes.BOOL
        if not reset(None):
            raise OSError("The background helper could not reset its library search.")


def _keep_children_in_job() -> None:
    """Make all later children die when Windows closes this helper process.

    The private, non-inheritable job handle intentionally lives until process
    exit. Closing it while this helper is running would also terminate us.
    Assigning ourselves before spawning prevents a child-assignment race.
    """
    if sys.platform != "win32":
        return

    class BasicLimits(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong),
                    ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD),
                    ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t),
                    ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t),
                    ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class IoCounters(ctypes.Structure):
        _fields_ = [(name, ctypes.c_ulonglong) for name in
                    ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                     "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", BasicLimits), ("IoInfo", IoCounters),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateJobObjectW
    create.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    create.restype = wintypes.HANDLE
    configure = kernel32.SetInformationJobObject
    configure.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    configure.restype = wintypes.BOOL
    assign = kernel32.AssignProcessToJobObject
    assign.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    assign.restype = wintypes.BOOL
    current = kernel32.GetCurrentProcess
    current.argtypes = []
    current.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL

    job = create(None, None)
    if not job:
        raise OSError("The background helper could not create child-process cleanup.")
    limits = ExtendedLimits()
    limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not configure(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)) or not assign(job, current()):
        close(job)
        raise OSError("The background helper could not configure child-process cleanup.")
    _JOB_HANDLES.append(job)


def run_external(command: Sequence[str]) -> int:
    """Run direct argv with inherited IPC handles from the console helper."""
    if not command or any(not isinstance(value, str) for value in command):
        raise ValueError("A local program command is required.")
    if sys.stdin is None or sys.stdout is None or sys.stderr is None:
        raise RuntimeError("The background helper requires its IPC streams.")
    reset_helper_library_search()
    _keep_children_in_job()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    with subprocess.Popen(list(command), stdin=sys.stdin.buffer, stdout=sys.stdout.buffer,
                          stderr=sys.stderr.buffer, env=external_environment(),
                          shell=False, creationflags=flags) as process:
        return process.wait()
