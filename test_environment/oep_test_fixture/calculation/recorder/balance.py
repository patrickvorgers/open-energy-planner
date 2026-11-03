"""Plan exact Recorder replacements from detached source frames without performing HA writes."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from ...policy import PARTS_PER_QUARTER, QUARTER_MINUTES, STATISTIC_MINUTES
from .history import quarter_start

HOUR_SECONDS = 3600


QUARTERS_PER_HOUR = HOUR_SECONDS // (QUARTER_MINUTES * 60)


ENERGY_TOLERANCE_KWH = 0.00001


COUNTER_TOLERANCE_KWH = 0.000000001


FINE_SECONDS = STATISTIC_MINUTES * 60


PARTS_PER_HOUR = HOUR_SECONDS // FINE_SECONDS


def proposal(statistics, roles, consumers, controlled):
    """Identify impossible synthetic hours and grid-origin discontinuities without hiding other resets."""
    from ..energy import quarter_energy

    channels = {
        entity: {
            row["start"]: row["sum"]
            for row in statistics.get(entity, [])
            if row.get("sum") is not None
        }
        for entity in (*roles, *consumers)
    }
    if not channels or any(not values for values in channels.values()):
        raise ValueError("Required simulation history is missing")
    stamps = sorted(set.intersection(*(set(values) for values in channels.values())))
    repairs = []
    for previous, current in pairwise(stamps):
        if current - previous != HOUR_SECONDS:
            continue
        deltas = {entity: values[current] - values[previous] for entity, values in channels.items()}
        grid_resets = {
            entity
            for entity, amount in deltas.items()
            if amount < -COUNTER_TOLERANCE_KWH
            and roles.get(entity) in ("grid_import", "grid_export")
        }
        if any(
            amount < -ENERGY_TOLERANCE_KWH and entity not in grid_resets
            for entity, amount in deltas.items()
        ):
            resets = {
                entity: amount
                for entity, amount in deltas.items()
                if amount < -ENERGY_TOLERANCE_KWH
            }
            raise ValueError(
                f"Counter resets require separate investigation at {datetime.fromtimestamp(current, UTC).isoformat()}: {resets}"
            )
        household = sum(
            deltas[entity] * (-1 if role in ("grid_export", "battery_charge") else 1)
            for entity, role in roles.items()
        )
        selected = sum(deltas[entity] for entity in consumers)
        if not grid_resets and household + ENERGY_TOLERANCE_KWH >= selected:
            continue
        start = datetime.fromtimestamp(current, UTC)
        baseline = 0
        for quarter in range(QUARTERS_PER_HOUR):
            sample = quarter_energy(
                start + timedelta(minutes=quarter * QUARTER_MINUTES), include_dishwasher=False
            )
            baseline += (
                sample["grid_import"]
                - sample["grid_export"]
                + sample["solar_roof"]
                + sample["solar_garage"]
            )
        expected = baseline + sum(deltas[entity] for entity in controlled)
        if expected + ENERGY_TOLERANCE_KWH < selected:
            raise ValueError("Known simulation profile cannot explain this discrepancy")
        repairs.append(
            {
                "start": start.isoformat(),
                "household_kwh": household,
                "selected_kwh": selected,
                "expected_kwh": expected,
                "reason": "grid_origin_discontinuity"
                if grid_resets
                else "missing_controlled_energy",
            }
        )
    return repairs


def frame_replacements(originals, frames, fine_maps, entities):
    """Replace complete known simulation frames and shift later sums without changing counter states."""
    writes = {"hour": {}, "5minute": {}}
    for entity in entities:
        for period, original in originals.items():
            changed = []
            for row in original.get(entity, []):
                stamp = row["start"]
                value = row["sum"]
                for start, frame in frames.items():
                    if entity not in frame:
                        continue
                    if period == "hour" and stamp >= start:
                        value += frame[entity]["hour_offset"]
                    elif period == "5minute":
                        if stamp >= start + HOUR_SECONDS:
                            value += frame[entity]["fine_offset"]
                        elif stamp >= start:
                            part = int((stamp - start) // FINE_SECONDS)
                            value += (
                                fine_maps[entity][start - FINE_SECONDS]["sum"]
                                + sum(frame[entity]["values"][: part + 1])
                                - row["sum"]
                            )
                if abs(value - row["sum"]) > COUNTER_TOLERANCE_KWH:
                    changed.append({**row, "sum": value})
            if changed:
                writes[period][entity] = changed
    return writes


def repair_documents(hourly, fine, roles, consumers, controlled, *, sample):
    """Reconstruct simulated source frames and grid flows without inventing measured device use.

    Preserve unrelated deltas and counter states. Align fine sums with the last
    completed hour so Recorder can compile subsequent hours without an origin jump.
    An adjacent source decrease beyond COUNTER_TOLERANCE_KWH identifies an origin
    transition. Reconstruct its complete affected hour from the canonical PV or
    stationary-battery simulator profile, removing positive and negative spikes
    together. Gaps at such transitions raise ValueError. Controlled meters use
    recorded consumption. Only equal bounding counters establish a constant gap.
    Return preview records and replacement sums, retaining original source states.
    """
    from ..energy import quarter_energy

    originals = {"hour": hourly, "5minute": fine}
    normalized = {}
    source_repairs = []
    corrected_sources = set()
    source_frames = {}
    source_maps = {entity: {row["start"]: row for row in rows} for entity, rows in fine.items()}
    for entity, role in roles.items():
        if role not in ("solar", "battery_charge", "battery_discharge"):
            continue
        affected = set()
        hours = {row["start"]: row for row in hourly.get(entity, [])}
        for period, width in (("hour", HOUR_SECONDS), ("5minute", FINE_SECONDS)):
            for previous, current in pairwise(originals[period].get(entity, [])):
                if current["sum"] - previous["sum"] >= -COUNTER_TOLERANCE_KWH:
                    continue
                if current["start"] - previous["start"] != width:
                    raise ValueError(
                        "A simulated source reset across a history gap requires investigation"
                    )
                affected.add(current["start"] // HOUR_SECONDS * HOUR_SECONDS)
                previous_hour = previous["start"] // HOUR_SECONDS * HOUR_SECONDS
                if (
                    period == "5minute"
                    and previous_hour != current["start"] // HOUR_SECONDS * HOUR_SECONDS
                    and previous_hour in hours
                    and previous_hour - HOUR_SECONDS in hours
                ):
                    expected_previous = sum(
                        sample(
                            datetime.fromtimestamp(previous_hour, UTC)
                            + timedelta(minutes=part * QUARTER_MINUTES)
                        )[entity]
                        for part in range(QUARTERS_PER_HOUR)
                    )
                    boundary = source_maps.get(entity, {}).get(previous_hour - FINE_SECONDS)
                    if abs(
                        hours[previous_hour]["sum"]
                        - hours[previous_hour - HOUR_SECONDS]["sum"]
                        - expected_previous
                    ) > ENERGY_TOLERANCE_KWH or (
                        boundary
                        and abs(previous["sum"] - boundary["sum"] - expected_previous)
                        > ENERGY_TOLERANCE_KWH
                    ):
                        affected.add(previous_hour)
        for start in sorted(affected):
            channel = source_maps.get(entity, {})
            stamps = [
                start - FINE_SECONDS,
                *(start + part * FINE_SECONDS for part in range(PARTS_PER_HOUR)),
            ]
            if (
                any(stamp not in channel for stamp in stamps)
                or start not in hours
                or start - HOUR_SECONDS not in hours
            ):
                raise ValueError(
                    "Complete source frames are required to reconstruct a simulated origin transition"
                )
            values = [
                sample(quarter_start(datetime.fromtimestamp(stamp, UTC)))[entity]
                / PARTS_PER_QUARTER
                for stamp in stamps[1:]
            ]
            total = sum(values)
            source_frames.setdefault(start, {})[entity] = {
                "values": values,
                "hour_offset": total - (hours[start]["sum"] - hours[start - HOUR_SECONDS]["sum"]),
                "fine_offset": total - (channel[stamps[-1]]["sum"] - channel[stamps[0]]["sum"]),
            }
            corrected_sources.add(entity)
            source_repairs.append(
                {
                    "start": datetime.fromtimestamp(start, UTC).isoformat(),
                    "reason": "simulated_source_origin_discontinuity",
                    "entity_id": entity,
                    "expected_kwh": total,
                }
            )
    source_writes = frame_replacements(originals, source_frames, source_maps, corrected_sources)
    for period, original in originals.items():
        normalized[period] = dict(original)
        for entity, rows in source_writes[period].items():
            replacements = {row["start"]: row for row in rows}
            normalized[period][entity] = [
                replacements.get(row["start"], row) for row in original[entity]
            ]
    hourly, fine = normalized["hour"], normalized["5minute"]

    grid = {
        entity: role for entity, role in roles.items() if role in ("grid_import", "grid_export")
    }
    maps = {entity: {row["start"]: row for row in rows} for entity, rows in fine.items()}
    hours = {entity: {row["start"]: row for row in rows} for entity, rows in hourly.items()}
    repairs = {
        datetime.fromisoformat(row["start"]).timestamp(): row
        for row in proposal(hourly, roles, consumers, controlled)
    }
    for entity in grid:
        for previous, current in pairwise(sorted(maps.get(entity, {}))):
            if (
                current - previous == FINE_SECONDS
                and maps[entity][current]["sum"] - maps[entity][previous]["sum"]
                < -COUNTER_TOLERANCE_KWH
            ):
                start = current // HOUR_SECONDS * HOUR_SECONDS
                repairs.setdefault(
                    start,
                    {
                        "start": datetime.fromtimestamp(start, UTC).isoformat(),
                        "reason": "fine_grid_origin_discontinuity",
                    },
                )
    frames = {}
    for start in sorted(repairs):
        stamps = [
            start - FINE_SECONDS,
            *(start + part * FINE_SECONDS for part in range(PARTS_PER_HOUR)),
        ]
        required = {*roles, *controlled}
        inferred = []
        for entity in required - grid.keys():
            values = maps.get(entity, {})
            known = sorted(values)
            for stamp in stamps:
                previous_stamp = max((point for point in known if point < stamp), default=None)
                following_stamp = min((point for point in known if point > stamp), default=None)
                previous = values.get(previous_stamp)
                following = values.get(following_stamp)
                if (
                    stamp not in values
                    and previous
                    and following
                    and abs(previous["sum"] - following["sum"]) <= COUNTER_TOLERANCE_KWH
                ):
                    # Equal bounding total-increasing counters prove no intervening consumption.
                    values[stamp] = {"start": stamp, "sum": previous["sum"]}
                    inferred.append(
                        {
                            "entity_id": entity,
                            "start": datetime.fromtimestamp(stamp, UTC).isoformat(),
                        }
                    )
        if inferred:
            repairs[start]["proven_constant_counter_points"] = inferred
        if any(any(stamp not in maps.get(entity, {}) for stamp in stamps) for entity in required):
            missing = {
                entity: [
                    datetime.fromtimestamp(stamp, UTC).isoformat()
                    for stamp in stamps
                    if stamp not in maps.get(entity, {})
                ][:2]
                for entity in required
                if any(stamp not in maps.get(entity, {}) for stamp in stamps)
            }
            raise ValueError(
                f"Complete five-minute evidence is required at {datetime.fromtimestamp(start, UTC).isoformat()}: {missing}"
            )
        desired = {entity: [] for entity in grid}
        for previous, current in pairwise(stamps):
            delta = {
                entity: maps[entity][current]["sum"] - maps[entity][previous]["sum"]
                for entity in required
            }
            if any(
                amount < -ENERGY_TOLERANCE_KWH
                for entity, amount in delta.items()
                if entity not in grid
            ):
                raise ValueError("Non-grid counter resets require separate investigation")
            instant = datetime.fromtimestamp(current, UTC)
            sample = quarter_energy(quarter_start(instant), include_dishwasher=False)
            baseline = (
                sample["grid_import"]
                - sample["grid_export"]
                + sample["solar_roof"]
                + sample["solar_garage"]
            ) / PARTS_PER_QUARTER
            supplied = sum(
                delta[entity] * (-1 if role == "battery_charge" else 1)
                for entity, role in roles.items()
                if entity not in grid
            )
            net = baseline + sum(max(0, delta[entity]) for entity in controlled) - supplied
            for role, energy in (("grid_import", max(0, net)), ("grid_export", max(0, -net))):
                members = [entity for entity, member_role in grid.items() if member_role == role]
                if energy > ENERGY_TOLERANCE_KWH and not members:
                    raise ValueError("The simulated Energy dashboard cannot represent this flow")
                for entity in members:
                    desired[entity].append(energy / len(members))
        frame = {}
        for entity, values in desired.items():
            if start not in hours.get(entity, {}) or start - HOUR_SECONDS not in hours[entity]:
                raise ValueError("Complete hourly evidence is required for this simulated repair")
            total = sum(values)
            hour_delta = hours[entity][start]["sum"] - hours[entity][start - HOUR_SECONDS]["sum"]
            fine_delta = maps[entity][stamps[-1]]["sum"] - maps[entity][stamps[0]]["sum"]
            frame[entity] = {
                "values": values,
                "hour_offset": total - hour_delta,
                "fine_offset": total - fine_delta,
            }
        frames[start] = frame
        repairs[start]["adjustments"] = {
            entity: value["hour_offset"] for entity, value in frame.items()
        }
    writes = frame_replacements({"hour": hourly, "5minute": fine}, frames, maps, grid)
    for period, entities in writes.items():
        for entity in corrected_sources:
            before = {row["start"]: row for row in originals[period].get(entity, [])}
            changed = [
                row
                for row in normalized[period].get(entity, [])
                if abs(row["sum"] - before[row["start"]]["sum"]) > COUNTER_TOLERANCE_KWH
            ]
            if changed:
                entities[entity] = changed
    # Recorder copies the last fine sum into its hourly table. Both must share an origin.
    for entity in (*grid, *corrected_sources):
        revised_hours = {
            **hours.get(entity, {}),
            **{row["start"]: row for row in writes["hour"].get(entity, [])},
        }
        revised_fine = {
            **maps.get(entity, {}),
            **{row["start"]: row for row in writes["5minute"].get(entity, [])},
        }
        anchors = [
            stamp for stamp in revised_hours if stamp + HOUR_SECONDS - FINE_SECONDS in revised_fine
        ]
        if not anchors:
            raise ValueError("A shared completed hour is required to align Recorder sum origins")
        anchor = max(anchors)
        shift = (
            revised_hours[anchor]["sum"] - revised_fine[anchor + HOUR_SECONDS - FINE_SECONDS]["sum"]
        )
        if abs(shift) <= COUNTER_TOLERANCE_KWH:
            continue
        writes["5minute"][entity] = [
            {**row, "sum": revised_fine[row["start"]]["sum"] + shift}
            for row in originals["5minute"].get(entity, [])
            if abs(revised_fine[row["start"]]["sum"] + shift - row["sum"]) > COUNTER_TOLERANCE_KWH
        ]
        repairs.setdefault(
            anchor,
            {
                "start": datetime.fromtimestamp(anchor, UTC).isoformat(),
                "reason": "recorder_origin_alignment",
            },
        )
        repairs[anchor].setdefault("fine_origin_shifts", {})[entity] = shift
    return [*repairs.values(), *source_repairs], writes
