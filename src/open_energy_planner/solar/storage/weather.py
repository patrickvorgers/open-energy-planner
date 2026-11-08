"""Durable complete monthly weather prefixes with immutable values and actual receipt times."""

from dataclasses import asdict
from datetime import UTC, date, datetime, timedelta

from open_energy_planner.core.units import HOURS_PER_DAY
from open_energy_planner.prices.model import aware
from open_energy_planner.solar.calculation.model import SolarWeatherHour, version

WEATHER_NORMALIZATION_REVISION = "end-labelled-hour-negative-radiation-zero-v1"
MAX_MONTH_DAYS = 31
MAX_MONTH_HOURS = MAX_MONTH_DAYS * HOURS_PER_DAY
MAX_MONTH_BLOCKS = MAX_MONTH_DAYS + 1
HOUR = timedelta(hours=1)
NORMALIZED_WEATHER_UNITS = {
    "ghi": "W/m²",
    "dni": "W/m²",
    "dhi": "W/m²",
    "celsius": "°C",
    "wind_ms": "m/s",
}


class HistoricalWeatherCache:
    """Own active-database cache pointers while preserving original normalized evidence.

    The query identity includes the provider, every result-affecting parameter,
    normalization and exact request dates. A month pointer may extend only a
    complete prefix. Daily immutable value blocks are reused between extensions.
    This cache establishes retrieval coverage, never an old issued forecast.
    """

    def __init__(self, store):
        """Borrow the active SolarStore without opening another engine or local file store."""
        self.store = store

    @staticmethod
    def _query(url, params, first, last):
        """Bind normalized values to their full provider request and normalization contract."""
        return {
            "url": url,
            "params": {**params, "start_date": first.isoformat(), "end_date": last.isoformat()},
            "normalization": WEATHER_NORMALIZATION_REVISION,
            "units": NORMALIZED_WEATHER_UNITS,
        }

    def _index(self, url, params, first):
        """Keep one coverage pointer per provider/parameter/month family, excluding its growing end."""
        if first.day != 1 or "start_date" in params or "end_date" in params:
            raise ValueError("Invalid historical weather cache request")
        return "weather-cache-index-" + version(self._query(url, params, first, first)) + ".json"

    @staticmethod
    def _complete(rows, first, last):
        """Require every normalized preceding-hour interval of the complete provider date range."""
        hours = ((last - first).days + 1) * HOURS_PER_DAY
        if not 0 < hours <= MAX_MONTH_HOURS or last.month != first.month or last.year != first.year:
            raise ValueError("Invalid historical weather cache span")
        start = datetime.combine(first, datetime.min.time(), UTC) - HOUR
        if len(rows) != hours or any(
            not isinstance(row, SolarWeatherHour) or row.start != start + index * HOUR
            for index, row in enumerate(rows)
        ):
            raise ValueError("Incomplete historical weather cache range")

    def read(self, url, params, first):
        """Restore a verified complete prefix and its original knowledge times.

        Missing pointers return no coverage. Malformed or inconsistent retained
        evidence raises ValueError and remains unchanged. A caller can retain its
        independently usable current forecast when optional history is unavailable.
        """
        pointer = self._index(url, params, first)
        if not self.store.documents.exists(pointer):
            return (), first - timedelta(days=1)
        try:
            reference = self.store.documents.read(pointer)
            receipt = self.store.documents.read(f"weather-cache-receipt-{reference}.json")
            if not isinstance(receipt, dict) or version(receipt) != reference:
                raise ValueError("Invalid historical weather cache receipt")
            last = date.fromisoformat(receipt["query"]["params"]["end_date"])
            query = self._query(url, params, first, last)
            if receipt["query"] != query:
                raise ValueError("Historical weather cache request mismatch")
            data_ref = receipt["data_reference"]
            manifest = self.store.documents.read(f"weather-cache-data-{data_ref}.json")
            if (
                manifest["entity_id"] is not None
                or not isinstance(manifest["blocks"], list)
                or not 1 <= len(manifest["blocks"]) <= MAX_MONTH_BLOCKS
            ):
                raise ValueError("Invalid historical weather cache manifest")
            values = []
            for block in manifest["blocks"]:
                body = self.store.documents.read(f"block-{block['block']}.json")
                if version(body) != block["block"] or body["entity_id"] is not None:
                    raise ValueError("Invalid historical weather cache block")
                if block["receipts"]:
                    raise ValueError("Weather cache values contain receipt times")
                values.extend(body["hours"])
                if len(values) > MAX_MONTH_HOURS:
                    raise ValueError("Historical weather cache range too large")
            if version({"query": query, "hours": values}) != data_ref:
                raise ValueError("Historical weather cache values mismatch")
            requests = receipt["requests"]
            if not isinstance(requests, list) or not 1 <= len(requests) <= MAX_MONTH_DAYS:
                raise ValueError("Invalid historical weather cache requests")
            stamps, cursor = [], first
            for request in requests:
                fetched_end = date.fromisoformat(request["query"]["params"]["end_date"])
                if (
                    request["query"] != self._query(url, params, cursor, fetched_end)
                    or not cursor <= fetched_end <= last
                ):
                    raise ValueError("Historical weather cache fetch coverage mismatch")
                stamp = aware(datetime.fromisoformat(request["received_at"]))
                stamps.extend([stamp] * (((fetched_end - cursor).days + 1) * HOURS_PER_DAY))
                cursor = fetched_end + timedelta(days=1)
            if cursor != last + timedelta(days=1) or len(stamps) != len(values):
                raise ValueError("Incomplete historical weather cache receipts")
            rows = []
            for index, value in enumerate(values):
                rows.append(
                    SolarWeatherHour(
                        **{**value, "start": datetime.fromisoformat(value["start"])},
                        received_at=stamps[index],
                    )
                )
            self._complete(rows, first, last)
            return tuple(rows), last
        except (KeyError, TypeError, IndexError, OverflowError) as error:
            raise ValueError("Invalid retained historical weather cache") from error

    def write(self, url, params, first, last, rows):
        """Atomically extend complete coverage without rewriting old values or receipt times.

        Args:
            url: Exact provider endpoint, without credentials.
            params: Result-affecting query parameters excluding the month dates.
            first: First calendar date of this cache month.
            last: Inclusive last fully received calendar date.
            rows: Complete normalized month prefix, including original receipt times.

        Raises:
            ValueError: Coverage is incomplete or a retained prefix differs.
            OSError: The active archive cannot complete its transaction.
        """
        self._complete(rows, first, last)
        pointer = self._index(url, params, first)
        query = self._query(url, params, first, last)
        values = []
        for row in rows:
            body = asdict(row)
            body["start"] = row.start.isoformat()
            body.pop("received_at")
            values.append(body)
        data_ref = version({"query": query, "hours": values})
        with self.store.transaction():
            previous, covered = self.read(url, params, first)
            if previous and tuple(rows[: len(previous)]) != previous:
                raise ValueError("Historical weather cache prefix changed")
            if len(previous) == len(rows):
                return
            requests = []
            if previous:
                old_ref = self.store.documents.read(pointer)
                requests = self.store.documents.read(f"weather-cache-receipt-{old_ref}.json")[
                    "requests"
                ]
            suffix = rows[len(previous) :]
            received = suffix[0].received_at
            if any(row.received_at != received for row in suffix):
                raise ValueError("Historical weather fetch receipt differs")
            requests.append(
                {
                    "query": self._query(url, params, covered + timedelta(days=1), last),
                    "received_at": received.isoformat(),
                }
            )
            receipt = {"query": query, "data_reference": data_ref, "requests": requests}
            receipt_ref = version(receipt)
            self.store.archive_series("weather-cache-data", data_ref, values)
            self.store.archive("weather-cache-receipt", receipt_ref, receipt)
            self.store.documents.write(pointer, receipt_ref)
