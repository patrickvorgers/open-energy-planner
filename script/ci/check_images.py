"""Fail before long suites when the selected packaged tests or entry modules cannot load."""

import json
import os
import shlex
import subprocess
from pathlib import Path
from time import monotonic

from script.ci.browser_suites import BROWSER_WORKERS, collections
from script.ci.check_browser import worker_arguments
from script.ci.ci_scope import FUNCTIONAL_DATABASE_SUITES, PYTEST_AREAS, matrix_profile
from script.ci.ha_suites import HA_WORKERS
from script.ci.pytest_suites import pytest_arguments


def image_checks(profile, suites):
    """Use execution's suite arguments and worker plugin without running fixtures or services."""
    if profile.startswith("selected:"):
        yield "ci", "native", pytest_arguments("ci", collect_only=True)
        for suite in sorted(suites & PYTEST_AREAS.keys()):
            service = "ha-tests" if suite.startswith("ha-") else "native"
            yield suite, service, pytest_arguments(suite, collect_only=True)
    else:
        parent = matrix_profile(profile)
        native_suite = parent if parent in {"documentation", "ci"} else "native"
        native_arguments = (
            ("--ignore=tests/database",) if suites & set(FUNCTIONAL_DATABASE_SUITES) else ()
        )
        yield (
            native_suite,
            "native",
            pytest_arguments(native_suite, collect_only=True, arguments=native_arguments),
        )
    for suite in sorted(suites & set(FUNCTIONAL_DATABASE_SUITES)):
        yield suite, "db-tests", pytest_arguments(suite, collect_only=True)
    if "performance-queries" in suites:
        yield (
            "performance-queries",
            "db-tests",
            pytest_arguments("performance-queries", collect_only=True),
        )
    if "ha" in suites:
        yield "ha-contracts", "ha-tests", pytest_arguments("ha-contracts", collect_only=True)
        for worker in range(HA_WORKERS):
            yield (
                f"ha-{worker}",
                "ha-tests",
                pytest_arguments(
                    "ha",
                    worker=worker,
                    collect_only=True,
                    arguments=("--ignore=tests/custom_component/integration/contracts",),
                ),
            )
    if "ha" in suites or "provisioning" in suites:
        yield (
            "provisioning-imports",
            "provision-tests",
            [
                "-c",
                (
                    "import importlib\n"
                    "for name in ('script.home_assistant.bootstrap.provision_ha', 'script.supervisor.provision_supervisor', "
                    "'script.home_assistant.provision_smoke', 'script.supervisor.supervisor_smoke'):\n"
                    "    importlib.import_module(name)"
                ),
            ],
        )
    if "browser" in suites:
        for worker in range(BROWSER_WORKERS):
            yield (
                f"browser-{worker}",
                "ui-worker",
                pytest_arguments("browser", worker=worker, collect_only=True),
            )


def check_images(compose, project, profile, suites):
    """Stop on the first load failure and retain image, command, full output and an Actions summary."""
    output = Path("results/ci")
    output.mkdir(parents=True, exist_ok=True)
    for suite, service, arguments in image_checks(profile, suites):
        image = f"{project}-{'ui-tests' if service == 'ui-worker' else service}"
        command = [
            *compose,
            "run",
            "--no-deps",
            "--pull",
            "never",
            "--rm",
            "--entrypoint",
            "python",
            service,
            *arguments,
        ]
        if service == "ui-worker":
            worker = int(arguments[arguments.index("--browser-shard") + 1])
            command = [*compose, *worker_arguments(output, worker, collect_only=True)]
        print(f"::group::Packaged image check: {suite} ({image})", flush=True)
        started = monotonic()
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        elapsed = monotonic() - started
        transcript = result.stdout + result.stderr
        log = output / f"preflight-{suite}.log"
        empty_worker = False
        if service == "ui-worker" and result.returncode == 5:
            try:
                record = json.loads(
                    (output / f"browser-{worker}" / "browser-collected.json").read_text(
                        encoding="utf-8"
                    )
                )
                empty_worker = (
                    isinstance(record, dict)
                    and record.get("selected") == []
                    and bool(record.get("all"))
                )
            except (OSError, ValueError):
                pass
        passed = result.returncode == 0 or empty_worker
        collection_error = None
        if passed and service == "ui-worker" and worker == BROWSER_WORKERS - 1:
            try:
                collections(output)
            except RuntimeError as error:
                collection_error = error
                passed = False
                transcript += f"\nBrowser inventory validation failed: {error}\n"
        log.write_text(shlex.join(command) + "\n" + transcript, encoding="utf-8")
        status = "PASS" if passed else "FAIL"
        print(f"{status}: {suite} in {image}, {elapsed:.2f}s. Full output: {log}", flush=True)
        if not passed:
            print(transcript, flush=True)
        print("::endgroup::", flush=True)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with Path(summary).open("a", encoding="utf-8") as document:
                document.write(f"### Packaged image check: {suite}\n\n")
                document.write(f"**{status}** Â· `{image}` Â· {elapsed:.2f}s\n\n")
                document.write(f"Command: `{shlex.join(command)}`\n\n")
                document.write(f"Full output: `{log}` in the test reports artifact.\n\n")
                if not passed:
                    document.write("```text\n" + transcript[-6000:] + "\n```\n\n")
        if not passed:
            if collection_error is not None:
                raise collection_error
            result.check_returncode()
