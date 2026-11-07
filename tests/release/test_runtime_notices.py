"""Preserve wheel and native dependency licences in the standalone package notice."""

import importlib.metadata
import runpy
import sys
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("bundled_runtime", [False, True])
def test_wheel_notices_include_bundled_gcc_terms(tmp_path, monkeypatch, bundled_runtime):
    """Only wheels carrying libgomp add its original notice, GPL and runtime exception."""
    licence = tmp_path / "LICENSE"
    licence.write_text("Original wheel licence: \N{COPYRIGHT SIGN} Example.\n", encoding="utf-8")
    files = [PurePosixPath("xgboost_cpu.dist-info/licenses/LICENSE")]
    if bundled_runtime:
        files.append(PurePosixPath("xgboost_cpu.libs/libgomp-example.so.1.0.0"))
    package = SimpleNamespace(
        metadata={"Name": "xgboost-cpu", "License-Expression": "Apache-2.0"},
        version="3.4.1",
        files=files,
        locate_file=lambda _file: licence,
    )
    monkeypatch.setattr(importlib.metadata, "distributions", lambda: [package])
    output = tmp_path / "notices.md"
    monkeypatch.setattr(sys, "argv", ["runtime_notices", str(output)])
    runpy.run_module("script.release.runtime_notices", run_name="__main__")
    text = output.read_text(encoding="utf-8")
    assert "Metadata: Apache-2.0" in text
    assert "Original wheel licence: \N{COPYRIGHT SIGN} Example.\n" in text
    assert ("Bundled GCC libgomp runtime" in text) is bundled_runtime
    if bundled_runtime:
        assert "Files: xgboost_cpu.libs/libgomp*.so*" in text
        assert "GPL-3.0-or-later WITH GCC-exception-3.1" in text
        for filename in ("libgomp-NOTICE.txt", "COPYING3", "COPYING.RUNTIME"):
            original = Path("script/release/licenses", filename).read_text(encoding="utf-8")
            assert original in text
