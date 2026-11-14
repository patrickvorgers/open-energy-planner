"""Keep measured running evidence distinct from authenticated physical execution feedback."""

from copy import deepcopy
from datetime import timedelta

from open_energy_planner.demand.calculation.model import digest

from .learning import SAMPLE_SECONDS
from .model import PROFILE_STEP_SECONDS, ApplianceWork, CycleProfile, instant

OBSERVATION_MAX_AGE_SECONDS = 2 * SAMPLE_SECONDS


def observe(settings, rows, cycles, activity, power_entity, now):
    """Persist a recent run or uncertainty. Only a quiet-bracketed measured cycle closes evidence.

    Historical batches never overwrite a newer observation. Unknown data never
    confirms completion. A learned profile is frozen when a new run is observed.
    This record cannot authorize starts or synthesize task feedback.
    """
    if not rows:
        return
    observed_until = instant(rows[-1]["at"]) + timedelta(seconds=SAMPLE_SECONDS)
    previous = settings.get("observed_run")
    if previous and observed_until <= instant(previous["observed_until"]):
        return
    if now - observed_until > timedelta(seconds=OBSERVATION_MAX_AGE_SECONDS):
        return
    if activity.get("running"):
        current = activity["running"]
        same = previous and previous["started_at"] == current["started_at"]
        settings["observed_run"] = {
            **current,
            "power_entity": power_entity,
            "profile": deepcopy(previous.get("profile") if same else settings.get("profile")),
        }
    elif previous and previous["status"] not in ("complete", "idle"):
        closed = any(start <= instant(previous["started_at"]) < end for start, end, _ in cycles)
        settings["observed_run"] = {
            **previous,
            "observed_until": observed_until.isoformat(),
            "status": "complete" if closed else "idle" if activity.get("quiet") else "uncertain",
        }


def work(entity, run, now):
    """Return a fixed remaining profile only while recent observed evidence supports its timing.

    Retain the measured start and frozen shape. Elapsed phases are integrated
    out by the existing fixed-work solver. Exhausted profiles and stale evidence
    retain ordinary forecasts, while unresolved observations still block starts.
    """
    value = run.get("profile")
    if not value or now - instant(run["observed_until"]) > timedelta(
        seconds=OBSERVATION_MAX_AGE_SECONDS
    ):
        return None
    cycle = CycleProfile(
        tuple(value["power_w"]),
        value["samples"],
        value["evidence"],
        value.get("measured_duration_seconds"),
        value.get("phase_seconds", PROFILE_STEP_SECONDS),
    )
    start = instant(run["started_at"])
    end = start + timedelta(seconds=cycle.duration_seconds)
    if end <= now:
        return None
    return ApplianceWork(
        "observed-" + digest([entity, run["started_at"]])[:40], entity, start, end, cycle, start
    )
