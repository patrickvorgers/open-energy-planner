"""Validated normalized-price versions and current pointers in the active database."""

from dataclasses import asdict

from open_energy_planner.prices.adapters._parsing import price_rows, stamp
from open_energy_planner.prices.model import MAX_PRICE_SERIES, PriceKind, PricePoint, PriceSeries

ARCHIVE_HASH_LENGTH = 64


class PriceStore:
    """Normalized-price cache plus immutable source-version documents.

    The shared document adapter owns SQL transactions. No raw API secrets or
    provider payloads are retained. A failed pointer
    replacement cannot install an uncommitted in-memory source response.
    """

    def __init__(self, documents):
        """Bind the price document namespace without opening files or owning an engine."""
        self.documents = documents
        self.current_name = "current.json"

    def load(self):
        """Read normalized cache references. Reject corruption rather than inventing empty prices."""
        if not self.documents.exists(self.current_name):
            return ()
        references = self.documents.read(self.current_name)
        if not isinstance(references, list) or len(references) > MAX_PRICE_SERIES:
            raise ValueError("Expected a bounded price reference list")
        series = tuple(self._read(name) for name in references)
        if hasattr(self.documents, "engine"):
            from open_energy_planner.prices.storage.adapters.published import retain

            with self.documents.transaction():
                retain(self.documents.pending.connection, series)
        return series

    def _read(self, name):
        """Validate an archive reference, its object structure and canonical content hash."""
        if (
            not isinstance(name, str)
            or len(name) != ARCHIVE_HASH_LENGTH
            or any(candidate not in "0123456789abcdef" for candidate in name)
        ):
            raise ValueError("Invalid price archive reference")
        data = self.documents.read(f"{name}.json")
        if not isinstance(data, dict):
            raise TypeError("Expected a price archive object")
        points = tuple(
            PricePoint(
                stamp(price_row["start"]),
                stamp(price_row["end"]),
                price_row["value"],
                PriceKind(price_row["kind"]),
                price_row["native_resolution_minutes"],
            )
            for price_row in price_rows(data.pop("points"))
        )
        for key in ("received_at", "issued_at", "expires_at"):
            data[key] = stamp(data[key]) if data[key] else None
        value = PriceSeries(**data, points=points)
        if value.version != name:
            raise ValueError("Price archive hash mismatch")
        return value

    def save(self, series):
        """Write immutable versions before atomically advancing the current cache reference list."""
        with self.documents.transaction():
            if len(series) > MAX_PRICE_SERIES:
                raise ValueError("Too many cached price sources")
            for source in series:
                target = f"{source.version}.json"
                if not self.documents.exists(target):
                    self._replace(target, asdict(source))
                else:
                    # Do not report recovery while pointing back at corrupt archive data.
                    try:
                        self._read(source.version)
                    except (ValueError, TypeError, KeyError) as error:
                        raise OSError("Existing price archive is unusable") from error
            self._replace(self.current_name, [series_item.version for series_item in series])
            if hasattr(self.documents, "engine"):
                from open_energy_planner.prices.storage.adapters.published import retain

                retain(self.documents.pending.connection, series)

    def _replace(self, path, data):
        """Write through the configured archive, with SQL transactions owned by the store operation."""
        self.documents.write(path, data)
