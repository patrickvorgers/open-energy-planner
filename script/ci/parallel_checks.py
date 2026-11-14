"""Distribute selected suites across hosted machines and join their source-bound evidence."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from script.ci import check_suite_completion as completion
from script.ci.browser_suites import (
    BROWSER_MACHINES,
    collections,
    combine_reports,
    execution_reports,
    machine_workers,
    worker_directory,
)
from script.ci.ci_groups import FUNCTIONAL_DATABASE_SUITES, selected_suites
from script.ci.proof_git import checkout_tree

LANES = ("native", "browser", "databases", "supervisor")
PREPARED = Path(".local/ci-prepared")


def lane_suites(profile):
    """Assign every required suite exactly once, retaining legacy HA and documentation coverage."""
    required = completion.required_suites(profile)
    groups = {
        "browser": required & {"browser"},
        "databases": required & {*FUNCTIONAL_DATABASE_SUITES, "performance-queries"},
        "supervisor": required & {"supervisor"},
    }
    groups["native"] = required - set().union(*groups.values())
    return {lane: groups[lane] for lane in LANES if groups[lane]}


def lane_images(profile, lane):
    """Load only images used on this machine, sharing the native image with the database lane."""
    suites = lane_suites(profile).get(lane, set())
    if not suites:
        raise ValueError("Unselected CI lane")
    if lane == "supervisor":
        return []
    if lane == "browser":
        return ["ui-tests"]
    if lane == "databases":
        return ["native"]
    images = ["native"]
    if "ha" in suites or any(suite.startswith("ha-") for suite in suites):
        images.append("ha-tests")
    if suites & {"ha", "ha-controls", "ha-solver", "provisioning"}:
        images.append("app")
    return images


def machine_matrix(profile):
    """Expand only a selected browser lane into two machines without changing suite selection."""
    machines = []
    for lane in lane_suites(profile):
        indices = range(BROWSER_MACHINES) if lane == "browser" else (None,)
        for machine in indices:
            machines.append(
                {
                    "lane": lane,
                    "name": lane if machine is None else f"browser-{machine}",
                    "browser_machine": machine,
                    "images": lane_images(profile, lane),
                }
            )
    return machines


def browser_counts(directory, machine):
    """Count one machine's proven assigned cases without claiming the entire browser suite passed."""
    execution_reports(directory, machine=machine)
    return {
        "browser": sum(
            len(record["selected"]) for record in collections(directory, machine=machine)
        )
    }


def identity(profile):
    """Bind transferred images and reports to this exact checkout and Actions attempt."""
    return {
        "profile": profile,
        "source_tree": checkout_tree(),
        "run_id": os.environ["GITHUB_RUN_ID"],
        "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
        "tested_commit": os.environ["GITHUB_SHA"],
    }


def file_digest(path):
    """Hash potentially large image archives without loading them into memory."""
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_plan(profile):
    """Reject stale plans or altered suite assignments before loading images or joining evidence."""
    plan = json.loads((PREPARED / "parallel-plan.json").read_text(encoding="utf-8"))
    if (
        any(plan.get(key) != value for key, value in identity(profile).items())
        or plan.get("lanes")
        != {lane: sorted(suites) for lane, suites in lane_suites(profile).items()}
        or plan.get("machines") != machine_matrix(profile)
    ):
        raise RuntimeError("Parallel plan does not match this candidate")
    return plan


def export_images(profile):
    """Save each already built image once, retaining aliases and its inspected Docker identity."""
    PREPARED.mkdir(parents=True, exist_ok=True)
    plan = identity(profile)
    plan["lanes"] = {lane: sorted(suites) for lane, suites in lane_suites(profile).items()}
    plan["machines"] = machine_matrix(profile)
    plan["images"] = {}
    project = os.environ["COMPOSE_PROJECT_NAME"]
    targets = sorted({image for lane in plan["lanes"] for image in lane_images(profile, lane)})
    for target in targets:
        aliases = [target, "db-tests", "provision-tests"] if target == "native" else [target]
        tags = [f"{project}-{alias}" for alias in aliases]
        identifiers = json.loads(subprocess.check_output(["docker", "image", "inspect", *tags]))
        if len({image["Id"] for image in identifiers}) != 1:
            raise RuntimeError("Shared image aliases do not identify the same build")
        archive = PREPARED / f"{target}.tar"
        subprocess.run(["docker", "image", "save", "-o", str(archive), *tags], check=True)
        plan["images"][target] = {
            "sha256": file_digest(archive),
            "image_id": identifiers[0]["Id"],
            "tags": tags,
        }
    (PREPARED / "parallel-plan.json").write_text(json.dumps(plan, indent=2), encoding="utf-8")
    shutil.copytree("results/ci", PREPARED / "preflight", dirs_exist_ok=True)
    matrix = {"include": plan["machines"]}
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write("matrix=" + json.dumps(matrix) + "\n")


def load_images(profile, lane, *, browser_machine=None):
    """Verify transferred archive bytes and loaded image IDs before running any lane workload."""
    if lane != "browser" and browser_machine is not None:
        raise ValueError("Only browser lanes have a browser machine")
    plan = read_plan(profile)
    for target in lane_images(profile, lane):
        archive = PREPARED / f"{target}.tar"
        expected = plan["images"][target]
        if file_digest(archive) != expected["sha256"]:
            raise RuntimeError("Transferred test image checksum mismatch")
        subprocess.run(["docker", "image", "load", "-i", str(archive)], check=True)
        actual = json.loads(
            subprocess.check_output(["docker", "image", "inspect", *expected["tags"]])
        )
        if len(actual) != len(expected["tags"]) or any(
            image["Id"] != expected["image_id"] for image in actual
        ):
            raise RuntimeError("Loaded test image differs from the prepared build")
        archive.unlink()
    if lane == "browser":
        for worker in machine_workers(browser_machine):
            source = worker_directory(PREPARED / "preflight", worker) / "browser-collected.json"
            target = completion.REPORT.parent / source.relative_to(PREPARED / "preflight")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def write_lane_completion(profile, lane, completed, *, browser_machine=None):
    """Publish successful lane evidence only for its exact assigned suites and passing reports."""
    expected = lane_suites(profile).get(lane)
    if not expected or completed != expected:
        raise RuntimeError("Lane suite completion mismatch")
    document = identity(profile)
    if lane == "browser":
        if browser_machine is None:
            raise RuntimeError("Browser lane completion requires its machine identity")
        counts = browser_counts(completion.REPORT.parent, browser_machine)
        document["browser_machine"] = browser_machine
        name = f"browser-{browser_machine}"
    else:
        if browser_machine is not None:
            raise RuntimeError("Only browser lanes have a browser machine")
        counts = completion.execution_counts(profile, suites=completed)
        name = lane
    document.update(
        lane=lane,
        completed=sorted(completed),
        executed_tests=counts,
    )
    path = completion.REPORT.parent / f"lane-completion-{name}.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def join_reports(profile, directory):
    """Reject missing, duplicate or foreign lanes, then require the original whole-profile proof."""
    completion.REPORT.unlink(missing_ok=True)
    plan = read_plan(profile)
    expected_identity = identity(profile)
    required = lane_suites(profile)
    machines = machine_matrix(profile)
    if {path.name for path in directory.iterdir()} != {
        f"ci-reports-{machine['name']}" for machine in machines
    }:
        raise RuntimeError("Missing or unexpected parallel lane artifacts")
    destination = completion.REPORT.parent
    destination.mkdir(parents=True, exist_ok=True)
    completed = set()
    for machine in machines:
        lane = machine["lane"]
        name = machine["name"]
        suites = required[lane]
        source = directory / f"ci-reports-{name}"
        document = json.loads((source / f"lane-completion-{name}.json").read_text("utf-8"))
        if lane == "browser":
            machine_index = machine["browser_machine"]
            if (
                type(document.get("browser_machine")) is not int
                or document["browser_machine"] != machine_index
            ):
                raise RuntimeError("Browser machine evidence does not match its assignment")
            counts = browser_counts(source, machine_index)
            allowed_reports = set()
            for worker in machine_workers(machine_index):
                location = worker_directory(source, worker)
                original = (
                    worker_directory(PREPARED / "preflight", worker) / "browser-collected.json"
                )
                if json.loads(
                    (location / "browser-collected.json").read_text("utf-8")
                ) != json.loads(original.read_text("utf-8")):
                    raise RuntimeError(
                        "Browser execution inventory differs from original preflight"
                    )
                allowed_reports.add((location / "ui.xml").relative_to(source))
        else:
            counts = completion.execution_counts(profile, source, suites=suites)
            allowed_reports = {
                path.relative_to(source)
                for suite, paths in completion.junit_reports(profile, source).items()
                if suite in suites
                for path in paths
            }
        if (
            any(document.get(key) != value for key, value in expected_identity.items())
            or document.get("lane") != lane
            or document.get("completed") != sorted(suites)
            or document.get("executed_tests") != counts
        ):
            raise RuntimeError("Parallel lane evidence does not match this candidate")
        actual_reports = {path.relative_to(source) for path in source.rglob("*.xml")}
        if actual_reports != allowed_reports:
            raise RuntimeError("Unexpected or missing lane test reports")
        for path in source.rglob("*"):
            if path.is_file():
                target = destination / path.relative_to(source)
                if target.exists() or path.is_symlink():
                    raise RuntimeError("Duplicate or unsafe parallel report path")
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
        completed.update(suites)
    if "browser" in completed:
        combine_reports(destination)
    for path in (PREPARED / "preflight").rglob("*"):
        if path.is_file() and "browser-collected.json" != path.name:
            target = destination / path.relative_to(PREPARED / "preflight")
            if target.exists():
                raise RuntimeError("Duplicate preflight report path")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
    shutil.copyfile(PREPARED / "parallel-plan.json", destination / "parallel-plan.json")
    completion.write_completion(profile, completed)
    if plan["source_tree"] != checkout_tree():
        raise RuntimeError("Checkout changed while joining parallel evidence")


def main():
    """Prepare, load or join exactly the selected machines without changing scope policy."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("export", "load", "join"))
    parser.add_argument("--profile", required=True)
    parser.add_argument("--lane", choices=LANES)
    parser.add_argument("--browser-machine", type=int, choices=range(BROWSER_MACHINES))
    options = parser.parse_args()
    selected_suites(options.profile)
    if options.action == "export":
        export_images(options.profile)
    elif options.action == "load":
        if options.lane != "browser" and options.browser_machine is not None:
            parser.error("--browser-machine requires the browser lane")
        load_images(options.profile, options.lane, browser_machine=options.browser_machine)
    else:
        join_reports(options.profile, Path(".local/ci-lanes"))


if __name__ == "__main__":
    main()
