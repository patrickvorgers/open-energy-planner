/** A measured battery cash comparison, independent of Planning's economic forecasts. */
import {dateLabel, localDate, money, when} from "./display.js";
import {calendarDayStart} from "./chart_days.js";
import {createPeriodSelector, shiftDay} from "./period-selector.js";

const VALUE_LABEL_FONT_PX = 13;
const CHART_LABEL_GAP_PX = 8;
const TOOLTIP_VIEWPORT_MARGIN_PX = 12;

function hasMissingComponents(cash) {
    return cash?.coverage_seconds > 0 && (!cash.breakdown?.actual || !cash.breakdown?.without_battery);
}

/** Keep selection, loading and failed reads coherent across all amounts and the daily chart. */
export function createSavingsView(host, api) {
    const find = id => host.querySelector(`#${id}`);
    const tooltip = document.createElement("div");
    tooltip.id = "savings-chart-tooltip"; tooltip.className = "chart-tooltip savings-tooltip";
    tooltip.hidden = true; tooltip.setAttribute("role", "tooltip"); host.append(tooltip);
    /** Remove both visible and accessible details when their selection is no longer inspected. */
    function hideTooltip() {
        tooltip.hidden = true; tooltip.replaceChildren();
        find("savings-chart").removeAttribute("aria-describedby");
        document.removeEventListener("keydown", dismissTooltip);
        window.removeEventListener("scroll", dismissOnScroll, true);
    }
    /** Hover does not move focus, so Escape must also work outside the Savings section. */
    function dismissTooltip(event) { if (event.key === "Escape") hideTooltip(); }
    /** Page movement invalidates fixed coordinates, while scrolling the table itself preserves its details. */
    function dismissOnScroll(event) { if (!(event.target instanceof Node && tooltip.contains(event.target))) hideTooltip(); }
    host.addEventListener("pointerdown", event => { if (event.target !== find("savings-chart") && !tooltip.contains(event.target)) hideTooltip(); });
    window.addEventListener("hashchange", hideTooltip);
    tooltip.onpointerleave = () => { if (document.activeElement !== find("savings-chart")) hideTooltip(); };
    let chart = null, request = 0, initialized = false, selectedIndex = 0;
    const period = createPeriodSelector(find("savings-period"), {onChange: () => read()});
    /** Never leave an old period's money visible beneath a new selection. */
    function clear(message) {
        chart?.destroy(); chart = null;
        hideTooltip();
        find("savings-chart").onkeydown = null;
        find("savings-chart").tabIndex = -1;
        find("savings-total").textContent = message;
        find("savings-total").classList.remove("savings-loss");
        find("savings-outcome").textContent = "Estimated battery saving";
        find("savings-bonus-basis").hidden = true;
        for (const id of ["savings-actual", "savings-without"]) find(id).textContent = "Unavailable";
        for (const id of ["savings-actual-bar", "savings-without-bar"]) find(id).style.width = "0%";
        find("savings-selected").textContent = "";
        renderBreakdown(null, "");
        find("savings-chart").setAttribute("aria-label", "Daily battery savings unavailable");
    }
    /** Show only backend-verified components. Broader scenario coverage never explains a matched net cost. */
    function breakdownRows(body, cash, currency) {
        body.replaceChildren();
        for (const [key, label] of [["purchase", "Import cost"], ["sale", "Export income"], ["bonus", "Bonus income"], [null, "Net electricity cost"]]) {
            const row = document.createElement("tr"), heading = document.createElement("th");
            heading.scope = "row"; heading.textContent = label; row.append(heading);
            for (const kind of ["actual", "without_battery"]) {
                const cell = document.createElement("td");
                const value = key ? cash?.breakdown?.[kind]?.[key] : cash?.[`${kind}_cost`];
                cell.textContent = value == null ? "Unavailable" : `${money(value)} ${currency}`.trim(); row.append(cell);
            }
            body.append(row);
        }
    }
    /** Keep the period disclosure on the same backend components as each bar tooltip. */
    function renderBreakdown(cash, currency) {
        breakdownRows(find("savings-breakdown-values"), cash, currency);
        find("savings-breakdown-total").textContent = cash?.savings == null ? "Savings unavailable" : `Saving: ${money(cash.savings)} ${currency}`.trim();
        find("savings-breakdown-status").textContent = hasMissingComponents(cash) ? "Some component details are unavailable for the same comparable time. The net costs and saving still use only matching measurements." : "";
    }
    /** Initialize once in the confirmed HA timezone. Today is not a completed Savings day. */
    function initialize() {
        const yesterday = shiftDay(localDate(), -1);
        if (!initialized || !period.value.start) period.set({start: shiftDay(yesterday, -6), end: yesterday, max: yesterday, selection: "seven"});
        else period.set({max: yesterday});
        initialized = true;
    }
    function showEmptyCalendar() {
        period.set({start: null, end: null, enabled: false});
        clear("Not available");
        find("savings-status").textContent = "Savings become available after the first measured day has completed.";
    }
    function updateCalendar(calendar, start, end) {
        if (!calendar) return false;
        period.set({min: calendar.min_day, max: calendar.max_day});
        return period.value.start !== start || period.value.end !== end;
    }
    function renderCosts(cash, currency, known) {
        const amount = value => `${money(value)} ${currency}`.trim();
        find("savings-total").textContent = known ? amount(Math.abs(cash.savings)) : "Not available";
        const loss = known && cash.savings < 0;
        find("savings-total").classList.toggle("savings-loss", loss);
        if (loss) find("savings-outcome").textContent = "Estimated extra electricity cost";
        find("savings-bonus-basis").hidden = !known;
        if (!known) return;

        const maximum = Math.max(Math.abs(cash.actual_cost), Math.abs(cash.without_battery_cost));
        for (const [id, cost] of [["actual", cash.actual_cost], ["without", cash.without_battery_cost]]) {
            find(`savings-${id}`).textContent = amount(cost);
            find(`savings-${id}-bar`).style.width = `${maximum ? Math.abs(cost) / maximum * 100 : 0}%`;
        }
    }
    function renderResults(data, start, end) {
        const cash = data.cash, currency = cash?.currency || data.currency || "";
        const known = cash?.savings != null && cash.coverage_seconds > 0;
        renderCosts(cash, currency, known);
        renderBreakdown(cash, currency);
        const complete = known && cash.coverage_seconds === cash.expected_seconds;
        const status = find("savings-status");
        status.textContent = complete ? "All measured time in this period is included." : known ? "Partial period: Both costs include only the same available measured time. Missing intervals are excluded." : "No savings estimate is available for this period.";
        if (known && cash.estimated_seconds > 0) status.textContent += " Includes reconstructed measurements.";
        if (data.issue) status.textContent += " " + data.issue;
        draw(cash?.chart || [], currency, start, end, cash?.aggregation);
    }
    /** Read only stored results. Date changes never start a simulation or reset a bonus counter. */
    async function read(signal) {
        initialize();
        const current = ++request, {start, end} = period.value;
        clear("Loading…"); find("savings-status").textContent = "Loading measured savings…";
        try {
            const query = new URLSearchParams({start: new Date(calendarDayStart(start)).toISOString(), end: new Date(calendarDayStart(shiftDay(end, 1))).toISOString()});
            const data = await api(`v1/savings?${query}`, signal);
            if (current !== request) return false;
            if (data.calendar?.has_completed_days === false) {
                showEmptyCalendar();
                return true;
            }
            if (updateCalendar(data.calendar, start, end)) return read(signal);
            renderResults(data, start, end);
            return true;
        } catch {
            if (current !== request) return false;
            clear("Not available"); find("savings-status").textContent = "Savings could not be loaded. Check the connection and choose Refresh overview.";
            return false;
        }
    }
    /** Expose each signed daily/monthly amount through tooltips and native keyboard inspection. */
    function draw(bins, currency, start, end, aggregation) {
        const canvas = find("savings-chart");
        const points = bins.map(bin => ({x: (Date.parse(bin.start) + Date.parse(bin.end)) / 2, y: bin.savings, bin}));
        selectedIndex = 0;
        canvas.tabIndex = points.length ? 0 : -1;
        const monthly = aggregation === "month";
        find("savings-daily-heading").textContent = monthly ? "Savings by month" : "Daily savings";
        find("savings-chart-basis").textContent = `Compared with without a battery${currency ? ": " + currency : ""}`;
        const yearsDiffer = start.slice(0, 4) !== end.slice(0, 4);
        const axisDate = value => when(value, monthly ? {month: "short", year: "numeric"} : {day: "numeric", month: "short", ...(yearsDiffer ? {year: "numeric"} : {})});
        canvas.setAttribute("aria-label", "Battery savings. Use Left and Right to inspect values.");
        const title = bin => `${dateLabel(localDate(bin.start))}${localDate(bin.start) === localDate(Date.parse(bin.end) - 1) ? "" : " – " + dateLabel(localDate(Date.parse(bin.end) - 1))}`;
        /** Present the selected bar's retained costs without calculating financial values in the browser. */
        function renderTooltip(bin) {
            const heading = document.createElement("p");
            heading.className = "chart-tooltip-title"; heading.textContent = `${title(bin)}: ${currency}`;
            const table = document.createElement("table"), body = document.createElement("tbody");
            table.setAttribute("aria-label", "Savings breakdown for selected bar");
            table.append(find("savings-breakdown").querySelector("thead").cloneNode(true), body);
            breakdownRows(body, bin, currency);
            const total = document.createElement("p"); total.className = "chart-tooltip-explanation";
            total.textContent = bin.savings == null ? "No comparable measurements" : `Saving: ${money(bin.savings)} ${currency}`;
            if (hasMissingComponents(bin)) total.textContent += "\nSome component details are unavailable for the same comparable time.";
            if (bin.estimated_seconds) total.textContent += "\nIncludes reconstructed measurements.";
            if (bin.savings != null) total.textContent += "\n" + find("savings-bonus-basis").textContent;
            tooltip.replaceChildren(heading, table, total);
        }
        function isInspectingTooltip() {
            if (tooltip.hidden) return false;
            return canvas.matches(":hover") || tooltip.matches(":hover") || document.activeElement === canvas;
        }
        function showBreakdown({tooltip: state}) {
            if (canvas.closest("[hidden]")) { hideTooltip(); return; }
            if (!state.opacity) {
                if (!isInspectingTooltip()) hideTooltip();
                return;
            }
            if (!state.dataPoints?.length) { hideTooltip(); return; }
            tooltip.hidden = false;
            renderTooltip(state.dataPoints[0].raw.bin);
            canvas.setAttribute("aria-describedby", tooltip.id);
            document.addEventListener("keydown", dismissTooltip);
            window.addEventListener("scroll", dismissOnScroll, true);
            const bounds = canvas.getBoundingClientRect(), margin = TOOLTIP_VIEWPORT_MARGIN_PX;
            tooltip.style.left = `${Math.max(margin, Math.min(bounds.left + state.caretX + margin, innerWidth - tooltip.offsetWidth - margin))}px`;
            tooltip.style.top = `${Math.max(margin, Math.min(bounds.top + state.caretY + margin, innerHeight - tooltip.offsetHeight - margin))}px`;
        }
        chart = new Chart(canvas, {
            type: "bar", data: {datasets: [{label: `Battery saving (${currency})`, data: points, backgroundColor: points.map(point => point.y < 0 ? "#b76b4b" : "#439884"), borderRadius: 4, maxBarThickness: 64}]},
            plugins: [{id: "savingsAmounts", afterDatasetsDraw(instance) {
                const context = instance.ctx, bars = instance.getDatasetMeta(0).data;
                context.save(); context.font = `${VALUE_LABEL_FONT_PX}px ${Chart.defaults.font.family}`;
                context.textAlign = "center"; context.textBaseline = "bottom";
                let previousRight = -Infinity;
                points.forEach((point, index) => {
                    if (point.y == null || !bars[index]) return;
                    const bar = bars[index], label = money(point.y), half = context.measureText(label).width / 2;
                    if (bar.x - half < instance.chartArea.left || bar.x + half > instance.chartArea.right || bar.x - half < previousRight + CHART_LABEL_GAP_PX) return;
                    context.fillStyle = point.y < 0 ? "#a34e30" : getComputedStyle(canvas).color;
                    context.fillText(label, bar.x, Math.min(bar.y, bar.base) - CHART_LABEL_GAP_PX);
                    previousRight = bar.x + half;
                });
                context.restore();
            }}],
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                layout: {padding: {top: VALUE_LABEL_FONT_PX + CHART_LABEL_GAP_PX}},
                scales: {x: {type: "linear", min: calendarDayStart(start), max: calendarDayStart(shiftDay(end, 1)), afterBuildTicks: axis => {axis.ticks = points.map(point => ({value: point.x}));}, grid: {display: false}, ticks: {maxRotation: 0, autoSkip: true, autoSkipPadding: CHART_LABEL_GAP_PX, callback: axisDate}}, y: {beginAtZero: true, ticks: {callback: money}}},
                plugins: {legend: {display: false}, tooltip: {enabled: false, external: showBreakdown}},
            },
        });
        canvas.onkeydown = event => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || !points.length) return;
            event.preventDefault(); selectedIndex = event.key === "Home" ? 0 : event.key === "End" ? points.length - 1 : Math.max(0, Math.min(points.length - 1, selectedIndex + (event.key === "ArrowRight" ? 1 : -1)));
            const point = points[selectedIndex];
            find("savings-selected").textContent = `${title(point.bin)}: ${point.y == null ? "No comparable measurements" : money(point.y) + " " + currency}`;
            const active = [{datasetIndex: 0, index: selectedIndex}], bar = chart.getDatasetMeta(0).data[selectedIndex];
            chart.setActiveElements(active); chart.tooltip.setActiveElements(active, {x: bar.x, y: bar.y}); chart.update("none");
            showBreakdown({tooltip: chart.tooltip});
        };
        canvas.onblur = hideTooltip;
        canvas.onpointerleave = event => { if (!tooltip.contains(event.relatedTarget) && document.activeElement !== canvas) hideTooltip(); };
    }
    return {read, initialize};
}
