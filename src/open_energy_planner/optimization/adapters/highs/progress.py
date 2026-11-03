"""Observe financial search without changing objectives, limits or candidate admission."""

import logging
from math import isfinite
from time import perf_counter

from open_energy_planner.optimization.domain import SolveSample

MAX_PROGRESS_SAMPLES = 256
# Native objective/bound magnitudes at this currency threshold denote unavailable evidence.
NATIVE_INFINITY_THRESHOLD = 1e30
_LOGGER = logging.getLogger(__name__)


class FinancialProgress:
    """Keep a bounded history of incumbent and proof improvements, including first and latest."""

    def __init__(self, started):
        """Use the whole-solve monotonic origin, independent of repeated native runs."""
        self.started = started
        self.samples = []
        self.first_candidate_seconds = None

    def record(self, objective, bound, elapsed=None):
        """Ignore unavailable bounds and duplicate observations. Never manufacture a zero gap."""
        if not isfinite(objective) or abs(objective) >= NATIVE_INFINITY_THRESHOLD:
            return
        bound = float(bound) if isfinite(bound) and abs(bound) < NATIVE_INFINITY_THRESHOLD else None
        if self.samples and (objective, bound) == (
            self.samples[-1].objective,
            self.samples[-1].best_bound,
        ):
            return
        sample = SolveSample(
            perf_counter() - self.started if elapsed is None else elapsed,
            float(objective),
            bound,
        )
        if len(self.samples) >= MAX_PROGRESS_SAMPLES:
            self.samples[1:-1] = self.samples[1:-1:2]
        self.samples.append(sample)

    def observe(self, event):
        """Contain optional telemetry faults so no callback can interrupt native planning."""
        try:
            self.record(event.data_out.mip_primal_bound, event.data_out.mip_dual_bound)
            if self.samples and self.first_candidate_seconds is None:
                self.first_candidate_seconds = self.samples[0].elapsed_seconds
        except Exception:
            _LOGGER.debug("Financial progress sample unavailable", exc_info=True)

    def subscribe(self, native):
        """Register only after service priorities are fixed. Missing callbacks leave timings usable."""
        try:
            native.cbMipImprovingSolution.subscribe(self.observe)
            native.cbMipLogging.subscribe(self.observe)
        except Exception:
            _LOGGER.debug("Financial progress callbacks unavailable", exc_info=True)

    def finish(self, objective, bound):
        """Contain terminal observation failures exactly like callback failures."""
        try:
            self.record(objective, bound)
        except Exception:
            _LOGGER.debug("Final financial progress unavailable", exc_info=True)
