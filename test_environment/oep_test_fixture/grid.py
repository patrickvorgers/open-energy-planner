"""One incremental AC grid meter for both laboratories. Counters never reallocate earlier energy."""

from datetime import datetime, timedelta

from .calculation.recorder.history import quarter_start
from .policy import QUARTER_MINUTES

GRID_ROLES = ("grid_import", "grid_export")
QUARTER_SECONDS = QUARTER_MINUTES * 60


class LiveGridMeter:
    """Accumulate signed interval energy into monotonically increasing import/export meters in kWh."""

    def __init__(self, roles, units, sample, data):
        """Borrow a pure baseline sampler and a durable detached counter snapshot."""
        self.roles = {entity: role for entity, role in roles.items() if role in GRID_ROLES}
        self.units, self.sample, self.data = units, sample, data

    def advance(self, now, dishwasher_total=None, dishwasher_initial=None, additional_totals=None):
        """Allocate only new measured consumption, splitting elapsed baseline means at quarter edges.

        The fixture's observed cumulative delta is a mean over this observation
        interval, never reapplied to another quarter. Counter direction changes
        therefore cannot reduce any previously published cumulative meter.
        """
        previous = datetime.fromisoformat(self.data["at"])
        if now <= previous:
            return self.values()
        if dishwasher_total is None and self.data["dishwasher_kwh"] is not None:
            return self.values()
        before = self.data["dishwasher_kwh"]
        if before is None:
            before = dishwasher_initial
        extra = (
            dishwasher_total - before if dishwasher_total is not None and before is not None else 0
        )
        if extra < 0:
            raise ValueError(
                "Simulated dishwasher counter reset. Restore its matching fixture state"
            )
        consumers = dict(self.data.get("additional_consumers", {}))
        for identity, (total, initial) in (additional_totals or {}).items():
            delta = total - consumers.get(identity, initial)
            if delta < 0:
                raise ValueError(
                    "Simulated consumer counter reset. Restore its matching fixture state"
                )
            extra += delta
            consumers[identity] = total
        duration = (now - previous).total_seconds()
        while previous < now:
            end = min(now, quarter_start(previous) + timedelta(minutes=QUARTER_MINUTES))
            seconds = (end - previous).total_seconds()
            baseline = self.sample(quarter_start(previous))
            net = (
                sum(
                    amount * (1 if self.roles[entity] == "grid_import" else -1)
                    for entity, amount in baseline.items()
                    if entity in self.roles
                )
                * seconds
                / QUARTER_SECONDS
                + extra * seconds / duration
            )
            for entity, role in self.roles.items():
                amount = max(0, net) if role == "grid_import" else max(0, -net)
                self.data["counters"][entity] += amount / list(self.roles.values()).count(role)
            previous = end
        self.data["at"] = now.isoformat()
        if consumers:
            self.data["additional_consumers"] = consumers
        if dishwasher_total is not None:
            self.data["dishwasher_kwh"] = dishwasher_total
        return self.values()

    def values(self):
        """Convert each cumulative kWh counter once to its original entity unit."""
        return {
            entity: amount * self.units[entity] for entity, amount in self.data["counters"].items()
        }


async def prepare_grid(hass, entry, roles, units, totals, now, sample):
    """Restore AC counters before publishing new states, preserving all other fixture/source history."""
    from homeassistant.helpers.storage import Store

    grid_roles = {entity: role for entity, role in roles.items() if role in GRID_ROLES}
    store = Store(hass, 1, "oep_test_live_grid." + entry.entry_id)
    saved = await store.async_load()
    if saved and saved["roles"] != grid_roles:
        raise ValueError(
            "Test grid membership changed. Explicitly reconfigure this disposable fixture"
        )
    if saved:
        data = saved
    else:
        states = {entity: hass.states.get(entity) for entity in grid_roles}
        available = all(
            state and state.state not in ("unknown", "unavailable") for state in states.values()
        )
        data = {
            "roles": grid_roles,
            "at": (now if available else quarter_start(now)).isoformat(),
            "counters": {
                entity: float(states[entity].state) / units[entity]
                if available
                else totals[entity] / units[entity]
                for entity in grid_roles
            },
            "dishwasher_kwh": None,
        }
    meter = LiveGridMeter(grid_roles, units, sample, data)

    def publish(instant):
        """Persist observed interval allocation and expose the same frozen result to both source renderers."""
        from .dishwasher import DATA_KEY

        dishwasher = hass.data.get(DATA_KEY)
        from .ev import live_extra as ev_extra

        ev_totals, _ = ev_extra(hass)
        from .dishwasher import laundry_extra

        laundry_totals, _ = laundry_extra(hass)
        from .thermal import DATA_KEY as THERMAL_KEY

        boiler = hass.data.get(THERMAL_KEY)
        thermal_totals = (
            {boiler.ids["energy"]: (boiler.energy_kwh, boiler.initial_kwh)} if boiler else {}
        )
        values = meter.advance(
            instant,
            dishwasher["data"]["total_kwh"] if dishwasher else None,
            dishwasher["initial_kwh"] if dishwasher else None,
            {**ev_totals, **laundry_totals, **thermal_totals},
        )
        snapshot = {**meter.data, "counters": dict(meter.data["counters"])}
        entry.async_create_task(hass, store.async_save(snapshot), "Save simulated grid counters")
        return values

    return publish
