"""Keep the generic notification owner independent from product domains and persistent stores."""

import ast
import sys
from importlib.util import resolve_name
from pathlib import Path


def test_notification_core_imports_only_its_contract_standard_library_and_http_adapter():
    """Directory ownership cannot conceal domain, solver, HA or database dependencies."""
    root = Path(__file__).resolve().parents[3] / "src/open_energy_planner/notifications"
    namespace = "open_energy_planner.notifications"
    for path in root.rglob("*.py"):
        package = ".".join((namespace, *path.relative_to(root).parent.parts))
        for statement in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(statement, ast.Import):
                modules = [alias.name for alias in statement.names]
            elif isinstance(statement, ast.ImportFrom):
                modules = [
                    resolve_name("." * statement.level + (statement.module or ""), package)
                    if statement.level
                    else statement.module
                ]
            else:
                continue
            for module in modules:
                assert (
                    module == namespace
                    or module.startswith(namespace + ".")
                    or module.split(".")[0] in sys.stdlib_module_names
                    or (module == "aiohttp" and path.name == "api.py")
                ), (path, module)
