"""Missing selected execution evidence must fail both candidates and reused full runs."""

import json

import pytest

from script.ci import check_suite_completion as completion
from tests.ci.browser_evidence import write_browser_evidence

EXPECTED_SUITES = {
    "selected:ci,ha": {"ci", "ha-contracts", "ha", "provisioning"},
    "selected:ci,ha-solver": {"ci", "ha-solver"},
    "selected:ci,unit-app-demand": {"ci", "unit-app-demand"},
    "documentation": {"documentation"},
    "ci": {"ci"},
    "native": {"native"},
    "frontend": {"native", "browser"},
    "ha": {"native", "browser", "ha-contracts", "ha", "provisioning"},
    "storage": {"native", "browser", "ha-contracts", "ha", "provisioning", "databases"},
    "integration": {"native", "browser", "ha-contracts", "ha", "provisioning", "databases"},
    "full": {"native", "browser", "ha-contracts", "ha", "provisioning", "databases", "supervisor"},
}

EXPECTED_SUITES["full+performance-queries"] = EXPECTED_SUITES["full"] | {"performance-queries"}

TREE = "1" * 40


@pytest.mark.parametrize("profile", tuple(EXPECTED_SUITES))
def test_every_profile_requires_exactly_its_executed_suites(tmp_path, monkeypatch, profile):
    """Only successful execution of the complete selected set can publish its tree-bound proof."""
    report = tmp_path / "suite-completion.json"
    monkeypatch.setattr(completion, "REPORT", report)
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *arguments, **options: TREE
    )
    expected = EXPECTED_SUITES[profile]
    for suite, reports in completion.junit_reports(profile, tmp_path).items():
        case_count = 12 if suite == "performance-queries" else 1
        for junit in reports:
            junit.write_text(
                "<testsuite>"
                + "".join(
                    f'<testcase classname="independent" name="passes_{index}" />'
                    for index in range(case_count)
                )
                + "</testsuite>",
                encoding="utf-8",
            )
    assert completion.required_suites(profile) == expected
    if "browser" in expected:
        write_browser_evidence(tmp_path)
    completion.write_completion(profile, expected)
    assert set(json.loads(report.read_text())["completed"]) == expected
    completion.verify_completion(profile, TREE, report)
    for missing in expected:
        report.unlink(missing_ok=True)
        with pytest.raises(RuntimeError, match="mismatch"):
            completion.write_completion(profile, expected - {missing})
        assert not report.exists()


@pytest.mark.parametrize("case_count", [1, 11, 12, 13])
def test_performance_completion_requires_all_twelve_volume_cases(tmp_path, monkeypatch, case_count):
    """Partial volume collection cannot publish or reuse proof, while twelve or more cases qualify."""
    report = tmp_path / "suite-completion.json"
    monkeypatch.setattr(completion, "REPORT", report)
    monkeypatch.setattr(
        "script.ci.proof_git.subprocess.check_output", lambda *arguments, **options: TREE
    )
    (tmp_path / "ci.xml").write_text('<testsuite><testcase name="ci" /></testsuite>')
    (tmp_path / "performance-queries.xml").write_text(
        "<testsuite>"
        + "".join(f'<testcase name="volume_{index}" />' for index in range(case_count))
        + "</testsuite>"
    )
    profile = "ci+performance-queries"
    if case_count < 12:
        with pytest.raises(RuntimeError, match="Insufficient.*performance-queries"):
            completion.write_completion(profile, {"ci", "performance-queries"})
        assert not report.exists()
        report.write_text(
            json.dumps(
                {
                    "profile": profile,
                    "source_tree": TREE,
                    "completed": ["ci", "performance-queries"],
                    "executed_tests": {"ci": 1, "performance-queries": 12},
                }
            )
        )
        with pytest.raises(RuntimeError, match="Insufficient.*performance-queries"):
            completion.verify_completion(profile, TREE, report)
    else:
        completion.write_completion(profile, {"ci", "performance-queries"})
        assert json.loads(report.read_text())["executed_tests"] == {
            "ci": 1,
            "performance-queries": case_count,
        }
        completion.verify_completion(profile, TREE, report)


@pytest.mark.parametrize(
    "evidence", ["missing", "empty", "skipped", "partly_skipped", "failed", "malformed"]
)
def test_exit_zero_cannot_replace_actual_selected_test_execution(tmp_path, monkeypatch, evidence):
    """Selected commands with no passing cases or bad reports cannot publish completion, even with a successful ledger."""
    monkeypatch.setattr(completion, "REPORT", tmp_path / "suite-completion.json")
    for reports in completion.junit_reports("selected:ci,unit-supervisor").values():
        for report in reports:
            report.write_text(
                '<testsuite><testcase classname="fixture" name="passes" /></testsuite>',
                encoding="utf-8",
            )
    report = tmp_path / "unit-supervisor.xml"
    if evidence == "missing":
        report.unlink()
    else:
        report.write_text(
            {
                "empty": '<testsuite tests="99" />',
                "skipped": '<testsuite><testcase name="not_run"><skipped /></testcase></testsuite>',
                "partly_skipped": '<testsuite><testcase name="passes"/><testcase name="not_run"><skipped /></testcase></testsuite>',
                "failed": '<testsuite><testcase name="failed"><failure /></testcase></testsuite>',
                "malformed": "not xml",
            }[evidence],
            encoding="utf-8",
        )
    with pytest.raises(RuntimeError, match="evidence|executed"):
        completion.write_completion("selected:ci,unit-supervisor", {"ci", "unit-supervisor"})
    assert not completion.REPORT.exists()


@pytest.mark.parametrize(
    "corruption", ["missing", "duplicate", "old_tree", "focused", "invalid_json", "not_object"]
)
def test_incomplete_or_stale_completion_cannot_publish_full_tree(tmp_path, corruption):
    """A partial, replayed or differently scoped report never qualifies as full-candidate proof."""
    report = tmp_path / "suite-completion.json"
    for name in (
        "native.xml",
        "ui.xml",
        "ha-contracts.xml",
        "ha-0.xml",
        "ha-1.xml",
        "databases.xml",
        "supervisor-replay.xml",
    ):
        (tmp_path / name).write_text(
            '<testsuite><testcase name="executed" /></testsuite>', encoding="utf-8"
        )
    document = {
        "profile": "full",
        "source_tree": TREE,
        "completed": sorted(EXPECTED_SUITES["full"]),
        "executed_tests": {
            "native": 1,
            "browser": 1,
            "ha-contracts": 1,
            "ha": 2,
            "databases": 1,
            "supervisor": 1,
        },
    }
    report.write_text(json.dumps(document), encoding="utf-8")
    write_browser_evidence(tmp_path)
    completion.verify_completion("full", TREE, report)
    if corruption == "missing":
        document["completed"].remove("supervisor")
    elif corruption == "duplicate":
        document["completed"].append("ha")
    elif corruption == "old_tree":
        document["source_tree"] = "2" * 40
    elif corruption == "focused":
        document["profile"] = "storage"
    if corruption == "not_object":
        document = []
    report.write_text("broken" if corruption == "invalid_json" else json.dumps(document))
    with pytest.raises((RuntimeError, ValueError, TypeError)):
        completion.verify_completion("full", TREE, report)


def test_ha_completion_requires_each_configured_worker_report(monkeypatch, tmp_path):
    """Increasing HA parallelism cannot silently omit a new worker from completion evidence."""
    monkeypatch.setattr(completion, "HA_WORKERS", 3)
    reports = completion.junit_reports("selected:ci,ha", tmp_path)
    assert [report.name for report in reports["ha"]] == ["ha-0.xml", "ha-1.xml", "ha-2.xml"]
    for paths in reports.values():
        for report in paths:
            if report.name != "ha-2.xml":
                report.write_text('<testsuite><testcase name="passed" /></testsuite>')
    with pytest.raises(RuntimeError, match="Missing or unreadable execution evidence for ha$"):
        completion.execution_counts("selected:ci,ha", tmp_path)
