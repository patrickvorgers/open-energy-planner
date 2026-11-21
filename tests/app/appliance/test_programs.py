"""Exercise opt-in and measured program selection through the normal appliance service."""

from datetime import timedelta

import pytest

from open_energy_planner.appliances.cycles.model import restore_profile
from open_energy_planner.appliances.cycles.observed import work
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
