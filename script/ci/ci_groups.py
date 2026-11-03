"""Define pure suite membership and profile encoding without selecting or executing workloads."""

# Legacy names retain their historical coverage when emitted by an older PR base.
PROFILES = {
    "documentation": frozenset(),
    "ci": frozenset({"ci"}),
    "native": frozenset({"native"}),
    "frontend": frozenset({"native", "browser"}),
    "ha": frozenset({"native", "browser", "ha"}),
    "storage": frozenset({"native", "browser", "ha", "databases"}),
    "integration": frozenset({"native", "browser", "ha", "databases"}),
    "full": frozenset({"native", "browser", "ha", "databases", "supervisor"}),
}
APP_TEST_AREAS = (
    "appliance",
    "contract",
    "demand",
    "ev",
    "forecast",
    "history",
    "installation",
    "notifications",
    "planning",
    "price",
    "runtime",
    "savings",
    "solar",
    "solver_regression",
    "thermal",
)
APP_UNIT_SUITES = frozenset("unit-app-" + area.replace("_", "-") for area in APP_TEST_AREAS)
CONTAINED_SUITES = {
    "unit-app": APP_UNIT_SUITES,
    "ha-controls": frozenset({"ha-solver"}),
    "databases": frozenset({"database-contracts", "database-migrations"}),
}
PYTEST_AREAS = {
    "unit-app": ("tests/app",),
    **{"unit-app-" + area.replace("_", "-"): ("tests/app/" + area,) for area in APP_TEST_AREAS},
    "unit-ha-contract": ("tests/ha_contract",),
    "unit-component": ("tests/custom_component/unit",),
    "unit-frontend": ("tests/frontend/unit",),
    "unit-supervisor": ("tests/supervisor/unit",),
    "unit-provisioning": ("tests/provisioning/unit",),
    "unit-test-environment": ("tests/test_environment/unit",),
    "unit-release": ("tests/release",),
    "ha-contracts": ("tests/custom_component/integration/contracts",),
    "ha-connection": ("tests/custom_component/integration/connection",),
    "ha-controls": ("tests/custom_component/integration/controls",),
    "ha-solver": ("tests/custom_component/integration/controls/remote",),
    "ha-history": ("tests/custom_component/integration/history",),
    "ha-thermal": ("tests/custom_component/integration/thermal",),
    "ha-test-environment": ("tests/test_environment/integration",),
}
CONTAINED_SUITES["ha"] = frozenset(suite for suite in PYTEST_AREAS if suite.startswith("ha-")) | {
    "provisioning"
}

ATOMIC_SUITES = frozenset(PYTEST_AREAS) | {
    "ha",
    "ci",
    "browser",
    "databases",
    "database-contracts",
    "database-migrations",
    "performance-queries",
    "provisioning",
    "supervisor",
}
FUNCTIONAL_DATABASE_SUITES = ("databases", "database-contracts", "database-migrations")
HA_TEST_DIRECTORIES = ("tests/custom_component/integration", "tests/test_environment/integration")


def minimal_suites(suites):
    """Do not execute contained test directories twice when their complete parent is selected."""
    result = set(suites)
    for parent, children in CONTAINED_SUITES.items():
        if parent in result:
            result.difference_update(children)
    return frozenset(result)


def selected_suites(profile):
    """Decode canonical independent suites while preserving every legacy profile's meaning."""
    if isinstance(profile, str) and profile.endswith("+performance-queries"):
        parent = profile.removesuffix("+performance-queries")
        if parent not in PROFILES or parent == "documentation":
            raise ValueError("Unknown additive CI selection")
        return PROFILES[parent] | {"performance-queries"}
    if profile in PROFILES:
        return PROFILES[profile]
    if not isinstance(profile, str) or not profile.startswith("selected:"):
        raise ValueError("Unknown CI selection")
    names = profile.removeprefix("selected:").split(",")
    suites = frozenset(names)
    if "ci" not in suites or not suites <= ATOMIC_SUITES or names != sorted(minimal_suites(suites)):
        raise ValueError("Malformed or noncanonical CI selection")
    return suites


def focused_profile(*suites):
    """Describe a union of independent suites with the cheap policy and quality gates."""
    names = minimal_suites(set(suites) | {"ci"})
    result = "selected:" + ",".join(sorted(names))
    selected_suites(result)
    return result


def add_performance_queries(profile):
    """Add volume validation without changing the original functional matrix or its profile."""
    if "performance-queries" in selected_suites(profile):
        return profile
    if profile.startswith("selected:"):
        return focused_profile(*selected_suites(profile), "performance-queries")
    result = profile + "+performance-queries"
    selected_suites(result)
    return result


def matrix_profile(profile):
    """Return the original matrix selection without the independently added volume group."""
    suites = selected_suites(profile)
    if profile.startswith("selected:") and "performance-queries" in suites:
        return focused_profile(*(suites - {"performance-queries"}))
    return profile.removesuffix("+performance-queries")
