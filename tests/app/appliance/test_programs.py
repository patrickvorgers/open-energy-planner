"""Exercise opt-in and measured program selection through the normal appliance service."""

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from open_energy_planner.appliances.cycles.model import CycleProfile, restore_profile
from open_energy_planner.appliances.cycles.observed import work
from open_energy_planner.appliances.cycles.programs import average_program
from tests.app.appliance.support import ENTITY, NOW, POWER


def intervals(start, powers):
    """Generate consistent independent five-minute power and energy measurements."""
    energy = 5.0
    result = []
    for index, power in enumerate(powers):
        energy += power / 12000
        result.append(
            {
                "at": (start + timedelta(minutes=index * 5)).isoformat(),
                "power_w": power,
                "kwh": energy,
            }
        )
    return result


def learn_synthetic_programs(service, *, distinguishable=True):
    """Observe recurring ninety- and 180-minute programs on independent days."""
    short = [350] * 18
    long = [350] * 3 + [900] * 33 if distinguishable else [350] * 36
    for day, powers in enumerate((short, long, short, long, short, long)):
        rows = intervals(NOW - timedelta(days=10 - day), [0] * 12 + powers + [0] * 12)
        service.learn_history(ENTITY, POWER, rows, False, NOW)


def test_additional_profile_requires_opt_in_and_distinguishable_evidence(service):
    """Six complete cycles only expose an extra profile after a versioned opt-in."""
    learn_synthetic_programs(service)
    before = service.document()["devices"][0]
    assert "additional_profile" not in before
    assert not before.get("learn_additional_profile")
    with pytest.raises(ValueError, match="Reload"):
        service.configure_additional_profile(ENTITY, True, "outdated")
    service.configure_additional_profile(ENTITY, True, service.revision)
    after = service.document()["devices"][0]
    assert restore_profile(after["profile"]).duration_seconds == 90 * 60
    assert restore_profile(after["additional_profile"]).duration_seconds == 180 * 60
    assert after["additional_profile"]["samples"] == after["profile"]["samples"] == 3

    service.configure(ENTITY, True)
    deadline = NOW + timedelta(hours=5)
    explicit = service.submit(ENTITY, "choose-long", NOW, deadline, NOW, program="additional")
    assert restore_profile(explicit["profile"]).duration_seconds == 180 * 60
    assert "additional_profile" not in explicit
    assert (
        service.submit(ENTITY, "choose-long", NOW, deadline, NOW, program="additional")["id"]
        == explicit["id"]
    )
    with pytest.raises(ValueError, match="different times or program"):
        service.submit(ENTITY, "choose-long", NOW, deadline, NOW, program="default")

    service.configure_additional_profile(ENTITY, False, service.revision)
    disabled = service.document()["devices"][0]
    assert not disabled["learn_additional_profile"]
    assert "additional_profile" not in disabled
    assert restore_profile(disabled["profile"]) == restore_profile(before["profile"])
    assert restore_profile(disabled["task"]["profile"]) == restore_profile(explicit["profile"])


def test_program_average_preserves_short_peaks_and_recency_weights():
    """A twenty-second measured peak keeps its boundary and decays over a ninety-day half-life."""
    old = CycleProfile((1000, 400), phase_durations_seconds=(20, 880))
    new = CycleProfile((3600, 400), phase_durations_seconds=(20, 880))
    result = average_program([(NOW - timedelta(days=90), old), (NOW, new)])
    assert result.phase_boundaries == (0, 20, 900)
    assert result.power_w == pytest.approx(((500 + 3600) / 1.5, 400))


def test_manual_start_preserves_explicit_profile_on_accepted_readiness(service):
    """An observed hand-start before a controller request uses the user's frozen selection."""
    learn_synthetic_programs(service)
    service.configure_additional_profile(ENTITY, True, service.revision)
    service.configure(ENTITY, True)
    accepted = service.submit(
        ENTITY, "manual-start", NOW, NOW + timedelta(hours=5), NOW, program="additional"
    )

    measured = intervals(NOW - timedelta(minutes=75), [0] * 12 + [350] * 3)
    service.learn_history(ENTITY, POWER, measured, False, NOW)
    device = service.document()["devices"][0]
    assert device["task"]["status"] == "execution_observed"
    assert device["task"]["attempt"] is None
    assert device["observed_run"]["program"] == "additional"
    assert restore_profile(device["observed_run"]["profile"]) == restore_profile(
        accepted["profile"]
    )


def test_recognized_profile_keeps_confirmed_start_in_planning(service):
    """A statistical bucket beginning early cannot shift phases away from an authenticated start."""
    from open_energy_planner.installation.model import InstallationProblem
    from open_energy_planner.optimization.domain import Interval
    from open_energy_planner.planning import InputSnapshot

    learn_synthetic_programs(service)
    service.configure_additional_profile(ENTITY, True, service.revision)
    service.configure(ENTITY, True)
    accepted = service.submit(ENTITY, "confirmed-start", NOW, NOW + timedelta(hours=6), NOW)
    plan = {
        "plan_id": "program-plan",
        "valid_until": (NOW + timedelta(minutes=20)).isoformat(),
        "appliance_schedule": [{"task_id": accepted["id"], "start": NOW.isoformat()}],
    }
    service.accept_plan(plan)
    service.tick(NOW, True, plan)
    attempt = service.state["tasks"][ENTITY]["attempt"]
    actual_start = NOW + timedelta(seconds=20)
    service.feedback(ENTITY, accepted["id"], attempt["id"], "started", actual_start, actual_start)

    now = NOW + timedelta(minutes=30)
    measured = intervals(NOW - timedelta(hours=1), [0] * 12 + [350] * 3 + [900] * 3)
    service.learn_history(ENTITY, POWER, measured, False, now)
    assert service.observed(ENTITY)["program"] == "additional"

    steps = [
        {"start": (now + timedelta(hours=index)).isoformat(), "duration_h": 1, "kwh": 0.3}
        for index in range(5)
    ]
    device_forecast = {
        "issued_at": now.isoformat(),
        "steps": [{**step, "kwh": 0.1} for step in steps],
    }
    service.consumers.result = {
        "devices": [
            {
                "entity_id": ENTITY,
                "forecast": device_forecast,
                "baseline_forecast": device_forecast,
            }
        ],
        "residual_baseline": {"issued_at": now.isoformat(), "steps": steps},
    }
    problem = InstallationProblem(
        tuple(Interval(now + timedelta(hours=index), 1, 0.2, 0, 0.4) for index in range(5)),
        (),
        (),
        (),
        3000,
        3000,
    )
    snapshot = InputSnapshot(
        "snapshot", "configuration", now, "measurement", "forecast", "prices", problem
    )
    [prepared] = service.prepare(snapshot, SimpleNamespace(), now).problem.appliances
    assert prepared.fixed_start == actual_start
    assert prepared.profile.duration_seconds == 180 * 60


def test_hand_start_uses_only_profiles_available_at_readiness(service):
    """An unselected hand-start keeps its acknowledged default when the learner changes before start."""
    service.configure_additional_profile(ENTITY, True, service.revision)
    service.set_manual_profile(ENTITY, 80, 0.6, service.revision)
    service.configure(ENTITY, True)
    accepted = service.submit(ENTITY, "before-training", NOW, NOW + timedelta(hours=5), NOW)
    learn_synthetic_programs(service)
    assert "additional_profile" in service.document()["devices"][0]

    measured = intervals(NOW - timedelta(minutes=90), [0] * 12 + [350] * 3 + [900] * 3)
    service.learn_history(ENTITY, POWER, measured, False, NOW)
    run = service.observed(ENTITY)
    assert "additional_profile" not in run and "program" not in run
    assert restore_profile(run["profile"]) == restore_profile(accepted["profile"])


def test_program_http_errors_keep_their_owned_explanations(service):
    """Expected profile failures keep their actionable messages across the HTTP boundary."""
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    from open_energy_planner.activities.api import EVENTS
    from open_energy_planner.activities.events import Events
    from open_energy_planner.appliances.cycles.api import register
    from open_energy_planner.storage.adapters.activities import ActivityStore

    async def scenario():
        """Use the real route admission and repository while observing only its public response."""

        async def refresh_async(**_):
            """Accept the planning contract without starting a calculation in this test."""

        app = web.Application()
        events = Events(ActivityStore(service.documents.engine))
        app[EVENTS] = events
        service.configure(ENTITY, True)
        service.set_manual_profile(ENTITY, 90, 0.6, service.revision)
        register(
            app,
            service,
            SimpleNamespace(state={"enabled": False, "plan": None}),
            SimpleNamespace(refresh_async=refresh_async),
            asyncio.Lock(),
            SimpleNamespace(state={"points": []}),
            SimpleNamespace(state={"stores": []}),
        )
        now = datetime.now(UTC)
        ready = {
            "entity_id": ENTITY,
            "request_id": "program-request",
            "ready_at": now.isoformat(),
            "deadline": (now + timedelta(hours=5)).isoformat(),
        }
        async with TestClient(TestServer(app)) as client:
            endpoint = "/v1/appliances/tasks"
            for program, detail in (
                ("other", "Choose the default or additional program"),
                ("additional", "No additional program has been learned for this appliance"),
            ):
                response = await client.post(endpoint, json={**ready, "program": program})
                assert response.status == 400
                assert await response.text() == detail
            response = await client.post(
                "/v1/appliances/profile_options",
                json={
                    "entity_id": ENTITY,
                    "learn_additional_profile": "enabled",
                    "expected_version": service.revision,
                },
            )
            assert response.status == 400
            assert await response.text() == "Additional profile learning must be boolean"
            response = await client.post(endpoint, json={**ready, "program": "default"})
            assert response.status == 200
            response = await client.post(endpoint, json=ready)
            assert response.status == 400
            assert (
                await response.text() == "Readiness identity reused with different times or program"
            )
        await events.close()

    asyncio.run(scenario())


def test_indistinguishable_prefix_keeps_one_profile(service):
    """Distinct lengths alone cannot unlock live recognition for identical power starts."""
    service.configure_additional_profile(ENTITY, True, service.revision)
    learn_synthetic_programs(service, distinguishable=False)
    device = service.document()["devices"][0]
    assert "additional_profile" not in device
    assert device["learn_additional_profile"] is True


@pytest.mark.parametrize(
    ("later_power", "program", "duration_minutes"),
    [(350, "default", 90), (900, "additional", 180)],
)
def test_running_cycle_defaults_then_uses_measured_power(
    service, later_power, program, duration_minutes
):
    """An ambiguous start stays on the default, then measured power updates only remaining work."""
    learn_synthetic_programs(service)
    service.configure_additional_profile(ENTITY, True, service.revision)
    start = NOW - timedelta(minutes=15)
    prefix_start = start - timedelta(hours=1)
    initial = intervals(prefix_start, [0] * 12 + [350] * 3)
    service.learn_history(ENTITY, POWER, initial, False, NOW)
    first = service.observed(ENTITY)
    assert "program" not in first
    assert restore_profile(first["profile"]).duration_seconds == 90 * 60

    later = intervals(prefix_start, [0] * 12 + [350] * 3 + [later_power] * 3)
    service.learn_history(ENTITY, POWER, later, False, NOW + timedelta(minutes=15))
    matched = service.observed(ENTITY)
    assert matched["program"] == program
    assert matched["started_at"] == first["started_at"]
    remaining = work(ENTITY, matched, NOW + timedelta(minutes=15))
    assert remaining.fixed_start == start
    assert remaining.deadline == start + timedelta(minutes=duration_minutes)
    assert "additional_profile" not in matched
    assert restore_profile(service.document()["devices"][0]["profile"]).duration_seconds == 90 * 60
