"""Coordinate pair-matched source quality and durable manual preferences."""

import hashlib
import json
from datetime import UTC, date

from ..planning_policy import PRICE_COVERAGE_HOURS
from .policy import LEAD_BUCKET_COUNT, LEAD_BUCKET_HOURS, MIN_MATCHED_HOURS, MIN_SETTLED_DAYS
from .selection import ForecastScore


class SourceSelection:
    """Reuse the paired assessment policy without comparing errors from unrelated samples.

    Each challenger is compared directly with the current preference on matched
    original receipts. The greatest qualifying absolute improvement wins. A
    changing challenger restarts the daily streak. Manual preferences affect use,
    never collection or the independently maintained automatic recommendation.
    """

    def __init__(
        self,
        comparisons,
        documents,
        *,
        defaults=None,
        source_horizons=None,
        market_area=None,
        sources=None,
        known_sources=None,
    ):
        """Borrow pair selectors and SQL documents, retaining historical source decisions.

        Args:
            comparisons: Pair selectors that identify the market and forecast sources.
            documents: App-owned SQL documents for recommendations and manual revisions.
            defaults: Initial source per lead bucket. Defaults to the primary source.
            source_horizons: Maximum requested horizon per source in positive whole
                hours. Every configured source must have exactly one entry. When
                omitted, all sources use the shared price-coverage horizon.
            market_area: Explicit area when fewer than two sources are eligible.
            sources: Market-eligible source IDs, including single-source markets.
            known_sources: Registered IDs allowed in historical preferences.
                Ineligible choices remain stored but are inactive at runtime.

        Raises:
            ValueError: Source horizons are incomplete, unknown or nonpositive,
                or initial preferences have an invalid count or source identity.
        """
        self.comparisons = tuple(comparisons)
        first = self.comparisons[0] if self.comparisons else None
        self.area = market_area if market_area is not None else first.area
        self.sources = (
            tuple(sources)
            if sources is not None
            else tuple(
                dict.fromkeys(
                    source
                    for pair in self.comparisons
                    for source in (pair.primary, pair.alternative)
                )
            )
        )
        self.primary = self.sources[0] if self.sources else None
        self.alternative = self.sources[1] if len(self.sources) > 1 else None
        self.known_sources = tuple(known_sources) if known_sources is not None else self.sources
        self.documents = documents
        self.source_horizons = (
            dict(source_horizons)
            if source_horizons is not None
            else dict.fromkeys(self.sources, PRICE_COVERAGE_HOURS)
        )
        if set(self.source_horizons) != set(self.sources) or any(
            type(hours) is not int or hours <= 0 for hours in self.source_horizons.values()
        ):
            raise ValueError("Invalid forecast source horizons")
        self.defaults = (
            tuple(defaults) if defaults is not None else (self.primary,) * LEAD_BUCKET_COUNT
        )
        if len(self.defaults) != LEAD_BUCKET_COUNT or any(
            source is not None and source not in self.sources for source in self.defaults
        ):
            raise ValueError("Invalid initial forecast preferences")
        self.states = {
            bucket: state
            for bucket, state in (first.states.items() if first else ())
            if state.get("preferred_source") in (first.primary, first.alternative)
            and not (
                self.defaults[bucket] != self.primary
                and (
                    state.get("matched_hours", 0) < MIN_MATCHED_HOURS
                    or state.get("settled_days", 0) < MIN_SETTLED_DAYS
                )
                and state.get("preferred_source") == self.primary
            )
        }
        self.issue = next((pair.issue for pair in self.comparisons if pair.issue), None)
        self.manual = (None,) * LEAD_BUCKET_COUNT
        self.configuration_version = None
        self.state_name = f"source-selection-{self.area}.json"
        self.settings_name = f"source-preferences-{self.area}.json"
        self._restoration_issue = self.issue
        try:
            if documents.exists(self.state_name):
                loaded = documents.read(self.state_name)
                buckets = {str(index) for index in range(LEAD_BUCKET_COUNT)}
                if not isinstance(loaded, dict) or any(
                    str(bucket) not in buckets
                    or not isinstance(state, dict)
                    or state.get("preferred_source") not in self.known_sources
                    or state.get("challenger") not in (None, *self.known_sources)
                    or type(state.get("streak")) is not int
                    or state["streak"] < 0
                    for bucket, state in loaded.items()
                ):
                    raise ValueError("Invalid saved forecast recommendation")
                for state in loaded.values():
                    date.fromisoformat(state["assessed_on"])
                self.states = {}
                current_pairs = {(pair.primary, pair.alternative) for pair in self.comparisons}
                for bucket, state in loaded.items():
                    if state["preferred_source"] not in self.sources:
                        continue
                    identity = (state.get("primary_source"), state.get("alternative_source"))
                    if (
                        not self.comparisons
                        or state.get("challenger") not in (None, *self.sources)
                        or identity != (None, None)
                        and identity not in current_pairs
                    ):
                        state = {"preferred_source": state["preferred_source"]}
                    self.states[int(bucket)] = state
            if documents.exists(self.settings_name):
                saved = documents.read(self.settings_name)
                # Retain historical choices without silently rewriting unrelated preferences.
                preferences = self.validate(
                    saved["preferences"], enforce_horizons=False, restoring=True
                )
                if saved["version"] != self.preference_version(preferences):
                    raise ValueError("Invalid saved forecast preferences")
                self.manual, self.configuration_version = preferences, saved["version"]
        except (OSError, ValueError, TypeError, KeyError):
            self.issue = "forecast_selection_storage_unavailable"
            self._restoration_issue = self.issue

    def validate(
        self, preferences, *, enforce_horizons=True, restoring=False, retain_inactive=False
    ):
        """Validate choices without coercion or changes to memory and durable storage.

        Args:
            preferences: One known source or None for Automatic per lead bucket.
            enforce_horizons: Require each chosen source to support the entire
                bucket through its end, clipped to the shared price horizon.
                Disable only when restoring legacy revisions so previously saved
                choices remain readable. New preference writes always enforce it.
            restoring: Accept registered but market-ineligible historical choices.
            retain_inactive: Allow only an unchanged stored ineligible choice in
                its original bucket when saving other preference changes.

        Returns:
            The unchanged preferences as an immutable tuple.

        Raises:
            ValueError: The shape, count or source identity is invalid, or an
                enforced source horizon cannot span its selected window.
        """
        if (
            not isinstance(preferences, (list, tuple))
            or len(preferences) != LEAD_BUCKET_COUNT
            or any(
                source is not None
                and source not in (self.known_sources if restoring else self.sources)
                and not (retain_inactive and source == self.manual[bucket])
                for bucket, source in enumerate(preferences)
            )
        ):
            raise ValueError(
                "Choose Automatic or an available forecast source for every lead period"
            )
        if enforce_horizons and any(
            source is not None
            and source in self.sources
            and min((bucket + 1) * LEAD_BUCKET_HOURS, PRICE_COVERAGE_HOURS)
            > self.source_horizons[source]
            for bucket, source in enumerate(preferences)
        ):
            raise ValueError("Forecast source does not support the selected lead period")
        return tuple(preferences)

    def save_preferences(self, preferences, expected_version):
        """Atomically save an immutable preference revision before installing it."""
        preferences = self.validate(preferences, retain_inactive=True)
        with self.documents.transaction():
            actual = (
                self.documents.read(self.settings_name)["version"]
                if self.documents.exists(self.settings_name)
                else None
            )
            if expected_version != actual:
                raise ValueError("Price source preferences changed. Reload before saving")
            version = self.preference_version(preferences)
            document = {"preferences": list(preferences), "version": version}
            self.documents.write(f"source-preferences-{version}.json", document)
            self.documents.write(self.settings_name, document)
        self.manual, self.configuration_version = preferences, version

    def preference_version(self, preferences):
        """Hash the area and exact preference tuple for both restoration and optimistic writes."""
        return hashlib.sha256(json.dumps([self.area, preferences]).encode()).hexdigest()

    @property
    def active_manual(self):
        """Exclude unsupported historical choices without modifying their saved revision."""
        return tuple(source if source in self.sources else None for source in self.manual)

    @property
    def preferred(self):
        """Apply explicit manual choices while preserving the automatic recommendation."""
        return tuple(
            self.active_manual[bucket]
            or self.state(bucket).get("preferred_source", self.defaults[bucket])
            for bucket in range(LEAD_BUCKET_COUNT)
        )

    def state(self, bucket):
        """Expose pairwise quality with its own sample size and recommendation."""
        state = dict(self.states.get(bucket, {}))
        recommended = state.get("preferred_source", self.defaults[bucket])
        quality = []
        for pair in self.comparisons:
            score = pair.state(bucket)
            quality.append(
                {"primary_source": pair.primary, "alternative_source": pair.alternative, **score}
            )
        return {
            **state,
            "recommended_source": recommended,
            "selection_mode": "manual" if self.active_manual[bucket] else "automatic",
            "comparisons": quality,
        }

    def update(self, series, now, paired=()):
        """Archive each independent pair and retain one global recommendation per UTC day.

        Raises:
            OSError: Initial history, preference or decision restoration failed,
                or an assessment write failed. Initial failures block assessment
                work until a successful reload. Later write failures can retry.
        """
        if self._restoration_issue:
            # Only a successful reload can establish that restoration recovered.
            raise OSError(self._restoration_issue)
        if not self.comparisons:
            return self.states
        for pair in self.comparisons:
            pair.states = pair.update(series, now, paired)
        day = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        proposed = dict(self.states)
        for bucket in range(LEAD_BUCKET_COUNT):
            old = self.states.get(bucket, {})
            if old.get("assessed_on") == day.date().isoformat():
                continue
            current = old.get("preferred_source", self.defaults[bucket])
            candidates, retained = [], []
            for pair in self.comparisons:
                if current not in (pair.primary, pair.alternative):
                    continue
                state = pair.state(bucket)
                score = ForecastScore(
                    state.get("primary_mae"),
                    state.get("alternative_mae"),
                    state.get("matched_hours", 0),
                    state.get("settled_days", 0),
                )
                decision = pair.decide({**old, "preferred_source": current}, score, day)
                decision.update(primary_source=pair.primary, alternative_source=pair.alternative)
                other = pair.alternative if current == pair.primary else pair.primary
                if decision["challenger"] == other or decision["preferred_source"] == other:
                    improvement = (score.primary_mae - score.alternative_mae) * (
                        1 if current == pair.primary else -1
                    )
                    candidates.append((improvement, other, decision))
                else:
                    retained.append(decision)
            if candidates:
                proposed[bucket] = max(
                    candidates, key=lambda candidate: (candidate[0], candidate[1])
                )[2]
            else:
                proposed[bucket] = max(retained, key=lambda decision: decision["matched_hours"])
        with self.documents.transaction():
            if proposed != self.states:
                self.documents.write(
                    f"source-selection-{self.area}-{day.date().isoformat()}.json", proposed
                )
                self.documents.write(self.state_name, proposed)
        return proposed
