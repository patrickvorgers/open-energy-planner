"""Track bounded periods of genuinely observed HA power reports independently of Recorder holds."""

from datetime import timedelta
from math import isfinite

from .model import MAX_CYCLE_POWER_W, instant

MAX_REPORT_GAP_SECONDS = 600
RETENTION_HOURS = 14


def advance(previous, body, *, continuous):
    """Extend coverage only within one live observer session and ten-minute report/capture gaps.

    The first capture establishes an observation boundary, never retrospective
    coverage. App restarts, HA reloads and invalid reports start a new period.
    Replayed captures cannot extend coverage or erase a recorded interruption.
    """
    captured = instant(body["end"])
    session = body.get("observer_session")
    if session is not None and (not isinstance(session, str) or not 1 <= len(session) <= 64):
        raise ValueError("Invalid appliance observer session")
    previous = previous or {}
    if previous.get("captured_at") and captured <= instant(previous["captured_at"]):
        return previous
    windows = [
        window
        for window in previous.get("windows", [])
        if instant(window[1]) >= captured - timedelta(hours=RETENTION_HOURS)
    ]
    reported = instant(body["power_reported_at"]) if body.get("power_reported_at") else None
    valid = (
        session is not None
        and reported is not None
        and timedelta(0) <= captured - reported <= timedelta(seconds=MAX_REPORT_GAP_SECONDS)
    )
    value = body["power_states"][-1] if body["power_states"] else None
    try:
        power = float(value["state"]) * {"W": 1, "kW": 1000}[value["unit"]]
        usable = isfinite(power) and 0 <= power <= MAX_CYCLE_POWER_W
    except (TypeError, KeyError, ValueError, OverflowError):
        usable = False
    if not usable:
        valid = False
    extends = (
        valid
        and continuous
        and previous.get("session") == session
        and previous.get("power_entity") == body["power_entity"]
        and previous.get("valid")
        and windows
        and timedelta(0)
        < captured - instant(previous["captured_at"])
        <= timedelta(seconds=MAX_REPORT_GAP_SECONDS)
        and timedelta(0)
        <= reported - instant(previous["reported_at"])
        <= timedelta(seconds=MAX_REPORT_GAP_SECONDS)
    )
    if valid:
        if extends:
            windows[-1] = [windows[-1][0], max(instant(windows[-1][1]), reported).isoformat()]
        else:
            windows.append([captured.isoformat(), captured.isoformat()])
    return {
        "power_entity": body["power_entity"],
        "session": session,
        "captured_at": captured.isoformat(),
        "reported_at": reported.isoformat() if reported else None,
        "valid": valid,
        "windows": windows[-168:],
    }


def covers(coverage, start, end):
    """Require one uninterrupted observation period covering the run and its quiet boundaries."""
    return any(
        instant(first) <= start and end <= instant(last)
        for first, last in coverage.get("windows", [])
    )
