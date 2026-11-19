/** A measured battery cash comparison, independent of Planning's economic forecasts. */
import {dateLabel, localDate, money, when} from "./display.js";
import {calendarDayStart} from "./chart_days.js";
import {createPeriodSelector, shiftDay} from "./period-selector.js";

const VALUE_LABEL_FONT_PX = 13;
const CHART_LABEL_GAP_PX = 8;

/** Keep selection, loading and failed reads coherent across all amounts and the daily chart. */
export function createSavingsView(host, api) {
    const find = id => host.querySelector(`#${id}`);
    let chart = null, request = 0, initialized = false, selectedIndex = 0;
    const period = createPeriodSelector(find("savings-period"), {onChange: () => read()});
    /** Never leave an old period's money visible beneath a new selection. */
    function clear(message) {
        chart?.destroy(); chart = null;
        find("savings-chart").onkeydown = null;
        find("savings-chart").tabIndex = -1;
        find("savings-total").textContent = message;
        find("savings-total").classList.remove("savings-loss");
        find("savings-outcome").textContent = "Your battery saved";
        for (const id of ["savings-actual", "savings-without"]) find(id).textContent = "Unavailable";
        for (const id of ["savings-actual-bar", "savings-without-bar"]) find(id).style.width = "0%";
        find("savings-selected").textContent = "";
        renderBreakdown(null, "");
        find("savings-chart").setAttribute("aria-label", "Daily battery savings unavailable");
    }
    /** Show only backend-verified components. Broader scenario coverage never explains a matched net cost. */
    function renderBreakdown(cash, currency) {
        const body = find("savings-breakdown-values");
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
        find("savings-breakdown-total").textContent = cash?.savings == null ? "Savings unavailable" : `Saving: ${money(cash.savings)} ${currency}`.trim();
        find("savings-breakdown-status").textContent = cash?.coverage_seconds > 0 && (!cash.breakdown?.actual || !cash.breakdown?.without_battery) ? "Some component details are unavailable for the same comparable time. The net costs and saving still use only matching measurements." : "";
    }
    /** Initialize once in the confirmed HA timezone. Today is not a completed Savings day. */
    function initialize() {
        const yesterday = shiftDay(localDate(), -1);
        if (!initialized || !period.value.start) period.set({start: shiftDay(yesterday, -6), end: yesterday, max: yesterday, selection: "seven"});
        else period.set({max: yesterday});
        initialized = true;
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
            if (data.calendar) {
                if (data.calendar.has_completed_days === false) {
                    period.set({start: null, end: null, enabled: false});
                    clear("Not available");
                    find("savings-status").textContent = "Savings become available after the first measured day has completed.";
                    return true;
                }
                period.set({min: data.calendar.min_day, max: data.calendar.max_day});
                if (period.value.start !== start || period.value.end !== end) return read(signal);
            }
            const cash = data.cash, currency = cash?.currency || data.currency || "";
            const known = cash?.savings != null && cash.coverage_seconds > 0;
            const amount = value => `${money(value)} ${currency}`.trim();
            find("savings-total").textContent = known ? amount(Math.abs(cash.savings)) : "Not available";
            find("savings-total").classList.toggle("savings-loss", known && cash.savings < 0);
            find("savings-outcome").textContent = known && cash.savings < 0 ? "Extra electricity cost" : "Your battery saved";
            if (known) {
                find("savings-actual").textContent = amount(cash.actual_cost);
                find("savings-without").textContent = amount(cash.without_battery_cost);
                const maximum = Math.max(Math.abs(cash.actual_cost), Math.abs(cash.without_battery_cost));
                find("savings-actual-bar").style.width = `${maximum ? Math.abs(cash.actual_cost) / maximum * 100 : 0}%`;
                find("savings-without-bar").style.width = `${maximum ? Math.abs(cash.without_battery_cost) / maximum * 100 : 0}%`;
            }
            renderBreakdown(cash, currency);
            const legacy = data.days?.filter(day => !day.cash).length || 0;
            find("savings-legacy").hidden = !legacy;
            const complete = known && cash.coverage_seconds === cash.expected_seconds;
            find("savings-status").textContent = complete ? "All measured time in this period is included." : known ? "Partial period: Both costs include only the same available measured time. Missing intervals are excluded." : legacy ? "These retained days use the previous calculation. New measured savings become available for newly evaluated days." : "Waiting for completed measurements and published electricity prices for this period.";
            if (known && cash.estimated_seconds > 0) find("savings-status").textContent += " Includes reconstructed measurements.";
            if (data.issue) find("savings-status").textContent += " " + data.issue;
            draw(cash?.chart || [], currency, start, end, cash?.aggregation);
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
                plugins: {legend: {display: false}, tooltip: {callbacks: {title: items => title(items[0].raw.bin), label: item => item.raw.y == null ? "No comparable measurements" : `${money(item.raw.y)} ${currency}`, afterLabel: item => item.raw.bin.estimated_seconds ? "Includes reconstructed measurements" : ""}}},
            },
        });
        canvas.onkeydown = event => {
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || !points.length) return;
            event.preventDefault(); selectedIndex = event.key === "Home" ? 0 : event.key === "End" ? points.length - 1 : Math.max(0, Math.min(points.length - 1, selectedIndex + (event.key === "ArrowRight" ? 1 : -1)));
            const point = points[selectedIndex];
            find("savings-selected").textContent = `${title(point.bin)}: ${point.y == null ? "No comparable measurements" : money(point.y) + " " + currency}`;
            chart.setActiveElements([{datasetIndex: 0, index: selectedIndex}]); chart.update("none");
        };
    }
    return {read, initialize};
}
