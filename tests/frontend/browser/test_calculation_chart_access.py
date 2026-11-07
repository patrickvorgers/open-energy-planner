"""Daily calculation charts expose exact details without a duplicate visible record list."""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

import pytest
from playwright.sync_api import expect

from tests.frontend.browser.helpers import TOKEN, discover


@pytest.fixture
def daily_history(app_page):
    """Serve 96 independent synthetic quarter-hour summaries through the existing daily wire contract."""
    page, url = app_page
    discover(page, url)
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = page.request.get(url + "/v1/planning-performance", headers=headers)
    assert response.ok
    evidence = response.json()
    template = evidence["calculations"][0]
    detail = page.request.get(
        url + "/v1/planning-performance/" + template["id"], headers=headers
    ).json()
    start = datetime.now(ZoneInfo("Europe/Amsterdam")).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    rows = []
    for index in range(96):
        duration = 7 + (index % 9) * 1.5 + max(0, 24 - abs(index - 32) * 3)
        rows.append(
            {
                **template,
                "id": f"synthetic-calculation-{index:03}",
                "started_at": (start + timedelta(minutes=15 * index)).astimezone(UTC).isoformat(),
                "duration_seconds": duration,
                "first_candidate_seconds": None if index % 13 == 0 else 4,
                "reason": "candidate_rejected" if index == 11 else "published",
            }
        )
    evidence = {**evidence, "calculations": rows}
    detail_reads, daily_reads = [], []

    def respond(route):
        """Return literal dated evidence and record exact requested identities without changing stored history."""
        address = urlsplit(route.request.url)
        identity = address.path.rsplit("/", 1)[-1]
        if identity != "planning-performance":
            detail_reads.append(identity)
            selected = next(row for row in rows if row["id"] == identity)
            route.fulfill(json={**detail, **selected, "progress": []})
            return
        selected_day = parse_qs(address.query).get("day", [evidence["day"]])[0]
        daily_reads.append(selected_day)
        previous = (start - timedelta(days=1)).date().isoformat()
        selected_rows = rows if selected_day == evidence["day"] else []
        if selected_day == previous:
            selected_rows = [
                {**rows[0], "started_at": (start - timedelta(days=1)).astimezone(UTC).isoformat()}
            ]
        route.fulfill(json={**evidence, "day": selected_day, "calculations": selected_rows})

    page.route("**/v1/planning-performance**", respond)
    page.goto(url + "/planning-performance")
    page.get_by_label("Installation API token").fill(TOKEN)
    page.get_by_role("button", name="Connect", exact=True).click()
    expect(page.locator("#performance-summary")).to_contain_text("96 completed calculations")
    return page, evidence, detail_reads, daily_reads


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_chart_keyboard_access_preserves_all_records_and_detail_identity(daily_history):
    """A full day stays reachable by chart keys alone without rendering a list."""
    page, evidence, detail_reads, daily_reads = daily_history
    canvas = page.locator("#performance-chart")
    selection = page.locator("#performance-chart")
    expect(selection).to_have_attribute("aria-valuemax", "95")
    expect(canvas).to_have_role("slider")
    expect(page.get_by_role("heading", name="Calculations", exact=True)).to_have_count(0)
    expect(page.locator("#performance-list")).to_have_count(0)
    expect(page.locator(".performance-access")).to_have_count(0)
    expect(page.get_by_role("button", name="View calculation at")).to_have_count(0)
    captures = Path("results/ci/calculation-history-list")
    captures.mkdir(parents=True, exist_ok=True)
    width = page.viewport_size["width"]
    page.screenshot(path=str(captures / f"chart-only-{width}.png"), full_page=True)
    canvas.focus()
    canvas.press("End")
    expect(selection).to_have_attribute("aria-valuenow", "95")
    canvas.press("Enter")
    dialog = page.get_by_role("dialog")
    expect(dialog).to_be_visible()
    assert detail_reads == ["synthetic-calculation-095"]
    page.keyboard.press("Escape")
    expect(canvas).to_be_focused()
    expect(selection).to_have_attribute("aria-valuenow", "95")
    canvas.press("Home")
    canvas.press("ArrowUp")
    expect(canvas).to_have_attribute("aria-valuenow", "1")
    canvas.press("ArrowDown")
    expect(canvas).to_have_attribute("aria-valuenow", "0")
    canvas.press("ArrowRight")
    canvas.press(" ")
    expect(dialog).to_be_visible()
    assert detail_reads[-1] == "synthetic-calculation-001"
    page.keyboard.press("Escape")
    selection.focus()
    selection.press("End")
    expect(selection).to_have_attribute("aria-valuenow", "95")
    page.screenshot(path=str(captures / f"keyboard-chart-{width}.png"), full_page=False)
    page.locator("#performance-chart").press("Enter")
    expect(dialog).to_be_visible()
    assert detail_reads[-1] == "synthetic-calculation-095"
    page.keyboard.press("Escape")
    assert daily_reads == [evidence["day"]]
    for resized_width in (768, 390, 1280):
        page.set_viewport_size({"width": resized_width, "height": 900})
        page.wait_for_function("() => document.documentElement.scrollWidth <= innerWidth")
        assert (
            canvas.evaluate("canvas => Chart.getChart(canvas).data.datasets[0].data.length") == 96
        )
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.set_viewport_size({"width": width, "height": 900 if width > 500 else 844})
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#performance-status")).to_have_text("")
    expect(selection).to_have_attribute("aria-valuenow", "95")
    previous_day = (datetime.fromisoformat(evidence["day"]) - timedelta(days=1)).date().isoformat()
    page.get_by_label("Day", exact=True).fill(previous_day)
    expect(selection).to_have_attribute("aria-valuemax", "0")
    page.get_by_role("button", name="Today", exact=True).click()
    expect(selection).to_have_attribute("aria-valuemax", "95")


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_chart_access_waits_for_reads_and_recovers_without_stale_day_data(daily_history):
    """Pending, failed and superseded reads cannot open a calculation under another selected day."""
    page, evidence, _, _ = daily_history
    pending = []
    page.route("**/v1/planning-performance?day=*", lambda route: pending.append(route))
    canvas = page.locator("#performance-chart")
    selection = page.locator("#performance-chart")
    canvas.focus()
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(selection).to_have_attribute("aria-disabled", "true")
    expect(page.locator("#performance-open")).to_have_count(0)
    expect(page.locator("#performance-status")).to_contain_text("Loading")
    canvas.focus()
    canvas.press("Enter")
    expect(page.get_by_role("dialog")).not_to_be_visible()
    assert len(pending) == 1
    pending.pop().fulfill(status=503, body="private provider response")
    expect(page.locator("#performance-status")).to_contain_text("could not be loaded")
    expect(selection).to_have_attribute("aria-disabled", "true")
    expect(page.locator("body")).not_to_contain_text("private provider response")
    page.get_by_role("button", name="Refresh", exact=True).click()
    assert len(pending) == 1
    pending.pop().fulfill(json=evidence)
    expect(selection).to_have_attribute("aria-disabled", "false")
    expect(selection).to_have_attribute("aria-valuemax", "95")
    page.get_by_label("Day", exact=True).fill("2020-01-01")
    page.get_by_role("button", name="Today", exact=True).click()
    assert len(pending) == 2
    old_read, current_read = pending
    current_read.fulfill(json=evidence)
    expect(page.locator("#performance-summary")).to_contain_text("96 completed calculations")
    old_read.fulfill(json={**evidence, "day": "2020-01-01", "calculations": []})
    expect(page.get_by_label("Day", exact=True)).to_have_value(evidence["day"])
    expect(selection).to_have_attribute("aria-valuemax", "95")
    page.route(
        "**/v1/planning-performance?day=2020-01-02",
        lambda route: route.fulfill(json={**evidence, "day": "2020-01-02", "calculations": []}),
    )
    page.get_by_label("Day", exact=True).fill("2020-01-02")
    expect(page.locator("#performance-summary")).to_contain_text("No completed calculations")
    expect(selection).to_have_attribute("aria-disabled", "true")
    expect(page.locator("#performance-open")).to_have_count(0)


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_chart_access_preserves_zero_long_and_intermediate_calculations(daily_history):
    """Zero-length and long timings retain seconds, and intermediate runs remain reachable without a list."""
    page, evidence, detail_reads, _ = daily_history
    failures = []
    page.on("pageerror", lambda error: failures.append(str(error)))
    zero, long = evidence["calculations"][:2]
    zero.update(
        started_at=(datetime.fromisoformat(zero["started_at"]) + timedelta(minutes=5)).isoformat(),
        duration_seconds=0,
        first_candidate_seconds=0,
    )
    long.update(duration_seconds=3600.2345, first_candidate_seconds=11.24982)
    page.route(
        "**/v1/planning-performance?day=*",
        lambda route: route.fulfill(json={**evidence, "calculations": [zero, long]}),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#performance-summary")).to_contain_text("2 completed calculations")
    expect(page.locator("#performance-selected")).to_contain_text("3,600.2 s")
    canvas = page.locator("#performance-chart")
    assert canvas.evaluate("canvas => Chart.getChart(canvas).data.datasets[0].data.length") == 1
    extra = page.get_by_label("Show additional calculations between quarter hours")
    extra.check()
    canvas.focus()
    canvas.press("Home")
    canvas.press("Enter")
    expect(page.locator("#calculation-summary")).to_contain_text("Total 0 s")
    assert detail_reads[-1] == zero["id"]
    page.keyboard.press("Escape")
    for toggle in page.locator("#performance-legend input").all():
        toggle.uncheck()
    canvas.focus()
    canvas.press("End")
    canvas.press("Enter")
    expect(page.locator("#calculation-summary")).to_contain_text("Total 3,600.2 s")
    assert detail_reads[-1] == long["id"]
    page.keyboard.press("Escape")
    extra.uncheck()
    page.route(
        "**/v1/planning-performance?day=*",
        lambda route: route.fulfill(json={**evidence, "calculations": [zero]}),
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#performance-summary")).to_contain_text("Enable additional calculations")
    expect(page.locator("#performance-chart")).to_have_attribute("aria-disabled", "true")
    extra.check()
    expect(page.locator("#performance-chart")).to_have_attribute("aria-disabled", "false")
    assert not failures


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
@pytest.mark.parametrize(
    ("locale", "first_time", "further_time", "total_time", "unknown_time"),
    [("en-US", "11.2", "15.6", "26.8", "8.5"), ("nl-NL", "11,2", "15,6", "26,8", "8,5")],
)
def test_stacked_calculation_tooltip_combines_sections_and_rounds_seconds(
    daily_history, browser, locale, first_time, further_time, total_time, unknown_time
):
    """Either section reveals the same complete localized tooltip without rounding stored chart values."""
    original_page, evidence, _, _ = daily_history
    address = urlsplit(original_page.url)
    url = f"{address.scheme}://{address.netloc}"
    headers = {"Authorization": f"Bearer {TOKEN}"}
    original_summary = original_page.request.get(
        url + "/v1/planning-performance", headers=headers
    ).json()["calculations"][0]
    original_detail = original_page.request.get(
        url + "/v1/planning-performance/" + original_summary["id"], headers=headers
    ).json()
    rows = [dict(row) for row in evidence["calculations"]]
    rows[4].update(first_candidate_seconds=11.24982, duration_seconds=26.80314)
    rows[5].update(first_candidate_seconds=None, duration_seconds=8.456789)
    calculation = {
        **original_detail,
        **rows[4],
        "progress": [
            {"elapsed_seconds": 11.24982, "objective": 10.0, "best_bound": 9.8},
            {"elapsed_seconds": 26.80314, "objective": 9.9, "best_bound": 9.8},
        ],
    }
    detail_reads = []

    def respond(route):
        """Keep precise independent timings and route the clicked identity to its original detail contract."""
        if urlsplit(route.request.url).path.endswith("/v1/planning-performance"):
            route.fulfill(json={**evidence, "calculations": rows})
        else:
            detail_reads.append(route.request.url.rsplit("/", 1)[-1])
            route.fulfill(json=calculation)

    with browser.new_context(
        viewport=original_page.viewport_size,
        has_touch=original_page.viewport_size["width"] < 500,
        locale=locale,
        timezone_id="UTC",
    ) as context:
        page = context.new_page()
        page.route("**/v1/planning-performance**", respond)
        page.goto(url + "/planning-performance")
        page.get_by_label("Installation API token").fill(TOKEN)
        page.get_by_role("button", name="Connect", exact=True).click()
        expect(page.locator("#performance-summary")).to_contain_text("96 completed calculations")

        def hover_segment(canvas, dataset_index, row_index):
            """Move the actual pointer into a rendered segment, then read the tooltip produced by Chart.js."""
            canvas.scroll_into_view_if_needed()
            point = canvas.evaluate(
                """(canvas, indices) => {
                    const bar = Chart.getChart(canvas).getDatasetMeta(indices[0]).data[indices[1]];
                    return {x: bar.x, y: Number.isFinite(bar.base) ? (bar.y + bar.base) / 2 : bar.y};
                }""",
                [dataset_index, row_index],
            )
            box = canvas.bounding_box()
            page.mouse.move(box["x"] + point["x"], box["y"] + point["y"])
            page.wait_for_function(
                """({canvas, index}) => {const tooltip = Chart.getChart(canvas).tooltip;
                    return tooltip.opacity > 0 && tooltip.dataPoints.some(item => item.dataIndex === index);} """,
                arg={"canvas": canvas.element_handle(), "index": row_index},
            )
            return canvas.evaluate(
                """canvas => {const tooltip = Chart.getChart(canvas).tooltip;
                    return {body: tooltip.body.flatMap(item => item.lines), after: tooltip.afterBody,
                            title: tooltip.title};} """
            )

        canvas = page.locator("#performance-chart")
        first = hover_segment(canvas, 0, 4)
        assert first["body"] == [
            f"First candidate observed: {first_time} s",
            f"Further calculation: {further_time} s",
        ]
        assert first["after"] == [f"Total calculation: {total_time} s", "Plan available"]
        assert hover_segment(canvas, 1, 4) == first
        assert (
            canvas.evaluate("canvas => Chart.getChart(canvas).data.datasets[0].data[4].y")
            == 11.24982
        )
        if locale == "nl-NL":
            captures = Path("results/ci/calculation-history-list")
            canvas.screenshot(
                path=str(captures / f"combined-tooltip-{page.viewport_size['width']}.png")
            )
        unknown = hover_segment(canvas, 2, 5)
        assert unknown["body"] == [f"First time unknown: {unknown_time} s"]
        assert unknown["after"] == [
            f"Total calculation: {unknown_time} s",
            "First candidate time not recorded.",
            "Plan available",
        ]
        hover_segment(canvas, 0, 4)
        page.mouse.down()
        page.mouse.up()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible()
        assert detail_reads == ["synthetic-calculation-004"]
        improvement = hover_segment(page.locator("#improvement-chart"), 0, 0)
        assert improvement["title"] == [f"{first_time} s"]


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_chart_modifier_shortcuts_do_not_select_or_open_calculations(daily_history):
    """Browser and assistive shortcuts retain their default and cannot change the selected calculation."""
    page, _, detail_reads, _ = daily_history
    canvas = page.locator("#performance-chart")
    canvas.focus()
    original = page.locator("#performance-selected").text_content()
    observed = canvas.evaluate(
        """canvas => {
        const results = [];
        for (const modifier of ['altKey', 'ctrlKey', 'metaKey', 'shiftKey']) {
            for (const key of ['ArrowLeft', 'ArrowRight', 'Home', 'End', 'Enter', ' ']) {
                const event = new KeyboardEvent('keydown', {key, [modifier]: true, bubbles: true, cancelable: true});
                canvas.dispatchEvent(event);
                const opened = document.querySelector('#calculation-detail').open;
                results.push({modifier, key, prevented: event.defaultPrevented, opened,
                    selected: document.querySelector('#performance-selected').textContent});
                if (opened) document.querySelector('#calculation-detail').close();
            }
        }
        return results;
        }"""
    )
    assert all(
        not event["prevented"] and not event["opened"] and event["selected"] == original
        for event in observed
    ), observed
    assert not detail_reads
    canvas.press("ArrowRight")
    expect(page.locator("#performance-selected")).not_to_have_text(original)
    canvas.press("Home")
    expect(page.locator("#performance-selected")).to_have_text(original)
    canvas.press("Enter")
    expect(page.get_by_role("dialog")).to_be_visible()
    assert detail_reads == ["synthetic-calculation-000"]


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
@pytest.mark.parametrize("target", ["history", "detail", "session"])
@pytest.mark.parametrize("failure", ["fetch", "json", "render"])
def test_calculation_read_failures_never_display_exception_text(daily_history, target, failure):
    """Transport, parsing and projection failures show owned advice across every evidence entry point."""
    page, _, _, _ = daily_history
    secret = "synthetic-private-exception-identity"
    script = """({target, failure, secret}) => {
        const original = window.fetch;
        window.fetch = async (path, options) => {
            const address = String(path);
            const selected = target === 'session' ? address.endsWith('setup/session') :
                target === 'detail' ? address.includes('v1/planning-performance/') :
                address.includes('v1/planning-performance?');
            if (!selected) return original(path, options);
            if (failure === 'fetch') throw new Error(secret);
            const response = await original(path, options);
            if (failure === 'json') response.json = async () => { throw new SyntaxError(secret); };
            else {
                const read = response.json.bind(response);
                response.json = async () => {
                    const value = await read();
                    const field = target === 'session' ? 'timezone' : target === 'detail' ? 'progress' : 'calculations';
                    Object.defineProperty(value, field, {get() { throw new Error(secret); }});
                    return value;
                };
            }
            return response;
        };
    }"""
    values = {"target": target, "failure": failure, "secret": secret}
    if target == "session":
        page.add_init_script("(" + script + ")(" + json.dumps(values) + ")")
        page.reload()
    else:
        page.evaluate(script, values)
        if target == "detail":
            page.locator("#performance-chart").focus()
            page.locator("#performance-chart").press("Enter")
        else:
            page.get_by_role("button", name="Refresh", exact=True).click()
    status = page.locator("#calculation-summary" if target == "detail" else "#performance-status")
    expect(status).to_contain_text("could not be loaded")
    expect(page.locator("body")).not_to_contain_text(secret)


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_calculation_history_coalesces_refresh_and_settles_replaced_day(daily_history):
    """A replacement waits for an aborted read to settle, while repeated refresh shares one request."""
    page, evidence, _, _ = daily_history
    page.evaluate(
        """() => {
        const original = window.fetch;
        window.calculationReads = {starts: 0, active: 0, maximum: 0, aborted: 0};
        const state = window.calculationReads;
        window.fetch = (path, options) => {
            if (!String(path).includes('v1/planning-performance?')) return original(path, options);
            state.starts++; state.active++; state.maximum = Math.max(state.maximum, state.active);
            return new Promise((resolve, reject) => {
                state.finish = value => {state.active--; resolve(new Response(JSON.stringify(value)));};
                options?.signal?.addEventListener('abort', () => {
                    state.aborted++;
                    state.settleAborted = () => {state.active--; reject(new DOMException('Aborted', 'AbortError'));};
                }, {once: true});
            });
        };
        }"""
    )
    refresh = page.get_by_role("button", name="Refresh", exact=True)
    refresh.click()
    refresh.click()
    refresh.click()
    assert page.evaluate("calculationReads.starts") == 1
    page.get_by_label("Day", exact=True).fill("2020-01-01")
    expect(page.locator("#performance-chart")).to_have_attribute("aria-disabled", "true")
    assert page.evaluate("calculationReads.aborted") == 1
    assert page.evaluate("calculationReads.starts") == 1
    page.evaluate("calculationReads.settleAborted()")
    page.wait_for_function("() => calculationReads.starts === 2")
    page.evaluate("value => calculationReads.finish(value)", {**evidence, "day": "2020-01-01"})
    expect(page.locator("#performance-status")).to_have_text("")
    expect(page.get_by_label("Day", exact=True)).to_have_value("2020-01-01")
    assert page.evaluate("calculationReads.active") == 0
    assert page.evaluate("calculationReads.maximum") == 1


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
@pytest.mark.parametrize("target", ["history", "detail"])
@pytest.mark.parametrize("stage", ["headers", "body"])
def test_calculation_reads_time_out_during_headers_or_body_and_recover(
    daily_history, target, stage
):
    """A five-second budget includes body parsing and is cleaned up before a successful manual retry."""
    page, _, _, _ = daily_history
    beginning = datetime(2026, 11, 7, tzinfo=UTC)
    page.clock.install(time=beginning)
    page.clock.pause_at(beginning + timedelta(seconds=1))
    page.evaluate(
        """({target, stage}) => {
        window.originalCalculationFetch = window.fetch;
        window.calculationAborts = 0;
        window.fetch = async (path, options) => {
            const selected = target === 'detail' ? String(path).includes('v1/planning-performance/') :
                String(path).includes('v1/planning-performance?');
            if (!selected) return window.originalCalculationFetch(path, options);
            const stalled = () => new Promise((resolve, reject) => options?.signal?.addEventListener('abort', () => {
                window.calculationAborts++; reject(new DOMException('Aborted', 'AbortError'));
            }, {once: true}));
            return stage === 'headers' ? stalled() : {ok: true, status: 200, json: stalled};
        };
        }""",
        {"target": target, "stage": stage},
    )
    if target == "detail":
        page.locator("#performance-chart").focus()
        page.locator("#performance-chart").press("Enter")
    else:
        page.get_by_role("button", name="Refresh", exact=True).click()
    status = page.locator("#calculation-summary" if target == "detail" else "#performance-status")
    expect(status).to_contain_text("Loading")
    page.clock.run_for(4999)
    expect(status).to_contain_text("Loading")
    page.clock.run_for(2)
    expect(status).to_contain_text("could not be loaded")
    assert page.evaluate("calculationAborts") == 1
    page.evaluate("window.fetch = window.originalCalculationFetch")
    if target == "detail":
        page.keyboard.press("Escape")
        page.locator("#performance-chart").press("Enter")
        expect(status).to_contain_text("Total")
    else:
        page.get_by_role("button", name="Refresh", exact=True).click()
        expect(status).to_have_text("")
        expect(page.locator("#performance-chart")).to_have_attribute("aria-disabled", "false")
    page.clock.run_for(5001)
    assert page.evaluate("calculationAborts") == 1


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_calculation_detail_can_reopen_before_its_previous_close_event(daily_history):
    """A queued close event belongs to the closed popup and cannot cancel the newly opened detail."""
    page, _, _, _ = daily_history
    canvas = page.locator("#performance-chart")
    canvas.focus()
    canvas.press("Enter")
    expect(page.locator("#calculation-summary")).to_contain_text("Total")
    for _ in range(3):
        canvas.evaluate(
            """canvas => {
            document.querySelector('#calculation-detail').close();
            canvas.focus();
            canvas.dispatchEvent(new KeyboardEvent('keydown', {key:'Enter', bubbles:true, cancelable:true}));
            }"""
        )
        expect(page.locator("#calculation-summary")).to_contain_text("Total")


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
@pytest.mark.parametrize("target", ["history", "detail", "session"])
def test_calculation_authentication_failure_keeps_connect_advice(daily_history, target):
    """Only actual unauthorized responses reveal Connect and retain canonical recovery advice."""
    page, _, _, _ = daily_history
    pattern = {
        "history": "**/v1/planning-performance?*",
        "detail": "**/v1/planning-performance/*",
        "session": "**/setup/session",
    }[target]
    page.route(pattern, lambda route: route.fulfill(status=401, body="synthetic-private-body"))
    if target == "session":
        page.reload()
    elif target == "detail":
        page.locator("#performance-chart").focus()
        page.locator("#performance-chart").press("Enter")
    else:
        page.get_by_role("button", name="Refresh", exact=True).click()
    status = page.locator("#calculation-summary" if target == "detail" else "#performance-status")
    expect(status).to_have_text("Connect to read calculation history.")
    if target != "detail":
        expect(page.get_by_label("Installation API token")).to_be_visible()
    expect(page.locator("body")).not_to_contain_text("synthetic-private-body")


@pytest.mark.parametrize("app_site", ["planning_performance"], indirect=True)
def test_calculation_fetch_error_cannot_impersonate_authentication_failure(daily_history):
    """A lookalike fetch exception is still an untrusted failure, not app-owned authentication advice."""
    page, _, _, _ = daily_history
    page.evaluate(
        """() => {
        const original = window.fetch;
        window.fetch = (path, options) => String(path).includes('v1/planning-performance?') ?
            Promise.reject(new Error('Connect to read calculation history.')) : original(path, options);
        }"""
    )
    page.get_by_role("button", name="Refresh", exact=True).click()
    expect(page.locator("#performance-status")).to_have_text(
        "Calculation history could not be loaded. Try Refresh."
    )
    expect(page.locator("#connect")).not_to_be_visible()
