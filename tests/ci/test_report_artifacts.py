"""Retain synthetic browser evidence at every depth in the candidate report artifact."""

from pathlib import Path


def test_report_artifact_preserves_root_and_nested_browser_captures(tmp_path):
    """Configured report globs include ordinary and nested synthetic PNG captures."""
    workflow = (Path(__file__).resolve().parents[2] / ".github/workflows/checks.yaml").read_text(
        encoding="utf-8"
    )
    report_step = workflow.split("- name: Preserve test reports\n", 1)[1]
    patterns = report_step.split("          path: |\n", 1)[1].split(
        "          if-no-files-found:", 1
    )[0]
    captures = {
        "results/ci/thermal-controls-desktop.png",
        "results/ci/ev-saved-mobile.png",
        "results/ci/notification-empty-desktop.png",
        "results/ci/planning-performance/detail-390.png",
        "results/ci/annual-setup/overview-1280.png",
    }
    for capture in captures:
        destination = tmp_path / capture
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"synthetic-capture")
    retained = {
        path.relative_to(tmp_path).as_posix()
        for pattern in patterns.splitlines()
        if pattern.strip()
        for path in tmp_path.glob(pattern.strip())
    }
    assert retained == captures
