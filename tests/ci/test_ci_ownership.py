"""Keep ordinary domain/provisioning code outside direct Supervisor lifecycle control."""

import ast
import importlib.util
import re
import sys
from pathlib import Path, PurePosixPath

import pytest

from script.ci.ci_scope import (
    CALCULATION_DIRECTORIES,
    DOMAIN_DIRECTORIES,
    NATIVE_SOLVER_DIRECTORIES,
    PROOF_CI_FILES,
    path_profile,
    selected_suites,
)

ROOT = Path(__file__).resolve().parents[2]
READ_ONLY_FORBIDDEN_ATTRIBUTES = frozenset(
    {
        "insert",
        "update",
        "delete",
        "create",
        "drop",
        "create_all",
        "drop_all",
        "exec_driver_sql",
        "executemany",
        "executescript",
        "cursor",
        "raw_connection",
        "get_raw_connection",
        "driver_connection",
        "connection",
        "commit",
        "rollback",
        "begin",
        "begin_nested",
        "begin_twophase",
        "into",
        "write",
    }
)
LIFECYCLE_MODULES = {
    "open_energy_planner.app",
    "open_energy_planner.live_inputs",
    "homeassistant",
    "custom_components",
    "open_energy_planner.supervisor",
    "open_energy_planner.discovery",
    "open_energy_planner.ingress",
    "open_energy_planner.pairing",
    "open_energy_planner.runtime",
    "script.runtime",
    "script.supervisor",
    "script.supervisor.provision_supervisor",
    "script.supervisor.supervisor_smoke",
    "script.supervisor.check_supervisor",
    "script.supervisor.dev",
}


CALCULATION_FORBIDDEN_MODULES = LIFECYCLE_MODULES | {
    "aiohttp",
    "homeassistant",
    "sqlalchemy",
    "sqlite3",
    "psycopg",
    "psycopg2",
    "pymysql",
    "mysql.connector",
    "script",
    "requests",
    "httpx",
    "importlib",
    "__import__",
    "eval",
    "exec",
    "open",
    "builtins",
    "os",
    "io",
    "pathlib",
    "socket",
    "subprocess",
}


NATIVE_SOLVER_FORBIDDEN_MODULES = (CALCULATION_FORBIDDEN_MODULES - {"highspy"}) | {
    "open_energy_planner.storage",
    "open_energy_planner.home_assistant",
    "open_energy_planner.frontend",
}


# Only reviewed exports may cross into owners with conservative CI coverage.
PROOF_CAPABILITIES = {
    "script.ci.proof_git": {
        "VerificationError",
        "sha",
        "fetch_pr_history",
        "introduced_commits",
        "base_test_only_profile",
        "checkout_tree",
        "merged_tree",
        "immutable_test_sources",
        "raw_changes",
    },
    "script.ci.ci_scope": {"matrix_profile", "select_profile", "selected_suites"},
    "subprocess": {"CalledProcessError", "SubprocessError"},
    "pytest": {"hookimpl"},
    "pathlib": {"Path", "PurePosixPath"},
    "os": {"environ"},
    "sys": {"exit", "path", "stderr", "stdout"},
}
PROOF_STDLIB = {
    "argparse",
    "ast",
    "datetime",
    "http",
    "json",
    "pathlib",
    "re",
    "sys",
    "urllib",
    "xml",
}
PROOF_DYNAMIC_NAMES = {
    "__import__",
    "eval",
    "exec",
    "compile",
    "getattr",
    "setattr",
    "globals",
    "locals",
    "vars",
    "modules",
    "load_module",
    "exec_module",
    "os",
    "_os",
    "_sys",
    "subprocess",
    "importlib",
}
PROOF_EXECUTION_ATTRIBUTES = PROOF_DYNAMIC_NAMES | {
    "system",
    "popen",
    "spawn",
    "fork",
    "execv",
    "execve",
    "execl",
    "execlp",
    "execvp",
    "posix_spawn",
    "startfile",
    "run",
    "Popen",
    "check_call",
    "check_output",
    "connect",
    "cursor",
    "execute",
}


def unreviewed_query_execution(node, tree):
    """Permit execute access only as a direct call with a reviewed inline Select expression."""
    if not isinstance(node, ast.Attribute) or node.attr != "execute":
        return False
    call = next(
        (
            candidate
            for candidate in ast.walk(tree)
            if isinstance(candidate, ast.Call) and candidate.func is node
        ),
        None,
    )
    statement = call.args[0] if call is not None and call.args else None
    while isinstance(statement, ast.Call) and isinstance(statement.func, ast.Attribute):
        statement = statement.func.value
    select_names = {
        alias.asname or alias.name
        for imported in ast.walk(tree)
        if isinstance(imported, ast.ImportFrom) and imported.module == "sqlalchemy"
        for alias in imported.names
        if alias.name == "select"
    }
    return not (
        isinstance(statement, ast.Call)
        and isinstance(statement.func, ast.Name)
        and statement.func.id in select_names
    )


def lifecycle_dependencies(
    source,
    module,
    sources=None,
    visited=None,
    *,
    ingress_reader=False,
    diagnostics_reader=False,
    environment_reader=False,
    query_reader=False,
    proof_reader=False,
    forbidden=None,
):
    """Statically follow imports, implicit ancestor packages and forbidden dependency access.

    Args:
        source: Python source to parse without importing production modules.
        module: Qualified name used to resolve relative imports.
        sources: Available internal module sources and their qualified names.
        visited: Modules already traversed to avoid cycles.
        ingress_reader: Permit only the prefix read from the ingress module.
        diagnostics_reader: Permit the reviewed process diagnostics area, never platform startup.
        environment_reader: Permit importing only os.environ for native worker settings.
        query_reader: Permit only the reviewed SQLAlchemy select import for read-only projections.
        proof_reader: Restrict metadata owners to reviewed imports and fixed process adapters.
        forbidden: Module, builtin or attribute names to reject. Defaults to platform lifecycle owners.

    Dynamic imports beyond the checked forbidden builtins are not proven absent.
    """
    sources = {} if sources is None else sources
    forbidden = LIFECYCLE_MODULES if forbidden is None else forbidden
    visited = set() if visited is None else visited
    if module in visited:
        return []
    visited.add(module)
    violations = []
    package_parts = module.removesuffix(".__init__").split(".")
    for depth in range(1, len(package_parts)):
        ancestor = ".".join(package_parts[:depth])
        if ancestor in sources:
            ancestor_source, ancestor_module = sources[ancestor]
            violations.extend(
                lifecycle_dependencies(
                    ancestor_source,
                    ancestor_module,
                    sources,
                    visited,
                    ingress_reader=ingress_reader,
                    diagnostics_reader=diagnostics_reader,
                    environment_reader=environment_reader,
                    query_reader=query_reader,
                    proof_reader=proof_reader,
                    forbidden=forbidden,
                )
            )
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = importlib.util.resolve_name(
                    "." * node.level + base, module.rpartition(".")[0]
                )
            names = [base, *(base + "." + alias.name for alias in node.names)]
            # Rendering may consume the existing trust decision, never own it.
            if (
                ingress_reader
                and base == "open_energy_planner.ingress"
                and all(alias.name == "prefix" for alias in node.names)
            ):
                names = []
            if (
                environment_reader
                and base == "os"
                and all(alias.name == "environ" for alias in node.names)
            ):
                names = []
            if (
                query_reader
                and base == "sqlalchemy"
                and all(alias.name == "select" for alias in node.names)
            ):
                names = []
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in forbidden
        ):
            names = [node.func.id]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "text"
            and ("sqlalchemy" in forbidden or "sqlalchemy.text" in forbidden)
        ):
            # Raw SQL does not qualify for the read-only owner, regardless of import alias.
            names = ["sqlalchemy.text"]
        elif (
            (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "execute"
                and node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and ("sqlalchemy" in forbidden or "sqlalchemy.text" in forbidden)
            )
            or query_reader
            and unreviewed_query_execution(node, tree)
        ):
            names = ["sqlalchemy.text"]
        elif isinstance(node, ast.Attribute) and node.attr in forbidden:
            names = [node.attr]
        else:
            names = []
        if proof_reader:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.ImportFrom) and any(
                    alias.name in PROOF_DYNAMIC_NAMES for alias in node.names
                ):
                    violations.append(base + ": execution module export")
                if isinstance(node, ast.ImportFrom) and base in PROOF_CAPABILITIES:
                    if not all(alias.name in PROOF_CAPABILITIES[base] for alias in node.names):
                        violations.append(base + ": unreviewed capability")
                    # These owners keep broad CI and own their own process dependencies.
                    if not any(
                        base == owner or base.startswith(owner + ".") for owner in forbidden
                    ):
                        names = [base.rpartition(".")[0]] if base.startswith("script.") else []
                else:
                    for name in names:
                        if name.split(".")[0] not in PROOF_STDLIB and not (
                            isinstance(node, ast.Import)
                            and name in sources
                            or isinstance(node, ast.ImportFrom)
                            and base in sources
                        ):
                            violations.append(name + ": unreviewed import")
            if isinstance(node, ast.Name) and node.id in PROOF_DYNAMIC_NAMES:
                violations.append(node.id + ": dynamic execution")
            if isinstance(node, ast.Attribute) and (
                node.attr in PROOF_EXECUTION_ATTRIBUTES
                or node.attr.startswith(("exec", "spawn", "popen", "fork"))
                or node.attr.startswith("__")
                and node.attr != "__init__"
            ):
                violations.append(node.attr + ": execution access")
        names = [
            "sqlalchemy.text" if name.startswith("sqlalchemy.") and name.endswith(".text") else name
            for name in names
        ]
        violations.extend(
            name
            for name in names
            if any(name == owner or name.startswith(owner + ".") for owner in forbidden)
            and not (
                diagnostics_reader
                and (
                    name == "open_energy_planner.runtime.diagnostics"
                    or name.startswith("open_energy_planner.runtime.diagnostics.")
                )
            )
        )
        for name in names:
            imported_parts = name.split(".")
            for depth in range(1, len(imported_parts) + 1):
                imported_name = ".".join(imported_parts[:depth])
                if imported_name in sources:
                    imported_source, imported_module = sources[imported_name]
                    violations.extend(
                        lifecycle_dependencies(
                            imported_source,
                            imported_module,
                            sources,
                            visited,
                            ingress_reader=ingress_reader,
                            diagnostics_reader=diagnostics_reader,
                            environment_reader=environment_reader,
                            query_reader=query_reader,
                            proof_reader=proof_reader,
                            forbidden=forbidden,
                        )
                    )
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and (
                node.value in {"SUPERVISOR_TOKEN", "supervisor/api"}
                or node.value.startswith("http://supervisor")
            )
        ):
            violations.append(node.value)
    return violations


def test_domains_and_example_provisioning_do_not_own_platform_control():
    """New files in an established area cannot silently introduce direct Supervisor dependencies."""
    directories = [ROOT / "src/open_energy_planner" / name for name in DOMAIN_DIRECTORIES]
    directories.append(ROOT / "script/provisioning")
    directories.append(ROOT / "src/open_energy_planner/home_assistant")
    directories.append(ROOT / "src/open_energy_planner/frontend")
    directories.append(ROOT / "src/open_energy_planner/runtime/diagnostics")
    sources = {}
    for parent in (ROOT / "src", ROOT):
        package = parent / ("open_energy_planner" if parent.name == "src" else "script")
        for path in package.rglob("*.py"):
            module = path.relative_to(parent).with_suffix("").as_posix().replace("/", ".")
            sources[module.removesuffix(".__init__")] = (path.read_text(encoding="utf-8"), module)
    failures = []
    for directory in directories:
        for path in directory.rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix().removeprefix("src/")
            module = relative.removesuffix(".py").replace("/", ".")
            dependencies = lifecycle_dependencies(
                path.read_text(encoding="utf-8"),
                module,
                sources,
                ingress_reader=relative.startswith("open_energy_planner/frontend/"),
                diagnostics_reader=True,
            )
            if dependencies:
                failures.append((relative, dependencies))
    assert not failures, failures


def test_ownership_guard_recognizes_relative_and_parent_imports():
    """Both relative platform imports and imports from the package root must trigger review."""
    assert lifecycle_dependencies("from .. import supervisor", "open_energy_planner.appliances.new")
    assert lifecycle_dependencies(
        "from script.supervisor import provision_supervisor", "script.provisioning.new"
    )
    assert lifecycle_dependencies(
        "from ..supervisor import connect", "open_energy_planner.appliances.new"
    )
    assert not lifecycle_dependencies(
        "from .model import Profile", "open_energy_planner.appliances.new"
    )


def test_indirect_platform_import_cannot_hide_behind_an_ordinary_helper():
    """A new domain file importing an apparently ordinary helper still exposes its Supervisor dependency."""
    sources = {
        "open_energy_planner.shared_helper": (
            "from .runtime.entrypoint import main",
            "open_energy_planner.shared_helper",
        ),
    }
    assert lifecycle_dependencies(
        "from ..shared_helper import prepare", "open_energy_planner.appliances.new", sources
    )
    assert lifecycle_dependencies('command = {"type": "supervisor/api"}', "script.provisioning.new")


def test_frontend_can_read_ingress_prefix_but_cannot_own_platform_control():
    """New renderers can read the trusted prefix without importing trust setters or Supervisor helpers."""
    assert not lifecycle_dependencies(
        "from ..ingress import prefix", "open_energy_planner.frontend.new", ingress_reader=True
    )
    for source in (
        "from ..ingress import INGRESS_PEER",
        "import open_energy_planner.ingress",
        "from ..supervisor import connect",
    ):
        assert lifecycle_dependencies(
            source, "open_energy_planner.frontend.new", ingress_reader=True
        )


def test_native_calculation_and_solver_areas_keep_independent_boundaries():
    """New native files keep focused coverage only while transitive dependencies remain outside storage and HA transports."""
    production = ROOT / "src"
    sources = {}
    for path in production.rglob("*.py"):
        module = path.relative_to(production).with_suffix("").as_posix().replace("/", ".")
        sources[module.removesuffix(".__init__")] = (path.read_text(encoding="utf-8"), module)
    storage_modules = {module for module in sources if "storage" in module.split(".")}
    failures = []
    for directory in (*CALCULATION_DIRECTORIES, *NATIVE_SOLVER_DIRECTORIES):
        forbidden = (
            NATIVE_SOLVER_FORBIDDEN_MODULES
            if directory in NATIVE_SOLVER_DIRECTORIES
            else CALCULATION_FORBIDDEN_MODULES
        ) | storage_modules
        for path in (production / "open_energy_planner" / directory).rglob("*.py"):
            module = path.relative_to(production).with_suffix("").as_posix().replace("/", ".")
            dependencies = lifecycle_dependencies(
                path.read_text(encoding="utf-8"),
                module,
                sources,
                environment_reader=directory in NATIVE_SOLVER_DIRECTORIES,
                forbidden=forbidden,
            )
            if dependencies:
                failures.append((path.relative_to(production).as_posix(), dependencies))
    assert not failures, failures


def test_calculation_guard_detects_hidden_storage_through_a_new_helper():
    """A new helper cannot introduce an indirect database dependency under the cheap calculation rule."""
    sources = {
        "open_energy_planner.shared": (
            "from .storage.adapters.documents import SqlDocuments",
            "open_energy_planner.shared",
        ),
    }
    assert lifecycle_dependencies(
        "from ...shared import prepare",
        "open_energy_planner.solar.calculation.future",
        sources,
        forbidden={"open_energy_planner.storage"},
    )


def test_calculation_guard_includes_implicit_package_initialization():
    """Root, calculation ancestors and transitively imported packages cannot hide platform dependencies."""
    for ancestor in (
        "open_energy_planner",
        "open_energy_planner.contracts",
        "open_energy_planner.helpers",
    ):
        sources = {
            ancestor: ("import sqlalchemy", ancestor + ".__init__"),
            "open_energy_planner.helpers.value": ("value = 1", "open_energy_planner.helpers.value"),
        }
        assert lifecycle_dependencies(
            "from ...helpers.value import value",
            "open_energy_planner.contracts.calculation.formula",
            sources,
            forbidden={"sqlalchemy"},
        ), ancestor


def test_calculation_guard_rejects_direct_database_drivers():
    """Calculation files cannot bypass storage ownership by importing a database driver directly."""
    for module in ("sqlite3", "psycopg", "psycopg2", "pymysql", "mysql.connector"):
        assert lifecycle_dependencies(
            "import " + module,
            "open_energy_planner.contracts.calculation.future",
            forbidden=CALCULATION_FORBIDDEN_MODULES,
        ), module


def test_calculation_owners_share_their_selection_and_dependency_guard():
    """Every reviewed domain calculation area inherits cheap tests and the same import prohibition."""
    for directory in CALCULATION_DIRECTORIES:
        path = PurePosixPath("src/open_energy_planner") / directory / "future.py"
        assert selected_suites(path_profile(path)) == {"ci", "unit-app"}


def test_calculation_guard_rejects_private_file_writes():
    """A new math helper cannot hide bootstrap-file replacement or network/process IO behind cheap ownership."""
    for source in (
        'open("archive-backend.json", "w")',
        'import builtins; builtins.open("archive-backend.json", "w")',
        "import os; os.replace(source, target)",
        "from pathlib import Path; Path(target).write_text(value)",
        "from subprocess import run; run(command)",
    ):
        assert lifecycle_dependencies(
            source,
            "open_energy_planner.installation.calculation.future",
            forbidden=CALCULATION_FORBIDDEN_MODULES,
        )


def writes_private_file(source):
    """Recognize direct OS writes and explicit writable open flags used by private bootstrap owners."""
    for node in ast.walk(ast.parse(source)):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
        ):
            continue
        if node.func.attr in {"replace", "fsync", "write"}:
            return True
        if node.func.attr == "open" and any(
            isinstance(flag, ast.Attribute)
            and isinstance(flag.value, ast.Name)
            and flag.value.id == "os"
            and flag.attr in {"O_WRONLY", "O_RDWR", "O_CREAT", "O_TRUNC", "O_APPEND"}
            for flag in ast.walk(node)
        ):
            return True
    return False


def test_private_bootstrap_writers_keep_real_lifecycle_acceptance():
    """Atomic bootstrap writes remain under an installation owner, including future helpers."""
    writers = []
    for path in (ROOT / "src/open_energy_planner").rglob("*.py"):
        if writes_private_file(path.read_text()):
            writers.append(path)
            suites = selected_suites(path_profile(PurePosixPath(path.relative_to(ROOT).as_posix())))
            assert "supervisor" in suites, path
    assert writers


def test_diagnostics_permission_cannot_hide_platform_startup():
    """Domain logging may read diagnostics, but diagnostics importing startup still require lifecycle ownership."""
    module = "open_energy_planner.runtime.diagnostics.logging"
    source = "from open_energy_planner.runtime.diagnostics.logging import register_lifecycle"
    assert not lifecycle_dependencies(
        source, "open_energy_planner.demand.future", diagnostics_reader=True
    )
    assert lifecycle_dependencies(
        source,
        "open_energy_planner.demand.future",
        {module: ("from open_energy_planner.runtime.entrypoint import main", module)},
        diagnostics_reader=True,
    )
    assert lifecycle_dependencies(
        source,
        "open_energy_planner.demand.calculation.future",
        forbidden=CALCULATION_FORBIDDEN_MODULES,
    )


def test_native_solver_guard_rejects_indirect_storage_and_ha_transports():
    """A future solver helper cannot hide database or transport coupling behind an apparently ordinary import."""
    for target in (
        "open_energy_planner.storage.adapters.documents",
        "open_energy_planner.forecast_quality.storage",
        "open_energy_planner.home_assistant.catalogue",
        "open_energy_planner.frontend.assets",
        "sqlalchemy",
        "sqlite3",
        "aiohttp",
        "homeassistant",
    ):
        helper = "open_energy_planner.optimization.future_helper"
        sources = {helper: ("import " + target, helper), target: ("", target)}
        forbidden = NATIVE_SOLVER_FORBIDDEN_MODULES | {
            module for module in sources if "storage" in module.split(".")
        }
        assert lifecycle_dependencies(
            "from open_energy_planner.optimization.future_helper import result",
            "open_energy_planner.optimization.adapters.highs.future",
            sources,
            forbidden=forbidden,
        ), target


def test_native_solver_environment_access_cannot_grant_file_writes():
    """The worker can configure its child environment without acquiring descriptor or file-write capabilities."""
    module = "open_energy_planner.optimization.adapters.process.future"
    assert not lifecycle_dependencies(
        'from os import environ as process_environment; process_environment["OMP_NUM_THREADS"] = "1"',
        module,
        environment_reader=True,
        forbidden=NATIVE_SOLVER_FORBIDDEN_MODULES,
    )
    for source in (
        "import os; os.open(path, os.O_WRONLY | os.O_CREAT)",
        "import os as system; system.write(descriptor, data)",
        "from os import open as create_file",
        "from os import write",
        "from os import environ, open",
    ):
        assert lifecycle_dependencies(
            source,
            module,
            environment_reader=True,
            forbidden=NATIVE_SOLVER_FORBIDDEN_MODULES,
        ), source


def test_private_writer_detection_keeps_descriptor_writes_under_lifecycle_coverage():
    """Writable descriptors, replacement and flushes require lifecycle ownership. A known read-only descriptor does not."""
    for source in (
        "os.open(path, os.O_WRONLY | os.O_CREAT)",
        "os.open(path, flags=os.O_RDWR)",
        "os.write(descriptor, data)",
        "os.fsync(descriptor)",
        "os.replace(pending, target)",
    ):
        assert writes_private_file(source), source
    assert not writes_private_file("os.open(path, os.O_RDONLY)")


def test_forecast_admission_policy_has_no_runtime_or_transport_dependencies():
    """The cheap shared freshness policy cannot acquire storage, HA or process behavior."""
    path = ROOT / "src/open_energy_planner/installation/forecast_policy.py"
    production = ROOT / "src"
    sources = {}
    for source_path in production.rglob("*.py"):
        module = source_path.relative_to(production).with_suffix("").as_posix().replace("/", ".")
        sources[module.removesuffix(".__init__")] = (
            source_path.read_text(encoding="utf-8"),
            module,
        )
    assert not lifecycle_dependencies(
        path.read_text(encoding="utf-8"),
        "open_energy_planner.installation.forecast_policy",
        sources=sources,
        forbidden=CALCULATION_FORBIDDEN_MODULES,
    )


def test_domains_cannot_reach_back_into_app_composition():
    """A direct or transitive app import cannot silently broaden a domain's responsibility."""
    assert lifecycle_dependencies(
        "from ..app import create_app", "open_energy_planner.solar.future"
    )
    assert lifecycle_dependencies(
        "from ..helper import live",
        "open_energy_planner.solar.future",
        {
            "open_energy_planner.helper": (
                "from .live_inputs import LiveInputs",
                "open_energy_planner.helper",
            )
        },
    )


def query_writes(source):
    """Recognize SQL, schema and raw-connection mutation entry points in read-only query owners."""
    forbidden = READ_ONLY_FORBIDDEN_ATTRIBUTES | {"text"}
    violations = []
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in forbidden:
            violations.append(node.attr)
        elif unreviewed_query_execution(node, tree):
            violations.append("execute")
    return violations


def test_read_only_query_owners_cannot_acquire_writes_or_runtime_adapters():
    """Skipping transfers requires read-only SQL and no transitive storage writer or framework dependency."""
    forbidden = (
        CALCULATION_FORBIDDEN_MODULES
        | {"open_energy_planner.storage.adapters"}
        | READ_ONLY_FORBIDDEN_ATTRIBUTES
    )
    production = ROOT / "src"
    sources = {}
    for path in production.rglob("*.py"):
        module = path.relative_to(production).with_suffix("").as_posix().replace("/", ".")
        sources[module.removesuffix(".__init__")] = (path.read_text(encoding="utf-8"), module)
    owners = []
    for path in (production / "open_energy_planner").rglob("*.py"):
        if not {"storage", "queries"} <= set(path.parts):
            continue
        owners.append(path)
        source = path.read_text(encoding="utf-8")
        module = path.relative_to(production).with_suffix("").as_posix().replace("/", ".")
        assert not query_writes(source), path
        assert not lifecycle_dependencies(
            source, module, sources, forbidden=forbidden, query_reader=True
        ), path
    assert owners


def test_read_only_query_guard_rejects_mutation_and_hidden_sql_builders():
    """A write call or aliased SQL update cannot silently inherit the query-only selection."""
    assert set(query_writes("connection.execute(table.update().values(price=1))")) == {
        "execute",
        "update",
    }
    assert query_writes("schema.drop_all(engine)") == ["drop_all"]
    assert lifecycle_dependencies(
        "from sqlalchemy import update as change_rows",
        "open_energy_planner.prices.storage.queries.future",
        forbidden={"sqlalchemy.update"},
    )


def test_forecast_policy_guard_rejects_a_hidden_storage_helper(tmp_path, monkeypatch):
    """A pure-looking internal import cannot hide database work behind the forecast policy route."""
    import pytest

    package = tmp_path / "src/open_energy_planner/installation"
    package.mkdir(parents=True)
    (package / "forecast_policy.py").write_text("from .helper import prepare\n", encoding="utf-8")
    (package / "helper.py").write_text("import sqlalchemy\n", encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        test_forecast_admission_policy_has_no_runtime_or_transport_dependencies()


def test_read_only_query_guard_follows_mutation_calls_in_internal_helpers():
    """A helper without ORM imports must not hide a write through the borrowed connection."""
    assert lifecycle_dependencies(
        "from .helper import read",
        "open_energy_planner.prices.storage.adapters.queries.future",
        {
            "open_energy_planner.prices.storage.adapters.queries.helper": (
                "def read(connection, table):\n    connection.execute(table.update())\n",
                "open_energy_planner.prices.storage.adapters.queries.helper",
            )
        },
        forbidden={"update"},
    ) == ["update"]


@pytest.mark.parametrize(
    "source,query_writes_detects",
    [
        pytest.param(
            'import sqlalchemy as sa\nconnection.execute(sa.text("DELETE FROM retained"))',
            True,
            id="module-alias",
        ),
        pytest.param(
            'from sqlalchemy import text as statement\nconnection.execute(statement("DELETE FROM retained"))',
            True,
            id="constructor-alias",
        ),
        pytest.param(
            'from sqlalchemy.sql.expression import text as statement\nconnection.execute(statement("DELETE FROM retained"))',
            True,
            id="expression-alias",
        ),
        pytest.param('connection.execute("DELETE FROM retained")', True, id="literal"),
    ],
)
def test_read_only_guard_rejects_aliased_or_literal_raw_sql(source, query_writes_detects):
    """Raw SQL cannot bypass write detection through a SQLAlchemy alias or a literal execute call."""
    assert lifecycle_dependencies(
        source,
        "open_energy_planner.prices.storage.adapters.queries.future",
        forbidden={"sqlalchemy.text"},
    )
    assert bool(query_writes(source)) is query_writes_detects


@pytest.mark.parametrize("transaction_method", ["begin_nested", "begin_twophase"])
def test_read_only_guard_rejects_transaction_entry_points(
    transaction_method, tmp_path, monkeypatch
):
    """Nested and distributed transaction ownership cannot inherit the read-only query route."""
    source = f"connection.{transaction_method}()"
    assert query_writes(source) == [transaction_method]
    assert lifecycle_dependencies(
        source,
        "open_energy_planner.prices.storage.adapters.queries.future",
        forbidden={transaction_method},
    ) == [transaction_method]

    package = tmp_path / "src/open_energy_planner/prices"
    query = package / "storage/adapters/queries/future.py"
    query.parent.mkdir(parents=True)
    query.write_text(
        "from open_energy_planner.prices.query_helper import start\n", encoding="utf-8"
    )
    (package / "query_helper.py").write_text(
        f"def start(connection):\n    connection.{transaction_method}()\n", encoding="utf-8"
    )
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError, match=re.escape(str(query))):
        test_read_only_query_owners_cannot_acquire_writes_or_runtime_adapters()


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy.dialects.sqlite import insert as sqlite_upsert\nconnection.execute(sqlite_upsert(table).values(value=1))",
        "from sqlalchemy.dialects.postgresql import insert as postgres_upsert\nconnection.execute(postgres_upsert(table).values(value=1))",
        "from sqlalchemy.sql.dml import Insert as statement\nconnection.execute(statement(table).values(value=1))",
        "import sqlalchemy as database\nconnection.execute(database.Insert(table).values(value=1))",
        "mutator = table.insert\nconnection.execute(mutator().values(value=1))",
        'connection.connection.cursor().execute("DELETE FROM retained")',
        "cursor = connection.connection.cursor()\ncursor.execute(statement_text)",
        "def read(driver, statement_text):\n    driver.execute(statement_text)",
        "def read(driver, statement_text):\n    execute = driver.execute\n    execute(statement_text)",
        "from sqlalchemy import select\nconnection.execute(select(table).into('snapshot'))",
    ],
)
def test_read_only_query_guard_rejects_hidden_write_constructors(tmp_path, monkeypatch, source):
    """A dialect alias, DML class or saved table method cannot enter through an internal helper."""
    package = tmp_path / "src/open_energy_planner/prices"
    query = package / "storage/adapters/queries/future.py"
    query.parent.mkdir(parents=True)
    query.write_text("from open_energy_planner.prices.query_helper import read\n", encoding="utf-8")
    (package / "query_helper.py").write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    with pytest.raises(AssertionError, match=re.escape(str(query))):
        test_read_only_query_owners_cannot_acquire_writes_or_runtime_adapters()


def test_read_only_query_guard_accepts_only_the_reviewed_select_import():
    """The supported read constructor may be aliased without exposing SQLAlchemy write APIs."""
    assert not lifecycle_dependencies(
        "from sqlalchemy import select as read\nconnection.execute(read(table))",
        "open_energy_planner.prices.storage.adapters.queries.future",
        forbidden={"sqlalchemy"},
        query_reader=True,
    )
    assert not query_writes(
        "from sqlalchemy import select as read\nconnection.execute(read(table))"
    )


def test_proof_owners_only_consume_reviewed_metadata_capabilities():
    """Cheap proof modules and transitive helpers cannot acquire workload execution dependencies."""
    sources = {}
    for parent, package in ((ROOT, "script"), (ROOT / "src", "open_energy_planner")):
        for path in (parent / package).rglob("*.py"):
            module = path.relative_to(parent).with_suffix("").as_posix().replace("/", ".")
            sources[module.removesuffix(".__init__")] = (path.read_text(encoding="utf-8"), module)
    failures = {}
    for name in PROOF_CI_FILES:
        module = name.removesuffix(".py").replace("/", ".")
        if module not in sources:
            continue
        violations = lifecycle_dependencies(
            sources[module][0], module, sources, proof_reader=True, forbidden=set()
        )
        if violations:
            failures[name] = violations
    assert not failures, failures


@pytest.mark.parametrize(
    "source",
    [
        "import sqlalchemy",
        "import sqlite3",
        "import docker",
        "import subprocess",
        "from subprocess import run",
        "from os import system",
        "import os; os.system(command)",
        "import os as platform; platform.system(command)",
        "import importlib; importlib.import_module(name)",
        "__import__(name)",
        "eval(source)",
        "exec(source)",
        "getattr(value, name)(command)",
        "import runpy",
        "from script.ci.proof_git import subprocess",
        "import script.ci.proof_git",
        "from script.ci.ci_scope import subprocess",
        "from script.ci.proof_git import *",
        "import sys; sys.modules[name]",
        "from script.missing import read",
        "from pathlib import os as platform; platform.system(command)",
        "from pathlib import os as platform; platform.execvpe(command, args, env)",
        "import pathlib as paths; paths.os.execvpe(command, args, env)",
        "from urllib.request import os as platform; platform.startfile(command)",
        "def launch(platform):\n    platform.execvpe(command, args, env)",
        "def use(database):\n    database.execute(statement)",
    ],
)
def test_proof_guard_rejects_execution_escape(source):
    """Direct workload imports, process launch and dynamic import escapes fail the cheap boundary."""
    assert lifecycle_dependencies(source, "script.ci.future", proof_reader=True, forbidden=set())


@pytest.mark.parametrize("helper", ["script", "script.ci", "script.ci.helper"])
def test_proof_guard_follows_helpers_and_ancestor_initializers(helper):
    """An innocent import cannot hide database access in its helper or implicit package initializer."""
    sources = {
        "script.ci.helper": ("", "script.ci.helper"),
        helper: ("import sqlite3", helper),
    }
    violations = lifecycle_dependencies(
        "from .helper import read",
        "script.ci.future",
        sources,
        proof_reader=True,
        forbidden=set(),
    )
    assert "sqlite3: unreviewed import" in violations


def test_proof_guard_accepts_pure_helper_cycles_and_fixed_git_capabilities():
    """Pure transitive helpers may cycle and consume reviewed metadata operations without workloads."""
    sources = {
        "script": ("", "script.__init__"),
        "script.ci": ("", "script.ci.__init__"),
        "script.ci.helper": (
            "from .peer import value\nfrom .proof_git import sha\ndef read(value):\n    return sha(value)\n",
            "script.ci.helper",
        ),
        "script.ci.peer": ("from .helper import read\nvalue = 1", "script.ci.peer"),
    }
    assert not lifecycle_dependencies(
        "from .helper import read",
        "script.ci.future",
        sources,
        proof_reader=True,
        forbidden=set(),
    )


@pytest.mark.parametrize(
    "source",
    [
        'from pathlib import Path as FilePath; name = FilePath("metadata.json").name',
        'import pathlib as paths; name = paths.Path("metadata.json").name',
    ],
)
def test_proof_guard_preserves_nonprocess_path_operations(source):
    """Ordinary path exports and module aliases remain available without leaking process modules."""
    assert not lifecycle_dependencies(
        source, "script.ci.future", proof_reader=True, forbidden=set()
    )


def pure_ci_dependencies(source, module, sources=None):
    """Pure suite data and HA collection plugins cannot acquire metadata process capabilities."""
    return lifecycle_dependencies(
        source,
        module,
        sources,
        proof_reader=True,
        forbidden={"script.ci.proof_git", "script.ci.ci_scope", "os", "sys", "http", "urllib"},
    )


def test_pure_ci_group_and_ha_owners_have_no_process_or_workload_dependencies():
    """Group definitions and HA partition/argument rules remain pure through every local helper."""
    sources = {}
    for path in (ROOT / "script").rglob("*.py"):
        module = path.relative_to(ROOT).with_suffix("").as_posix().replace("/", ".")
        sources[module.removesuffix(".__init__")] = (path.read_text(encoding="utf-8"), module)
    for module in ("script.ci.ci_groups", "script.ci.ha_suites", "script.ci.ha_shard"):
        assert not pure_ci_dependencies(sources[module][0], module, sources), module


@pytest.mark.parametrize(
    "source",
    [
        "from script.ci.proof_git import raw_changes",
        "from script.ci.ci_scope import select_profile",
        "from os import environ",
        "import subprocess",
        "import docker",
        "import sqlalchemy",
        "from pytest import main",
        "import pytest as tests; tests.main(arguments)",
        "from pathlib import os as platform; platform.execvpe(command, args, env)",
    ],
)
def test_pure_ci_owner_rejects_execution_including_reviewed_metadata_capabilities(source):
    """A capability allowed for metadata verification is still forbidden in pure suite/HA configuration."""
    assert pure_ci_dependencies(source, "script.ci.future")


def test_pure_ci_guard_follows_process_capabilities_through_helpers_and_packages():
    """Neither an innocuous helper nor package initialization can import the protected Git process owner."""
    for owner in ("script", "script.ci", "script.ci.helper"):
        sources = {
            "script.ci.helper": ("", "script.ci.helper"),
            owner: ("from script.ci.proof_git import raw_changes", owner),
        }
        violations = pure_ci_dependencies("from .helper import read", "script.ci.future", sources)
        assert "script.ci.proof_git" in violations, owner
