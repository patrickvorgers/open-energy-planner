"""Check complexity growth in authored Python and JavaScript against a Git base."""

import argparse
import subprocess
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import lizard

SOURCE_ROOTS = {"src", "custom_components", "script", "test_environment", "ha_app", "docs/site"}
VENDORED_CHART = "src/open_energy_planner/web/public/chart.umd.min.js"
MAX_COMPLEXITY = 20
MAX_CODE_LINES = 80


@dataclass(frozen=True)
class Function:
    """Hold measurements and source text independently of the function's placement."""

    source: tuple[str, ...]
    complexity: int
    code_lines: int
    line: int = field(compare=False)


def authored_source(name):
    """Recognize the application, integration and first-party tooling source roots."""
    path = Path(name)
    return (
        path.suffix in {".py", ".js", ".mjs", ".cjs"}
        and name != VENDORED_CHART
        and any(name.startswith(root + "/") for root in SOURCE_ROOTS)
    )


def git_text(root, *arguments):
    """Read Git objects and path inventories without invoking a shell."""
    return subprocess.check_output(["git", *arguments], cwd=root, timeout=60).decode("utf-8")


def changed_sources(root, base):
    """Yield current paths and their prior paths, including local untracked source."""
    tracked = iter(
        git_text(root, "diff", "--no-ext-diff", "--name-status", "-z", "--find-renames", base, "--")
        .rstrip("\0")
        .split("\0")
    )
    for status in filter(None, tracked):
        old_path = next(tracked)
        path = next(tracked) if status.startswith(("R", "C")) else old_path
        if status == "D" or not authored_source(path):
            continue
        if status.startswith(("A", "C")) or not authored_source(old_path):
            old_path = None
        yield path, old_path
    for path in git_text(root, "ls-files", "--others", "--exclude-standard", "-z").split("\0"):
        if authored_source(path):
            yield path, None


def functions(path, source):
    """Group measured functions by analyzer name and retain their own source ranges."""
    lines = source.splitlines()
    by_name = defaultdict(list)
    for measured in lizard.analyze_file.analyze_source_code(path, source).function_list:
        by_name[measured.name].append(
            Function(
                tuple(lines[measured.start_line - 1 : measured.end_line]),
                measured.cyclomatic_complexity,
                measured.nloc,
                measured.start_line,
            )
        )
    return by_name


def unmatched(functions_in_revision, other_revision):
    """Remove identical functions one occurrence at a time, regardless of line number."""
    remaining = Counter(other_revision)
    for function in functions_in_revision:
        if remaining[function]:
            remaining[function] -= 1
        else:
            yield function


def code_health_findings(path, current_source, old_path=None, old_source=""):
    """Flag new or growing over-budget measurements without a checked-in baseline."""
    previous = functions(old_path, old_source) if old_path else {}
    for name, current in functions(path, current_source).items():
        prior = previous.get(name, ())
        modified = list(unmatched(current, prior))
        replaced = list(unmatched(prior, current))
        former = replaced[0] if len(modified) == len(replaced) == 1 else None
        for function in modified:
            for label, value, before, limit in (
                (
                    "cyclomatic complexity",
                    function.complexity,
                    former.complexity if former else 0,
                    MAX_COMPLEXITY,
                ),
                (
                    "lines of code",
                    function.code_lines,
                    former.code_lines if former else 0,
                    MAX_CODE_LINES,
                ),
            ):
                if value > max(limit, before):
                    measurement = f"{before} to {value}" if former else str(value)
                    yield f"{path}:{function.line}: {name}: {label} {measurement} (limit {limit})"


def main():
    """Report new numerical budget violations from the local or CI worktree."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--base", default="origin/main", help="Git commit or ref to compare against"
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        base = git_text(
            root, "rev-parse", "--verify", "--end-of-options", f"{args.base}^{{commit}}"
        )
        base = base.strip()
        findings = []
        for path, old_path in changed_sources(root, base):
            source_path = root / path
            if source_path.is_symlink():
                continue
            prior = git_text(root, "show", f"{base}:{old_path}") if old_path else ""
            findings.extend(
                code_health_findings(path, source_path.read_text(encoding="utf-8"), old_path, prior)
            )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(2, f"Could not check code health: {error}\n")
    if findings:
        print("\n".join(findings))
        print(
            "Keep each function responsible for one operation. See docs/development/CI-STAGES.md."
        )
        return 1
    print("Code health budgets satisfied.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
