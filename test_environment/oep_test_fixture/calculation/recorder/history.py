"""Construct detached fine/hourly Recorder documents without importing the HA framework."""

from datetime import timedelta

from ...policy import PARTS_PER_QUARTER, QUARTER_MINUTES, STATISTIC_MINUTES


def quarter_start(instant):
    """Align simulator sampling to the containing UTC quarter, preserving timezone awareness."""
    return instant.replace(
        minute=instant.minute // QUARTER_MINUTES * QUARTER_MINUTES, second=0, microsecond=0
    )


def rows(anchor, end, baseline, sample):
    """Integrate quarter kWh into native five-minute and hourly end-labelled cumulative statistics.

    Each Recorder row stores the counter at the end of its five-minute/hour
    period. No unfinished future period is imported. Quarter weights sum to four,
    preserving the existing hourly profile while exposing within-hour variation.
    """
    totals = dict(baseline)
    hourly = {key: [] for key in baseline}
    fine = {key: [] for key in baseline}
    cursor = anchor
    while cursor < end:
        delta = sample(cursor)
        for part in range(PARTS_PER_QUARTER):
            start = cursor + timedelta(minutes=part * STATISTIC_MINUTES)
            for key in baseline:
                totals[key] += delta[key] / PARTS_PER_QUARTER
                fine[key].append({"start": start, "sum": totals[key], "state": totals[key]})
        cursor += timedelta(minutes=QUARTER_MINUTES)
        if cursor.minute == 0:
            for key in baseline:
                hourly[key].append(
                    {"start": cursor - timedelta(hours=1), "sum": totals[key], "state": totals[key]}
                )
    return hourly, fine, totals


def align_statistics(statistics, offset):
    """Keep counter states and energy differences while matching Recorder's independent sum origin."""
    return [{**row, "sum": row["state"] + offset} for row in statistics]
