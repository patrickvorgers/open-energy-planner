"""Partition selected execution across four machines and join their source-bound evidence."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

from script.ci import check_suite_completion as completion
from script.ci.browser_suites import BROWSER_WORKERS, worker_directory
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
    if any(plan.get(key) != value for key, value in identity(profile).items()) or plan.get(
        "lanes"
    ) != {lane: sorted(suites) for lane, suites in lane_suites(profile).items()}:
        raise RuntimeError("Parallel plan does not match this candidate")
    return plan


def export_images(profile):
    """Save each already built image once, retaining aliases and its inspected Docker identity."""
    PREPARED.mkdir(parents=True, exist_ok=True)
    plan = identity(profile)
    plan["lanes"] = {lane: sorted(suites) for lane, suites in lane_suites(profile).items()}
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
    matrix = {
        "include": [{"lane": lane, "images": lane_images(profile, lane)} for lane in plan["lanes"]]
    }
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write("matrix=" + json.dumps(matrix) + "\n")


def load_images(profile, lane):
    """Verify transferred archive bytes and loaded image IDs before running any lane workload."""
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
        for source in (PREPARED / "preflight").glob("browser-*/browser-collected.json"):
            target = completion.REPORT.parent / source.relative_to(PREPARED / "preflight")
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)


def write_lane_completion(profile, lane, completed):
    """Publish successful lane evidence only for its exact assigned suites and passing reports."""
    expected = lane_suites(profile).get(lane)
    if not expected or completed != expected:
        raise RuntimeError("Lane suite completion mismatch")
    document = identity(profile)
    document.update(
        lane=lane,
        completed=sorted(completed),
        executed_tests=completion.execution_counts(profile, suites=completed),
    )
    path = completion.REPORT.parent / f"lane-completion-{lane}.json"
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")


def join_reports(profile, directory):
    """Reject missing, duplicate or foreign lanes, then require the original whole-profile proof."""
    completion.REPORT.unlink(missing_ok=True)
    plan = read_plan(profile)
    expected_identity = identity(profile)
    required = lane_suites(profile)
    if {path.name for path in directory.iterdir()} != {f"ci-reports-{lane}" for lane in required}:
        raise RuntimeError("Missing or unexpected parallel lane artifacts")
    destination = completion.REPORT.parent
    destination.mkdir(parents=True, exist_ok=True)
    completed = set()
    for lane, suites in required.items():
        source = directory / f"ci-reports-{lane}"
        document = json.loads((source / f"lane-completion-{lane}.json").read_text("utf-8"))
        if (
            any(document.get(key) != value for key, value in expected_identity.items())
            or document.get("lane") != lane
            or document.get("completed") != sorted(suites)
            or document.get("executed_tests")
            != completion.execution_counts(profile, source, suites=suites)
        ):
            raise RuntimeError("Parallel lane evidence does not match this candidate")
        allowed_reports = {
            path.relative_to(source)
            for suite, paths in completion.junit_reports(profile, source).items()
            if suite in suites
            for path in paths
        }
        if "browser" in suites:
            allowed_reports.update(
                (worker_directory(source, worker) / "ui.xml").relative_to(source)
                for worker in range(BROWSER_WORKERS)
            )
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
    options = parser.parse_args()
    selected_suites(options.profile)
    if options.action == "export":
        export_images(options.profile)
    elif options.action == "load":
        load_images(options.profile, options.lane)
    else:
        join_reports(options.profile, Path(".local/ci-lanes"))


if __name__ == "__main__":
    main()
