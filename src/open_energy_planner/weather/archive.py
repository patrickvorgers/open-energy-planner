"""Transfer stored provider weather to the weather owner without changing references."""

from open_energy_planner.storage.series import SERIES_ARCHIVE_READ_BATCH_BLOCKS


def move_weather_documents(solar, weather):
    """Transfer original runs, receipts and their blocks atomically on the active engine."""
    prefixes = ("weather-", "forecast-weather-")
    if not any(solar.first_before(prefix, prefix + "z") for prefix in prefixes):
        return
    with solar.transaction(weather):
        blocks = set()
        for prefix in prefixes:
            while item := solar.first_before(prefix, prefix + "z"):
                name, body = item
                if isinstance(body, dict) and "blocks" in body:
                    if body.get("entity_id") is not None:
                        raise ValueError("Weather manifest has a meter owner")
                    blocks.update(f"block-{row['block']}.json" for row in body["blocks"])
                _move(solar, weather, name, body)
        block_names = sorted(blocks)
        for offset in range(0, len(block_names), SERIES_ARCHIVE_READ_BATCH_BLOCKS):
            batch = block_names[offset : offset + SERIES_ARCHIVE_READ_BATCH_BLOCKS]
            bodies = solar.read_many(batch)
            if len(bodies) != len(batch):
                raise ValueError("Stored weather blocks are missing")
            for name, body in bodies.items():
                # Weather blocks have no entity; solar meter history has its own source identity.
                if body.get("entity_id") is not None:
                    raise ValueError("Weather block has a meter owner")
                _move(solar, weather, name, body)


def _move(solar, weather, name, body):
    """Preserve equal destination evidence and roll back a conflicting transfer."""
    current = weather.read_many((name,))
    if name not in current:
        weather.write(name, body)
    elif current[name] != body:
        raise ValueError("Stored weather document conflicts with destination")
    solar.delete(name)
