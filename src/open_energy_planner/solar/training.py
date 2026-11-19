"""Persist one source-bound daily learning result independently of live forecast issuance."""

from dataclasses import replace
from datetime import UTC
from importlib.metadata import version as distribution_version
from zoneinfo import ZoneInfo

from .boosting import (
    MODEL_ID,
    TRAINING_POLICY,
    SolarBoostedModel,
    TrainingResult,
    TrainingStatus,
    TrainingSummary,
)
from .calculation.model import version
from .forecast_weather import FORECAST_WEATHER_POLICY
from .wire import document

RETRYABLE_TRAINING = frozenset(
    {
        TrainingStatus.FAILED,
        TrainingStatus.BLOCKED,
        TrainingStatus.INSUFFICIENT_HISTORY,
        TrainingStatus.INSUFFICIENT_VALIDATION,
    }
)
MAX_CACHED_TRAINING_RESULTS = 16


def restore_result(store, reference, binding):
    """Verify immutable report identity and restore checked weights and typed admission evidence."""
    record = store.documents.read("ml-training-" + reference + ".json")
    if version(record) != reference or record.get("binding") != binding:
        raise ValueError("Solar training report checksum mismatch")

    def model(metadata):
        """Resolve shared native JSON through the existing strict model restoration boundary."""
        if metadata is None:
            return None
        body = dict(metadata)
        weights = store.documents.read("ml-weights-" + body.pop("weights_reference") + ".json")
        return SolarBoostedModel.restore({**body, "model_json": weights["model_json"]})

    return TrainingResult(
        model(record["model"]),
        TrainingSummary.restore(record["summary"]),
        reference,
        model(record.get("challenger")),
    )


class DailySolarTraining:
    """Keep a durable daily attempt and last usable result for each physical source definition."""

    def __init__(self, store):
        """Borrow the domain store so pointers and immutable reports share its transaction."""
        self.store = store
        self.results = {}

    def prepare(self, source, inverter, location, history, weather, runs, feedback, now):
        """Reuse today's completed result, retry changed incomplete inputs and skip unchanged new days.

        Live weather and newly issued forecasts are deliberately absent from the
        binding. Only completed historical inputs enter the daily input identity.
        Configuration, measurement boundary and model policy changes bind a new fit.
        """
        local = now.astimezone(ZoneInfo(location.timezone))
        boundary = local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        binding = version(
            {
                "source": document(source),
                "inverter": document(inverter),
                "location": document(location),
                "model": MODEL_ID,
                "training_policy": TRAINING_POLICY,
                "weather_policy": FORECAST_WEATHER_POLICY,
                "library_version": distribution_version("xgboost-cpu"),
            }
        )
        name = "training-current-" + binding + ".json"
        pointer = self.store.documents.read(name) if self.store.documents.exists(name) else None
        day = local.date().isoformat()
        if pointer is not None and (
            not isinstance(pointer, dict)
            or set(pointer) != {"binding", "day", "inputs", "report", "usable"}
            or pointer["binding"] != binding
            or not isinstance(pointer["day"], str)
            or pointer["day"] > day
        ):
            raise ValueError("Invalid solar training pointer")
        previous = self.read(pointer["report"], binding) if pointer else None
        token = {"name": name, "binding": binding, "day": day, "previous": pointer}
        if pointer and pointer["day"] == day and previous.summary.status not in RETRYABLE_TRAINING:
            return self.usable(pointer, previous), token
        inputs = version(
            {
                "hours": [document(row) for row in history.hours if row.start <= boundary],
                "weather": [document(row) for row in weather if row.start < boundary],
                "runs": [run.version for run in runs if run.initialized_at < boundary],
                "feedback": {
                    "checked_until": min(feedback.checked_until, boundary),
                    "readings": [row for row in feedback.readings if row[0] < boundary],
                }
                if feedback
                else None,
            }
        )
        token["inputs"] = inputs
        if pointer and pointer["inputs"] == inputs:
            if pointer["day"] != day:
                self.store.documents.write(name, {**pointer, "day": day})
            return self.usable(pointer, previous), token
        return None, token

    def read(self, reference, binding):
        """Cache a validated immutable result without changing its original creation time."""
        key = binding, reference
        if key not in self.results:
            if len(self.results) >= MAX_CACHED_TRAINING_RESULTS:
                self.results.pop(next(iter(self.results)))
            self.results[key] = restore_result(self.store, reference, binding)
        return self.results[key]

    def usable(self, pointer, latest):
        """Preserve a previously admitted same-binding model when today's data is insufficient."""
        reference = pointer.get("usable")
        return (
            self.read(reference, pointer["binding"])
            if latest.model is None and reference
            else latest
        )

    def save(self, token, result, record):
        """Atomically publish one original attempt and a reference to the latest usable model."""
        record = {
            **record,
            "binding": token["binding"],
            "challenger": self.store.archive_model(result.challenger)
            if result.challenger is not None
            else None,
        }
        reference = version(record)
        previous = token["previous"]
        pointer = {
            "binding": token["binding"],
            "day": token["day"],
            "inputs": token["inputs"],
            "report": reference,
            "usable": reference
            if result.model is not None
            else previous.get("usable")
            if previous
            else None,
        }
        with self.store.transaction():
            self.store.archive("ml-training", reference, record)
            self.store.documents.write(token["name"], pointer)
        result = replace(result, archive_reference=reference)
        if len(self.results) >= MAX_CACHED_TRAINING_RESULTS:
            self.results.pop(next(iter(self.results)))
        self.results[token["binding"], reference] = result
        return self.usable(pointer, result)

    def fallback(self, token):
        """Read an earlier usable same-binding model after a transient training failure."""
        pointer = token.get("previous")
        return (
            self.read(pointer["usable"], pointer["binding"])
            if pointer and pointer.get("usable")
            else None
        )
