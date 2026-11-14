"""Measure frozen CI lanes with resource overlays while preserving their original orchestration."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from time import monotonic
from xml.etree import ElementTree

SOURCE = "1d08355e1ae3e9a12fa22826a878033064e496de"
SOURCE_TREE = "62b80e01d25f5aeb99a4a7174eac33742bf51439"
PLAN_DIGEST = "d950d73529c79353f246b8febe7b73f90488dc73e7ce8ff33319f0efc5dbdc80"
EXPECTED_COUNTS = {
    "native": {
        "ci.xml": 1755,
        "unit-app.xml": 2291,
        "unit-ha-contract.xml": 202,
        "unit-provisioning.xml": 66,
        "ha-controls.xml": 62,
        "ha-history.xml": 117,
        "ha-thermal.xml": 34,
    },
    "databases": {"databases.xml": 2177, "performance-queries.xml": 32},
    "supervisor": {"supervisor-replay.xml": 3},
}


def digest(path):
    """Hash an image archive without copying its bytes into memory."""
    with Path(path).open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def prepare(workload, prepared, lane, project):
    """Verify original image bytes and add the same measurement-only plugin to both variants."""
    if (
        subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workload, text=True).strip()
        != SOURCE
    ):
        raise RuntimeError("Unexpected frozen workload commit")
    plan_file = prepared / "parallel-plan.json"
    if digest(plan_file) != PLAN_DIGEST:
        raise RuntimeError("Original image manifest differs")
    plan = json.loads(plan_file.read_text(encoding="utf-8"))
    if plan["source_tree"] != SOURCE_TREE:
        raise RuntimeError("Unexpected frozen source tree")
    inputs = ("native", "ha-tests", "app") if lane == "native" else ("native",)
    evidence = {}
    for name in inputs:
        expected = plan["images"][name]
        archive = prepared / f"{name}.tar"
        if digest(archive) != expected["sha256"]:
            raise RuntimeError("Original image archive differs")
        subprocess.run(["docker", "load", "-i", str(archive)], check=True)
        actual = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", expected["tags"][0]],
            text=True,
        ).strip()
        if actual != expected["image_id"]:
            raise RuntimeError("Original image identity differs")
        archive.unlink()
        target = f"{project}-{name}"
        if name == "app":
            subprocess.run(["docker", "tag", actual, target], check=True)
        else:
            context = prepared / (name + "-probe")
            context.mkdir()
            shutil.copyfile(
                Path(__file__).with_name("resource_probe.py"), context / "oep_resource_probe.py"
            )
            (context / "Dockerfile").write_text(
                f"FROM {expected['tags'][0]}\n"
                "COPY oep_resource_probe.py /opt/oep_resource_probe.py\n"
                'RUN python -c "import pathlib,sysconfig; '
                "pathlib.Path(sysconfig.get_path('purelib'), 'oep_resource_probe.pth')"
                ".write_text('/opt'+chr(10))\"\n"
                "ENV PYTEST_PLUGINS=oep_resource_probe\n",
                encoding="utf-8",
            )
            subprocess.run(["docker", "build", "-t", target, str(context)], check=True)
            smoke = context / "smoke"
            smoke.mkdir()
            fixture = context / "test_probe.py"
            fixture.write_text(
                'def test_measured():\n    """Expose one real passing execution to the probe."""\n'
                "    assert 3 * 7 == 21\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--volume",
                    f"{smoke}:/workspace/results/ci",
                    "--volume",
                    f"{fixture}:/workspace/test_oep_probe_smoke.py:ro",
                    target,
                    "-m",
                    "pytest",
                    "-q",
                    "test_oep_probe_smoke.py",
                    "--junitxml=results/ci/probe.xml",
                    "-p",
                    "no:cacheprovider",
                ],
                check=True,
            )
            if validate_probe(smoke) != {"probe.xml": ["test_oep_probe_smoke.py::test_measured"]}:
                raise RuntimeError("Measurement instrumentation did not prove actual execution")
        measured = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", target],
            text=True,
        ).strip()
        evidence[name] = {"original": expected, "measured_image": measured}
        if name == "native":
            for alias in ("db-tests", "provision-tests"):
                subprocess.run(["docker", "tag", measured, f"{project}-{alias}"], check=True)
    (prepared / "measured-images.json").write_text(
        json.dumps(
            {
                "images": evidence,
                "probe_sha256": digest(Path(__file__).with_name("resource_probe.py")),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return plan["profile"]


def resource_overlay(lane, roomy, probe):
    """Allocate a machine budget without introducing limits on previously unlimited database services."""
    if lane == "supervisor":
        return {
            "services": {
                "dev": {
                    "cpus": 4 if roomy else 2,
                    "mem_limit": "8g" if roomy else "4g",
                    "volumes": [f"{probe}:/opt/oep_resource_probe.py:ro"],
                    "environment": {
                        "PYTEST_PLUGINS": "oep_resource_probe",
                        "PYTHONPATH": "/opt:/workspace/src",
                    },
                }
            }
        }
    services = {}
    if lane == "native" and roomy:
        services = {
            "native": {"cpus": 4, "mem_limit": "4g"},
            "ha-tests": {"cpus": 4, "mem_limit": "4g"},
            "app": {"cpus": 2, "mem_limit": "1g"},
            "homeassistant-test": {"cpus": 4, "mem_limit": "4g"},
        }
    return {"services": services}


def docker_arguments(arguments, overlay, lane):
    """Append only this benchmark's Compose overlay, retaining private Supervisor isolation overrides."""
    if not arguments or arguments[0] != "compose":
        return arguments
    files = [arguments[index + 1] for index, value in enumerate(arguments[:-1]) if value == "-f"]
    expected = "compose.dev.yaml" if lane == "supervisor" else "compose.yaml"
    if not files or Path(files[0]).name != expected:
        return arguments
    index = 1
    while index < len(arguments):
        if arguments[index] in {"-f", "-p", "--profile", "--project-directory"}:
            index += 2
        else:
            break
    return [*arguments[:index], "-f", overlay, *arguments[index:]]


def sample_resources(stop, evidence, docker, run_id):
    """Retain observed host cgroup counters and actual limits for this disposable job's containers."""
    while not stop.is_set():
        identifiers = subprocess.run(
            [docker, "ps", "-q", "--filter", "label=com.docker.compose.project"],
            capture_output=True,
            text=True,
            check=False,
        ).stdout.split()
        if identifiers:
            inspected = subprocess.run(
                [docker, "inspect", *identifiers], capture_output=True, text=True, check=False
            )
            if inspected.returncode == 0:
                for container in json.loads(inspected.stdout):
                    project = container["Config"]["Labels"]["com.docker.compose.project"]
                    if not project.startswith((f"oep-check-{run_id}-", "oep-supervisor-check-")):
                        continue
                    record = evidence.setdefault(
                        container["Id"],
                        {
                            "project": project,
                            "image": container["Image"],
                            "service": container["Config"]["Labels"].get(
                                "com.docker.compose.service"
                            ),
                            "nano_cpus": container["HostConfig"]["NanoCpus"],
                            "memory_limit": container["HostConfig"]["Memory"],
                            "samples": [],
                        },
                    )
                    try:
                        groups = Path(f"/proc/{container['State']['Pid']}/cgroup").read_text()
                        relative = next(
                            line[3:] for line in groups.splitlines() if line.startswith("0::")
                        )
                        location = Path("/sys/fs/cgroup") / relative.lstrip("/")
                        counters = {
                            name: (location / name).read_text()
                            for name in (
                                "cpu.stat",
                                "memory.peak",
                                "memory.events",
                                "memory.swap.peak",
                            )
                        }
                        record["samples"].append(counters)
                    except (OSError, StopIteration):
                        record["unreadable_samples"] = record.get("unreadable_samples", 0) + 1
        stop.wait(2)


def run_lane(workload, output, prepared, lane, roomy):
    """Execute one unchanged original lane and preserve failed timings before any further comparison."""
    output.mkdir(parents=True, exist_ok=True)
    original = shutil.which("docker")
    if not original:
        raise RuntimeError("Docker unavailable")
    revision = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=workload, text=True
    ).strip()
    if revision != SOURCE:
        raise RuntimeError("Unexpected workload checkout")
    projects = subprocess.check_output(
        [
            original,
            "ps",
            "-a",
            "--filter",
            "label=com.docker.compose.project",
            "--format",
            '{{.Label "com.docker.compose.project"}}',
        ],
        text=True,
    ).splitlines()
    if any(
        project == os.environ["COMPOSE_PROJECT_NAME"]
        or (lane == "supervisor" and project.startswith("oep-supervisor-check-"))
        for project in projects
    ):
        raise RuntimeError("Previous benchmark resources remain. Do not reuse their state")
    overlay = output / "resources.json"
    overlay.write_text(
        json.dumps(
            resource_overlay(lane, roomy, Path(__file__).with_name("resource_probe.py").resolve())
        ),
        encoding="utf-8",
    )
    binary = output / "bin"
    binary.mkdir()
    launcher = binary / "docker"
    launcher.write_text(
        f"#!{sys.executable}\nimport subprocess,sys\nsys.exit(subprocess.run("
        f"[{sys.executable!r}, {str(Path(__file__).resolve())!r}, 'docker', *sys.argv[1:]]).returncode)\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    plan = (
        json.loads((prepared / "parallel-plan.json").read_text()) if lane != "supervisor" else None
    )
    profile = plan["profile"] if plan else "full+performance-queries"
    environment = {
        **os.environ,
        "PATH": str(binary) + os.pathsep + os.environ["PATH"],
        "OEP_REAL_DOCKER": original,
        "OEP_RESOURCE_OVERLAY": str(overlay),
        "OEP_RESOURCE_LANE": lane,
        "OEP_SUPERVISOR_IMAGES": str(output / "supervisor-platform.json"),
        "GITHUB_SHA": SOURCE,
    }
    evidence = {}
    stop = threading.Event()
    sampler = threading.Thread(
        target=sample_resources,
        args=(stop, evidence, original, os.environ["GITHUB_RUN_ID"]),
        daemon=True,
    )
    sampler.start()
    started = monotonic()
    result = None
    try:
        result = subprocess.run(
            [
                sys.executable,
                "script/ci/ci.py",
                "--prebuilt",
                "--stage",
                "tests",
                "--profile",
                profile,
                "--lane",
                lane,
            ],
            cwd=workload,
            env=environment,
            check=False,
        )
    finally:
        stop.set()
        sampler.join()
        receipt = {
            "lane": lane,
            "roomy": roomy,
            "seconds": monotonic() - started,
            "exit_code": result.returncode if result else None,
            "source": SOURCE,
            "source_tree": SOURCE_TREE,
            "profile": profile,
            "host_cpus": os.cpu_count(),
            "host_memory": Path("/proc/meminfo").read_text(),
            "source_clean": subprocess.run(
                ["git", "diff", "--quiet", "HEAD"], cwd=workload, check=False
            ).returncode
            == 0,
        }
        (output / "measurement.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        (output / "container-resources.json").write_text(json.dumps(evidence), encoding="utf-8")
        reports = workload / "results/ci"
        if reports.exists():
            shutil.move(str(reports), output / "reports")
    if result is None or result.returncode or not receipt["source_clean"]:
        raise RuntimeError("Original lane failed. Preserve this variant as an invalid reference")


def validate_probe(directory):
    """Bind every original successful JUnit case to its selected identity and actual passing phases."""
    result = {}
    for report in directory.glob("*.xml"):
        probe = report.with_suffix(".probe.json")
        record = json.loads(probe.read_text(encoding="utf-8"))
        identities = []
        for case in ElementTree.parse(report).iter("testcase"):
            if any(case.find(tag) is not None for tag in ("failure", "error", "skipped")):
                raise ValueError("Unsuccessful original lane report")
            values = [
                prop.get("value")
                for prop in case.findall("./properties/property")
                if prop.get("name") == "oep_benchmark_nodeid"
            ]
            if len(values) != 1 or not values[0]:
                raise ValueError("Missing original execution identity")
            identities.extend(values)
        if (
            not identities
            or sorted(identities) != record["selected"]
            or len(identities) != len(set(identities))
            or record["exit_code"] != 0
            or set(record["outcomes"]) != set(identities)
        ):
            raise ValueError("Incomplete original lane execution")
        for phases in record["outcomes"].values():
            if set(phases) != {"setup", "call", "teardown"} or any(
                phase["outcome"] != "passed" for phase in phases.values()
            ):
                raise ValueError("Incomplete original execution phases")
        result[report.name] = sorted(identities)
    if not result:
        raise ValueError("No original lane reports")
    return result


def compare(directory):
    """Compare only identical full selections after both original workloads have passed."""
    controls = directory / "control"
    candidates = directory / "roomy"
    baseline = json.loads((controls / "measurement.json").read_text())
    identities = validate_probe(controls / "reports")
    counts = {name: len(items) for name, items in identities.items()}
    if (
        baseline["exit_code"] != 0
        or not baseline["source_clean"]
        or counts != EXPECTED_COUNTS[baseline["lane"]]
    ):
        raise ValueError("Original independently recorded lane inventory is incomplete")
    for name in identities:
        original = directory / "original" / name
        if baseline["lane"] == "supervisor":
            original = directory / "original" / "unit-app.xml"
        expected = [
            (case.get("classname"), case.get("name"))
            for case in ElementTree.parse(original).iter("testcase")
        ]
        actual = [
            (case.get("classname"), case.get("name"))
            for case in ElementTree.parse(controls / "reports" / name).iter("testcase")
        ]
        if baseline["lane"] == "supervisor":
            functions = {
                "test_future_issued_challenger_cannot_create_historical_decision_evidence",
                "test_three_real_shadow_windows_promote_and_a_later_loss_restores_baseline",
                "test_estimated_or_missing_measurements_cannot_promote_a_model",
            }
            expected = [case for case in expected if case[1] in functions]
        if sorted(expected) != sorted(actual):
            raise ValueError("Independent original JUnit identities differ")
    if baseline["lane"] == "databases":
        result = {
            "lane": "databases",
            "observation_seconds": baseline["seconds"],
            "cases": counts,
            "comparison": "Already uncapped. No higher limit exists.",
        }
        (directory / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
        return
    candidate = json.loads((candidates / "measurement.json").read_text())
    if candidate["exit_code"] != 0 or not candidate["source_clean"]:
        raise ValueError("Invalid benchmark variant")
    for name in ("source", "source_tree", "lane", "profile"):
        if baseline[name] != candidate[name]:
            raise ValueError("Lane benchmark inputs differ")
    if identities != validate_probe(candidates / "reports"):
        raise ValueError("Lane selections differ")
    if baseline["lane"] == "supervisor":
        images = []
        for location in (controls, candidates):
            observed = json.loads((location / "container-resources.json").read_text())
            images.append(
                {record["image"] for record in observed.values() if record["service"] == "dev"}
            )
            platform = json.loads((location / "supervisor-platform.json").read_text())
            if not isinstance(platform, list) or not platform:
                raise ValueError("Supervisor platform images were not observed")
        if len(images[0]) != 1 or images[0] != images[1]:
            raise ValueError("Supervisor outer images differ")
        if (controls / "supervisor-platform.json").read_text() != (
            candidates / "supervisor-platform.json"
        ).read_text():
            raise ValueError("Supervisor platform images differ")
    result = {
        "lane": baseline["lane"],
        "control_seconds": baseline["seconds"],
        "roomy_seconds": candidate["seconds"],
        "cases": {name: len(items) for name, items in identities.items()},
    }
    (directory / "comparison.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


def capture_supervisor_platform(arguments):
    """Read only image identities from this invocation's private nested Docker engine."""
    project = arguments[arguments.index("-p") + 1]
    if not project.startswith("oep-supervisor-check-"):
        return
    docker = os.environ["OEP_REAL_DOCKER"]
    identifiers = subprocess.check_output(
        [docker, "ps", "-q", "--filter", f"label=com.docker.compose.project={project}"], text=True
    ).split()
    images = []
    for identifier in identifiers:
        result = subprocess.run(
            [
                docker,
                "exec",
                identifier,
                "docker",
                "--host",
                "unix:///run/docker.sock",
                "image",
                "ls",
                "--no-trunc",
                "--digests",
                "--format",
                "{{json .}}",
            ],
            text=True,
            capture_output=True,
            check=False,
            timeout=60,
        )
        if result.returncode == 0:
            images.extend(
                json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")
            )
    observed = sorted(
        {
            (row["Repository"], row["ID"], row["Digest"])
            for row in images
            if row["Repository"].startswith("ghcr.io/home-assistant/")
        }
    )
    Path(os.environ["OEP_SUPERVISOR_IMAGES"]).write_text(json.dumps(observed), encoding="utf-8")


def main():
    """Run the fixed benchmark operations without changing the product CI scripts."""
    if len(sys.argv) > 1 and sys.argv[1] == "docker":
        arguments = docker_arguments(
            sys.argv[2:], os.environ["OEP_RESOURCE_OVERLAY"], os.environ["OEP_RESOURCE_LANE"]
        )
        if (
            arguments
            and arguments[0] == "compose"
            and "down" in arguments
            and os.environ["OEP_RESOURCE_LANE"] == "supervisor"
        ):
            try:
                capture_supervisor_platform(arguments)
            except (OSError, subprocess.SubprocessError, ValueError, KeyError, IndexError) as error:
                try:
                    Path(os.environ["OEP_SUPERVISOR_IMAGES"]).write_text(
                        json.dumps({"measurement_error": type(error).__name__}), encoding="utf-8"
                    )
                except OSError:
                    pass  # Cleanup must still execute if measurement storage is unavailable.
        raise SystemExit(
            subprocess.run([os.environ["OEP_REAL_DOCKER"], *arguments], check=False).returncode
        )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "compare"))
    parser.add_argument("--lane", choices=("native", "databases", "supervisor"))
    parser.add_argument("--workload", type=Path, default=Path("workload"))
    parser.add_argument("--prepared", type=Path, default=Path(".local/lane-images"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--roomy", action="store_true")
    options = parser.parse_args()
    if options.action == "prepare":
        prepare(
            options.workload.resolve(),
            options.prepared.resolve(),
            options.lane,
            os.environ["COMPOSE_PROJECT_NAME"],
        )
    elif options.action == "run":
        run_lane(
            options.workload.resolve(),
            options.output.resolve(),
            options.prepared.resolve(),
            options.lane,
            options.roomy,
        )
    else:
        compare(options.output.resolve())


if __name__ == "__main__":
    main()
