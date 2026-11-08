"""Verify revision-matched watch confirmations and explicit source-refresh request handling."""

from datetime import UTC, datetime, timedelta

import pytest

from open_energy_planner.home_assistant.catalogue import HACatalogue
from tests.ha_contract.support.metadata import setup_metadata


def test_matching_confirmation_preserves_publication_and_expires_without_contact():
    """An exact revision extends watch freshness while preserving data and its publication timestamp."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    body = setup_metadata()
    catalogue.receive(body, now)
    revision = catalogue.revision
    checked = now + timedelta(minutes=14)
    catalogue.confirm(revision, checked)
    assert catalogue.updated == now
    assert catalogue.value is body
    assert not catalogue.current(now + timedelta(minutes=28))["stale"]
    assert catalogue.current(now + timedelta(minutes=30))["stale"]
    assert catalogue.refresh_status()["published_at"] == now.isoformat()
    assert catalogue.refresh_status()["confirmed_at"] == checked.isoformat()


@pytest.mark.parametrize("revision", [None, "", "different", "0" * 64])
def test_wrong_confirmation_never_hides_stale_sources(revision):
    """Malformed or unrelated revision receipts retain the last proven freshness time."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    catalogue.receive(setup_metadata(), now)
    with pytest.raises(ValueError):
        catalogue.confirm(revision, now + timedelta(minutes=20))
    assert catalogue.confirmed == now
    assert catalogue.current(now + timedelta(minutes=20))["stale"]


def test_pending_requests_coalesce_and_failed_attempt_can_be_retried():
    """Explicit refresh is independent from configuration, and a stale failure cannot cancel a newer request."""
    catalogue = HACatalogue()
    first = catalogue.request_refresh()
    assert catalogue.request_refresh() == first
    assert catalogue.refresh_status()["pending"]
    catalogue.fail_refresh(first)
    assert catalogue.refresh_status()["failed"]
    assert not catalogue.refresh_status()["pending"]
    second = catalogue.request_refresh()
    assert second != first
    with pytest.raises(ValueError):
        catalogue.fail_refresh(first)
    assert catalogue.requested == second
    assert catalogue.refresh_status()["pending"]


def test_partial_active_transfer_cannot_confirm_an_old_revision():
    """An unchanged old digest cannot hide an incomplete newer active capture."""
    from tests.ha_contract.test_catalogue_transfer import snapshot_pages

    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    catalogue.receive(setup_metadata(), now)
    catalogue.receive_page(snapshot_pages(setup_metadata(), now)[0], now)
    with pytest.raises(ValueError):
        catalogue.confirm(catalogue.revision, now + timedelta(minutes=14))
    assert catalogue.confirmed == now


def test_lost_refresh_outcome_expires_without_blocking_explicit_retry():
    """A missing peer reply cannot trap every later user refresh behind the same old nonce."""
    now = datetime(2026, 11, 7, tzinfo=UTC)
    catalogue = HACatalogue()
    first = catalogue.request_refresh(now)
    assert catalogue.refresh_status(now + timedelta(seconds=59))["pending"]
    expired = catalogue.refresh_status(now + timedelta(seconds=60))
    assert not expired["pending"] and expired["failed"]
    second = catalogue.request_refresh(now + timedelta(seconds=61))
    assert second != first
    with pytest.raises(ValueError):
        catalogue.fail_refresh(first)
    assert catalogue.refresh_status(now + timedelta(seconds=61))["id"] == second
