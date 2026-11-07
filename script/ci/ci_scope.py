"""Select conservative test routes from complete immutable base and intended merge trees."""

import ast
import json
import re
import sys
from os import environ
from pathlib import Path, PurePosixPath
from subprocess import SubprocessError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from script.ci.ci_groups import (
    APP_TEST_AREAS,
    APP_UNIT_SUITES,
    ATOMIC_SUITES,
    CONTAINED_SUITES,
    FUNCTIONAL_DATABASE_SUITES,
    HA_TEST_DIRECTORIES,
    PROFILES,
    PYTEST_AREAS,
    add_performance_queries,
    focused_profile,
    matrix_profile,
    minimal_suites,
    selected_suites,
)
from script.ci.proof_git import immutable_blob, immutable_test_sources, raw_changes

__all__ = [
    "APP_TEST_AREAS",
    "APP_UNIT_SUITES",
    "ATOMIC_SUITES",
    "CONTAINED_SUITES",
    "FUNCTIONAL_DATABASE_SUITES",
    "HA_TEST_DIRECTORIES",
    "PROFILES",
    "PYTEST_AREAS",
    "add_performance_queries",
    "focused_profile",
    "immutable_test_sources",
    "matrix_profile",
    "minimal_suites",
    "selected_suites",
]

DOCUMENTATION_FILES = frozenset({"README.md", "CONTRIBUTING.md", "AGENTS.md"})
DOCUMENTATION_DIRECTORIES = frozenset({"docs", "design"})
DOMAIN_DIRECTORIES = frozenset(
    {
        "appliances",
        "contracts",
        "demand",
        "ev",
        "forecast_quality",
        "forecast_selection",
        "optimization",
        "prices",
        "savings",
        "solar",
        "thermal",
        "installation",
        "history",
    }
)
# Calculation ownership and its import guard share the same reviewed domain set.
CALCULATION_DIRECTORIES = (
    "core",
    *(domain + "/calculation" for domain in sorted(DOMAIN_DIRECTORIES)),
)

NATIVE_SOLVER_DIRECTORIES = (
    "optimization/adapters/highs",
    "optimization/adapters/process",
)

# Flat composition modules keep their actual contracts without adding every HA feature.
APP_ROOT_PROFILES = {
    "app.py": focused_profile(
        "unit-app",
        "unit-ha-contract",
        "unit-supervisor",
        "browser",
        "databases",
        "ha-contracts",
        "ha-connection",
        "provisioning",
        "supervisor",
    ),
    "live_inputs.py": focused_profile(
        "unit-app", "unit-ha-contract", "ha-contracts", "ha-controls"
    ),
    "planning.py": focused_profile(
        "unit-app",
        "unit-ha-contract",
        "browser",
        "databases",
        "ha-contracts",
        "ha-controls",
        "ha-history",
        "ha-thermal",
    ),
    "discovery.py": focused_profile(
        "unit-app-thermal", "unit-ha-contract", "unit-component", "ha-connection", "supervisor"
    ),
}

# Native app domains use the separately owned peer contracts. Execution owners retain real controls.
DOMAIN_HA_SUITES = {
    "appliances": ("ha-controls", "ha-thermal", "ha-history"),
    "ev": ("ha-controls",),
    "thermal": ("ha-thermal",),
    "installation": ("ha-contracts", "ha-controls", "ha-thermal", "ha-history"),
    "optimization": ("ha-controls",),
    "forecast_quality": ("ha-contracts", "ha-history"),
}


PROOF_CI_FILES = frozenset(
    "script/ci/" + name + ".py"
    for name in (
        "label_ci_scope",
        "check_ci_candidate",
        "check_commit_signatures",
        "check_main_ci_reuse",
        "check_suite_completion",
        "review_preflight",
        "review_orchestration",
        "scope_report",
        "ci_scope",
        "ci_groups",
    )
)


HA_CI_FILES = frozenset({"script/ci/ha_suites.py", "script/ci/ha_shard.py"})
ADMISSION_WORKFLOW = ".github/workflows/ci-admission.yaml"
REVIEW_METADATA_FILES = frozenset(
    {
        ".coderabbit.yaml",
        ".github/workflows/review-preflight.yaml",
        ".github/workflows/review-orchestration.yaml",
    }
)
CHECKS_WORKFLOW = ".github/workflows/checks.yaml"
SETUP_PYTHON_REFERENCE = (
    r"(?m)^([ \t]*uses: actions/setup-python@)[0-9a-f]{40}(?:[ \t]+#[^\r\n]*)?[ \t]*$"
)


# Most-specific directories win. New files inherit their area's reviewed contract.
DIRECTORY_PROFILES = {
    "script/ci/": "full",
    "script/supervisor/": focused_profile("unit-supervisor", "supervisor"),
    "script/release/": "full",
    "script/runtime/": focused_profile("unit-supervisor", "supervisor", "provisioning"),
    "script/home_assistant/bootstrap/": focused_profile(
        "unit-provisioning", "unit-supervisor", "provisioning", "supervisor", "ha-connection"
    ),
    "script/home_assistant/": focused_profile("unit-provisioning", "provisioning"),
    "script/app/": focused_profile("unit-app"),
    "script/provisioning/": focused_profile("unit-provisioning", "unit-supervisor", "provisioning"),
    "tests/ci/": "ci",
    "tests/documentation/": "documentation",
    "tests/frontend/browser/": focused_profile("browser"),
    "tests/supervisor/support/": focused_profile("unit-supervisor"),
    "custom_components/open_energy_planner/supervisor/": focused_profile(
        "unit-component", "unit-supervisor", "ha-connection", "supervisor"
    ),
    "tests/custom_component/support/": focused_profile(
        "ha-contracts",
        "ha-connection",
        "ha-controls",
        "ha-history",
        "ha-thermal",
        "ha-test-environment",
    ),
    "tests/test_environment/support/": focused_profile("unit-test-environment"),
    "tests/ha_contract/support/": focused_profile("unit-ha-contract"),
    "tests/provisioning/support/": focused_profile("unit-provisioning"),
    "tests/database/": focused_profile("databases"),
    "tests/database/contracts/": focused_profile("database-contracts"),
    "tests/database/migrations/": focused_profile("database-migrations"),
    "tests/database/performance/": "ci",
    "tests/support/": "full",
    "test_environment/oep_test_fixture/calculation/recorder/": focused_profile(
        "unit-test-environment", "ha-test-environment", "provisioning"
    ),
    "test_environment/oep_test_fixture/calculation/": focused_profile("unit-test-environment"),
    "test_environment/oep_test_fixture/": focused_profile(
        "unit-test-environment", "ha-test-environment", "provisioning"
    ),
    **{
        "src/open_energy_planner/" + directory + "/": focused_profile("unit-app", "ha-solver")
        for directory in NATIVE_SOLVER_DIRECTORIES
    },
    "src/open_energy_planner/supervisor/": focused_profile("unit-supervisor", "supervisor"),
    "src/open_energy_planner/frontend/": focused_profile("unit-frontend", "browser"),
    "src/open_energy_planner/notifications/": focused_profile(
        "unit-app", "unit-frontend", "browser"
    ),
    "src/open_energy_planner/activities/": focused_profile(
        "unit-app", "unit-frontend", "browser", "database-contracts"
    ),
    "src/open_energy_planner/web/": focused_profile("unit-frontend", "browser"),
    "src/open_energy_planner/home_assistant/": focused_profile(
        "unit-ha-contract", "ha-contracts", "ha-history", "ha-controls"
    ),
    "src/open_energy_planner/runtime/diagnostics/": focused_profile(
        "unit-app", "unit-supervisor", "browser"
    ),
    "src/open_energy_planner/runtime/": focused_profile(
        "unit-app", "unit-supervisor", "ha-connection", "provisioning", "supervisor"
    ),
    "src/open_energy_planner/storage/adapters/lifecycle/": focused_profile(
        "unit-app",
        "databases",
        "ha-contracts",
        "ha-connection",
        "provisioning",
        "supervisor",
        "unit-supervisor",
    ),
}
for directory in CALCULATION_DIRECTORIES:
    DIRECTORY_PROFILES["src/open_energy_planner/" + directory + "/"] = focused_profile("unit-app")
for suite, directories in PYTEST_AREAS.items():
    for directory in directories:
        DIRECTORY_PROFILES[directory + "/"] = focused_profile(suite)
REGULAR_FILE_MODES = frozenset({b"000000", b"100644"})
PYTEST_MODULE_PATTERNS = ("test_*.py", "*_test.py")


def is_pytest_module(path):
    """Match pytest's default module patterns independently of the containing suite directory."""
    return any(PurePosixPath(path).match(pattern) for pattern in PYTEST_MODULE_PATTERNS)


def test_consumer_graph(sources):
    """Parse each source once and return per-helper reachability without candidate execution.

    Package initialization and ancestor conftest files are implicit dependencies.
    Dynamic import users are conservatively potential consumers of every helper.
    Runtime payloads and external commands still require explicit area contracts.
    """
    test_modules = {
        path.removesuffix(".py").replace("/", ".").removesuffix(".__init__"): path
        for path in sources
    }
    dependents = {}
    fixture_consumers = {}
    dynamic_importers = set()
    for path, source in sources.items():
        module = path.removesuffix(".py").replace("/", ".")
        package = (
            module.removesuffix(".__init__")
            if path.endswith("/__init__.py")
            else module.rpartition(".")[0]
        )
        dependencies = set()
        tree = ast.parse(source, filename=path)
        import_aliases = {
            (alias.asname or alias.name, alias.name)
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
            if (node.module, alias.name)
            in {("importlib", "import_module"), ("builtins", "__import__")}
        }
        dynamic_names = {"__import__", "import_module"} | {name for name, _ in import_aliases}
        builtin_import_names = {"__import__"} | {
            name for name, original in import_aliases if original == "__import__"
        }
        direct_calls = {id(node.func) for node in ast.walk(tree) if isinstance(node, ast.Call)}
        for node in ast.walk(tree):
            if id(node) not in direct_calls and (
                isinstance(node, ast.Name)
                and isinstance(node.ctx, ast.Load)
                and node.id in dynamic_names
                or isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in {"import_module", "__import__"}
            ):
                dynamic_importers.add(path)
            if (
                isinstance(node, ast.Name)
                and node.id == "pytest_plugins"
                and isinstance(node.ctx, ast.Store)
                or isinstance(node, ast.alias)
                and (
                    node.name == "*" or (node.asname or node.name.split(".")[0]) == "pytest_plugins"
                )
            ):
                raise ValueError(
                    "Unclassified pytest plugin or wildcard declarations require full CI"
                )
            names = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level:
                    parts = package.split(".")
                    if node.level > len(parts):
                        raise ValueError(f"Invalid relative test import in {path}")
                    base = ".".join(parts[: len(parts) - node.level + 1] + ([base] if base else []))
                names = [base, *(base + "." + alias.name for alias in node.names)]
            elif isinstance(node, ast.Call) and (
                isinstance(node.func, ast.Name)
                and node.func.id in dynamic_names
                or isinstance(node.func, ast.Attribute)
                and node.func.attr in {"import_module", "__import__"}
            ):
                builtin_import = (
                    isinstance(node.func, ast.Name)
                    and node.func.id in builtin_import_names
                    or isinstance(node.func, ast.Attribute)
                    and node.func.attr == "__import__"
                )
                if (
                    node.args
                    and isinstance(node.args[0], ast.Constant)
                    and isinstance(node.args[0].value, str)
                    and not node.args[0].value.startswith(".")
                    and not (builtin_import and (len(node.args) > 1 or node.keywords))
                ):
                    names = [node.args[0].value]
                else:
                    dynamic_importers.add(path)
            for name in names:
                parts = name.split(".")
                dependencies.update(
                    test_modules[prefix]
                    for index in range(1, len(parts) + 1)
                    if (prefix := ".".join(parts[:index])) in test_modules
                )
        for parent in PurePosixPath(path).parents:
            initializer = str(parent / "__init__.py")
            if initializer in sources:
                dependencies.add(initializer)
            fixture = str(parent / "conftest.py")
            if fixture in sources and is_pytest_module(path):
                fixture_consumers.setdefault(fixture, set()).add(path)
        for dependency in dependencies:
            dependents.setdefault(dependency, set()).add(path)

    def scenarios_for(helper, *, include_dynamic=True):
        """Keep static ownership proof separate from conservative dynamic suite coverage."""
        reached = {helper}
        if include_dynamic:
            reached.update(dynamic_importers)
        pending = list(reached)
        while pending:
            for consumer in dependents.get(pending.pop(), ()):
                if consumer not in reached:
                    reached.add(consumer)
                    pending.append(consumer)
        scenarios = {path for path in reached if is_pytest_module(path)}
        for path in reached:
            scenarios.update(fixture_consumers.get(path, ()))
        return scenarios

    return scenarios_for


def test_helper_consumers(sources, changed):
    """Return the union for existing callers while sharing one parsed dependency graph."""
    scenarios_for = test_consumer_graph(sources)
    return set().union(*(scenarios_for(helper) for helper in changed))


def path_profile(path):
    """Classify explicit ownership boundaries. Unknown or shared infrastructure remains full."""
    name = str(path)
    if name.startswith("docs/"):
        return "documentation"
    if path.suffix == ".md" and (
        name in DOCUMENTATION_FILES
        or len(path.parts) > 1
        and path.parts[0] in DOCUMENTATION_DIRECTORIES
    ):
        return "documentation"
    if (
        name.startswith("tests/app/")
        and len(path.parts) > 3
        and path.parts[2] not in APP_TEST_AREAS
    ):
        return "full"
    # HA owns this fixed installation contract, including test integrations.
    if path.name == "manifest.json" and name.startswith(
        ("custom_components/", "test_environment/")
    ):
        return "full"
    if name == "src/open_energy_planner/installation/forecast_policy.py":
        return focused_profile("unit-app")
    if name_is_shared_root(path) and path.name in APP_ROOT_PROFILES:
        return APP_ROOT_PROFILES[path.name]
    if name in PROOF_CI_FILES or name == ADMISSION_WORKFLOW or name in REVIEW_METADATA_FILES:
        return "ci"
    if name in HA_CI_FILES:
        return focused_profile("ha")
    for directory in sorted(DIRECTORY_PROFILES, key=len, reverse=True):
        profile = DIRECTORY_PROFILES[directory]
        if name.startswith(directory):
            return profile
    if name in {
        ".github/workflows/developer-page.yaml",
        ".github/workflows/pages.yaml",
        "Dockerfile.ui-test",
    }:
        return focused_profile("unit-frontend", "browser")
    if name == "Dockerfile.native-test":
        return focused_profile(
            *(suite for suite in PYTEST_AREAS if suite.startswith("unit-")),
            "databases",
            "provisioning",
        )
    # The HA framework image also pins the Core version used for provisioning.
    if name == "Dockerfile.chain-test":
        return "full"
    if name.startswith("src/open_energy_planner/"):
        module = name.removeprefix("src/open_energy_planner/")
        if "/" in module and module.split("/", 1)[0] not in DOMAIN_DIRECTORIES | {"storage"}:
            return "full"
        if "/" not in module:
            return "full"
        if path.stem in {"wire", "model"} or path.stem.endswith("_wire"):
            return focused_profile(
                "unit-app",
                "unit-ha-contract",
                "browser",
                "databases",
                *DOMAIN_HA_SUITES.get(module.split("/", 1)[0], ()),
            )
        if {"storage", "adapters", "queries"} <= set(module.split("/")[:-1]):
            return focused_profile(
                "unit-app",
                "browser",
                "database-contracts",
                *(("unit-ha-contract",) if not module.startswith("storage/") else ()),
                *DOMAIN_HA_SUITES.get(module.split("/", 1)[0], ()),
            )
        if module.startswith("storage/"):
            return focused_profile("unit-app", "browser", "databases")
        domain_storage = "storage" in module.split("/")[:-1] or path.stem == "storage"
        return (
            focused_profile(
                "unit-app",
                "unit-ha-contract",
                "browser",
                *DOMAIN_HA_SUITES.get(module.split("/", 1)[0], ()),
                *(("databases",) if domain_storage else ()),
            )
            if path.suffix == ".py"
            else "full"
        )
    if name == "custom_components/open_energy_planner/config_flow.py":
        return focused_profile(
            "unit-component",
            "unit-supervisor",
            "ha-contracts",
            "ha-connection",
            "provisioning",
            "supervisor",
        )
    if name.startswith("custom_components/"):
        if not name.startswith("custom_components/open_energy_planner/"):
            return "full"
        return (
            "full"
            if path.name
            in {
                "__init__.py",
                "manifest.json",
                "build-info.json",
                "config_flow.py",
                "supervisor.py",
            }
            else focused_profile(
                "unit-component",
                "unit-ha-contract",
                "ha-contracts",
                "ha-connection",
                "ha-controls",
                "ha-history",
                "ha-thermal",
            )
        )
    return "full"


def name_is_shared_root(path):
    """New flat app modules have no declared domain owner and must fail closed."""
    return str(path).startswith("src/open_energy_planner/") and len(path.parts) == 3


def workflow_step_reference(text, position):
    """Recognize a block-style step field, conservatively rejecting ambiguous YAML prefixes."""
    parents = []
    offset = 0
    for line in text.splitlines(keepends=True):
        reference = offset == position
        offset += len(line)
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if "\t" in line:
            return False
        indentation = len(line) - len(line.lstrip(" "))
        while parents and parents[-1][0] >= indentation:
            parents.pop()
        if parents and parents[-1][2]:
            continue
        if reference:
            return (
                len(parents) == 4
                and [parent[1] for parent in parents][::2] == ["jobs", "steps"]
                and parents[-1][1] == "-"
                and indentation == parents[-1][0] + 2
            )
        field = re.fullmatch(
            r" *(?P<item>- )?(?P<key>[A-Za-z_][A-Za-z0-9_-]*):(?P<value>[^\r\n]*)[\r\n]*", line
        )
        if field is None:
            return False
        value = field["value"].strip()
        # shortcut: quoted, tagged and complex flow prefixes keep full CI until explicitly supported.
        if value.startswith(("'", '"', "&", "*", "!", "{")) or (
            value.startswith("[")
            and re.fullmatch(r"\[[A-Za-z0-9_, .-]*\](?: +#.*)?", value) is None
        ):
            return False
        if field["item"]:
            parents.append((indentation, "-", False))
            indentation += 2
        parents.append((indentation, field["key"], bool(value and not value.startswith("#"))))
    return False


def setup_python_reference_only(old_blob, new_blob, root):
    """Recognize one pinned setup action update in otherwise identical immutable workflow text."""
    normalized = []
    for blob in (old_blob, new_blob):
        text = immutable_blob(blob.decode("ascii"), root).decode("utf-8")
        matches = list(re.finditer(SETUP_PYTHON_REFERENCE, text))
        if len(matches) != 1 or not workflow_step_reference(text, matches[0].start()):
            return False
        normalized.append(re.subn(SETUP_PYTHON_REFERENCE, r"\1PIN", text))
    return normalized[0] == normalized[1] and normalized[0][1] == 1


def _select_functional_profile(base, tested, root=".", *, reasons=None, fallback=None):
    """Union checks for every changed path. Uncertainty and special modes require full CI.

    CI compares the PR base with its checked-out merge commit, not just the last
    push. Rename detection is disabled so both old and new paths must qualify.
    Trees also permit isolated regression fixtures without creating commits.
    """
    if reasons is None:
        reasons = []

    def full(reason):
        """Retain an explicit uncertainty reason rather than silently widening coverage."""
        if fallback is not None:
            fallback.append(True)
        reasons.append(("full", reason))
        return "full"

    if not all(
        isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision)
        for revision in (base, tested)
    ):
        return full("Invalid or unavailable immutable comparison revisions")
    try:
        output = raw_changes(base, tested, root)
        fields = output.split(b"\0")
        if not output or fields.pop() != b"" or len(fields) % 2:
            return full("Empty or malformed complete PR diff")
        required = set()
        changed_helpers = set()
        for metadata, raw_path in zip(fields[::2], fields[1::2], strict=True):
            old_mode, new_mode, old_blob, new_blob, status = metadata.split()
            if (
                old_mode.removeprefix(b":") not in REGULAR_FILE_MODES
                or new_mode not in REGULAR_FILE_MODES
                or status not in (b"A", b"M", b"D")
            ):
                return full("Unsupported file mode or change status")
            path = PurePosixPath(raw_path.decode("utf-8"))
            profile = path_profile(path)
            if (
                str(path) == CHECKS_WORKFLOW
                and status == b"M"
                and setup_python_reference_only(old_blob, new_blob, root)
            ):
                profile = "ci"
            # Unknown flat modules have no owner. Known directories inherit their
            # contract for additions and deletions as well as modifications.
            if status != b"M" and name_is_shared_root(path):
                profile = "full"
            reasons.append((profile, str(path)))
            required.update(selected_suites(profile))
            if path.parts[0] == "tests" and path.suffix == ".py":
                changed_helpers.add(str(path))
        if any(route == "full" for route, _ in reasons):
            return "full"
        if changed_helpers:
            static_consumers_by_helper = {helper: set() for helper in changed_helpers}
            consumers = set()
            for revision in (base, tested):
                sources = immutable_test_sources(revision, root)
                scenarios_for = test_consumer_graph(sources)
                for helper, static_consumers in static_consumers_by_helper.items():
                    static_consumers.update(scenarios_for(helper, include_dynamic=False))
                    consumers.update(scenarios_for(helper))
            if any(not consumers for consumers in static_consumers_by_helper.values()):
                return full("Changed test helper has no statically known scenario consumers")
            for consumer in sorted(consumers):
                profile = path_profile(PurePosixPath(consumer))
                reasons.append((profile, "Test helper consumer: " + consumer))
                if profile == "full":
                    return "full"
                required.update(selected_suites(profile))
    except (
        OSError,
        SubprocessError,
        UnicodeError,
        ValueError,
        SyntaxError,
    ):
        return full("Unreadable or malformed complete PR diff")
    # Native checks already execute the CI policy tests. A mixed PR does not
    # need a second CI-only invocation or a broader integration profile.
    if "native" in required:
        required.discard("ci")
    if not required:
        return "documentation"
    if required == {"ci"}:
        return "ci"
    if not required <= ATOMIC_SUITES:
        return next((profile for profile, suites in PROFILES.items() if required <= suites), "full")
    return focused_profile(*required)


def performance_queries_path(path):
    """Add volume proof for measured query code, fixtures and runtime environments, not CI control."""
    name = str(path)
    if name in {
        "Dockerfile.native-test",
        "requirements.lock",
        "pyproject.toml",
        "compose.yaml",
    }:
        return True
    if name.startswith(("tests/database/performance/", "tests/database/support/")) or name == (
        "tests/database/conftest.py"
    ):
        return True
    if name.startswith("src/open_energy_planner/"):
        module = path.parts[2:]
        if path_profile(path) != "full" and ("storage" in module[:-1] or path.stem == "storage"):
            return True
    return False


def select_profile(base, tested, root=".", *, reasons=None):
    """Select functional coverage, then independently add query-volume validation."""
    if reasons is None:
        reasons = []
    fallback = []
    profile = _select_functional_profile(base, tested, root, reasons=reasons, fallback=fallback)
    if fallback:
        return profile
    volume_paths = [
        path
        for _route, path in reasons
        if performance_queries_path(PurePosixPath(path.removeprefix("Test helper consumer: ")))
    ]
    if volume_paths:
        reasons.extend((focused_profile("performance-queries"), path) for path in volume_paths)
        return add_performance_queries(profile)
    return profile


def performance_queries_required(base, tested, root="."):
    """Expose the immutable policy's independent performance decision for main reuse."""
    return "performance-queries" in selected_suites(select_profile(base, tested, root))


def documentation_only(base, tested, root="."):
    """Retain the documentation eligibility contract for existing callers and regressions."""
    return select_profile(base, tested, root) == "documentation"


def test_only_profile(base, tested, root=".", *, profile=None, reasons=None):
    """Admit only owned Python tests and their known consumers, never policy or runtime inputs.

    The complete diff must contain at least one test and no other changed path.
    Unknown areas, non-Python fixtures and uncertain imports retain ordinary CI.
    Tests are excluded from the shipped app and integration packaging contracts.
    """
    if profile is None or reasons is None:
        reasons = []
        profile = select_profile(base, tested, root, reasons=reasons)
    changed = [path for _, path in reasons if not path.startswith("Test helper consumer: ")]
    if matrix_profile(profile) in {"full", "documentation"} or not changed:
        return None
    if all(path.startswith("tests/") and PurePosixPath(path).suffix == ".py" for path in changed):
        return profile
    return None


def candidate_reuse_profile(base, tested, root="."):
    """Identify exact focused coverage for product or test changes using immutable base policy."""
    profile = select_profile(base, tested, root)
    return profile if matrix_profile(profile) == "ci" or profile.startswith("selected:") else None


def selection_summary(profile, reasons):
    """Capture authoritative suite decisions and reasons without importing a report renderer."""

    def reported_suites(route):
        """Expose subgroups inside legacy parent commands without double-counting execution."""
        result = set(selected_suites(route))
        if "native" in result:
            result.update(suite for suite in PYTEST_AREAS if suite.startswith("unit-"))
        for parent, children in CONTAINED_SUITES.items():
            if parent in result:
                result.update(children)
        if "ha" in result:
            result.update(suite for suite in PYTEST_AREAS if suite.startswith("ha-"))
            result.add("provisioning")
        return result

    selected = reported_suites(profile)
    rows = []
    for suite in (
        *PYTEST_AREAS,
        "documentation",
        "ci",
        "native",
        "browser",
        "ha",
        "provisioning",
        "databases",
        "database-contracts",
        "database-migrations",
        "performance-queries",
        "supervisor",
    ):
        owner = suite
        evidence = [
            path
            for route, path in reasons
            if (
                route == "documentation"
                if suite == "documentation"
                else owner in reported_suites(route)
                or suite == "ci"
                and "native" in reported_suites(route)
            )
        ]
        required = profile == "documentation" if suite == "documentation" else owner in selected
        included_in = None
        if suite.startswith("unit-") and "native" in selected:
            included_in = "native"
        elif suite.startswith("ha-") and suite != "ha-contracts" and "ha" in selected:
            included_in = "ha"
        elif suite == "ci" and "native" in selected:
            included_in = "native"
        elif suite in APP_UNIT_SUITES and "unit-app" in selected:
            included_in = "unit-app"
        elif suite in {"database-contracts", "database-migrations"} and "databases" in selected:
            included_in = "databases"
        elif suite == "ha-solver" and "ha-controls" in selected:
            included_in = "ha-controls"
        elif suite == "documentation" and "native" in selected:
            included_in = "native"
        elif suite == "documentation" and "ci" in selected:
            included_in = "ci"
        rows.append(
            {"suite": suite, "required": required, "included_in": included_in, "reasons": evidence}
        )
    return {
        "profile": profile,
        "policy_base": environ.get("PR_BASE_SHA"),
        "tested_commit": environ.get("GITHUB_SHA"),
        "suites": rows,
    }


def main():
    """Emit one Actions output. Pushes, manual runs and unreadable event/Git data use full checks."""
    profile = "full"
    reasons = []
    if environ.get("GITHUB_EVENT_NAME") in {"pull_request", "push"}:
        try:
            event = json.loads(Path(environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
            base = (
                event["pull_request"]["base"]["sha"]
                if environ["GITHUB_EVENT_NAME"] == "pull_request"
                else event["before"]
            )
            selection = select_profile(base, environ.get("GITHUB_SHA"), reasons=reasons)
            profile = (
                selection
                if environ["GITHUB_EVENT_NAME"] == "pull_request"
                else add_performance_queries("full")
                if "performance-queries" in selected_suites(selection)
                else "full"
            )
        except (OSError, KeyError, TypeError, ValueError):
            pass
    tests_only = test_only_profile(None, None, profile=profile, reasons=reasons) is not None
    with Path(environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"documentation_only={str(profile == 'documentation').lower()}\n")
        output.write(f"test_only={str(tests_only).lower()}\n")
        output.write(f"profile={profile}\n")
        output.write(f"matrix_profile={matrix_profile(profile)}\n")
        targets = ["native"]
        if (
            "ha" in selected_suites(profile)
            or "ha-controls" in selected_suites(profile)
            or "ha-solver" in selected_suites(profile)
            or "provisioning" in selected_suites(profile)
        ):
            targets.append("app")
        if "ha" in selected_suites(profile) or any(
            suite.startswith("ha-") for suite in selected_suites(profile)
        ):
            targets.append("ha-tests")
        if "browser" in selected_suites(profile):
            targets.append("ui-tests")
        output.write(f"targets={','.join(targets)}\n")
    print(f"CI scope: {profile} checks")
    report = Path("results/ci/selection.json")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(
        json.dumps(
            {
                "profile": profile,
                "summary": selection_summary(profile, reasons),
                "matrix_profile": matrix_profile(profile),
                "performance_queries": "performance-queries" in selected_suites(profile),
                "test_only": tests_only,
                "selected": sorted(selected_suites(profile)),
                "policy_base": environ.get("PR_BASE_SHA"),
                "tested_commit": environ.get("GITHUB_SHA"),
                "reasons": [
                    {"profile": route, "path_or_reason": reason} for route, reason in reasons
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
