"""Persist one source-bound daily learning result independently of live forecast issuance."""

from dataclasses import replace
from datetime import UTC, date, datetime
from importlib.metadata import version as distribution_version
from re import fullmatch
from zoneinfo import ZoneInfo

from open_energy_planner.weather.run import FORECAST_WEATHER_POLICY

from .boosting import (
    MODEL_ID,
    TRAINING_POLICY,
    SolarBoostedModel,
    TrainingResult,
    TrainingStatus,
    TrainingSummary,
)
from .calculation.model import version
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

    def revision(self, entity):
        """Read one compact durable evidence generation without scanning retained source history."""
        name = "training-evidence-" + version(entity) + ".json"
        if not self.store.documents.exists(name):
            return 0
        value = self.store.documents.read(name)
        if (
            not isinstance(value, dict)
            or set(value) != {"entity_id", "revision"}
            or value["entity_id"] != entity
            or type(value["revision"]) is not int
            or value["revision"] < 0
        ):
            raise ValueError("Invalid solar training evidence revision")
        return value["revision"]

    def site_revision(self, site):
        """Read the site's run-evidence generation independently of currently configured source entities."""
        name = "training-site-evidence-" + site + ".json"
        if not self.store.documents.exists(name):
            return 0
        value = self.store.documents.read(name)
        if (
            not isinstance(value, dict)
            or set(value) != {"site", "revision"}
            or value["site"] != site
            or type(value["revision"]) is not int
            or value["revision"] < 0
        ):
            raise ValueError("Invalid solar training site revision")
        return value["revision"]

    def revisions(self, entities):
        """Snapshot configured entity generations while their caller still owns the write transaction."""
        return {entity: self.revision(entity) for entity in sorted(set(entities))}

    def invalidate(self, entities):
        """Revoke both current weights and usable fallback after destructive evidence changes."""
        with self.store.transaction():
            for entity in sorted(set(entities)):
                self.store.documents.write(
                    "training-evidence-" + version(entity) + ".json",
                    {"entity_id": entity, "revision": self.revision(entity) + 1},
                )

    def accept_runs(self, site, runs, entities, *, authoritative=True):
        """Merge partial admissions or reconcile an explicit authoritative snapshot using only run identities."""
        name = "training-runs-" + site + ".json"
        incoming = {run.initialized_at.isoformat(): run.version for run in runs}
        with self.store.transaction():
            previous = self.store.documents.read(name) if self.store.documents.exists(name) else {}
            if not isinstance(previous, dict) or any(
                not isinstance(at, str)
                or not isinstance(reference, str)
                or fullmatch("[0-9a-f]{64}", reference) is None
                for at, reference in previous.items()
            ):
                raise ValueError("Invalid solar training forecast-run cohort")
            withdrawn = any(
                (authoritative or at in incoming) and incoming.get(at) != reference
                for at, reference in previous.items()
            )
            if not authoritative:
                incoming = {**previous, **incoming}
            if withdrawn:
                self.store.documents.write(
                    "training-site-evidence-" + site + ".json",
                    {"site": site, "revision": self.site_revision(site) + 1},
                )
            if previous != incoming:
                self.store.documents.write(name, incoming)
            revisions = self.revisions(entities)
            site_revision = self.site_revision(site)
        return withdrawn, revisions, site_revision

    def prepare(
        self,
        source,
        inverter,
        location,
        history,
        weather,
        runs,
        feedback,
        now,
        *,
        evidence_revision=None,
        run_evidence_revision=None,
    ):
        """Reuse today's completed result, retry changed incomplete inputs and skip unchanged new days.

        Live weather and newly issued forecasts are deliberately absent from the
        binding. Only completed historical inputs enter the daily input identity.
        Configuration, measurement boundary and model policy changes bind a new fit.
        """
        local = now.astimezone(ZoneInfo(location.timezone))
        boundary = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        binding = version(
            {
                "source": document(source),
                "evidence_revision": self.revision(source.entity_id)
                if evidence_revision is None
                else evidence_revision,
                "run_evidence_revision": self.site_revision(version(document(location)))
                if run_evidence_revision is None
                else run_evidence_revision,
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
            or set(pointer) != {"binding", "day", "inputs", "report", "usable", "validation_end"}
            or pointer["binding"] != binding
            or not isinstance(pointer["day"], str)
            or pointer["day"] > day
            or not isinstance(pointer["report"], str)
            or fullmatch("[0-9a-f]{64}", pointer["report"]) is None
            or not isinstance(pointer["inputs"], str)
            or fullmatch("[0-9a-f]{64}", pointer["inputs"]) is None
            or (
                pointer["usable"] is not None
                and (
                    not isinstance(pointer["usable"], str)
                    or fullmatch("[0-9a-f]{64}", pointer["usable"]) is None
                )
            )
            or not isinstance(pointer["validation_end"], str)
        ):
            raise ValueError("Invalid solar training pointer")
        if pointer:
            try:
                validation_end = datetime.fromisoformat(pointer["validation_end"])
                if (
                    date.fromisoformat(pointer["day"]).isoformat() != pointer["day"]
                    or validation_end.tzinfo is None
                    or validation_end.utcoffset() != boundary.utcoffset()
                    or validation_end
                    != validation_end.replace(hour=0, minute=0, second=0, microsecond=0)
                    or validation_end > boundary
                    or validation_end.isoformat() != pointer["validation_end"]
                ):
                    raise ValueError("Invalid solar training pointer")
            except ValueError:
                raise ValueError("Invalid solar training pointer") from None
        previous = self.read(pointer["report"], binding) if pointer else None
        token = {
            "name": name,
            "binding": binding,
            "day": day,
            "previous": pointer,
            "validation_end": boundary.isoformat(),
        }
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
        if (
            pointer
            and pointer["inputs"] == inputs
            and (
                previous.summary.status not in RETRYABLE_TRAINING
                or pointer["validation_end"] == token["validation_end"]
            )
        ):
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
        if latest.model is not None or not reference:
            return latest
        return replace(
            self.read(reference, pointer["binding"]), attempt_status=latest.summary.status
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
            "validation_end": token["validation_end"],
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
            replace(
                self.read(pointer["usable"], pointer["binding"]),
                attempt_status=TrainingStatus.FAILED,
            )
            if pointer and pointer.get("usable")
            else None
        )
