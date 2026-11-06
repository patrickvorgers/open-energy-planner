"""Prove responsive maintenance editing and actual interrupted-migration recovery."""

from pathlib import Path

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN


def connect_storage(page, url):
    """Open the actual Data storage page with isolated fixture administration credentials."""
    page.goto(url + "/data-storage")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#database-connection")).to_be_visible()


def capture(page, state):
    """Retain desktop/mobile evidence without capturing credentials or private installation data."""
    destination = Path("results/ci")
    destination.mkdir(parents=True, exist_ok=True)
    page.evaluate("window.scrollTo(0, 0)")
    page.screenshot(
        path=str(destination / f"storage-migration-{page.viewport_size['width']}-{state}.png"),
        full_page=True,
    )


@pytest.mark.parametrize("app_site", ["maintenance"], indirect=True)
@pytest.mark.parametrize(
    "app_page",
    [{"width": 1440, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
)
def test_destination_editor_checks_same_database_and_preserves_input(app_page):
    """Desktop/mobile keep explicit editing and consent, reject self-copy and retain a recoverable editor."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    connect_storage(page, url)
    expect(page.locator("#connection-summary")).to_have_text("SQLite: Local database")
    expect(page.locator("#migration-editor")).to_be_hidden()
    capture(page, "normal")
    page.get_by_role("button", name="Change database", exact=True).click()
    expect(page.get_by_label("Destination database")).to_be_focused()
    page.get_by_label("Destination database").select_option("sqlite")
    page.get_by_role("button", name="Test connection", exact=True).click()
    expect(page.locator("#destination-result")).to_contain_text("same database")
    expect(page.get_by_role("button", name="Migrate database", exact=True)).to_be_hidden()
    expect(page.locator("#migration-editor")).to_be_visible()
    capture(page, "connection-error")
    page.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#migration-editor")).to_be_hidden()
    expect(page.get_by_role("button", name="Change database", exact=True)).to_be_focused()
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


@pytest.mark.parametrize("app_site", ["maintenance_interrupted"], indirect=True)
@pytest.mark.parametrize(
    "app_page",
    [{"width": 1440, "height": 900}, {"width": 768, "height": 1024}, {"width": 390, "height": 844}],
    indirect=True,
)
def test_interrupted_status_survives_reload_and_restores_retained_source(app_page):
    """An actual private interrupted journal stays readable and restores the source without a new copy."""
    page, url = app_page
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    connect_storage(page, url)
    expect(page.locator("#migration-phase")).to_have_text(
        "Migration interrupted: Review recovery options"
    )
    expect(page.get_by_role("button", name="Change database", exact=True)).to_be_disabled()
    page.get_by_text("Recovery instructions", exact=True).click()
    with page.expect_download() as downloaded:
        page.get_by_role("link", name="Download recovery instructions", exact=True).click()
    recovery = Path(downloaded.value.path()).read_text()
    assert "--recover-storage" in recovery
    assert "backup consistent with the released target" in recovery
    assert "Never restore pre-migration bootstrap settings after release" in recovery
    assert "fixture@" not in recovery
    capture(page, "interrupted")
    page.reload()
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#migration-phase")).to_have_text(
        "Migration interrupted: Review recovery options"
    )
    page.route(
        "**/v1/storage/migration/restore",
        lambda route: route.fulfill(
            status=503, body="Cannot save database maintenance state. Check free space."
        ),
    )
    page.get_by_role("button", name="Restore previous database", exact=True).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Restore database", exact=True
    ).click()
    expect(page.locator("#migration-error")).to_contain_text("Check free space")
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#migration-error")).to_contain_text("Check free space")
    page.unroute("**/v1/storage/migration/restore")
    page.get_by_role("button", name="Restore previous database", exact=True).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Restore database", exact=True
    ).click()
    expect(page.locator("#migration-phase")).to_have_text(
        "Previous database restored", timeout=15000
    )
    expect(page.locator("#storage")).to_be_visible()
    expect(page.locator("#migration-error")).to_be_hidden()
    expect(page.get_by_role("button", name="Change database", exact=True)).to_be_enabled()
    capture(page, "restored")
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    assert not errors


@pytest.mark.parametrize("app_site", ["maintenance"], indirect=True)
def test_destination_edits_invalidate_checked_overwrite_consent(app_page):
    """A simulated successful connection check cannot authorize a different edited destination."""
    page, url = app_page
    connect_storage(page, url)
    page.route(
        "**/v1/storage/migration/check",
        lambda route: route.fulfill(
            json={
                "backend": "PostgreSQL",
                "destination": "PostgreSQL: fixture-db / oep",
                "contains_oep_data": True,
                "check_id": "simulated-preflight-proof",
            }
        ),
    )
    page.get_by_role("button", name="Change database", exact=True).click()
    page.get_by_label("Connection URL", exact=True).fill(
        "postgresql+psycopg://fixture:fixture@fixture-db/oep"
    )
    page.get_by_role("button", name="Test connection", exact=True).click()
    expect(page.locator("#destination-result")).to_have_text(
        "Connection verified: Existing data belonging to this app will be replaced."
    )
    expect(
        page.get_by_text(
            "After migration and successful startup checks, the app resumes automatically using the new database. "
            "Planning may wait for fresh Home Assistant measurements and its other required inputs. "
            "The original database is retained but is no longer updated.",
            exact=True,
        )
    ).to_be_visible()
    capture(page, "checked-destination")
    expect(page.get_by_role("button", name="Migrate database", exact=True)).to_be_disabled()
    page.locator("#overwrite-destination").check()
    expect(page.get_by_role("button", name="Migrate database", exact=True)).to_be_enabled()
    page.route(
        "**/v1/storage/migration/start",
        lambda route: route.fulfill(
            status=503, body="Cannot save database maintenance state. Check free space."
        ),
    )
    page.get_by_role("button", name="Migrate database", exact=True).click()
    confirmation = page.get_by_role("alertdialog", name="Change database?", exact=True)
    expect(confirmation).to_contain_text("PostgreSQL: fixture-db / oep")
    confirmation.get_by_role("button", name="Cancel", exact=True).click()
    expect(page.locator("#destination-result")).to_contain_text("Connection verified")
    page.get_by_role("button", name="Migrate database", exact=True).click()
    page.get_by_role("alertdialog").get_by_role(
        "button", name="Change database", exact=True
    ).click()
    expect(page.locator("#destination-result")).to_contain_text("Check free space")
    expect(page.get_by_label("Connection URL", exact=True)).to_have_value(
        "postgresql+psycopg://fixture:fixture@fixture-db/oep"
    )
    expect(page.get_by_role("button", name="Migrate database", exact=True)).to_be_enabled()
    page.get_by_label("Connection URL", exact=True).fill(
        "postgresql+psycopg://fixture:fixture@another-db/oep"
    )
    expect(page.locator("#migration-confirmation")).to_be_hidden()
    expect(page.locator("#overwrite-destination")).not_to_be_checked()
    expect(page.locator("#start-migration")).to_be_disabled()


@pytest.mark.parametrize("app_site", ["maintenance"], indirect=True)
def test_cancelled_preflight_cannot_restore_confirmation_after_reopening(app_page):
    """A delayed success for a canceled editor must not authorize its old destination in a reopened editor."""
    page, url = app_page
    connect_storage(page, url)
    pending = []
    page.route("**/v1/storage/migration/check", lambda route: pending.append(route))
    page.get_by_role("button", name="Change database", exact=True).click()
    page.get_by_label("Connection URL", exact=True).fill(
        "postgresql+psycopg://fixture:fixture@old-destination/oep"
    )
    with page.expect_request("**/v1/storage/migration/check"):
        page.get_by_role("button", name="Test connection", exact=True).click()
    page.get_by_role("button", name="Cancel", exact=True).click()
    page.get_by_role("button", name="Change database", exact=True).click()
    pending[0].fulfill(
        json={
            "backend": "PostgreSQL",
            "destination": "PostgreSQL: old-destination / oep",
            "contains_oep_data": True,
            "check_id": "stale-editor-proof",
        }
    )
    expect(page.get_by_role("button", name="Test connection", exact=True)).to_be_enabled()
    expect(page.locator("#migration-editor")).to_be_visible()
    expect(page.locator("#migration-confirmation")).to_be_hidden()
    expect(page.locator("#start-migration")).to_be_disabled()
    expect(page.get_by_label("Connection URL", exact=True)).to_have_value("")


@pytest.mark.parametrize("app_site", ["maintenance_interrupted"], indirect=True)
def test_retry_confirmation_preserves_cancel_and_rejects_newer_recovery_state(app_page):
    """Cancellation and stale consent never restart copying, while an accepted retry retains real failure advice."""
    page, url = app_page
    connect_storage(page, url)
    attempts = []

    def reject_retry(route):
        """Record an actual admitted request without starting a database copy in a browser testcase."""
        attempts.append(route.request.post_data_json)
        route.fulfill(status=503, body="Cannot save database maintenance state. Check free space.")

    page.route("**/v1/storage/migration/retry", reject_retry)
    page.get_by_role("button", name="Retry migration", exact=True).click()
    confirmation = page.get_by_role("alertdialog", name="Retry database migration?", exact=True)
    expect(confirmation).to_contain_text("original database is retained")
    confirmation.get_by_role("button", name="Cancel", exact=True).click()
    assert attempts == []
    page.get_by_role("button", name="Retry migration", exact=True).click()
    confirmation.get_by_role("button", name="Retry migration", exact=True).click()
    expect(page.locator("#migration-error")).to_contain_text("Check free space")
    assert attempts == [{"overwrite": True}]

    page.get_by_role("button", name="Retry migration", exact=True).click()
    newer = page.request.get(
        url + "/v1/storage/migration", headers={"Authorization": f"Bearer {TOKEN}"}
    ).json()
    newer.update(phase="completed", released=True, can_retry=False, can_restore=False)
    page.route("**/v1/storage/migration", lambda route: route.fulfill(json=newer))
    expect(page.locator("#migration-phase")).to_have_text(
        "Migration complete: The app is using the new database"
    )
    confirmation.get_by_role("button", name="Retry migration", exact=True).click()
    expect(confirmation).to_have_count(0)
    assert attempts == [{"overwrite": True}]
