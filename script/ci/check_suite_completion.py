"""Require successful execution evidence for every suite selected by a candidate."""

import argparse
import json
from os import environ
from pathlib import Path
from xml.etree import ElementTree

from script.ci.ci_scope import selected_suites
from script.ci.ha_suites import HA_WORKERS
from script.ci.proof_git import checkout_tree

REPORT = Path("results/ci/suite-completion.json")
MINIMUM_PERFORMANCE_QUERY_CASES = 12


def required_suites(profile):
    """Include provisioning explicitly even though it shares HA's impact boundary."""
    if profile == "documentation":
        return {"documentation"}
    suites = set(selected_suites(profile))
    if "ha" in suites:
        suites.update({"ha-contracts", "provisioning"})
    return suites


def junit_reports(profile, directory=None):
    """Identify current-run pytest evidence, including the Supervisor environment replay."""
    directory = REPORT.parent if directory is None else directory
    names = {
        "browser": ("ui",),
        "ha": tuple(f"ha-{worker}" for worker in range(HA_WORKERS)),
        "supervisor": ("supervisor-replay",),
    }
    return {
        suite: tuple(directory / (name + ".xml") for name in names.get(suite, (suite,)))
        for suite in required_suites(profile)
        if suite != "provisioning"
    }


def execution_counts(profile, directory=None):
    """Reject missing, failed, skipped or insufficient execution instead of trusting exit zero alone."""
    counts = {}
    for suite, reports in junit_reports(profile, directory).items():
        cases = []
        try:
            for report in reports:
                cases.extend(ElementTree.parse(report).findall(".//testcase"))
        except (OSError, ElementTree.ParseError) as error:
            raise RuntimeError(f"Missing or unreadable execution evidence for {suite}") from error
        if any(
            case.find("failure") is not None or case.find("error") is not None for case in cases
        ):
            raise RuntimeError(f"Failed test evidence for {suite}")
        if any(case.find("skipped") is not None for case in cases):
            raise RuntimeError(f"Skipped test evidence for selected suite {suite}")
        executed = sum(case.find("skipped") is None for case in cases)
        if not executed:
            raise RuntimeError(f"No executed test cases for selected suite {suite}")
        if suite == "performance-queries" and executed < MINIMUM_PERFORMANCE_QUERY_CASES:
            raise RuntimeError("Insufficient executed test cases for performance-queries")
        counts[suite] = executed
    return counts


def write_completion(profile, completed):
    """Publish evidence only after every selected command succeeded in this invocation."""
    required = required_suites(profile)
    if completed != required:
        raise RuntimeError(
            f"Suite completion mismatch: required={sorted(required)}, completed={sorted(completed)}"
        )
    counts = execution_counts(profile)
    source_tree = checkout_tree()
    REPORT.write_text(
        json.dumps(
            {
                "profile": profile,
                "source_tree": source_tree,
                "completed": sorted(completed),
                "executed_tests": counts,
            }
        ),
        encoding="utf-8",
    )
    print(f"Selected suites completed: {', '.join(sorted(completed))}", flush=True)
    summary = environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a", encoding="utf-8") as output:
            output.write("\n## Executed suites\n\n" + ", ".join(sorted(completed)) + "\n")


def verify_completion(profile, source_tree, report=REPORT):
    """Reject stale, partial, differently scoped or malformed evidence before tree publication."""
    document = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError("Suite completion report must be an object")
    completed = document.get("completed")
    if (
        document.get("profile") != profile
        or document.get("source_tree") != source_tree
        or not isinstance(completed, list)
        or not all(isinstance(suite, str) for suite in completed)
        or len(completed) != len(set(completed))
        or set(completed) != required_suites(profile)
        or not isinstance(document.get("executed_tests"), dict)
        or not all(
            type(count) is int and count > 0
            for count in document.get("executed_tests", {}).values()
        )
    ):
        raise RuntimeError(
            "Selected suite completion is not proven for this source tree and profile"
        )
    if document["executed_tests"] != execution_counts(profile, report.parent):
        raise RuntimeError("Recorded execution counts differ from current test evidence")


def main():
    """Check a completed candidate's report using the caller's immutable scope and source tree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("--tree", required=True)
    options = parser.parse_args()
    verify_completion(options.profile, options.tree)


if __name__ == "__main__":
    main()
