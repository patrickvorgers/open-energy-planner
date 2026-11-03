"""Bound generated fine-history imports only inside isolated functional test chains."""

from contextlib import ExitStack, contextmanager
from datetime import timedelta
from unittest.mock import patch

from homeassistant.util.dt import utcnow

from test_environment.oep_test_fixture import consumer, dishwasher, ev, existing, runtime, thermal
from test_environment.oep_test_fixture.history import import_fine as import_full_history

FINE_HISTORY_LOOKBACK = timedelta(days=2)


async def import_recent_fixture_history(hass, metadata, statistics, hourly=None):
    """Keep complete recent fine hours and all original hourly evidence without mutating inputs.

    Only simulator-generator aliases use this wrapper. The canonical native
    importer, its validation and the persistent developer lab remain unchanged.
    The last fine point is retained, preserving the native sum-origin alignment.
    """
    fine_cutoff = utcnow().replace(minute=0, second=0, microsecond=0) - FINE_HISTORY_LOOKBACK
    recent_statistics = [row for row in statistics if row["start"] >= fine_cutoff]
    if statistics and not recent_statistics:
        recent_statistics = statistics[-1:]
    await import_full_history(hass, metadata, recent_statistics, hourly)


@contextmanager
def compact_fixture_history():
    """Restore every generator binding after the isolated chain, including nested HA instances."""
    with ExitStack() as stack:
        for generator_module in (consumer, dishwasher, ev, existing, runtime, thermal):
            stack.enter_context(
                patch.object(generator_module, "import_fine", import_recent_fixture_history)
            )
        yield
