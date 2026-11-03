"""Bounded temporal reconstruction of hourly forecast means, independent of providers."""

from math import copysign


def limited_slope(previous_hourly_mean, current_hourly_mean, following_hourly_mean):
    """Return a minmod slope preserving the hourly mean, nonnegative rates and flat extrema."""
    preceding_change = current_hourly_mean - previous_hourly_mean
    succeeding_change = following_hourly_mean - current_hourly_mean
    return (
        copysign(
            min(abs(preceding_change), abs(succeeding_change), 2 * current_hourly_mean),
            preceding_change,
        )
        if preceding_change * succeeding_change > 0
        else 0.0
    )
