"""Exercise the actual pinned Ruff configuration on independent documentation fixtures."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def lint(source, filename="src/documentation_probe.py"):
    """Run the actual pinned Ruff configuration on an in-memory file and return diagnostics/exit status."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "ruff",
            "check",
            "--no-cache",
            "--output-format=json",
            "--stdin-filename",
            filename,
            "-",
        ],
        input=source,
        text=True,
        capture_output=True,
        cwd=ROOT,
        check=False,
    )
    assert result.returncode in (0, 1), result.stderr
    return result, {item["code"] for item in json.loads(result.stdout)}


@pytest.mark.parametrize(
    "filename,source,expected",
    [
        ("src/probe.py", "VALUE = 1\n", "D100"),
        ("src/probe/__init__.py", "VALUE = 1\n", "D104"),
        ("src/probe.py", '"""Probe."""\nclass Meter:\n    pass\n', "D101"),
        (
            "src/probe.py",
            (
                '"""Probe."""\nclass Meter:\n    """Read measurements."""\n'
                "    def read(self):\n        return 1\n"
            ),
            "D102",
        ),
        ("src/probe.py", '"""Probe."""\nasync def read():\n    return 1\n', "D103"),
        (
            "src/probe.py",
            (
                '"""Probe."""\nclass Meter:\n    """Read measurements."""\n'
                '    def __str__(self):\n        return "meter"\n'
            ),
            "D105",
        ),
        (
            "src/probe.py",
            (
                '"""Probe."""\nclass Meter:\n    """Read measurements."""\n'
                "    class Reading:\n        pass\n"
            ),
            "D106",
        ),
        (
            "src/probe.py",
            (
                '"""Probe."""\nclass Meter:\n    """Read measurements."""\n'
                "    def __init__(self):\n        self.value = 1\n"
            ),
            "D107",
        ),
        ("src/probe.py", '"""Probe."""\ndef read():\n    """ """\n    return 1\n', "D419"),
    ],
)
def test_missing_or_empty_public_docstring_fails_real_quality_configuration(
    filename, source, expected
):
    """Omit or empty each supported public docstring kind. Require its specific diagnostic and a failing lint exit."""
    result, codes = lint(source, filename)
    assert result.returncode == 1
    assert expected in codes


def test_meaningful_summary_passes_without_a_percentage_or_extended_section_requirement():
    """Provide a useful concise summary. Accept it without imposing a coverage percentage or redundant sections."""
    result, codes = lint(
        '"""Normalize meter readings."""\n'
        'def read():\n    """Return the normalized reading in watts."""\n    return 1\n'
    )
    assert result.returncode == 0, result.stdout
    assert not codes


@pytest.mark.parametrize("filename", ["tests/test_probe.py", "tests/frontend/test_probe.py"])
def test_test_paths_require_docstrings_and_keep_other_lint_checks_enabled(filename):
    """Omit/empty test docstrings. Require documentation failures while retaining independent undefined-name checks."""
    result, codes = lint("def test_reading():\n    assert missing_reading\n", filename)
    assert result.returncode == 1
    assert "F821" in codes
    assert {"D100", "D103"} <= codes
    empty, empty_codes = lint(
        '"""Test readings."""\ndef test_reading():\n    """ """\n    assert 1 == 1\n',
        filename,
    )
    assert empty.returncode == 1
    assert "D419" in empty_codes
