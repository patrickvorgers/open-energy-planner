/** A measured battery cash comparison, independent of Planning's economic forecasts. */
import {dateLabel, localDate, money} from "./display.js";
import {calendarDayStart} from "./chart_days.js";
import {createPeriodSelector, shiftDay} from "./period-selector.js";

/** Keep selection, loading and failed reads coherent across all amounts and the daily chart. */
export function createSavingsView(host, api) {
    const find = id => host.querySelector(`#${id}`);
    let chart = null, request = 0, initialized = false, selectedIndex = 0;
    const period = createPeriodSelector(find("savings-period"), {onChange: () => read()});
    /** Never leave an old period's money visible beneath a new selection. */
    function clear(message) {
        chart?.destroy(); chart = null;
        find("savings-total").textContent = message;
        find("savings-total").classList.remove("savings-loss");
        find("savings-outcome").textContent = "Your battery saved";
        for (const id of ["savings-actual", "savings-without"]) find(id).textContent = "Unavailable";
        for (const id of ["savings-actual-bar", "savings-without-bar"]) find(id).style.width = "0%";
        find("savings-selected").textContent = "";
        find("savings-chart").setAttribute("aria-label", "Daily battery savings unavailable");
    }
    /** Initialize once in the confirmed HA timezone. Today is not a completed Savings day. */
    function initialize() {
        const yesterday = shiftDay(localDate(), -1);
        if (!initialized) period.set({start: shiftDay(yesterday, -6), end: yesterday, max: yesterday, selection: "seven"});
        else period.set({max: yesterday});
        initialized = true;
    }
    /** Read only stored results. Date changes never start a simulation or reset a bonus counter. */
    async function read(signal) {
        if (!initialized) initialize();
        const current = ++request, {start, end} = period.value;
        clear("Loading…"); find("savings-status").textContent = "Loading measured savings…";
        find("savings-period-label").textContent = `${dateLabel(start)} – ${dateLabel(end)}`;
        try {
            const query = new URLSearchParams({start: new Date(calendarDayStart(start)).toISOString(), end: new Date(calendarDayStart(shiftDay(end, 1))).toISOString()});
            const data = await api(`v1/savings?${query}`, signal);
            if (current !== request) return false;
            if (data.calendar) {
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
            const legacy = data.days?.filter(day => !day.cash).length || 0;
            find("savings-legacy").hidden = !legacy;
            const complete = known && cash.coverage_seconds === cash.expected_seconds;
            find("savings-status").textContent = complete ? "All measured time in this period is included." : known ? "Partial period: Both costs include only the same available measured time. Missing intervals are excluded." : legacy ? "These retained days use the previous calculation. New measured savings become available for newly evaluated days." : "Waiting for completed measurements and published electricity prices for this period.";
            if (known && cash.estimated_seconds > 0) find("savings-status").textContent += " Includes reconstructed measurements.";
            if (data.issue) find("savings-status").textContent += " " + data.issue;
            draw(cash?.chart || [], currency, start, end);
            return true;
        } catch {
            if (current !== request) return false;
            clear("Not available"); find("savings-status").textContent = "Savings could not be loaded. Check the connection and choose Refresh overview.";
            return false;
        }
    }
    /** Expose each signed daily/monthly amount through tooltips and native keyboard inspection. */
    function draw(bins, currency, start, end) {
        const canvas = find("savings-chart");
        const points = bins.map(bin => ({x: (Date.parse(bin.start) + Date.parse(bin.end)) / 2, y: bin.savings, bin}));
        selectedIndex = 0;
        canvas.tabIndex = points.length ? 0 : -1;
        const monthly = bins.some(bin => localDate(bin.start) !== localDate(Date.parse(bin.end) - 1));
        find("savings-daily-heading").textContent = monthly ? "Savings by month" : "Daily savings";
        canvas.setAttribute("aria-label", "Battery savings. Use Left and Right to inspect values.");
        const title = bin => `${dateLabel(localDate(bin.start))}${localDate(bin.start) === localDate(Date.parse(bin.end) - 1) ? "" : " – " + dateLabel(localDate(Date.parse(bin.end) - 1))}`;
        chart = new Chart(canvas, {
            type: "bar", data: {datasets: [{label: `Battery saving (${currency})`, data: points, backgroundColor: points.map(point => point.y < 0 ? "#b76b4b" : "#439884"), borderRadius: 4, maxBarThickness: 64}]},
            options: {
                responsive: true, maintainAspectRatio: false, animation: false,
                scales: {x: {type: "linear", min: calendarDayStart(start), max: calendarDayStart(shiftDay(end, 1)), ticks: {maxRotation: 0, maxTicksLimit: canvas.clientWidth < 500 ? 3 : 7, callback: value => dateLabel(localDate(value))}}, y: {beginAtZero: true, ticks: {callback: money}}},
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
