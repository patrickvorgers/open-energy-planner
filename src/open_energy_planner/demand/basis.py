"""Immutable archived demand forecast provenance."""

from dataclasses import dataclass


@dataclass(frozen=True)
class DemandEvidence:
    """Retain the original archived demand prediction, metering definition and model.

    The forecast version identifies the original immutable prediction archive.
    Transforming load does not change that source identity.
    """

    forecast_version: str
    configuration_version: str
    training_version: str
    model: str
    quality: str
    excluded_devices: tuple[str, ...]
