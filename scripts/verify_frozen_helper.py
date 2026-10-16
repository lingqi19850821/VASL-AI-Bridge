"""Verify frozen helper descendant cleanup using only synthetic Windows children.

The report contains fixed check names and booleans, without PIDs, command lines,
installation paths, captured output, or raw exception messages.
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading


ROOT = Path(__file__).resolve().parents[1]
READY_PATTERN = re.compile(rb"ASL_AI_HELPER_CHILDREN ([1-9][0-9]*) ([1-9][0-9]*)\r?\n")
DUMMY_COMMAND = r"""
$childInfo = New-Object System.Diagnostics.ProcessStartInfo
$childInfo.FileName = Join-Path $PSHOME 'powershell.exe'
$childInfo.Arguments = '-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 60"'
$childInfo.UseShellExecute = $false
$childInfo.CreateNoWindow = $true
$child = [System.Diagnostics.Process]::Start($childInfo)
[Console]::Out.WriteLine('ASL_AI_HELPER_CHILDREN ' + $PID + ' ' + $child.Id)
[Console]::Out.Flush()
Start-Sleep -Seconds 60
"""


class VerificationFailure(RuntimeError):
    """Fixed failure code suitable for the path-free verification report."""


class WindowsProcesses:
    """Hold native process handles so cleanup cannot target a reused PID."""

    def __init__(self):
        self.kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.open_process = self.kernel32.OpenProcess
        self.open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        self.open_process.restype = wintypes.HANDLE
        self.wait = self.kernel32.WaitForSingleObject
        self.wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        self.wait.restype = wintypes.DWORD
        self.terminate = self.kernel32.TerminateProcess
        self.terminate.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.terminate.restype = wintypes.BOOL
        self.close = self.kernel32.CloseHandle
        self.close.argtypes = [wintypes.HANDLE]
        self.close.restype = wintypes.BOOL

    def acquire(self, pid: int):
        # SYNCHRONIZE | PROCESS_TERMINATE, restricted to the reported synthetic
        # children after verifying that they are descendants of our helper.
        handle = self.open_process(0x00100001, False, pid)
        if not handle:
            raise VerificationFailure("synthetic_process_handle_unavailable")
        return handle

    def parents(self) -> dict[int, int]:
        class ProcessEntry(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]

        create = self.kernel32.CreateToolhelp32Snapshot
        create.argtypes = [wintypes.DWORD, wintypes.DWORD]
        create.restype = wintypes.HANDLE
        first = self.kernel32.Process32FirstW
        first.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        first.restype = wintypes.BOOL
        next_entry = self.kernel32.Process32NextW
        next_entry.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
        next_entry.restype = wintypes.BOOL
        snapshot = create(2, 0)  # TH32CS_SNAPPROCESS
        if snapshot == ctypes.c_void_p(-1).value:
            raise VerificationFailure("process_ownership_snapshot_unavailable")
        result = {}
        try:
            entry = ProcessEntry()
            entry.dwSize = ctypes.sizeof(entry)
            available = first(snapshot, ctypes.byref(entry))
            if not available:
                raise VerificationFailure("process_ownership_snapshot_empty")
            while available:
                result[entry.th32ProcessID] = entry.th32ParentProcessID
                available = next_entry(snapshot, ctypes.byref(entry))
        finally:
            self.close(snapshot)
        return result


def verify(payload: Path) -> dict:
    checks = {"synthetic_stdio_ready": False, "synthetic_descendants_owned": False,
              "helper_terminated": False, "external_child_terminated": False,
              "grandchild_terminated": False}
    if sys.platform != "win32":
        raise VerificationFailure("windows_required")
    helper = payload / "ASL_AI-Speech.exe"
    if not helper.is_file():
        raise VerificationFailure("frozen_helper_missing")
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise VerificationFailure("windows_powershell_missing")

    windows = WindowsProcesses()
    process = subprocess.Popen([str(helper), "--external", str(powershell), "-NoProfile",
                                "-NonInteractive", "-Command", DUMMY_COMMAND],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    handles = []
    owned = False
    try:
        # Bound readiness without blocking indefinitely on a broken pipe.
        messages = queue.Queue()
        reader = threading.Thread(target=lambda: messages.put(process.stdout.readline(256)), daemon=True)
        reader.start()
        try:
            ready = messages.get(timeout=15)
        except queue.Empty:
            raise VerificationFailure("synthetic_readiness_timeout") from None
        match = READY_PATTERN.fullmatch(ready)
        if match is None:
            raise VerificationFailure("synthetic_readiness_invalid")
        parent_pid, child_pid = (int(value) for value in match.groups())
        if process.poll() is not None or len({process.pid, parent_pid, child_pid}) != 3:
            raise VerificationFailure("synthetic_processes_not_distinct_and_running")
        parents = windows.parents()
        if parents.get(parent_pid) != process.pid or parents.get(child_pid) != parent_pid:
            raise VerificationFailure("synthetic_descendant_ownership_failed")
        handles.append(windows.acquire(parent_pid))
        handles.append(windows.acquire(child_pid))
        # Recheck membership while the helper is still alive. Cleanup uses these
        # handles, never a PID passed to a termination command or shell tool.
        parents = windows.parents()
        if process.poll() is not None or parents.get(parent_pid) != process.pid or parents.get(child_pid) != parent_pid:
            raise VerificationFailure("synthetic_descendant_ownership_changed")
        owned = True
        if any(windows.wait(handle, 0) != 258 for handle in handles):  # WAIT_TIMEOUT means alive.
            raise VerificationFailure("synthetic_child_exited_before_verification")
        checks["synthetic_stdio_ready"] = True
        checks["synthetic_descendants_owned"] = True
        process.terminate()
        process.wait(timeout=10)
        checks["helper_terminated"] = process.poll() is not None
        checks["external_child_terminated"] = windows.wait(handles[0], 5000) == 0
        checks["grandchild_terminated"] = windows.wait(handles[1], 5000) == 0
        if not all(checks.values()):
            raise VerificationFailure("synthetic_descendant_survived_helper_termination")
        return {"schema_version": 1, "status": "passed", "checks": checks}
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
        # Failed job cleanup can leave only our verified synthetic children
        # alive. Terminate their stable handles, preserving unrelated processes.
        for handle in reversed(handles):
            if owned and windows.wait(handle, 0) == 258:
                windows.terminate(handle, 1)
                windows.wait(handle, 5000)
            windows.close(handle)
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload-dir", type=Path, default=ROOT / "dist" / "VASL AI Bridge")
    parser.add_argument("--report", type=Path, default=ROOT / "build" / "windows" / "helper-verification.json")
    arguments = parser.parse_args()
    try:
        report = verify(arguments.payload_dir.resolve())
        result = 0
    except VerificationFailure as error:
        report = {"schema_version": 1, "status": "failed", "failure_code": str(error)}
        result = 1
    except (OSError, subprocess.SubprocessError, ValueError):
        report = {"schema_version": 1, "status": "failed", "failure_code": "verification_runtime_failure"}
        result = 1
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    arguments.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("Frozen helper verification passed." if result == 0 else "Frozen helper verification failed.")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
