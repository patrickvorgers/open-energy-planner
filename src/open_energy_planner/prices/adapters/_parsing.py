"""Shared bounded JSON price-row and aware timestamp parsing."""

from datetime import UTC, datetime

from ..model import MAX_PRICE_POINTS


def price_rows(value):
    """Require a bounded nonempty array of price objects before conversion or persistence."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_PRICE_POINTS:
        raise ValueError("Expected between one and 512 price entries")
    if not all(isinstance(row, dict) for row in value):
        raise TypeError("Expected price entry objects")
    return value


def stamp(value):
    """Parse an aware provider/storage timestamp as UTC. Never infer a timezone."""
    result = datetime.fromisoformat(value)
    if result.utcoffset() is None:
        raise ValueError("Missing price timezone")
    return result.astimezone(UTC)
