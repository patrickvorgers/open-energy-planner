"""Native-inclusive resident counters for Windows/Linux. Commit counters are Windows-only."""

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path


class ProcessMemoryCounters(ctypes.Structure):
    """Match the Windows process-memory ABI used for native-inclusive byte counters."""

    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
        ("PrivateUsage", ctypes.c_size_t),
    ]


def process_memory():
    """Return resident/peak byte counters on Linux or Windows, with Windows-only commit values.

    Linux reads /proc/self/status and reports commit counters as None. Unsupported
    platforms or unexpected counter formats fail rather than inventing measurements.
    """
    if sys.platform == "linux":
        values = {}
        for line in Path("/proc/self/status").read_text().splitlines():
            parts = line.split()
            if parts and parts[0] in {"VmRSS:", "VmHWM:"}:
                if len(parts) != 3 or parts[2] != "kB":
                    raise RuntimeError("Unexpected Linux process-memory units")
                values[parts[0]] = int(parts[1]) * 1024
        return {
            "working_set_bytes": values["VmRSS:"],
            "peak_working_set_bytes": values["VmHWM:"],
            "private_commit_bytes": None,
            "peak_private_commit_bytes": None,
        }
    if sys.platform != "win32":
        raise RuntimeError("This memory collector supports Windows and Linux only")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.GetCurrentProcess.argtypes = []
    query = kernel.K32GetProcessMemoryInfo
    query.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD]
    query.restype = wintypes.BOOL
    counters = ProcessMemoryCounters()
    counters.cb = ctypes.sizeof(counters)
    if not query(kernel.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError(ctypes.get_last_error())
    return {
        "working_set_bytes": counters.WorkingSetSize,
        "peak_working_set_bytes": counters.PeakWorkingSetSize,
        "private_commit_bytes": counters.PrivateUsage,
        "peak_private_commit_bytes": counters.PeakPagefileUsage,
    }
