"""Initialize interactive simulator examples once without replaying user intent on warm starts."""

import asyncio
import json
import os
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from math import isclose, isfinite
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

from aiohttp import ClientResponseError, ClientSession, ClientTimeout

ROOT = Path(__file__).resolve().parents[2]
PROFILE_FILE = ROOT / "test_environment/oep_test_fixture/appliance_profiles.json"
QUARTER_MINUTES = 15
REQUEST_TIMEOUT_SECONDS = 15
EXAMPLE_HISTORY_DAYS = 28
FIXTURE_READY_ATTEMPTS = 60
FIXTURE_POLL_SECONDS = 2
LEARNING_SAMPLE_MINUTES = 5
SECONDS_PER_DAY = 86400
SECONDS_PER_HOUR = 3600
WATTS_PER_KW = 1000
LEARNING_SAMPLES_PER_DAY = SECONDS_PER_DAY // (LEARNING_SAMPLE_MINUTES * 60)


class ExampleJournal:
    """Retain bootstrap receipts outside product settings so removed examples stay removed."""

    def __init__(self, path):
        """Restore a private provisioning journal, refusing ambiguous or linked state."""
        self.path = Path(path)
        if any(parent.is_symlink() for parent in (self.path, *self.path.parents)):
            raise ValueError("Example journal cannot use symlinks")
        self.state = (
            json.loads(self.path.read_text())
            if self.path.exists()
            else {"version": 1, "completed": [], "appliances": {}}
        )
        if (
            set(self.state) != {"version", "completed", "appliances"}
            or self.state["version"] != 1
            or not isinstance(self.state["completed"], list)
            or not isinstance(self.state["appliances"], dict)
        ):
            raise ValueError("Unreadable example provisioning state")

    def save(self):
        """Atomically replace only this technical receipt after each successful boundary."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(self.state, output)
        try:
            temporary.chmod(0o600)
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def complete(self, *keys):
        """Atomically record related ownership/completion receipts or deliberately preserved defaults."""
        missing = [key for key in keys if key not in self.state["completed"]]
        if missing:
            self.state["completed"].extend(missing)
            self.save()

    def discard(self, key):
        """Withdraw an attempted write receipt when the product explicitly rejects that write."""
        if key in self.state["completed"]:
            self.state["completed"].remove(key)
            self.save()


def identity(name):
    """Keep lab UUIDs stable across failed provisioning and fixture restarts."""
    return str(uuid5(NAMESPACE_URL, "open-energy-planner:development:" + name))


def zonneplan_contract(timezone, now=None):
    """Use the documented 2026 example, starting inside available simulated export history."""
    now = now or datetime.now(UTC)
    start = (
        now.astimezone(ZoneInfo(timezone)).date() - timedelta(days=EXAMPLE_HISTORY_DAYS)
    ).isoformat()
    return {
        "contract_id": "interactive-test-only",
        "market_area": "NL",
        "currency": "EUR",
        "periods": [
            {
                "effective_from": start,
                "effective_until": None,
                "contract_start": start,
                "import_formula": "(market + 0.09161) * 1.21 + 0.02",
                "export_formula": "market + 0.02",
            }
        ],
        "bonuses": [
            {
                "effective_from": start,
                "rate_formula": "0.10 * (market + 0.02)",
                "price_condition": {
                    "expression": "market + 0.02",
                    "operator": "greater_than",
                    "threshold": "0",
                },
                "cap_kwh": 7500,
                "timezone": timezone,
                "reset_month": 1,
                "reset_day": 1,
                "window": "daylight",
                "start_time": None,
                "end_time": None,
            }
        ],
    }


def unchanged_original_contract(document):
    """Recognize only the former exact, unmodified default, never a user's contract."""
    periods = document.get("periods", [])
    return (
        document.get("contract_id") == "interactive-test-only"
        and document.get("market_area") == "NL"
        and document.get("currency") == "EUR"
        and not document.get("bonuses")
        and len(periods) == 1
        and periods[0].get("effective_from") == "2026-01-01T00:00:00+00:00"
        and periods[0].get("effective_until") is None
        and not periods[0].get("contract_start")
        and periods[0].get("import_formula") == "(market + 0.10) * 1.21"
        and periods[0].get("export_formula") == "market - 0.02"
    )


BOOTSTRAP_POWER_TOLERANCE_W = 0.000001


def original_cycle_profile(profile, powers):
    """Recognize the old single synthetic bootstrap sample despite harmless integration rounding."""
    if (
        not isinstance(profile, dict)
        or type(profile.get("samples")) is not int
        or profile.get("samples") != 1
        or profile.get("evidence") != "synthetic"
    ):
        return False
    actual = profile.get("power_w")
    return (
        isinstance(actual, (list, tuple))
        and len(actual) == len(powers)
        and all(
            type(value) in (int, float)
            and isfinite(value)
            and isclose(value, expected, rel_tol=0, abs_tol=BOOTSTRAP_POWER_TOLERANCE_W)
            for value, expected in zip(actual, powers, strict=True)
        )
    )


def learning_example(entity, powers, now, *, cycles=2):
    """Generate labelled five-minute evidence for the real detector, including quiet brackets.

    These inputs are explicitly simulated, not claims about previously captured
    Recorder states. No learned profile or accepted cycle boundary is injected.
    """
    end = now.replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=cycles)
    total, rows = 50.0, []
    for index in range(cycles * LEARNING_SAMPLES_PER_DAY):
        at = start + timedelta(minutes=LEARNING_SAMPLE_MINUTES * index)
        phase = ((at - start).total_seconds() % SECONDS_PER_DAY - SECONDS_PER_HOUR) // (
            QUARTER_MINUTES * 60
        )
        power = powers[int(phase)] if 0 <= phase < len(powers) else 0
        total += power * LEARNING_SAMPLE_MINUTES / 60 / WATTS_PER_KW
        rows.append({"at": at.isoformat(), "power_w": power, "kwh": total})
    return {
        "entity_id": entity,
        "power_entity": entity.removesuffix("_energy") + "_power",
        "rows": rows,
        "synthetic": True,
    }


def ev_configuration():
    """Configure two explicitly identified cars and shared points, initially retaining disconnection."""
    vehicles, points = [], []
    for suffix, capacity, maximum, departure in (
        ("a", 60, 11000, "07:00"),
        ("b", 40, 7400, "08:00"),
    ):
        vehicles.append(
            {
                "id": identity("car-" + suffix),
                "soc_entity": f"sensor.oep_test_car_{suffix}_soc",
                "capacity_kwh": capacity,
                "maximum_w": maximum,
                "target_soc": 80,
                "departure_time": departure,
            }
        )
        points.append(
            {
                "id": identity("wallbox-" + suffix),
                "energy_entity": f"sensor.oep_test_wallbox_{suffix}_energy",
                "power_entity": f"sensor.oep_test_wallbox_{suffix}_power",
                "connected_entity": f"binary_sensor.oep_test_wallbox_{suffix}_connected",
                "minimum_w": 1380,
                "maximum_w": maximum,
                "control": "variable",
                "vehicle_ids": [identity("car-a"), identity("car-b")],
            }
        )
    return {"vehicles": vehicles, "points": points}


def boiler_configuration():
    """Match the declared 200-litre simulator and its independent morning/evening hot-water draw."""
    draw = [0.0] * 24
    draw[7], draw[19] = 1.4, 1.8
    return {
        "stores": [
            {
                "id": identity("boiler"),
                "energy_entity": "sensor.oep_test_boiler_energy",
                "temperature_entity": "sensor.oep_test_boiler_temperature",
                "power_entity": "sensor.oep_test_boiler_power",
                "volume_litres": 200,
                "minimum_c": 45,
                "maximum_c": 65,
                "ambient_c": 20,
                "loss_w_per_c": 1,
                "heater_w": 2000,
                "draw_profile_kwh": draw,
                "enabled": True,
            }
        ]
    }


def unchanged_ev_configuration(document):
    """Recover only the intended simulator bindings/settings, ignoring computed telemetry and names."""
    for section, intended in ev_configuration().items():
        saved = document[section]
        if len(saved) != len(intended):
            return False
        for expected in intended:
            actual = next((row for row in saved if row["id"] == expected["id"]), {})
            if any(actual.get(field) != value for field, value in expected.items()):
                return False
    return all(
        not row.get("departure_override") and not row.get("soc_timestamp_attribute")
        for row in document["vehicles"]
    )


async def ensure_source_choices(request, scope, required):
    """Wait for complete scoped fixture choices without replacing active Energy metadata.

    Reuse an in-flight request or usable result. A fresh request is needed after
    candidate expiry, including when profile provisioning delayed the save.
    Failed or incomplete results never authorize configuration writes.
    """
    path = "/v1/ha/source-choices/" + scope
    status = await request("GET", path)

    def missing_sources(status):
        """Check only fixed simulator identities in the product's complete choice result."""
        metadata = (status.get("catalogue") or {}).get("entities", [])
        return required - {source["entity_id"] for source in metadata}

    if (
        status.get("pending") is False
        and status.get("failed") is False
        and not missing_sources(status)
    ):
        return
    if status.get("pending") is not True:
        await request("POST", path, {})
    for attempt in range(FIXTURE_READY_ATTEMPTS):
        status = await request("GET", path)
        if status.get("failed") is True:
            raise ValueError(
                "Development example source choices failed. Refresh the fixture sources"
            )
        if status.get("pending") is False and status.get("failed") is False:
            if missing_sources(status):
                raise ValueError("Development example source choices lack required fixture sensors")
            return
        if attempt < FIXTURE_READY_ATTEMPTS - 1:
            await asyncio.sleep(FIXTURE_POLL_SECONDS)
    raise ValueError("Development example source choices did not complete. Check the HA connection")


async def configure(request, journal, now=None):
    """Use normal product APIs to fill only unowned simulator examples and retain all existing work."""
    now = now or datetime.now(UTC)
    completed = journal.state["completed"]

    async def initialize(section, method, path, body):
        """Separate attempted writes from confirmed ownership, preserving version-conflict winners."""
        journal.complete("attempted:" + section)
        try:
            result = await request(method, path, body)
        except ClientResponseError as error:
            if 400 <= error.status < 500:
                journal.discard("attempted:" + section)
            raise
        journal.complete("owns:" + section, section)
        return result

    profiles = json.loads(PROFILE_FILE.read_text())
    if {
        "consumers",
        "ev",
        "thermal",
        "bonus",
        *("appliance:" + name for name in profiles),
        *("automatic-learning-v1:" + name for name in profiles),
    } <= set(completed):
        return
    setup = await request("GET", "/v1/setup")
    if any(setup.get(key) is None for key in ("installation", "solar", "demand")):
        print("Device examples deferred. Complete the preserved partial installation first.")
        return
    selected = [
        {"entity_id": "sensor.oep_test_heat_pump_energy", "weather_sensitive": True},
        *(
            {"entity_id": f"sensor.oep_test_{name}_energy", "weather_sensitive": False}
            for name in profiles
        ),
    ]
    required = {row["entity_id"] for row in selected} | {
        "sensor.oep_test_wallbox_a_energy",
        "sensor.oep_test_wallbox_b_energy",
        "sensor.oep_test_boiler_energy",
    }
    choice_sources = {
        scope: {
            entity_id
            for configurations in configuration.values()
            for device in configurations
            for field, entity_id in device.items()
            if field.endswith("_entity")
        }
        for scope, configuration in (
            ("ev", ev_configuration()),
            ("thermal", boiler_configuration()),
        )
    }
    for attempt in range(FIXTURE_READY_ATTEMPTS):
        consumers = await request("GET", "/v1/consumers")
        setup = await request("GET", "/v1/setup")
        missing = required - {option["entity_id"] for option in consumers["options"]}
        if not missing:
            break
        if attempt < FIXTURE_READY_ATTEMPTS - 1:
            # A fresh foundation catalogue can predate asynchronously registered device fixtures.
            await request("GET", "/v1/setup?refresh=1")
            await asyncio.sleep(FIXTURE_POLL_SECONDS)
    else:
        raise ValueError(
            "Development examples require the complete simulated Energy fixture. Missing: "
            + ", ".join(sorted(missing))
        )
    for scope, sections in (("ev", ("vehicles", "points")), ("thermal", ("stores",))):
        if scope in completed or "attempted:" + scope in completed:
            continue
        document = await request("GET", "/v1/" + scope)
        configuration = document if scope == "ev" else document["configuration"]
        if not any(configuration[section] for section in sections):
            await ensure_source_choices(request, scope, choice_sources[scope])
    if "consumers" not in completed:
        # Empty state after an ambiguous write may be an intentional user removal.
        if not consumers["selected"] and "attempted:consumers" not in completed:
            consumers = await initialize(
                "consumers",
                "PUT",
                "/v1/consumers",
                {"selected": selected, "expected_version": consumers["version"]},
            )
        elif "attempted:consumers" in completed and sorted(
            consumers["selected"], key=lambda row: row["entity_id"]
        ) == sorted(selected, key=lambda row: row["entity_id"]):
            journal.complete("owns:consumers", "consumers")
        journal.complete("consumers")
    available = {row["entity_id"] for row in consumers["selected"]}
    power_bindings = {row["entity_id"]: row.get("power_entity") for row in consumers["options"]}
    for name, example in profiles.items():
        key, entity = "appliance:" + name, f"sensor.oep_test_{name}_energy"
        if key in completed:
            continue
        document = await request("GET", "/v1/appliances")
        existing = next((row for row in document["devices"] if row["entity_id"] == entity), None)
        if (
            "owns:consumers" not in completed
            or entity not in available
            or power_bindings.get(entity) != f"sensor.oep_test_{name}_power"
            or (existing and entity not in journal.state["appliances"])
        ):
            journal.complete(key)
            continue
        if entity not in journal.state["appliances"]:
            journal.state["appliances"][entity] = learning_example(
                entity, example["power_w"], now, cycles=1 if name == "dryer" else 2
            )
            journal.save()
        if existing is None:
            await request("POST", "/v1/appliances/device", {"entity_id": entity, "shiftable": True})
        if existing is None or (
            existing.get("shiftable") and not existing.get("task") and not existing.get("profile")
        ):
            if name == "dryer":
                current = await request("GET", "/v1/appliances")
                await request(
                    "POST",
                    "/v1/appliances/manual_profile",
                    {
                        "entity_id": entity,
                        "duration_minutes": 180,
                        "energy_kwh": 1.4,
                        "expected_version": current["revision"],
                    },
                )
            evidence = journal.state["appliances"][entity]
            if "rows" not in evidence:
                evidence = learning_example(
                    entity, example["power_w"], now, cycles=1 if name == "dryer" else 2
                )
            await request("POST", "/v1/appliances/history", evidence)
        journal.complete(key)
    for name, example in profiles.items():
        key = "automatic-learning-v1:" + name
        if key in completed:
            continue
        entity = f"sensor.oep_test_{name}_energy"
        document = await request("GET", "/v1/appliances")
        existing = next((row for row in document["devices"] if row["entity_id"] == entity), None)
        if (
            "owns:consumers" in completed
            and entity in available
            and power_bindings.get(entity) == f"sensor.oep_test_{name}_power"
            and existing
            and existing.get("shiftable")
            and not existing.get("task")
            and original_cycle_profile(existing.get("profile"), example["power_w"])
            and not existing.get("manual_profile")
        ):
            # Only the exact former bootstrap default is eligible. User profiles stay untouched.
            if name == "dryer":
                await request(
                    "POST",
                    "/v1/appliances/manual_profile",
                    {
                        "entity_id": entity,
                        "duration_minutes": 180,
                        "energy_kwh": 1.4,
                        "expected_version": document["revision"],
                    },
                )
            await request(
                "POST",
                "/v1/appliances/history",
                learning_example(
                    entity, example["power_w"], now, cycles=1 if name == "dryer" else 2
                ),
            )
        journal.complete(key)
    if "ev" not in completed:
        document = await request("GET", "/v1/ev")
        if not document["vehicles"] and not document["points"] and "attempted:ev" not in completed:
            await ensure_source_choices(request, "ev", choice_sources["ev"])
            await initialize(
                "ev",
                "POST",
                "/v1/ev/configuration",
                {
                    "configuration": ev_configuration(),
                    "expected_version": document["configuration_version"],
                },
            )
        elif "attempted:ev" in completed and unchanged_ev_configuration(document):
            journal.complete("owns:ev", "ev")
        journal.complete("ev")
    if "thermal" not in completed:
        document = await request("GET", "/v1/thermal")
        if not document["configuration"]["stores"] and "attempted:thermal" not in completed:
            await ensure_source_choices(request, "thermal", choice_sources["thermal"])
            await initialize(
                "thermal",
                "POST",
                "/v1/thermal/configuration",
                {
                    "configuration": boiler_configuration(),
                    "expected_version": document["configuration_version"],
                },
            )
        elif (
            "attempted:thermal" in completed and document["configuration"] == boiler_configuration()
        ):
            journal.complete("owns:thermal", "thermal")
        journal.complete("thermal")
    if "bonus" not in completed:
        contract = await request("GET", "/v1/contracts")
        if unchanged_original_contract(contract):
            setup = await request("GET", "/v1/setup")
            candidate = zonneplan_contract(setup["catalogue"]["data"]["timezone"], now)
            await request(
                "PUT", "/v1/contracts", {**candidate, "expected_version": contract["version"]}
            )
        journal.complete("bonus")


@asynccontextmanager
async def product_requests(*, app_url, token):
    """Keep a bounded private product API session available across dependent bootstrap steps."""
    async with ClientSession(
        headers={"Authorization": "Bearer " + token},
        timeout=ClientTimeout(total=REQUEST_TIMEOUT_SECONDS),
    ) as session:

        async def request(method, path, body=None):
            """Require successful bounded product responses without logging authentication data."""
            async with session.request(
                method, app_url + path, **({"json": body} if body is not None else {})
            ) as response:
                response.raise_for_status()
                return await response.json()

        yield request


async def provision(*, app_url, token, journal_path):
    """Authenticate privately against the already isolated app without dispatching sample tasks."""
    async with product_requests(app_url=app_url, token=token) as request:
        await configure(request, ExampleJournal(journal_path))
