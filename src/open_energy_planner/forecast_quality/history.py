"""Retain bounded Recorder evidence on the app's active SQLAlchemy database."""

from datetime import UTC, datetime, timedelta
from itertools import pairwise

from open_energy_planner.demand.calculation.model import digest
from open_energy_planner.forecast_quality.policy import (
    DAY,
    HISTORY_ERRORS,
    HISTORY_LOOKBACK_DAYS,
    HISTORY_RETRY_MINUTES,
    HOUR,
    RECORDER_DELAY_MINUTES,
)
from open_energy_planner.history.wire import parse_history


class HistoryArchive:
    """Own hourly receipt admission and immutable daily blocks, independently of model training."""

    def __init__(self, documents):
        """Borrow the app-owned document namespace. Live retry state is deliberately not restored."""
        self.documents = documents
        self.attempts = {}

    def request(self, now, sources):
        """Refresh recent completed hours once per hour and source. Missing meters cannot starve others."""
        end = (now.astimezone(UTC) - timedelta(minutes=RECORDER_DELAY_MINUTES)).replace(
            minute=0, second=0, microsecond=0
        )
        for source in sources:
            previous = self.attempts.get(source["version"])
            if previous and previous[0] == end and (previous[1] is None or now < previous[1]):
                continue
            body = {
                "entity_id": source["entity_id"],
                "start": (end - timedelta(days=HISTORY_LOOKBACK_DAYS) - HOUR).isoformat(),
                "end": end.isoformat(),
                "configuration_version": source["version"],
            }
            return {**body, "version": digest(body)}
        return None

    def receive(self, pending, body, now):
        """Archive an exact admitted range atomically. Corrections never overwrite retained block versions."""
        start, end = (datetime.fromisoformat(pending[key]) for key in ("start", "end"))
        if body.get("request_version") != pending["version"]:
            raise ValueError("Forecast history request changed")
        if set(body) == {"request_version", "error"} and body["error"] in HISTORY_ERRORS:
            self.attempts[pending["configuration_version"]] = (
                end,
                now + timedelta(minutes=HISTORY_RETRY_MINUTES),
            )
            return
        history = parse_history(body)
        if history.entity_id != pending["entity_id"] or any(
            not start <= hour.start < end for hour in history.hours
        ):
            raise ValueError("Forecast history source or range changed")
        by_day = {}
        for hour in history.hours:
            by_day.setdefault(hour.start.date().isoformat(), []).append(
                [hour.start.isoformat(), hour.total_kwh, hour.meter_kwh]
            )
        day = start.replace(hour=0)
        with self.documents.transaction():
            while day < end:
                date = day.date().isoformat()
                name = self._pointer(pending["configuration_version"], date)
                old = self.documents.read(name) if self.documents.exists(name) else None
                retained = self.documents.read(old["block"])["hours"] if old else []
                rows = sorted(
                    [row for row in retained if not start <= datetime.fromisoformat(row[0]) < end]
                    + by_day.get(date, [])
                )
                block = {"entity_id": history.entity_id, "hours": rows}
                block_name = "quality-block-" + digest(block) + ".json"
                if not self.documents.exists(block_name):
                    self.documents.write(block_name, block)
                receipt = {"block": block_name, "received_at": now.isoformat(), "request": pending}
                self.documents.write("quality-receipt-" + digest(receipt) + ".json", receipt)
                self.documents.write(name, receipt)
                day += DAY
        self.attempts[pending["configuration_version"]] = end, None

    def read(self, binding, start, end):
        """Read only requested days and contiguous cumulative endpoints. No interpolation or zero filling."""
        hours, receipts, pointers = {}, [], []
        day = (start - HOUR).replace(hour=0, minute=0, second=0, microsecond=0)
        while day < end:
            pointers.append(self._pointer(binding["version"], day.date().isoformat()))
            day += DAY
        # One pointer snapshot prevents a concurrent correction mixing cumulative baselines across midnight.
        for receipt in self.documents.read_many(pointers).values():
            block = self.documents.read(receipt["block"])
            if block["entity_id"] != binding["entity_id"]:
                raise ValueError("Forecast measurement binding changed")
            receipts.append(receipt["received_at"])
            for instant, total, _meter in block["hours"]:
                instant = datetime.fromisoformat(instant)
                if start - HOUR <= instant < end:
                    hours[instant] = total
        deltas = {}
        for left, right in pairwise(sorted(hours)):
            if right - left == HOUR and hours[right] >= hours[left] and right >= start:
                deltas[right] = hours[right] - hours[left]
        return deltas, max(receipts) if receipts else None

    @staticmethod
    def _pointer(version, day):
        """Address one bounded source/day without a global archive listing or unbounded index."""
        return "quality-history-" + version + "-" + day + ".json"
