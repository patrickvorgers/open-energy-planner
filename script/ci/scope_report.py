"""Render authoritative impact decisions without selecting or executing any suite."""

import json
from os import environ
from pathlib import Path


def write_selection_summary(report):
    """Format captured base-policy decisions as Markdown without recomputing their meaning."""
    lines = [
        "## CI impact selection",
        "",
        f"Profile: `{report['profile']}`",
        "",
        f"Base: `{report['policy_base'] or 'event base'}`. Tested: `{report['tested_commit'] or 'local'}`",
        "",
        "| Suite | Decision | Changed areas requiring it |",
        "| --- | --- | --- |",
    ]
    explanations = {
        ("ci", "native"): "Complete native execution includes the CI policy tests",
        ("documentation", "native"): "Complete native execution includes documentation regressions",
        ("documentation", "ci"): "The CI policy route also executes documentation regressions",
    }
    parent_explanations = {
        "unit-app": "The complete app suite contains this reviewed domain",
        "databases": "The legacy functional database suite contains both correctness and transfer contracts",
        "ha-controls": "The complete controls suite contains real remote solver handoff",
    }
    for row in report["suites"]:
        suite = row["suite"]
        parent = row["included_in"]
        detail = ", ".join(
            "`" + reason.replace("|", "&#124;").replace("`", "&#96;").replace("\n", " ") + "`"
            for reason in row["reasons"]
        )
        if row["required"]:
            decision = "Selected"
            detail = detail or "Full policy: non-PR event or unavailable comparison evidence"
        else:
            decision = "Not selected"
            detail = (
                "Focused documentation checks only"
                if report["profile"] == "documentation"
                else "No changed area requires this suite"
            )
        if parent:
            decision = "Included in " + parent
            detail = explanations.get((suite, parent), parent_explanations.get(parent, detail))
        lines.append(f"| {suite} | {decision} | {detail} |")
    lines.extend(
        [
            "",
            "Included rows execute inside their named parent suite. Complete native checks also include documentation regressions.",
        ]
    )
    summary = "\n".join(lines) + "\n"
    print(summary)
    destination = environ.get("GITHUB_STEP_SUMMARY")
    if destination:
        with Path(destination).open("a", encoding="utf-8") as output:
            output.write(summary)


def main():
    """Render the existing selection artifact, leaving old standalone-policy summaries intact."""
    path = Path("results/ci/selection.json")
    if path.exists():
        report = json.loads(path.read_text(encoding="utf-8"))
        # Older base policies already print their own table and have no rendering payload.
        if "summary" in report:
            write_selection_summary(report["summary"])


if __name__ == "__main__":
    main()
