"""Prove experimental machine splitting preserves real pytest worker evidence."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from script.ci.browser_benchmark import run, summarize


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
    for variant, machines in (
        ("baseline", 1),
        ("memory", 1),
        ("split", 2),
        ("cpu", 1),
        ("wide", 1),
        ("split-wide", 2),
    ):
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
    summarize(tmp_path, comparison="cpu")
    cpu_result = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))
    assert cpu_result["variants"] == {
        "memory": {"seconds": 10, "machine_seconds": 10, "cases": 8},
        "cpu": {"seconds": 10, "machine_seconds": 10, "cases": 8},
    }
    summarize(tmp_path, comparison="wide")
    wide = json.loads((tmp_path / "comparison.json").read_text(encoding="utf-8"))
    assert wide["variants"] == {
        "wide": {"seconds": 10, "machine_seconds": 10, "cases": 8},
        "split-wide": {"seconds": 5, "machine_seconds": 10, "cases": 8},
    }
    metadata = tmp_path / "split-1/benchmark.json"
    record = json.loads(metadata.read_text(encoding="utf-8"))
    record["image_id"] = "sha256:different-image"
    metadata.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="images differ"):
        summarize(tmp_path)


def test_failed_cpu_measurement_retains_timing_and_limits(tmp_path, monkeypatch):
    """A failed container keeps an unsuccessful timing receipt instead of losing the measurement."""
    original_read = Path.read_text

    def read_host_memory(path, *arguments, **keywords):
        """Keep the Linux host probe independent of the local test platform."""
        if str(path).replace("\\", "/") == "/proc/meminfo":
            return "MemTotal: 16000000 kB\n"
        return original_read(path, *arguments, **keywords)

    def fail_container(command, **keywords):
        """Expose a real subprocess failure before any complete execution proof exists."""
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(Path, "read_text", read_host_memory)
    monkeypatch.setattr(subprocess, "check_output", lambda command: b'[{"Id":"same-image"}]')
    monkeypatch.setattr(subprocess, "run", fail_container)
    with pytest.raises(subprocess.CalledProcessError):
        run("test-image", "cpu", 0, tmp_path)
    receipt = json.loads((tmp_path / "benchmark.json").read_text(encoding="utf-8"))
    assert receipt["successful"] is False
    assert receipt["seconds"] >= 0
    assert receipt["cpus_per_worker"] == "2"
    assert receipt["memory_per_worker"] == "2g"
