"""Plain aggregate-export bonus inputs and independent chronological settlement."""

from dataclasses import dataclass
from math import isfinite

MAX_BONUS_ID_LENGTH = 64
MAX_COUNTER_PERIODS = 1024


@dataclass(frozen=True)
class ExportBonus:
    """Prepared extra rates and caps per billing segment, counting all grid export.

    Period keys are absolute reset instants. Initial volume is measured/confirmed
    before the first segment. A new future period starts at zero. Rule changes
    inside one period may change the cap/rate but never reset actual volume.
    """

    rule_id: str
    rates: tuple[float, ...]
    caps_kwh: tuple[float, ...]
    periods: tuple[str, ...]
    initial_kwh: float

    def __post_init__(self):
        """Require bounded immutable aligned arrays and nonnegative finite monetary/energy inputs."""
        if not isinstance(self.rule_id, str) or not 1 <= len(self.rule_id) <= MAX_BONUS_ID_LENGTH:
            raise ValueError("Invalid bonus rule identity")
        if not all(
            isinstance(values, tuple) for values in (self.rates, self.caps_kwh, self.periods)
        ):
            raise ValueError("Bonus arrays must be immutable")
        if not 1 <= len(self.rates) <= MAX_COUNTER_PERIODS or not len(self.rates) == len(
            self.caps_kwh
        ) == len(self.periods):
            raise ValueError("Bonus segment arrays disagree")
        if any(
            type(value) not in (int, float) or not isfinite(value) or value < 0
            for value in (*self.rates, *self.caps_kwh, self.initial_kwh)
        ):
            raise ValueError("Bonus amounts must be finite and nonnegative")
        if not all(isinstance(key, str) and key for key in self.periods):
            raise ValueError("Bonus periods must be explicit")


@dataclass(frozen=True)
class ExportBonusResult:
    """Independent or extracted per-rule segment credits and uncapped export counters."""

    rule_id: str
    quota_kwh: tuple[float, ...]
    counters_kwh: tuple[float, ...]


def settle(bonus: ExportBonus, exported: tuple[float, ...]) -> ExportBonusResult:
    """Allocate the first exported volumes chronologically. Never choose a more profitable later slot.

    All export advances the counter, even when the bonus rate is zero. Counters
    remain uncapped so an increased cap does not lose previously exported volume.
    """
    counter = bonus.initial_kwh
    credits, counters = [], []
    for index, (volume, cap) in enumerate(zip(exported, bonus.caps_kwh, strict=True)):
        if index and bonus.periods[index] != bonus.periods[index - 1]:
            counter = 0
        credits.append(min(max(0, volume), max(0, cap - counter)))
        counter += max(0, volume)
        counters.append(counter)
    return ExportBonusResult(bonus.rule_id, tuple(credits), tuple(counters))
