"""The shared calendar preserves page-owned day and range boundaries."""

from datetime import UTC, datetime

from playwright.sync_api import expect


def mount(page, url, *, day_only=False):
    """Mount the production calendar with synthetic dates and an observable selection callback."""
    page.clock.install(time=datetime(2026, 11, 18, 12, tzinfo=UTC))
    page.goto(url + "/activity")
    page.evaluate(
        """async dayOnly => {
        const {useHomeAssistantTimezone} = await import('./setup/display.js');
        useHomeAssistantTimezone('Europe/Amsterdam');
        const {createPeriodSelector} = await import('./setup/period-selector.js');
        const host = document.createElement('section'); host.id = 'calendar-test';
        document.querySelector('main').prepend(host);
        window.dateChanges = [];
        window.dateControl = createPeriodSelector(host, {dayOnly, onChange: value => window.dateChanges.push(value)});
        window.dateControl.set({start:'2026-11-18', end:'2026-11-18', min:'2026-10-01', max:'2026-11-18'});
    }""",
        day_only,
    )
    return page.locator("#calendar-test")


def test_single_day_calendar_rejects_unavailable_days_and_preserves_cancel(app_page):
    """Single-day pages offer no ranges, skip archived holes and do not apply cancelled drafts."""
    page, url = app_page
    host = mount(page, url, day_only=True)
    page.evaluate("window.dateControl.set({availableDays:['2026-11-16','2026-11-18']})")
    host.get_by_role("button", name="Previous day").click()
    assert page.evaluate("window.dateChanges") == [{"start": "2026-11-16", "end": "2026-11-16"}]
    host.get_by_role("button", name="Choose day", exact=True).click()
    dialog = page.get_by_role("dialog", name="Choose day", exact=True)
    expect(dialog.get_by_role("button", name="This week", exact=True)).to_have_count(0)
    expect(dialog.get_by_role("button", name="11/17/2026", exact=True)).to_be_disabled()
    expect(dialog.get_by_role("button", name="Yesterday", exact=True)).to_be_disabled()
    expect(dialog.get_by_label("To", exact=True)).to_be_hidden()
    dialog.get_by_label("Day", exact=True).fill("2026-11-18")
    page.keyboard.press("Escape")
    assert page.evaluate("window.dateControl.value") == {"start": "2026-11-16", "end": "2026-11-16"}
    host.get_by_role("button", name="Next day").click()
    expect(host.get_by_role("button", name="Next day")).to_be_disabled()
    assert page.evaluate("window.dateControl.value") == {"start": "2026-11-18", "end": "2026-11-18"}


def test_period_calendar_roundtrip_returns_to_clipped_current_month(app_page):
    """Previous month and Next return to this elapsed month, while cancelled custom drafts retain month navigation."""
    page, url = app_page
    host = mount(page, url)
    host.get_by_role("button", name="Choose period", exact=True).click()
    page.get_by_role("dialog", name="Choose period").get_by_role(
        "button", name="This month", exact=True
    ).click()
    host.get_by_role("button", name="Previous period").click()
    assert page.evaluate("window.dateControl.value") == {"start": "2026-10-01", "end": "2026-10-31"}
    host.get_by_role("button", name="Choose period", exact=True).click()
    dialog = page.get_by_role("dialog", name="Choose period")
    dialog.get_by_label("From", exact=True).fill("2026-10-05")
    dialog.get_by_role("button", name="Close date selector").click()
    host.get_by_role("button", name="Next period").click()
    assert page.evaluate("window.dateControl.value") == {"start": "2026-11-01", "end": "2026-11-18"}
    expect(host.get_by_role("button", name="Next period")).to_be_disabled()


def test_week_navigation_returns_from_a_clipped_first_week(app_page):
    """A first partial retained week still returns to the current Monday-based week with Next."""
    page, url = app_page
    host = mount(page, url)
    page.evaluate("window.dateControl.set({min:'2026-11-12'})")
    host.get_by_role("button", name="Choose period", exact=True).click()
    page.get_by_role("dialog", name="Choose period").get_by_role(
        "button", name="This week", exact=True
    ).click()
    host.get_by_role("button", name="Previous period").click()
    assert page.evaluate("window.dateControl.value") == {"start": "2026-11-12", "end": "2026-11-15"}
    host.get_by_role("button", name="Next period").click()
    assert page.evaluate("window.dateControl.value") == {"start": "2026-11-16", "end": "2026-11-18"}
    expect(host.get_by_role("button", name="Next period")).to_be_disabled()
