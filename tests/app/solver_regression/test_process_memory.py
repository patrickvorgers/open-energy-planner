"""Check that the native-inclusive memory collector returns meaningful process counters."""

import mmap
import sys
from contextlib import closing, nullcontext

import pytest

from script.app.process_memory import process_memory


def test_process_memory_high_water_counters_cover_resident_and_private_usage():
    """Read platform memory counters. Require plausible resident/high-water bytes and platform-appropriate commit values."""
    if sys.platform not in {"win32", "linux"}:
        with pytest.raises(RuntimeError, match="Windows and Linux only"):
            process_memory()
        return
    baseline = process_memory()
    # Linux heap allocations can reuse resident pages left by earlier solver tests.
    allocation_context = (
        closing(mmap.mmap(-1, 16 * 1024 * 1024))
        if sys.platform == "linux"
        else nullcontext(bytearray(16 * 1024 * 1024))
    )
    with allocation_context as allocation:
        for offset in range(0, len(allocation), 4096):
            allocation[offset] = 1
        observed = process_memory()
        assert observed["peak_working_set_bytes"] >= observed["working_set_bytes"] > 0
        if sys.platform == "win32":
            assert observed["peak_private_commit_bytes"] >= observed["private_commit_bytes"] > 0
            assert observed["private_commit_bytes"] > baseline["private_commit_bytes"] + 8 * 1024**2
        else:
            assert observed["private_commit_bytes"] is None
            assert observed["peak_private_commit_bytes"] is None
            assert observed["working_set_bytes"] > baseline["working_set_bytes"] + 8 * 1024**2
        assert observed["peak_working_set_bytes"] >= baseline["peak_working_set_bytes"]
