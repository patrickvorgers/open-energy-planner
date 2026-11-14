"""Prove experimental machine splitting preserves real pytest worker evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from script.ci.browser_benchmark import summarize


def test_benchmark_executes_each_identity_once_and_rejects_mismatched_image(tmp_path):
    """All variants execute the same eight real cases, and image drift invalidates comparison."""
    repository = Path(__file__).resolve().parents[2]
    test_file = tmp_path / "test_sample.py"
    test_file.write_text(
        'import pytest\n@pytest.mark.parametrize("number", range(8))\n'
        'def test_sample(number):\n    """Keep each independently named case passing."""\n'
        "    assert number >= 0\n",
        encoding="utf-8",
    )
    for variant, machines in (("baseline", 1), ("memory", 1), ("split", 2)):
        for machine in range(machines):
            location = tmp_path / f"{variant}-{machine}"
            location.mkdir()
            (location / "benchmark.json").write_text(
                json.dumps(
                    {
                        "variant": variant,
                        "machine": machine,
                        "image_id": "sha256:same-image",
                        "seconds": 10 / machines,
                    }
                ),
                encoding="utf-8",
            )
            for worker in range(2):
                working = tmp_path / f"work-{variant}-{machine}-{worker}"
                working.mkdir()
                environment = {
                    **os.environ,
                    "PYTHONPATH": str(repository),
                    "OEP_BENCH_MACHINES": str(machines),
                    "OEP_BENCH_MACHINE": str(machine),
                }
                command = [
                    sys.executable,
                    "-m",
                    "pytest",
                    "-q",
                    str(test_file),
                    "-p",
                    "script.ci.browser_benchmark",
                    "-p",
                    "script.ci.browser_shard",
                    "--browser-shard",
                    str(worker),
                    "-p",
                    "no:cacheprovider",
                ]
                for arguments in (["--collect-only"], ["--junitxml=results/ci/ui.xml"]):
                    result = subprocess.run(
                        [*command, *arguments],
                        cwd=working,
                        env=environment,
                        check=False,
                        capture_output=True,
                        text=True,
                    )
                    assert result.returncode == 0, result.stdout + result.stderr
                (working / "results/ci").rename(location / f"browser-{worker}")
    summarize(tmp_path)
    comparison = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))
    assert comparison["variants"] == {
        "baseline": {"seconds": 10, "machine_seconds": 10, "cases": 8},
        "memory": {"seconds": 10, "machine_seconds": 10, "cases": 8},
        "split": {"seconds": 5, "machine_seconds": 10, "cases": 8},
    }
    metadata = tmp_path / "split-1/benchmark.json"
    record = json.loads(metadata.read_text(encoding="utf-8"))
    record["image_id"] = "sha256:different-image"
    metadata.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="images differ"):
        summarize(tmp_path)
