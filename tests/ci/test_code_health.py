"""Exercise numerical function budgets and the changed source inventory."""

import pytest

from script.ci import check_code_health
from script.ci.check_code_health import code_health_findings


def python_function(name, branches):
    """Build one Python function with distinct branch sites."""
    return f"def {name}(value):\n" + "".join(
        f"    if value == {index}:\n        return {index}\n" for index in range(branches)
    )


def javascript_function(name, branches):
    """Build one JavaScript function with distinct branch sites."""
    body = "".join(f"    if (value === {index}) return {index};\n" for index in range(branches))
    return f"function {name}(value) {{\n{body}}}\n"


@pytest.mark.parametrize(
    ("path", "function_source"),
    [
        ("src/example.py", python_function),
        ("src/example.js", javascript_function),
        ("src/example.mjs", javascript_function),
        ("src/example.cjs", javascript_function),
    ],
)
def test_new_over_budget_function_fails_in_both_languages(path, function_source):
    """Accept the boundary and reject the next new independent path."""
    assert list(code_health_findings(path, function_source("plan", 19))) == []
    assert list(code_health_findings(path, function_source("plan", 20))) == [
        f"{path}:1: plan: cyclomatic complexity 21 (limit 20)"
    ]


def test_growing_existing_function_fails_and_decreasing_or_moving_it_passes():
    """Distinguish growth from unchanged, shifted and simplified source."""
    path = "src/example.py"
    former = python_function("observe", 22)
    current = python_function("observe", 23)
    assert list(code_health_findings(path, current, path, former)) == [
        f"{path}:1: observe: cyclomatic complexity 23 to 24 (limit 20)"
    ]
    assert list(code_health_findings(path, "\n" + former, path, former)) == []
    assert list(code_health_findings(path, former, path, current)) == []


def test_larger_body_fails_independently_of_complexity():
    """Count authored code lines and ignore comments and empty lines."""
    path = "script/example.py"
    former = "def calculate():\n" + "    total = 1\n" * 79
    current = former + "    # Keep the accumulator.\n\n    return total\n"
    assert list(code_health_findings(path, current, path, former)) == [
        f"{path}:1: calculate: lines of code 80 to 81 (limit 80)"
    ]


def test_an_unchanged_occurrence_cannot_hide_a_new_function_with_the_same_name():
    """Count equal occurrences and keep their measurements separate."""
    path = "src/example.js"
    former = javascript_function("calculate", 21)
    duplicate = former + "\n" + former
    assert list(code_health_findings(path, duplicate, path, former)) == [
        f"{path}:25: calculate: cyclomatic complexity 22 (limit 20)"
    ]


def test_changed_sources_include_untracked_changes_and_follow_tracked_renames(
    tmp_path, monkeypatch
):
    """Handle staged moves, deleted files and a new source file before staging."""

    def git(root, command, *arguments):
        """Return NUL-separated inventories as emitted by Git."""
        assert root == tmp_path
        if command == "diff":
            assert arguments[-2:] == ("base", "--")
            return (
                "R100\0src/original.py\0src/renamed.py\0"
                "D\0src/removed.py\0"
                "R100\0tests/example.py\0script/example.py\0"
                "M\0src/open_energy_planner/web/public/chart.umd.min.js\0"
                "M\0src/existing.py\0"
            )
        assert command == "ls-files"
        return "src/new device.js\0tests/new_example.py\0"

    monkeypatch.setattr(check_code_health, "git_text", git)
    assert dict(check_code_health.changed_sources(tmp_path, "base")) == {
        "src/renamed.py": "src/original.py",
        "script/example.py": None,
        "src/existing.py": "src/existing.py",
        "src/new device.js": None,
    }
