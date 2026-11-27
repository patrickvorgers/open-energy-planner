import {createSavingsView} from "./savings-view.js";
// Read-only views never activate an archived plan or reset the reference scenario.
import {updateCharts} from "./chart_theme.js";
import {calendarDays, calendarTime, calendarDayStart} from "./chart_days.js";
import {money, configuredRate, when, dateLabel, localDate, localInstant, nextDate, percentage, optionLabel, planName, useHomeAssistantTimezone, watchSourceNames} from "./display.js";
import {BATTERY_FLOW_GROUPS, batteryPlanSeries, batteryRouteValues, energyBalances, householdForecastEnergy, quarterBalanceSeries, balancePeriodEnergy, QUARTER_MILLISECONDS} from "./balance.js";
import {bindSettingsActions, sessionResponse} from "./settings-actions.js";
import {gridModeStrip, GRID_MODE_STRIP_HEIGHT_PX, ZERO_GRID_LABEL} from "./grid-mode-strip.js";
bindSettingsActions();
const $ = (id) => document.getElementById(id);
const savingsPage = createSavingsView($("savings-view"), api);
const CONNECTED_MESSAGE = "Connected";
const PLAN_REFRESH_MILLISECONDS = 15000;
const PLAN_READ_TIMEOUT_MILLISECONDS = 5000;
const base = document.querySelector('meta[name="oep-api-base"]').content;
let requestedPlan = new URL(location.href).searchParams.get("plan");
let selectedLinkedPlan = false;
let token = "", cursor = null, selectionRequest = 0, periodRequest = 0;
let planRefreshEnabled = false, planRefreshBusy = false, overviewLoads = 0, renderedPlanId = null;
let planReadPending = false;
let simulationReadPending = false;
let evidenceRequest = null;
let priceRefreshPending = false;
let periodCostPlan = null, periodCostKey = "", periodCostRequest = 0;
let overviewSessionReady = false, overviewIngress = false;
let sourceNamesGeneration = 0, planSourceNamesGeneration = 0;
const isMoneyUnit = (unit) => /^[A-Z]{3}(\/kWh)?$/.test(unit || "");
const number = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : "-";
/** Share numeric precision across evidence cards, tooltip columns and chart axes. Units remain separate. */
const valueText = (value, unit) => unit === "%" ? percentage(value, false) : unit === "W" ? Math.round(value).toLocaleString() : (isMoneyUnit(unit) ? money : number)(value);
/** Read JSON with owned guidance and a default deadline. An explicit signal owns its request group. */
async function api(path, signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS)) {
    let response;
    try {
        response = path === "setup/session" ? await sessionResponse(base, signal) : await fetch(base + path, {headers: token ? {Authorization: `Bearer ${token}`} : {}, redirect: "error", cache: "no-store", signal});
    } catch (error) {
        throw new Error(signal?.aborted || error?.name === "TimeoutError" ? "This view took too long to load. Please retry." : "Cannot read this view: Check the connection and retry.");
    }
    if (!response.ok) throw new Error(response.status === 401 ? "Enter the app administration token to continue." : `Cannot read this view (${response.status}).`);
    try { return await response.json(); }
    catch { throw new Error("Cannot read this view: The response is unreadable. Please retry."); }
}
/** Render text cells without interpreting source labels as markup. */
function row(parent, values) {
    const tr = document.createElement("tr");
    for (const value of values) {
        const td = document.createElement("td");
        td.textContent = value;
        tr.append(td);
    }
    parent.append(tr);
}
const charts = new Map();
const batteryPlanPanel = $("battery-plan-panel");
let pendingBatterySeries = null;
const syncChartLegends = new WeakMap();
const DEVICE_ROUNDING_KW = 0.1;
// Absorb only machine-precision addition error at the display threshold.
const DEVICE_ROUNDING_TOLERANCE_KW = DEVICE_ROUNDING_KW * Number.EPSILON;
const hiddenPriceSeries = new Set();
const palette = ["#078b80", "#e49b38", "#5d70bc", "#d56788", "#399bc1", "#8a65ad"];
const INTERVAL_BAR_FILL = 0.9;
const BATTERY_FLOW_DISPLAY_MIN = 0.0005;
const TOOLTIP_VIEWPORT_MARGIN_PX = 12;
const MILLISECONDS_PER_MINUTE = 60000;
const MILLISECONDS_PER_HOUR = 60 * MILLISECONDS_PER_MINUTE;
const MAX_TIME_AXIS_LABELS = 6;
const DAILY_DATE_LABEL_MIN_WIDTH_PX = 120;
const FORECAST_PRICE_DASH = [2, 4];
const PRICE_EXTREME_COLORS = {cheapest: "#16816b", most_expensive: "#bf5c32"};
const PLAN_RANK_NOTES = {
    cheapest: "Among the day's lowest import rates in this plan",
    most_expensive: "Among the day's highest export rates in this plan"
};
const LINKED_CHART_IDS = ["plan-chart", "price-chart"];
const INTERVAL_POINTER_EVENTS = new Set(["mousemove", "touchstart", "touchmove"]);
const LINKED_LEFT_AXIS_PX = 64;
const LINKED_RIGHT_AXIS_PX = 58;
const DAILY_HIGHLIGHT_QUARTERS = 12;
let linkedInterval = null;
const isLinkedChart = id => LINKED_CHART_IDS.includes(id);
const DEVICE_PATTERN_SIZE_PX = 8;
let inspectSteps = [], planningHours = 0, planEvidence = null, currentPriceWindow = null, priceRequest = 0, priceInputPlanId = null;

/** Stripe reserved device use while concrete schedules retain the same device's solid color. */
function deviceFill(color, state) {
    if (state !== "expected") return color;
    const tile = document.createElement("canvas");
    tile.width = tile.height = DEVICE_PATTERN_SIZE_PX;
    const brush = tile.getContext("2d");
    brush.fillStyle = `${color}55`; brush.fillRect(0, 0, tile.width, tile.height);
    brush.strokeStyle = color; brush.lineWidth = 2;
    brush.beginPath(); brush.moveTo(0, tile.height); brush.lineTo(tile.width, 0); brush.stroke();
    return brush.createPattern(tile, "repeat");
}

/** Render app-owned factual cards as text. Never infer reasons or interpret device names as markup. */
function evidenceCards(parent, cards) {
    parent.replaceChildren();
    const groups = new Map();
    for (const card of cards) {
        let target = parent;
        if (card.group) {
            if (!groups.has(card.group.id)) {
                const section = document.createElement("section"), title = document.createElement("h4"), grid = document.createElement("div");
                section.className = "evidence-inverter-group"; section.setAttribute("aria-label", card.group.title);
                title.textContent = card.group.title; grid.className = "evidence-grid";
                section.append(title, grid); parent.append(section); groups.set(card.group.id, grid);
            }
            target = groups.get(card.group.id);
        }
        const article = document.createElement("article"), heading = document.createElement(card.group ? "h5" : "h4"), list = document.createElement("dl");
        article.className = "evidence-card"; heading.textContent = card.title; article.append(heading);
        for (const fact of card.facts) {
            const term = document.createElement("dt"), value = document.createElement("dd");
            term.textContent = fact.label;
            value.textContent = fact.value == null ? "Not recorded" : typeof fact.value === "number" ? `${fact.key === "wear_cost_per_kwh" ? configuredRate(fact.value) : valueText(fact.value, fact.unit)} ${fact.unit}`.trim() : fact.value;
            list.append(term, value);
        }
        article.append(list);
        for (const observation of card.observations) {
            const text = document.createElement("p"); text.className = "evidence-observation"; text.textContent = observation; article.append(text);
        }
        target.append(article);
    }
}
/** Explain actual bar coverage and retain recorded commands only for the active optimizer view. */
function balanceNotes(bar, id, axisTime) {
    const notes = bar.coveredStart !== bar.start || bar.coveredEnd !== bar.end
        ? [`Partial quarter: Mean power covers ${axisTime(bar.coveredStart)} to ${axisTime(bar.coveredEnd)} only.`] : [];
    if (id === "plan-chart") {
        notes.push(...new Set(inspectSteps.filter(step => step.end > bar.coveredStart && step.start < bar.coveredEnd).flatMap(step => step.commands || [])));
    }
    return notes;
}
/** Render the visible rows and the totals of all flows separately. */
function tooltipSections(items, totalItems, groupedBalance, dayTotals) {
    const groups = groupedBalance ? ["Production", "Consumption", "Battery state"].map(name => [name, items.filter(item => item.dataset.tooltipGroup === name)]).filter(([name, items]) => items.length || totalItems.some(item => item.dataset.tooltipGroup === name)) : [[null, items]];
    const sections = document.createElement("div"); sections.className = groupedBalance ? "balance-tooltip-groups" : "";
    for (const [name, items] of groups) {
        const section = document.createElement("section"), table = document.createElement("table");
        if (name) {
            const heading = document.createElement("h4"); heading.textContent = name; section.append(heading);
            if (name === "Battery state") section.className = "chart-tooltip-state";
        }
        for (const item of items) {
            const line = document.createElement("tr"), label = document.createElement("td"), value = document.createElement("td"), unit = document.createElement("td");
            const swatch = document.createElement("span"); swatch.className = "series-swatch"; swatch.style.backgroundColor = item.dataset.swatchColor || item.dataset.backgroundColor;
            swatch.classList.toggle("series-swatch-expected", item.dataset.deviceState === "expected");
            label.append(swatch, document.createTextNode(item.dataset.label));
            value.className = "chart-tooltip-value";
            value.textContent = valueText(item.parsed.y, item.dataset.unit);
            unit.className = "chart-tooltip-unit"; unit.textContent = item.dataset.unit;
            line.append(label, value, unit); table.append(line);
        }
        const flows = totalItems.filter(item => item.dataset.tooltipGroup === name);
        if (flows.length) {
            const total = flows.every(item => Number.isFinite(item.parsed.y)) ? flows.reduce((sum, item) => sum + item.parsed.y, 0) : null;
            const line = document.createElement("tr"), label = document.createElement("td"), value = document.createElement("td"), unit = document.createElement("td");
            line.className = "chart-tooltip-total";
            label.textContent = `Total ${name.toLowerCase()}`;
            if (dayTotals) {
                label.textContent = name === "Consumption" ? "Total expected energy use" : "Total expected production";
            }
            value.className = "chart-tooltip-value"; value.textContent = valueText(total, flows[0].dataset.unit);
            unit.className = "chart-tooltip-unit"; unit.textContent = flows[0].dataset.unit;
            line.append(label, value, unit); table.append(line);
        }
        section.append(table); sections.append(section);
    }
    return sections;
}

/** Keep labels, right-aligned numbers and units in distinct columns without interpreting provider text as HTML. */
function renderTooltip({chart: instance, tooltip}, node, balance = false, balanceTime = null) {
    const batteryPlan = instance.canvas.id === "battery-plan-chart";
    node.classList.remove("chart-tooltip-day", "chart-tooltip-linked"); node.style.maxHeight = "";
    node.hidden = !tooltip.opacity || Boolean(instance.canvas.closest("[hidden]"));
    if (node.hidden) return;
    node.replaceChildren();
    let items = [...tooltip.dataPoints];
    if (balance && Number.isFinite(balanceTime)) {
        // Narrow screens can select a nearby SoC endpoint instead of the subpixel-width bar.
        const time = balanceTime;
        const bars = instance.data.datasets.flatMap((dataset, index) => {
            if (dataset.type !== "bar" || !instance.isDatasetVisible(index)) return [];
            const point = dataset.data.find(point => point.start <= time && time < point.end && point.y != null);
            return point ? [{dataset, raw: point, parsed: point}] : [];
        });
        if (bars.length) items = bars;
    }
    const interval = balance && items.find(item => item.raw.start != null);
    const title = document.createElement("p"); title.className = "chart-tooltip-title";
    title.textContent = interval ? `From ${when(interval.raw.start)} to ${when(interval.raw.end)}` : tooltip.title.join(" - "); node.append(title);
    if (tooltip.periodLabel) {
        const period = document.createElement("p");
        period.className = "chart-tooltip-period";
        period.textContent = tooltip.periodLabel;
        node.append(period);
    }
    if (interval) {
        // Flow bars use midpoints. Attach only recorded start states, never interpolate a missing SoC.
        instance.data.datasets.forEach((dataset, index) => {
            if (dataset.tooltipGroup !== "Battery state" || !instance.isDatasetVisible(index)) return;
            const point = dataset.data.find(point => point.x === (interval.raw.coveredStart ?? interval.raw.start) && point.y != null);
            if (point && !items.some(item => item.dataset === dataset)) items.push({dataset: {...dataset, label: `${dataset.label} at start`}, parsed: point});
        });
    }
    if (batteryPlan) items = items.filter(item => item.dataset.type !== "bar" || Math.abs(item.parsed.y) >= BATTERY_FLOW_DISPLAY_MIN);
    // Totals retain original flows even when the legend or display threshold hides a series.
    const totalItems = interval && !batteryPlan ? instance.data.datasets.filter(dataset => dataset.type === "bar").map(dataset => {
        const point = dataset.data.find(point => point.start === interval.raw.start && point.end === interval.raw.end);
        return {dataset, parsed: {y: point?.y}};
    }) : tooltip.balanceItems || [];
    const groupedBalance = balance && !batteryPlan;
    node.classList.toggle("chart-tooltip-balance", groupedBalance);
    const sections = tooltipSections(items, totalItems, groupedBalance, tooltip.balanceItems);
    if (isLinkedChart(instance.canvas.id) && linkedInterval) node.append(linkedCostText(linkedInterval));
    node.append(sections);
    const notes = [...(interval ? balanceNotes(interval.raw, instance.canvas.id, when) : tooltip.afterBody)];
    if (totalItems.length) notes.push("Totals include all flows, including hidden series. Supply is positive. Use is negative.");
    if (notes.length) {
        const explanation = document.createElement("p"); explanation.className = "chart-tooltip-explanation";
        explanation.textContent = notes.join("\n"); node.append(explanation);
    }
    positionChartTooltip(node, instance.canvas, tooltip.caretX, tooltip.caretY);
}
/** Show plan cash flows before bonus and battery valuation. Missing evidence remains unknown. */
function linkedCostText({gridImport, gridExport, buyRate, sellRate, currency}) {
    const exchange = (label, energy, rate, amountLabel) => {
        const quantity = knownQuantity(energy, number, "kWh");
        const amount = Number.isFinite(energy) && Number.isFinite(rate) ? energy * rate : null;
        const tariff = knownQuantity(rate, money, `${currency}/kWh`, "an unavailable rate");
        return `${label}: ${quantity}. ${amountLabel}: ${knownQuantity(amount, money, currency)} at ${tariff}.`;
    };
    const text = document.createElement("p");
    text.className = "chart-linked-cost";
    text.textContent = [
        exchange("Grid import", gridImport, buyRate, "Cost"),
        exchange("Grid export", gridExport, sellRate, "Revenue before bonus")
    ].join(" ");
    return text;
}

/** Format finite quantities while preserving the distinction between missing and zero. */
function knownQuantity(value, format, unit, unavailable = "Unavailable") {
    return Number.isFinite(value) ? `${format(value)} ${unit}` : unavailable;
}

/** Redraw one timestamp across both plots without changing their scales or datasets. */
function selectLinkedInterval(interval) {
    if (interval === linkedInterval) return;
    for (const id of LINKED_CHART_IDS) {
        const tooltip = $(`${id}-tooltip`);
        if (!tooltip?.classList.contains("chart-tooltip-linked")) continue;
        tooltip.hidden = true;
        tooltip.classList.remove("chart-tooltip-linked");
    }
    $("linked-chart-reading").textContent = "";
    linkedInterval = interval;
    for (const id of LINKED_CHART_IDS) charts.get(id)?.draw();
}

/** Discard the cursor and its announced values when a different interval set replaces the plan. */
function clearLinkedInspection() {
    selectLinkedInterval(null);
    $("linked-chart-reading").textContent = "";
}

/** Use the plan's real interval boundaries, including partial opening quarters. */
function linkedChartInteraction(canvas, tooltip, hideTooltip, signal) {
    const intervalAt = (chart, pixel) => {
        const time = chart.scales.x.getValueForPixel(pixel);
        return inspectSteps.find(interval => interval.start <= time && time < interval.end) ?? null;
    };
    const midpoint = interval => (interval.start + interval.end) / 2;
    const inspectInterval = (chart, interval, y) => {
        selectLinkedInterval(interval);
        hideTooltip();
        if (!interval) return;
        const peerTooltip = $(`${LINKED_CHART_IDS.find(id => id !== canvas.id)}-tooltip`);
        if (peerTooltip && !peerTooltip.classList.contains("chart-tooltip-day")) peerTooltip.hidden = true;
        const title = document.createElement("p");
        title.className = "chart-tooltip-title";
        title.textContent = `From ${when(interval.start)} to ${when(interval.end)}`;
        const cost = linkedCostText(interval);
        const rankings = planRankNotes(midpoint(interval));
        tooltip.classList.remove("chart-tooltip-balance", "chart-tooltip-day");
        tooltip.classList.add("chart-tooltip-linked");
        tooltip.style.maxHeight = "";
        tooltip.replaceChildren(title, cost);
        for (const ranking of rankings) {
            const note = document.createElement("p");
            note.className = "chart-tooltip-explanation";
            note.textContent = ranking;
            tooltip.append(note);
        }
        tooltip.hidden = false;
        positionChartTooltip(tooltip, canvas, chart.scales.x.getPixelForValue(midpoint(interval)), y);
        $("linked-chart-reading").textContent = [`${title.textContent}.`, cost.textContent, ...rankings].join(" ");
        $("inspect-time").value = inspectSteps.indexOf(interval);
        inspect();
    };
    canvas.tabIndex = 0;
    canvas.addEventListener("keydown", event => {
        const chart = charts.get(canvas.id);
        if (event.key === "Escape") { selectLinkedInterval(null); hideTooltip(); return; }
        const visible = inspectSteps.filter(interval => interval.end > chart.scales.x.min && interval.start < chart.scales.x.max);
        if (!priceInputPlanId || !visible.length) return;
        const index = visible.indexOf(linkedInterval);
        const next = {ArrowLeft: index < 0 ? 0 : index - 1, ArrowRight: index + 1, Home: 0, End: visible.length - 1}[event.key];
        if (next === undefined) return;
        event.preventDefault();
        inspectInterval(chart, visible[Math.min(visible.length - 1, Math.max(0, next))], chart.chartArea.top);
    }, {signal});
    canvas.addEventListener("blur", () => { hideTooltip(); selectLinkedInterval(null); }, {signal});
    return {
        id: "linkedPlanCursor",
        beforeEvent(chart, {event, inChartArea, replay}) {
            if (replay || !priceInputPlanId) return;
            if (INTERVAL_POINTER_EVENTS.has(event.type)) {
                selectLinkedInterval(inChartArea ? intervalAt(chart, event.x) : null);
            }
            if (event.type === "mouseout" && !tooltip.classList.contains("chart-tooltip-linked")) selectLinkedInterval(null);
        },
        afterEvent(chart, {event, inChartArea, replay}) {
            if (replay || !priceInputPlanId) return;
            if (inChartArea && event.type === "click") inspectInterval(chart, intervalAt(chart, event.x), event.y);
        },
        afterDraw(chart) {
            if (!linkedInterval || !priceInputPlanId || !chart.chartArea) return;
            const {ctx, chartArea: {top, bottom, left, right}, scales: {x}} = chart;
            const start = Math.max(left, x.getPixelForValue(linkedInterval.start));
            const end = Math.min(right, x.getPixelForValue(linkedInterval.end));
            if (end <= start) return;
            const pixel = (start + end) / 2;
            ctx.save(); ctx.fillStyle = "#3a637b26";
            ctx.fillRect(start, top, end - start, bottom - top);
            ctx.strokeStyle = "#3a637b"; ctx.setLineDash([4, 3]);
            ctx.beginPath(); ctx.moveTo(pixel, top); ctx.lineTo(pixel, bottom); ctx.stroke();
            ctx.restore();
        }
    };
}

/** Keep pointer and keyboard interval cards within the viewport. */
function positionChartTooltip(node, canvas, x, y) {
    const bounds = canvas.getBoundingClientRect(), margin = TOOLTIP_VIEWPORT_MARGIN_PX;
    const anchor = bounds.left + x;
    const width = node.getBoundingClientRect().width;
    const right = anchor + margin;
    const left = right + width + margin <= innerWidth ? right : anchor - margin - width;
    node.style.left = `${Math.max(margin, Math.min(left, innerWidth - width - margin))}px`;
    node.style.top = `${Math.max(margin, Math.min(bounds.top + y + margin, innerHeight - node.offsetHeight - margin))}px`;
}
/**
 * Place daily totals in viewport CSS pixels without covering their owning date.
 * @param {HTMLElement} tooltip Visible rendered summary. Mutates top, left and maxHeight styles.
 * @param {HTMLButtonElement} button Date control anchoring the summary.
 */
function positionDayTooltip(tooltip, button) {
    const bounds = button.getBoundingClientRect(), margin = TOOLTIP_VIEWPORT_MARGIN_PX;
    const below = innerHeight - bounds.bottom - margin, above = bounds.top - margin;
    const placeBelow = below >= above;
    tooltip.style.maxHeight = `${Math.max(0, placeBelow ? below : above)}px`;
    tooltip.style.top = `${placeBelow ? bounds.bottom : bounds.top - tooltip.offsetHeight}px`;
    tooltip.style.left = `${Math.max(margin, Math.min(bounds.left + (bounds.width - tooltip.offsetWidth) / 2, innerWidth - tooltip.offsetWidth - margin))}px`;
}
/**
 * Attach inspection of signed daily kWh without changing archived plan inputs.
 * @param {Chart} instance Chart supplying current series visibility and canvas geometry.
 * @param {Array} series Balance series with covered mean-power intervals in kW.
 * @param {HTMLElement} tooltip Shared content, visibility, classes, sizing and leave handler are mutable.
 * @param {{owner: HTMLButtonElement|null, pinned: boolean}} summary Exclusive per-chart owner.
 * Pinned means explicit click/tap activation rather than a hover/focus preview.
 * @param {HTMLElement} heading Calendar list item whose children are replaced and listeners installed.
 * @param {{date: string, start: number, end: number, full: string}} day Local date and accessible label,
 * with visible start/end in epoch milliseconds. Missing energy stays unavailable. No positive
 * covered duration means no summary opens. Opening and dismissal mutate summary ownership.
 */
function planningDayHeading(instance, series, tooltip, summary, heading, {date, start, end, full}) {
    const button = document.createElement("button");
    button.type = "button"; button.className = "chart-day-summary";
    button.setAttribute("aria-label", `Expected energy totals for ${full}`);
    button.append(...heading.childNodes); heading.append(button); heading.removeAttribute("title");
    let hovered = false, focused = false, dismissed = false;
    /** A late blur from the previous date must not dismiss its successor's content. */
    const hide = () => {
        button.removeAttribute("aria-describedby");
        if (summary.owner !== button) return;
        summary.owner = null; summary.pinned = false; tooltip.hidden = true;
        tooltip.classList.remove("chart-tooltip-day");
        tooltip.style.maxHeight = "";
    };
    /** Read current visible flows and transfer the accessible description to this date only. */
    const show = () => {
        if (dismissed) return;
        const intervals = series.find(entry => entry.type === "bar" && entry.intervals.length)?.intervals;
        if (!intervals) return;
        const from = Math.max(start, intervals[0].coveredStart ?? intervals[0].start);
        const to = Math.min(end, intervals.at(-1).coveredEnd ?? intervals.at(-1).end);
        if (to <= from) return;
        const balanceItems = series.flatMap((entry, index) => entry.type !== "bar" ? [] : [{
            dataset: {...instance.data.datasets[index], unit: "kWh"}, datasetIndex: index, raw: {},
            parsed: {y: balancePeriodEnergy(entry.intervals, from, to)}
        }]);
        const items = balanceItems.filter(item => instance.isDatasetVisible(item.datasetIndex));
        const nextDayStart = calendarDayStart(nextDate(date));
        const partial = from > calendarDayStart(date) || to < nextDayStart;
        const endDay = to === nextDayStart ? " (next day)" : "";
        const notes = [partial ? "Partial day. Totals cover only the shown planned period." : "Totals cover this complete calendar day."];
        if (balanceItems.some(item => item.parsed.y == null)) notes.push("A dash means that energy is unavailable because interval evidence is missing or inconsistent.");
        if (summary.owner !== button) {
            summary.owner?.removeAttribute("aria-describedby");
            summary.owner = button; summary.pinned = false;
        }
        const bounds = button.getBoundingClientRect(), canvas = instance.canvas.getBoundingClientRect();
        renderTooltip({chart: instance, tooltip: {opacity: 1, dataPoints: items, balanceItems, title: [`Expected energy totals: ${dateLabel(date)}`],
            periodLabel: partial ? `From ${calendarTime(from)} to ${calendarTime(to)}${endDay}.` : null, afterBody: notes, caretX: bounds.left + bounds.width / 2 - canvas.left, caretY: bounds.bottom - canvas.top}}, tooltip, true);
        tooltip.classList.add("chart-tooltip-day");
        button.setAttribute("aria-describedby", tooltip.id);
        const scenario = {"plan-chart": "optimizer", "self-consumption-chart": "self_consumption"}[instance.canvas.id];
        if (scenario) showDayBenefit(tooltip, button, scenario, from, to);
        positionDayTooltip(tooltip, button);
        tooltip.onmouseleave = event => { if (!focused && !button.contains(event.relatedTarget)) hide(); };
    };
    button.addEventListener("mouseenter", () => { hovered = true; dismissed = false; show(); });
    button.addEventListener("mouseleave", event => {
        hovered = false;
        if (!focused && !tooltip.contains(event.relatedTarget)) hide();
    });
    button.addEventListener("focus", () => { focused = true; dismissed = false; show(); });
    button.addEventListener("blur", () => { focused = false; if (!hovered) hide(); });
    button.addEventListener("click", () => {
        dismissed = summary.owner === button && summary.pinned && !tooltip.hidden;
        if (dismissed) hide();
        else { show(); summary.pinned = true; }
    });
    button.addEventListener("keydown", event => { if (event.key === "Escape") { dismissed = true; hide(); } });
}
/** Linear time spacing needs interval-specific widths: an hour must occupy four quarter-hour widths. */
const intervalWidths = {id: "intervalWidths", afterDatasetsUpdate(instance) {
    instance.data.datasets.forEach((dataset, datasetIndex) => {
        if (dataset.type !== "bar") return;
        const metadata = instance.getDatasetMeta(datasetIndex);
        metadata.data.forEach((element, pointIndex) => {
            const point = dataset.data[pointIndex];
            element.width = Math.abs(metadata.xScale.getPixelForValue(point.end) - metadata.xScale.getPixelForValue(point.start)) * INTERVAL_BAR_FILL;
        });
    });
}};
/** Paint backend-selected quarters. Placement and labels distinguish the two sets without color. */
const dailyPriceExtremes = {id: "dailyPriceExtremes", beforeDatasetsDraw(instance) {
    const {ctx, chartArea, scales: {x}} = instance;
    ctx.save();
    ctx.beginPath(); ctx.rect(chartArea.left, chartArea.top, chartArea.width, chartArea.height); ctx.clip();
    for (const quarter of instance.dailyPriceQuarters || []) {
        const left = x.getPixelForValue(quarter.start), right = x.getPixelForValue(quarter.end);
        ctx.fillStyle = PRICE_EXTREME_COLORS[quarter.extreme];
        ctx.globalAlpha = 0.10; ctx.fillRect(left, chartArea.top, right - left, chartArea.height);
        ctx.globalAlpha = 1;
        ctx.fillRect(left, quarter.extreme === "cheapest" ? chartArea.bottom - 5 : chartArea.top, right - left, 5);
    }
    ctx.restore();
}};
/** Read daily selections separately from immutable plan prices. Discard responses for replaced charts. */
async function loadDailyPrices(instance) {
    const timestamps = instance.data.datasets.flatMap(dataset => dataset.data.map(point => point.x)).filter(Number.isFinite);
    const status = $("price-daily-status");
    $("price-low-label").title = priceInputPlanId ? "Lowest import rates" : "Lowest spot prices";
    $("price-high-label").title = priceInputPlanId ? "Highest export rates" : "Highest spot prices";
    if (priceInputPlanId) { drawStoredPriceRankings(instance, status); return; }
    if (instance.dailyPricePending) return;
    if (!timestamps.length) { status.textContent = "Whole-day price rankings unavailable."; return; }
    instance.dailyPricePending = true;
    try {
        const start = new Date(Math.min(...timestamps)).toISOString(), end = new Date(Math.max(...timestamps)).toISOString();
        const data = await api(`v1/prices/daily?${new URLSearchParams({start, end})}`);
        if (charts.get("price-chart") !== instance) return;
        updateMarketRankings(instance, status, data.days || []);
    } catch {
        if (charts.get("price-chart") === instance) status.textContent = "Whole-day price rankings could not be loaded. Refresh to retry.";
    } finally {
        instance.dailyPricePending = false;
    }
}
/** Project the market preview selection and its accessible whole-day explanation. */
function updateMarketRankings(instance, status, days) {
    const dailyDocument = JSON.stringify(days);
    if (dailyDocument === instance.dailyPriceDocument) { status.textContent = instance.dailyPriceStatus; return; }
    const descriptions = [];
    instance.dailyPriceQuarters = [];
    for (const day of days) {
        if (day.status === "incomplete") continue;
        for (const [extreme, label] of [["cheapest", "12 cheapest quarters"], ["most_expensive", "12 most expensive quarters"]]) {
            const heading = `${dateLabel(day.day)}: ${label}: ${day.status === "final" ? "Final" : "Provisional"}`;
            const intervals = [];
            for (const selected of day[extreme] || []) {
                const quarter = day.quarters.find(quarter => quarter.start === selected);
                if (!quarter) continue;
                instance.dailyPriceQuarters.push({start: Date.parse(quarter.start), end: Date.parse(quarter.end), extreme, status: day.status});
                intervals.push(`${when(quarter.start)} to ${when(quarter.end)}: ${money(quarter.market_price)} ${day.currency}/kWh`);
            }
            descriptions.push(`${heading}: ${intervals.join(": ")}`);
        }
    }
    const provisional = days.some(day => day.status === "provisional"), incomplete = !days.length || days.some(day => day.status === "incomplete");
    status.textContent = `Spot market prices ranked across each complete installation day, including elapsed quarters. ${provisional ? "Provisional selections can change with new prices. " : ""}${days.some(day => day.status === "final") ? "Final selections are retained. " : ""}${incomplete ? "Incomplete days have no ranking. " : ""}${descriptions.join(": ")}`;
    instance.dailyPriceDocument = dailyDocument;
    instance.dailyPriceStatus = status.textContent;
    instance.update("none");
}

/** Rank once per original plan, including elapsed intervals outside the selected view. */
function drawStoredPriceRankings(instance, status) {
    if (instance.dailyPriceQuarters) return;
    // Rank the stored plan, including elapsed intervals. Narrowing the view never changes the selection.
    const days = new Map();
    for (const interval of inspectSteps) {
        if (!Number.isFinite(interval.start) || !(interval.end > interval.start)) continue;
        const date = localDate(interval.start);
        if (!days.has(date)) days.set(date, []);
        days.get(date).push(interval);
    }
    instance.dailyPriceQuarters = [...days.values()].flatMap(intervals => [
        ...rankPlanRates(intervals, "buyRate", "cheapest", 1),
        ...rankPlanRates(intervals, "rankedExportRate", "most_expensive", -1)
    ]);
    status.textContent = "Up to 12 intervals per day within the original plan. The cheapest quarters use import rates. The most expensive quarters use export rates including eligible bonus before the volume cap.";
    instance.draw();
}

/** Keep gaps out of the rate ranking and resolve equal rates consistently in time order. */
function rankPlanRates(intervals, field, extreme, order) {
    return intervals.filter(interval => Number.isFinite(interval[field]))
        .sort((a, b) => order * (a[field] - b[field]) || a.start - b.start)
        .slice(0, DAILY_HIGHLIGHT_QUARTERS).map(({start, end}) => ({start, end, extreme}));
}

/** Share the price highlights across mouse, touch and keyboard inspection. */
function planRankNotes(time) {
    const marks = charts.get("price-chart")?.dailyPriceQuarters ?? [];
    return marks.filter(mark => mark.start <= time && time < mark.end)
        .map(mark => PLAN_RANK_NOTES[mark.extreme]);
}
/** Explain the selected mark using the ranked contract rate or the spot preview's status. */
function priceTooltipNotes(item, step, priceHoverTime) {
    const time = Number.isFinite(priceHoverTime) ? priceHoverTime : item.parsed.x;
    if (priceInputPlanId) return [step?.priceStatus, ...planRankNotes(time)].filter(Boolean);
    const quarter = item.chart.dailyPriceQuarters?.find(quarter => quarter.start <= time && time < quarter.end);
    if (!quarter) return [];
    const label = quarter.extreme === "cheapest" ? "12 cheapest spot-price quarters" : "12 most expensive spot-price quarters";
    return [`One of this day's ${label}: ${optionLabel(quarter.status)}`];
}

/** Supply interval evidence and identify the applicable market or plan ranking. */
function chartTooltipNotes(items, {id, balance, axisTime, priceHoverTime}) {
    if (balance && items.length) {
        const bar = items.find(item => item.raw.start != null)?.raw;
        if (!bar) return [];
        return balanceNotes(bar, id, axisTime);
    }
    if (!items.length || ["forecast-chart", "reference-chart", "self-consumption-chart"].includes(id)) return "";
    const step = inspectSteps.find((step) => step.start <= items[0].parsed.x && items[0].parsed.x < step.end);
    if (id === "price-chart") return priceTooltipNotes(items[0], step, priceHoverTime);
    return step?.commands || [];
}

/** Show the actual visible datasets and keep checkbox choices in the shared series menu. */
function createOverviewLegend(id, series, {preserveVisibility, signal, gridSteps, legend}) {
    const planComparison = ["plan-chart", "reference-chart", "self-consumption-chart"].includes(id);
    // A retained immutable plan has the same legend group identities even after its labels change.
    const previousVisibility = preserveVisibility ? new Map([...legend.querySelectorAll("input")].map(input => [input.dataset.legendIdentity, input.checked])) : new Map();
    const seriesOpen = legend.querySelector("details")?.open;
    legend.classList.add("compact-planning-legend");
    legend.replaceChildren();
    let seriesHost = legend;
    const chartKeys = [];
    if (series.length) {
        const disclosure = document.createElement("details");
        const summary = document.createElement("summary");
        const panel = document.createElement("div");
        seriesHost = document.createElement("div");
        disclosure.name = "planning-chart-series";
        disclosure.open = Boolean(seriesOpen);
        summary.textContent = "Chart series";
        panel.className = "planning-series-options";
        seriesHost.className = "chart-legend";
        panel.append(seriesHost);
        disclosure.append(summary, panel); legend.append(disclosure);
        disclosure.addEventListener("keydown", event => {
            if (event.key === "Escape") { disclosure.open = false; summary.focus(); }
        });
        document.addEventListener("pointerdown", event => {
            if (disclosure.open && !disclosure.contains(event.target)) disclosure.open = false;
        }, {signal});
    }
    const legendEntries = [];
    let deviceKey = null, selectAll = null;
    /** Apply a choice the same way for a single checkbox and the bulk action. */
    function setLegendVisibility(result, {name, input, indexes}, checked) {
        if (id === "price-chart") {
            if (checked) hiddenPriceSeries.delete(name); else hiddenPriceSeries.add(name);
        }
        input.checked = checked;
        for (const index of indexes) result.setDatasetVisibility(index, checked);
    }
    /** Keep rounding eligibility separate from the user's checkbox selection. */
    function syncLegend(instance) {
        const minimum = instance.options.scales.x.min ?? -Infinity;
        const maximum = instance.options.scales.x.max ?? Infinity;
        for (const {label, input, indexes, device} of legendEntries) {
            if (!device) continue;
            const bars = new Map();
            let unknown = false;
            for (const index of indexes) for (const point of instance.data.datasets[index].data) {
                if (!(point.start < maximum && point.end > minimum)) continue;
                if (!Number.isFinite(point.y)) { unknown = true; continue; }
                const key = `${point.start}:${point.end}`;
                bars.set(key, (bars.get(key) || 0) + Math.abs(point.y));
            }
            label.hidden = !unknown && ![...bars.values()].some(power => DEVICE_ROUNDING_KW - power <= DEVICE_ROUNDING_TOLERANCE_KW);
            for (const index of indexes) instance.setDatasetVisibility(index, input.checked && !label.hidden);
        }
        if (deviceKey) deviceKey.hidden = !legendEntries.some(({label, device}) => device && !label.hidden);
        if (selectAll) {
            const eligible = legendEntries.filter(({label}) => !label.hidden);
            selectAll.disabled = !eligible.length;
            selectAll.textContent = eligible.every(({input}) => input.checked) ? "Deselect all" : "Select all";
        }
        chartKeys.forEach((item, index) => { item.hidden = !instance.isDatasetVisible(index); });
    }
    /** Attach controls after Chart.js has created the datasets. */
    function renderLegend(result) {
        const {datasets} = result.data;
        if (series.length) {
            selectAll = document.createElement("button");
            selectAll.type = "button"; selectAll.className = "secondary";
            selectAll.setAttribute("aria-controls", id);
            selectAll.addEventListener("click", () => {
                const eligible = legendEntries.filter(({label}) => !label.hidden);
                const checked = !eligible.every(({input}) => input.checked);
                for (const entry of eligible) setLegendVisibility(result, entry, checked);
                applyRange();
            });
            seriesHost.append(selectAll);
        }
        const legendGroups = new Map();
        series.forEach((s, index) => {
            const identity = s.legendId ?? index;
            if (!legendGroups.has(identity)) legendGroups.set(identity, {series: s, indexes: []});
            legendGroups.get(identity).indexes.push(index);
        });
        for (const [identity, {series: s, indexes}] of legendGroups) {
            const label = document.createElement("label"), input = document.createElement("input");
            input.type = "checkbox"; input.dataset.legendIdentity = String(identity);
            input.checked = previousVisibility.get(String(identity)) ?? indexes.every(index => result.isDatasetVisible(index));
            for (const index of indexes) result.setDatasetVisibility(index, input.checked);
            if (s.deviceState != null) input.setAttribute("aria-describedby", `${id}-device-key`);
            const entry = {name: s.name, label, input, indexes, device: s.deviceState != null};
            input.addEventListener("change", () => {
                setLegendVisibility(result, entry, input.checked);
                applyRange();
            });
            const swatch = document.createElement("span"); swatch.className = "series-swatch"; swatch.style.backgroundColor = s.legendColor || datasets[indexes[0]].swatchColor; swatch.setAttribute("aria-hidden", "true");
            label.append(input, swatch, document.createTextNode(s.legendName || s.name)); seriesHost.append(label);
            legendEntries.push(entry);
        }
        const disclosure = legend.querySelector("details");
        for (const dataset of datasets) {
            const item = document.createElement("span"), swatch = document.createElement("span");
            item.className = "planning-series";
            swatch.className = "series-swatch";
            swatch.style.backgroundColor = dataset.swatchColor;
            swatch.classList.toggle("series-swatch-expected", dataset.deviceState === "expected");
            swatch.setAttribute("aria-hidden", "true");
            item.append(swatch, dataset.label);
            legend.insertBefore(item, disclosure);
            chartKeys.push(item);
        }
        if (gridSteps) {
            const item = document.createElement("span"), swatch = document.createElement("span");
            item.className = "grid-mode-legend";
            swatch.className = "grid-mode-swatch";
            swatch.setAttribute("aria-hidden", "true");
            item.append(swatch, ZERO_GRID_LABEL);
            legend.insertBefore(item, disclosure);
        }
        if (series.some(s => s.deviceState != null)) {
            const key = document.createElement("p"); key.id = `${id}-device-key`; deviceKey = key;
            key.className = "chart-device-key";
            key.textContent = "Striped bars show expected use. Solid bars show scheduled use.";
            seriesHost.append(key);
        }
        if (!datasets.some((d) => d.data.length)) {
            const empty = document.createElement("p"); empty.textContent = "No covered data available."; legend.append(empty);
        }
    }
    return {planningLegend: planComparison, syncLegend, renderLegend};
}

/** Keep the grid comparison's existing shortcut to detailed plan evidence. */
function inspectGridExchange(datasets, element) {
    if (!element) return;
    const time = datasets[element.datasetIndex].data[element.index].x;
    const index = inspectSteps.findIndex(interval => interval.start <= time && time < interval.end);
    if (index < 0) return;
    $("inspect-time").value = index;
    inspect();
    $("plan-evidence-panel").open = true;
    $("interval-evidence-panel").open = true;
    $("evidence-time").scrollIntoView({block: "nearest"});
}

/** Use calendar dates for daily totals and quarter boundaries for planning charts. */
function buildOverviewTimeTicks(scale, canvas, datasets, calendarDates) {
    if (calendarDates) {
        const boundaries = [...new Set(datasets.flatMap(dataset => dataset.data.map(point => point.x)))].sort((left, right) => left - right);
        const labels = Math.max(2, Math.min(MAX_TIME_AXIS_LABELS, Math.floor((scale.maxWidth || canvas.clientWidth) / DAILY_DATE_LABEL_MIN_WIDTH_PX)));
        scale.ticks = [...new Set(Array.from({length: labels}, (_entry, index) => boundaries[Math.round(index * (boundaries.length - 1) / (labels - 1))]))].filter(Number.isFinite).map(value => ({value}));
        return;
    }
    const first = Math.ceil(scale.min / QUARTER_MILLISECONDS);
    const last = Math.floor(scale.max / QUARTER_MILLISECONDS);
    const stride = Math.max(1, Math.ceil((last - first) / (MAX_TIME_AXIS_LABELS - 1)));
    scale.ticks = [];
    for (let quarter = first; quarter <= last; quarter += stride) scale.ticks.push({value: quarter * QUARTER_MILLISECONDS});
}

/** Configure the primary value scale and the optional battery state scale. */
function overviewValueAxes({id, unit, minimum, balance, hasSoc, linked}) {
    return {
        y: {min: minimum ?? undefined, suggestedMin: balance ? 0 : undefined, stacked: balance, beginAtZero: id === "price-chart", title: {display: Boolean(unit), text: unit}, afterFit: linked ? scale => { scale.width = LINKED_LEFT_AXIS_PX; } : undefined, grid: {color: (context) => context.tick.value === 0 ? "#455d70" : "#edf1f5", lineWidth: (context) => context.tick.value === 0 ? 2 : 1}, ticks: {maxTicksLimit: 6, callback: (value) => unit === "W" ? number(value) : valueText(value, unit)}},
        ...(hasSoc ? {soc: {position: "right", min: 0, max: 100, stacked: false, afterFit: linked ? scale => { scale.width = LINKED_RIGHT_AXIS_PX; } : undefined, title: {display: true, text: "SoC (%)"}, ticks: {callback: (value) => percentage(value, false)}, grid: {drawOnChartArea: false}}} : {})
    };
}

/** Own one chart's tooltip, scroll positioning, and keyboard dismissal. */
function createOverviewTooltip(id) {
    $(`${id}-tooltip`)?.remove();
    const tooltip = document.createElement("div"); tooltip.id = `${id}-tooltip`; tooltip.className = "chart-tooltip chart-tooltip-point";
    tooltip.hidden = true; tooltip.setAttribute("role", "tooltip"); document.body.append(tooltip);
    const daySummary = {owner: null, pinned: false};
    /** Shared dismissal also revokes date ownership when scrolling or interval inspection replaces it. */
    const hideTooltip = () => {
        tooltip.hidden = true; tooltip.classList.remove("chart-tooltip-linked");
        daySummary.owner?.removeAttribute("aria-describedby");
        daySummary.owner = null; daySummary.pinned = false;
    };
    const tooltipEvents = new AbortController();
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") hideTooltip();
    }, {signal: tooltipEvents.signal});
    window.addEventListener("scroll", event => {
        if (event.target instanceof Node && tooltip.contains(event.target)) return;
        if (linkedInterval && tooltip.classList.contains("chart-tooltip-linked") && !tooltip.hidden) {
            const chart = charts.get(id), bounds = chart.canvas.getBoundingClientRect();
            if (bounds.bottom > 0 && bounds.top < innerHeight) {
                const time = (linkedInterval.start + linkedInterval.end) / 2;
                positionChartTooltip(tooltip, chart.canvas, chart.scales.x.getPixelForValue(time), chart.chartArea.top);
                return;
            }
        }
        const owner = daySummary.owner, bounds = owner?.getBoundingClientRect();
        const movesOwner = event.target === document || event.target === window
            || event.target instanceof Element && event.target.contains(owner);
        if (owner && owner === document.activeElement && movesOwner && !tooltip.hidden
            && bounds.bottom > 0 && bounds.top < innerHeight && bounds.right > 0 && bounds.left < innerWidth) {
            positionDayTooltip(tooltip, owner);
        } else hideTooltip();
    }, {capture: true, signal: tooltipEvents.signal});
    return {tooltip, daySummary, hideTooltip, tooltipEvents};
}

/** Convert the original line or interval evidence into one Chart.js dataset. */
function overviewDataset(entry, index, id, unit) {
    const values = [];
    for (const segment of entry.segments || [entry.points || []]) {
        if (values.length) values.push({x: segment[0][0], y: null});
        values.push(...segment.map(([x, y, priceKind]) => ({x, y, priceKind})));
    }
    if (entry.intervals) values.push(...entry.intervals.map(({start, end, value, coveredStart, coveredEnd}) => ({x: (start + end) / 2, y: value, start, end, coveredStart, coveredEnd})));
    const color = entry.color || palette[index % palette.length];
    return {
        label: entry.name,
        data: values,
        hidden: id === "price-chart" && hiddenPriceSeries.has(entry.name),
        type: entry.type || "line",
        stack: entry.stack,
        yAxisID: entry.axis || "y",
        unit: entry.unit || unit,
        tooltipGroup: entry.tooltipGroup,
        deviceState: entry.deviceState,
        swatchColor: color,
        borderColor: entry.outlineColor || color,
        backgroundColor: deviceFill(color, entry.deviceState),
        borderWidth: entry.outlineColor ? ({raw}) => raw?.y ? {top: 1, bottom: 1} : 0 : entry.type === "bar" ? 0 : 2,
        borderSkipped: entry.outlineColor ? false : undefined,
        borderDash: entry.reference ? [6, 4] : [],
        segment: {borderDash: ({p0}) => p0.raw.priceKind === "forecast" ? FORECAST_PRICE_DASH : undefined},
        pointRadius: 0,
        pointHitRadius: 10,
        spanGaps: false,
        order: entry.axis === "soc" ? 0 : entry.stackOrder ?? 1,
    };
}

/**
 * Render series into canvas id, destroying its previous chart, removing its tooltip
 * and rebuilding its accessible legend. Current-price series retain their visibility.
 * Same-plan callers retain explicit checkbox choices by immutable legend group identity.
 * Points/segments are timed lines with null
 * gaps. Price points retain their published/forecast kind for interval-specific strokes.
 * Balance intervals are projected onto quarter-hour bars with explicit coverage.
 * axis: "soc" uses the right-hand 0–100% axis. utcDays selects UTC date labels.
 * Calendar headings, alternating bands and midnight separators follow installation
 * time unless utcDays selects UTC. Timestamped charts reserve heading space and
 * remove their headings on destruction. Clock ticks show installation time
 * without timezone names while the headings supply the weekday and date.
 * unit names the default y unit. Minimum fixes a physical y baseline. balance stacks series
 * sharing a stack and includes zero. Rendering applies the selected planning range
 * without category-style end padding. Interval timestamps already determine bar widths.
 */
function chart(id, series, {utcDays = false, calendarDates = false, unit = "", minimum = null, balance = false, preserveVisibility = false, deferRange = false, gridSteps = null} = {}) {
    let balanceTime = null, priceHoverTime = null;
    const batteryPlan = id === "battery-plan-chart";
    if (balance) series = quarterBalanceSeries(series);
    charts.get(id)?.destroy();
    const {tooltip, daySummary, hideTooltip, tooltipEvents} = createOverviewTooltip(id);
    const canvas = $(id), legend = $(`${id}-legend`);
    const linked = isLinkedChart(id);
    const hasSoc = series.some(entry => entry.axis === "soc");
    const {planningLegend, syncLegend, renderLegend} = createOverviewLegend(id, series, {preserveVisibility, signal: tooltipEvents.signal, gridSteps, legend});
    const axisTime = calendarDates ? value => dateLabel(localDate(value)) : utcDays ? (value) => dateLabel(new Date(value).toISOString().slice(0, 10)) : value => when(value);
    const datasets = series.map((entry, index) => overviewDataset(entry, index, id, unit));
    const result = new Chart(canvas, {
        type: "line", data: {datasets: planningLegend ? [] : datasets}, plugins: [intervalWidths, ...(id === "price-chart" ? [dailyPriceExtremes] : []), ...(calendarDates ? [] : [calendarDays]), ...(gridSteps ? [gridModeStrip(gridSteps, positionDayTooltip)] : []), ...(linked ? [linkedChartInteraction(canvas, tooltip, hideTooltip, tooltipEvents.signal)] : []), {
            id: "deviceLegend",
            beforeUpdate: syncLegend
        }, {id: "ownedTooltip", afterDestroy() { hideTooltip(); tooltipEvents.abort(); tooltip.remove(); }}],
        options: {responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
            layout: {padding: {right: linked && !hasSoc ? LINKED_RIGHT_AXIS_PX : 0, bottom: gridSteps ? GRID_MODE_STRIP_HEIGHT_PX : 0}},
            interaction: {mode: "nearest", axis: "x", intersect: false},
            onHover: (event, _elements, instance) => {
                if (id === "price-chart") priceHoverTime = instance.scales.x.getValueForPixel(event.x);
                if (balance) {
                    const hits = instance.getElementsAtEventForMode(event, "nearest", {axis: "xy", intersect: true}, false);
                    balanceTime = !batteryPlan && hits.some(hit => instance.data.datasets[hit.datasetIndex].tooltipGroup === "Battery state")
                        ? null : instance.scales.x.getValueForPixel(event.x);
                }
            },
            plugins: {calendarDays: {utc: utcDays, onHeading: planningLegend ? (instance, heading, day) => { hideTooltip(); planningDayHeading(instance, series, tooltip, daySummary, heading, day); } : null}, valueAxisHeadroom: {group: null}, legend: {display: false}, tooltip: {enabled: false, external: (context) => {
                if (!context.tooltip.opacity && tooltip.classList.contains("chart-tooltip-linked")) return;
                hideTooltip();
                const time = balanceTime; balanceTime = null;
                renderTooltip(context, tooltip, balance, time);
            }, callbacks: {
                title: (items) => !items.length ? "" : items[0].raw.start != null ? `From ${axisTime(items[0].raw.start)} to ${axisTime(items[0].raw.end)}` : axisTime(items[0].parsed.x),
                label: (item) => `${item.dataset.label}: ${valueText(item.parsed.y, item.dataset.unit)} ${item.dataset.unit}`,
                afterBody: items => chartTooltipNotes(items, {id, balance, axisTime, priceHoverTime})
            }}},
            scales: {x: {type: "linear", offset: false, grid: {display: false}, display: id !== "plan-chart",
                afterBuildTicks: utcDays ? undefined : (scale) => buildOverviewTimeTicks(scale, canvas, datasets, calendarDates),
                ticks: {maxTicksLimit: MAX_TIME_AXIS_LABELS, maxRotation: 0, callback: (value) => utcDays || calendarDates ? axisTime(value) : calendarTime(value)},
                title: {display: utcDays || calendarDates, text: "Calendar date"}},
                ...overviewValueAxes({id, unit, minimum, balance, hasSoc, linked})},
            onClick: id === "energy-chart" ? (_event, elements) => inspectGridExchange(datasets, elements[0]) : undefined
        }
    });
    // Complete scenario data before the owner applies one shared range for this batch.
    if (planningLegend) {
        result.data.datasets = datasets;
        result.options.plugins.valueAxisHeadroom.group = "planning-comparison";
    }
    charts.set(id, result);
    syncChartLegends.set(result, syncLegend);
    renderLegend(result);
    if (!deferRange) applyRange();
    if (id === "price-chart") loadDailyPrices(result);
}
/**
 * Set shared axes without changing frozen data: the latest plan starts at the
 * current quarter, including after its last interval. Historical advice starts at its original containing quarter.
 * Partial bars keep their actual timestamps. End after the selected hours or at the
 * original last endpoint for full horizon. All labels fall on quarter boundaries.
 * Savings and realization charts keep their independent date ranges.
 */
function applyRange() {
    const first = inspectSteps[0]?.start, end = inspectSteps.at(-1)?.end;
    const now = Date.now();
    const currentQuarter = Math.floor(now / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS;
    const currentPlan = !selectedLinkedPlan && $("plan-version").selectedIndex === 0 && first <= now;
    const start = first == null ? undefined : currentPlan ? currentQuarter : Math.floor(first / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS;
    for (const [id, instance] of charts) {
        if (id === "price-chart" && !priceInputPlanId) {
            const priceStart = currentPriceWindow ? Math.max(currentPriceWindow.start, currentQuarter) : undefined;
            const priceEnd = currentPriceWindow ? Math.max(currentPriceWindow.end, priceStart + QUARTER_MILLISECONDS) : undefined;
            instance.options.scales.x.min = priceStart;
            instance.options.scales.x.max = planningHours && priceStart != null ? priceStart + planningHours * MILLISECONDS_PER_HOUR : priceEnd;
            continue;
        }
        const bars = instance.data.datasets.find((dataset) => dataset.type === "bar")?.data;
        const fullEnd = end == null ? bars?.at(-1)?.end : Math.max(Math.ceil(end / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS, start + QUARTER_MILLISECONDS);
        instance.options.scales.x.min = start ?? bars?.[0]?.start;
        instance.options.scales.x.max = planningHours && start != null ? start + planningHours * MILLISECONDS_PER_HOUR : fullEnd;
    }
    // Set all windows and rounding visibility before computing the shared comparison axes.
    for (const instance of charts.values()) syncChartLegends.get(instance)?.(instance);
    updateCharts(charts.values());
    if (first != null && end != null && start != null) {
        showPeriodCosts(start, planningHours ? start + planningHours * MILLISECONDS_PER_HOUR : Math.max(end, start + QUARTER_MILLISECONDS));
    }
}
/** Read original full-horizon scenario amounts without revaluing any flow. */
function originalScenarioCosts(plan) {
    const baseline = plan.scenarios?.without_battery?.comparison, self = plan.scenarios?.self_consumption?.comparison;
    return {
        optimizer: {costs: baseline?.ems_costs || self?.ems_costs, benefit_vs_without_battery: baseline?.expected_savings},
        self_consumption: {costs: self?.reference_costs, benefit_vs_without_battery: plan.scenarios?.self_consumption?.expected_savings_vs_without_battery},
        without_battery: {costs: baseline?.reference_costs}
    };
}
/** Share pending and successful original-contract cost slices for the displayed plan. */
function planWindowCosts(plan, start, end) {
    const key = `${start}:${end}`;
    if (!plan.windows.has(key)) {
        const query = new URLSearchParams({start: new Date(start).toISOString(), end: new Date(end).toISOString()});
        const result = api(`v1/plans/${encodeURIComponent(plan.id)}/comparison?${query}`).catch(error => {
            plan.windows.delete(key);
            throw error;
        });
        plan.windows.set(key, result);
    }
    return plan.windows.get(key);
}
/** Add the financial comparison for exactly the covered interval already shown in the tooltip. */
async function showDayBenefit(tooltip, button, scenario, start, end) {
    const plan = periodCostPlan;
    const line = document.createElement("p");
    line.className = "chart-tooltip-day-benefit";
    line.setAttribute("aria-live", "polite");
    const label = `Expected benefit versus without battery${plan?.projection.valuation ? " (original contract)" : ""}`;
    line.textContent = `${label}: ${plan ? "Loading…" : "Unavailable"}`;
    tooltip.querySelector(".balance-tooltip-groups").before(line);
    if (!plan) return;
    let benefit;
    try {
        const scenarios = start === plan.start && end === plan.end
            ? originalScenarioCosts(plan) : (await planWindowCosts(plan, start, end)).scenarios;
        benefit = scenarios?.[scenario]?.benefit_vs_without_battery;
    } catch { /* Keep energy totals usable when the financial comparison fails. */ }
    if (periodCostPlan !== plan || !line.isConnected || tooltip.hidden) return;
    const known = Number.isFinite(benefit);
    const zero = known && money(Math.abs(benefit)) === money(0);
    const amount = known ? `${money(zero ? 0 : benefit)} ${plan.currency}`.trim() : "Unavailable";
    line.textContent = `${label}: ${amount}`;
    positionDayTooltip(tooltip, button);
}
/** Keep successful full-plan valuations visible independently of selected-period loading or failure. */
function renderPeriodCosts(period, scenarios, currency, message, plan = null) {
    const valuation = plan?.projection.valuation, original = plan ? originalScenarioCosts(plan) : null;
    const fullPeriod = period && plan && Date.parse(period.start) === plan.start && Date.parse(period.end) === plan.end;
    for (const [name, host] of [["optimizer", "ems-costs"], ["self_consumption", "self-consumption-costs"], ["without_battery", "reference-costs"]]) {
        const entry = scenarios?.[name];
        const corrected = valuation && name !== "without_battery";
        const correctedCosts = corrected ? valuation[name === "optimizer" ? "ems_costs" : "reference_costs"] : null;
        const costs = corrected && fullPeriod ? correctedCosts : entry?.costs;
        renderScenarioCosts(host, costs, currency, corrected && fullPeriod ? null : entry?.benefit_vs_without_battery, name !== "without_battery", {
            valuation: corrected ? {costs: correctedCosts, evaluated_at: valuation.evaluated_at, period: plan.period, separate: !fullPeriod} : null,
            originalCosts: corrected ? original[name]?.costs : null,
            basis: valuation && !corrected ? "Original contract: A corrected no-battery valuation is unavailable." : ""
        });
    }
    const basis = !valuation ? "" : fullPeriod ? "Optimizer and self-consumption use the latest successful contract valuation. Without battery retains original contract rates." : "Original contract rates for this selected period. The full original plan valuation is shown separately.";
    for (const caption of document.querySelectorAll(".scenario-period")) caption.textContent = message || (period ? `From ${when(period.start)} to ${when(period.end)}.${basis ? ` ${basis}` : ""}` : "No planned intervals in this period. Select an earlier plan or wait for a new plan.");
}
/** Request financial slices from their owning module. Late responses cannot replace another plan or range. */
async function showPeriodCosts(start, end) {
    const plan = periodCostPlan;
    if (!plan) return;
    const key = `${plan.id}:${start}:${end}`;
    if (periodCostKey === key) return;
    periodCostKey = key;
    const request = ++periodCostRequest;
    if (start <= plan.start && end >= plan.end) {
        renderPeriodCosts(plan.period, originalScenarioCosts(plan), plan.currency, undefined, plan);
        return;
    }
    renderPeriodCosts(null, null, plan.currency, "Loading expected costs for the selected period…", plan);
    try {
        const data = await planWindowCosts(plan, start, end);
        if (request !== periodCostRequest || periodCostPlan !== plan) return;
        renderPeriodCosts(data.period, data.scenarios, plan.currency, undefined, plan);
        if (plan.projection.valuation) for (const caption of document.querySelectorAll(".scenario-period")) {
            caption.append(" ");
            const link = document.createElement("a"), target = new URL(location.href);
            target.searchParams.set("plan", plan.id); link.href = target.href;
            link.textContent = "Open full original plan";
            link.addEventListener("click", event => {
                if (event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
                event.preventDefault(); selectPlanningRange(0); selectOriginal(plan.id);
            });
            caption.append(link);
        }
        for (const [name, host] of [["optimizer", "ems-costs"], ["self_consumption", "self-consumption-costs"], ["without_battery", "reference-costs"]]) {
            const issue = data.scenarios?.[name]?.issue;
            if (issue) document.getElementById(host).closest("figcaption").querySelector(".scenario-period").textContent += ` ${issue}`;
        }
    } catch (error) {
        if (request !== periodCostRequest || periodCostPlan !== plan) return;
        periodCostKey = "";
        renderPeriodCosts(null, null, plan.currency, `Expected costs for the selected period are unavailable. ${error.message} Use Refresh overview to retry.`, plan);
    }
}
/** Use the same labelled facts as the original-plan evidence, without parsing command prose. */
function intervalCard(title, rows, group) {
    return {title, facts: rows.map(([label, value, unit = ""]) => ({label, value, unit})), observations: [], ...(group ? {group} : {})};
}
/** Present recorded interval quantities separately from optional signed technical balances. */
function renderIntervalValues(step, evidence) {
    const parent = $("inspect-values");
    if (!step) { parent.textContent = "No planned intervals available."; return; }
    let values = parent.querySelector("#interval-evidence");
    if (!values) {
        parent.replaceChildren();
        const period = document.createElement("p"), technical = document.createElement("details"), summary = document.createElement("summary"), balances = document.createElement("div");
        period.className = "interval-values-period";
        values = document.createElement("div");
        values.id = "interval-evidence"; values.className = "evidence-grid"; values.setAttribute("aria-label", "Selected interval evidence");
        summary.textContent = "Energy balance and source details"; balances.className = "evidence-grid";
        technical.append(summary, balances); parent.append(period, values, technical);
    }
    const period = parent.querySelector(".interval-values-period");
    period.hidden = Boolean(evidence); period.textContent = `From ${when(step.start)} to ${when(step.end)}`;
    evidenceCards(values, evidence?.cards?.length ? [...evidence.cards, ...step.applianceValues] : step.values);
    evidenceCards(parent.querySelector("details > .evidence-grid"), [step.priceSource, ...step.batteryFlowValues, ...step.balances]);
}
/** Explain exact interval advice with keyboard/touch access independent of canvas tooltips. */
function inspect() {
    const step = inspectSteps[Number($("inspect-time").value)];
    const evidence = planEvidence?.intervals?.find((interval) => Date.parse(interval.start) === step?.start);
    renderIntervalValues(step, evidence);
    $("evidence-time").textContent = evidence ? `From ${when(evidence.start)} to ${when(evidence.end)}: ${number((Date.parse(evidence.end) - Date.parse(evidence.start)) / MILLISECONDS_PER_MINUTE)} minutes: Energy quantities apply to this interval. Prices are rates per kWh.` : "";
    $("evidence-issue").textContent = evidence?.issue || planEvidence?.issue || (!planEvidence ? $("plan-evidence-status").textContent : !evidence ? "Original interval evidence is unavailable." : "");
}
/** Switch focused functional pages. Keep forms, selected versions and chart data in memory. */
function view() {
    const selected = location.hash === "#savings" ? "savings" : "planning";
    const title = selected === "savings" ? "Savings" : "Planning";
    $("page-title").textContent = title;
    $("page-icon").src = base + `setup/icons/${selected}.svg`;
    document.title = `Open Energy Planner: ${title}`;
    for (const name of ["planning", "savings"]) {
        $(`${name}-view`).hidden = name !== selected;
        document.querySelector(`[data-view="${name}"]`).setAttribute("aria-current", name === selected ? "page" : "false");
    }
    try {
        const host = parent !== window && parent.document.getElementById("workspace-content");
        if (host?.contentWindow === window) host.dispatchEvent(new Event("oep-view-change"));
    } catch { /* Standalone and cross-origin views have no shared navigation owner. */ }
    for (const instance of charts.values()) instance.resize();
    for (const tooltip of document.querySelectorAll(".chart-tooltip")) tooltip.hidden = true;
    if (overviewSessionReady && (overviewIngress || token)) {
        if (selected === "savings") savings();
        else if (renderedPlanId && renderedPlanId === $("plan-version").value && planSourceNamesGeneration !== sourceNamesGeneration) showPlan();
        else if (!planRefreshEnabled && !selectedLinkedPlan) load();
    }
}
window.addEventListener("hashchange", view);
$("inspect-time").addEventListener("input", inspect);
/** Keep the selected range and its controls aligned before rendering or opening original advice. */
function selectPlanningRange(hours) {
    planningHours = hours;
    for (const button of document.querySelectorAll("[data-hours]")) button.setAttribute("aria-pressed", String(Number(button.dataset.hours) === hours));
}
for (const button of document.querySelectorAll("[data-hours]")) button.addEventListener("click", () => {
    selectPlanningRange(Number(button.dataset.hours));
    applyRange();
});
view();
/** Display server-valued costs without mixing contract bases, retaining original amounts in the disclosure. */
function renderScenarioCosts(id, costs, currency, benefit, showBenefit, {valuation = null, originalCosts = null, basis = ""} = {}) {
    const host = $(id), expanded = host.querySelector("details")?.open || false;
    const totals = document.createElement("dl");
    const metric = (parent, label, value) => {
        const field = document.createElement("div"), name = document.createElement("dt"), amount = document.createElement("dd");
        name.textContent = label; amount.textContent = `${money(value)} ${currency}`;
        field.append(name, amount); parent.append(field);
    };
    if (valuation?.separate) totals.className = "selected-period-costs";
    metric(totals, valuation?.separate ? "Selected period economic cost: Original contract" : costs?.economic_cost == null ? "Expected costs unavailable" : "Expected economic cost", costs?.economic_cost);
    if (showBenefit) {
        const known = typeof benefit === "number" && Number.isFinite(benefit);
        const zero = known && money(Math.abs(benefit)) === money(0);
        metric(totals, valuation?.separate ? "Selected period benefit versus without battery: Original contract" : "Expected benefit versus without battery", zero ? 0 : benefit);
        totals.lastElementChild.dataset.benefitState = !known ? "unavailable" : zero ? "zero" : benefit > 0 ? "positive" : "negative";
    }
    const details = document.createElement("details"), summary = document.createElement("summary"), breakdown = document.createElement("dl");
    summary.textContent = "Cost breakdown"; details.open = expanded;
    /** Use the financial API's own component values for either valuation basis. */
    const components = (target, values) => {
        metric(target, "Import cost", values?.purchase);
        metric(target, "Export income", values?.sale);
        metric(target, "Bonus income", values?.bonus);
        metric(target, "Net electricity cost", values?.electricity_cost ?? values?.grid_cash);
        metric(target, "Wear", values?.wear);
        metric(target, "Battery value change (not cash)", values?.inventory_change);
    };
    components(breakdown, costs);
    const cashExplanation = document.createElement("p"), explanation = document.createElement("p");
    cashExplanation.textContent = "Net electricity cost = import cost - export income - bonus income. Amounts are rounded for display. Totals use full precision.";
    explanation.textContent = "Economic cost = net electricity cost + wear - battery value change. A positive value change reduces economic cost. Battery value change is not cash.";
    details.append(summary);
    if (valuation?.separate) {
        const title = document.createElement("h4"); title.textContent = "Selected period: Original contract"; details.append(title);
    }
    details.append(breakdown, cashExplanation, explanation);
    if (valuation?.separate) {
        const updated = document.createElement("section"), title = document.createElement("h4"), amounts = document.createElement("dl");
        updated.className = "valuation-costs"; title.textContent = "Latest valuation: Full original plan";
        components(amounts, valuation.costs); updated.append(title, amounts); details.append(updated);
    }
    if (valuation) {
        const original = document.createElement("section"), title = document.createElement("h4"), amounts = document.createElement("dl");
        original.className = "original-costs"; title.textContent = "Original expected costs";
        metric(amounts, "Original expected economic cost", originalCosts?.economic_cost);
        components(amounts, originalCosts);
        original.append(title, amounts); details.append(original);
    }
    host.replaceChildren(totals, details);
    if (valuation?.separate) {
        const updated = document.createElement("dl"); updated.className = "contract-valuation";
        metric(updated, "Latest expected economic cost: Full original plan", valuation.costs?.economic_cost);
        host.prepend(updated);
    }
    if (basis || valuation) {
        const note = document.createElement("p"); note.textContent = basis;
        if (valuation) {
            note.className = "valuation-period";
            const calculated = document.createElement("time"); calculated.dateTime = valuation.evaluated_at;
            calculated.className = "valuation-calculated";
            calculated.textContent = when(valuation.evaluated_at);
            const period = valuation.period ? `From ${when(valuation.period.start)} to ${when(valuation.period.end)}: ${number(valuation.period.duration_h)} hours.` : "Period unavailable.";
            note.replaceChildren("Contract updated: Calculated ", calculated, `: Full original plan: ${period} Repricing does not re-optimize the plan. Full-plan benefit versus without battery is unavailable because a corrected no-battery valuation is unavailable.`);
        }
        host.insertBefore(note, valuation?.separate ? totals : details);
    }
    if (!showBenefit) {
        const note = document.createElement("p"); note.textContent = "Baseline without storage"; host.append(note);
    }
}

/** Draw the selected plan's battery chart once the disclosure is open and measurable. */
function drawBatteryPlan(deferRange = false) {
    if (!batteryPlanPanel.open || !pendingBatterySeries) return;
    chart("battery-plan-chart", pendingBatterySeries, {unit: "kW", balance: true, preserveVisibility: true, deferRange});
    pendingBatterySeries = null;
}

/** Remove figures and legend choices before showing a different plan or a plan without batteries. */
function clearBatteryPlan() {
    pendingBatterySeries = null;
    batteryPlanPanel.hidden = true;
    batteryPlanPanel.open = false;
    charts.get("battery-plan-chart")?.destroy();
    charts.delete("battery-plan-chart");
    $("battery-plan-chart-legend").replaceChildren();
}

batteryPlanPanel.addEventListener("toggle", () => {
    if (batteryPlanPanel.open) drawBatteryPlan();
    else {
        const tooltip = $("battery-plan-chart-tooltip");
        if (tooltip) tooltip.hidden = true;
    }
});

for (const {title, routes} of BATTERY_FLOW_GROUPS) {
    const key = document.createElement("div"), heading = document.createElement("strong");
    heading.textContent = title;
    key.append(heading);
    for (const [, label, color] of routes) {
        const item = document.createElement("span"), swatch = document.createElement("i");
        swatch.className = "series-swatch";
        swatch.style.backgroundColor = color;
        swatch.setAttribute("aria-hidden", "true");
        item.append(swatch, document.createTextNode(label));
        key.append(item);
    }
    $("battery-flow-key").append(key);
}

/** Clear supplementary values as soon as a different original plan is displayed. */
function clearPlanComparisons() {
    evidenceRequest = null;
    simulationReadPending = false;
    periodCostPlan = null; periodCostKey = ""; periodCostRequest++;
    for (const chartId of ["reference-chart", "self-consumption-chart"]) {
        chart(chartId, [], {unit: "kW", balance: true, deferRange: true});
        $(chartId).closest(".chart-wrap").hidden = true;
    }
    $("reference-issue").textContent = $("self-consumption-issue").textContent = "";
    renderPeriodCosts(null, null, "", "Loading simulations and expected costs…");
    $("expected-benefit").textContent = "Loading simulation comparisons…";
    planEvidence = null;
    $("evidence-period").textContent = "";
    $("plan-evidence-status").textContent = "Open this panel to load original plan evidence.";
    evidenceCards($("plan-evidence"), []);
}
/**
 * Load evidence for an open disclosure, sharing completion through rendering and recovery.
 * Callers joining a pending read await the same lifecycle without issuing another fetch.
 */
function loadPlanEvidence() {
    if (evidenceRequest) return evidenceRequest;
    if (!$("plan-evidence-panel").open || !renderedPlanId || planEvidence) return Promise.resolve();
    const id = renderedPlanId;
    $("plan-evidence-status").textContent = "Loading original plan evidence…";
    $("evidence-issue").textContent = "Loading original plan evidence…";
    const pending = (async () => {
        try {
            const data = await api(`v1/plans/${encodeURIComponent(id)}/comparison?view=evidence`);
            if (evidenceRequest !== pending || id !== renderedPlanId) return;
            if (data.plan_id !== id) throw new Error("Cannot read this view: The plan identity does not match. Please retry.");
            planEvidence = data.evidence;
            $("evidence-period").textContent = planEvidence?.period ? `From ${when(planEvidence.period.start)} to ${when(planEvidence.period.end)}: ${number(planEvidence.period.duration_h)} hours: All totals below cover this whole period.` : "";
            $("plan-evidence-status").textContent = planEvidence?.issue || "";
            evidenceCards($("plan-evidence"), planEvidence?.summary || []);
            inspect();
        } catch {
            if (evidenceRequest !== pending || id !== renderedPlanId) return;
            planEvidence = null;
            const message = "Original plan evidence is unavailable. Close and reopen this panel to retry.";
            $("plan-evidence-status").textContent = $("evidence-issue").textContent = message;
        } finally {
            if (evidenceRequest === pending) evidenceRequest = null;
        }
    })();
    evidenceRequest = pending;
    return pending;
}
$("plan-evidence-panel").addEventListener("toggle", loadPlanEvidence);
$("interval-evidence-panel").addEventListener("toggle", loadPlanEvidence);
/** Project original grid flows and optional references using the already rendered interval boundaries. */
function gridExchangeSeries(plan, comparison = null, baseline = null) {
    const intervals = plan.input_snapshot?.problem?.intervals || [];
    const ems = [], reference = [], bareReference = [];
    intervals.forEach((step, index) => {
        const flow = plan.expected_flows?.[index] || {}, ref = comparison?.reference?.[index], bare = baseline?.reference?.[index];
        const start = inspectSteps[index].start, end = inspectSteps[index].end;
        const imported = flow.grid_import ?? (flow.grid_to_load != null && flow.grid_to_battery != null ? flow.grid_to_load + flow.grid_to_battery : null);
        const exported = flow.grid_export ?? (flow.pv_to_grid != null && flow.battery_to_grid != null ? flow.pv_to_grid + flow.battery_to_grid : null);
        const power = imported != null && exported != null ? (imported - exported) / step.duration_h * 1000 : null;
        ems.push([start, power], [end, power]);
        if (ref) reference.push([start, (ref.import_kwh - ref.export_kwh) / step.duration_h * 1000], [end, (ref.import_kwh - ref.export_kwh) / step.duration_h * 1000]);
        if (bare) bareReference.push([start, (bare.import_kwh - bare.export_kwh) / step.duration_h * 1000], [end, (bare.import_kwh - bare.export_kwh) / step.duration_h * 1000]);
    });
    const series = [{name: $("optimizer-heading").textContent, legendId: "optimizer", points: ems}];
    if (reference.length) series.push({name: $("self-consumption-heading").textContent, legendId: "self-consumption", points: reference, color: palette[2]});
    if (bareReference.length) series.push({name: $("reference-heading").textContent, legendId: "without-battery", points: bareReference, color: palette[1]});
    return series;
}
/** Add same-plan simulations and server-owned costs without rebuilding the user's plan charts. */
function renderPlanComparisons(data, plan, {deferRange = false} = {}) {
    const id = plan.plan_id, currency = plan.currency || "";
    const samePlan = renderedPlanId === id;
    const comparison = data.comparison || data.scenarios?.self_consumption?.comparison;
    const confirmedProjection = periodCostPlan?.projection === data;
    const withoutBattery = data.scenarios?.without_battery;
    const selfConsumption = data.scenarios?.self_consumption;
    if (!confirmedProjection) {
        for (const [chartId, entry, issueId] of [["reference-chart", withoutBattery, "reference-issue"], ["self-consumption-chart", selfConsumption, "self-consumption-issue"]]) {
            const balance = entry?.chart ? energyBalances(entry.chart, new Map(Object.entries(plan.comparison_names || {})))[0] : null;
            $(chartId).closest(".chart-wrap").hidden = !balance;
            chart(chartId, balance?.series || [], {unit: "kW", balance: true, preserveVisibility: samePlan, deferRange: true});
            $(issueId).textContent = entry?.issue || (balance?.missing ? `${balance.missing} interval(s) have incomplete physical evidence.` : "");
        }
    }
    const baseline = withoutBattery?.comparison;
    const period = inspectSteps.length ? {start: new Date(inspectSteps[0].start).toISOString(), end: new Date(inspectSteps.at(-1).end).toISOString(), duration_h: (plan.input_snapshot?.problem?.intervals || []).reduce((total, interval) => total + interval.duration_h, 0)} : null;
    let message = "";
    if (data.valuation) message += " Contract updated. Self-consumption costs were repriced without re-optimization.";
    if (data.valuation_status?.issue) message += ` Repricing unavailable: ${data.valuation_status.issue}.`;
    if (comparison?.reference_start?.status === "provisional_common_start") message += " Self-consumption reference initialization is provisional.";
    if (comparison?.reference_start?.estimated) message += " Self-consumption reference start is estimated.";
    $("expected-benefit").textContent = message.trim();
    if (!confirmedProjection) {
        chart("energy-chart", gridExchangeSeries(plan, comparison, baseline), {unit: "W", preserveVisibility: samePlan, deferRange: true});
        periodCostPlan = {id, currency, projection: data, scenarios: data.scenarios, period, start: inspectSteps[0]?.start, end: inspectSteps.at(-1)?.end, windows: new Map()};
        periodCostKey = ""; periodCostRequest++;
    }
    inspect(); if (!deferRange) applyRange();
}

/** Preserve frozen base and bonus-inclusive rates as distinct interval values. */
function intervalContractRates({plan, bonusRules, index, start, end, prices, tariff, bonusSell}) {
    let exportWithBonus = null;
    if (bonusRules.length) {
        // Frozen compiled rates already include rule/window eligibility. Intentionally ignore quota.
        const rates = bonusRules.map((rule) => rule.rates?.[index]);
        const tariffs = plan.input_snapshot?.tariff_evidence || [];
        const covered = !tariffs.length || tariffs.some((tariff) => Date.parse(tariff.start) <= start && Date.parse(tariff.end) >= end);
        const known = covered && Number.isFinite(prices.sell_price) && rates.every((rate) => Number.isFinite(rate) && rate >= 0);
        exportWithBonus = known ? prices.sell_price + rates.reduce((sum, rate) => sum + rate, 0) : null;
        bonusSell.push([start, exportWithBonus, tariff?.price_kind], [end, exportWithBonus, tariff?.price_kind]);
    }
    const buyRate = tariff ? tariff.buy_price : prices.buy_price;
    const sellRate = tariff ? tariff.sell_price : prices.sell_price;
    return {buyRate, sellRate, rankedExportRate: bonusRules.length ? exportWithBonus : sellRate};
}

/** Clear display state before reading a different plan or the live price preview. */
function resetPlanSelection(id) {
    periodCostPlan = null; periodCostKey = ""; periodCostRequest++;
    const clearPrices = Boolean(id || priceInputPlanId);
    if (clearPrices) {
        priceInputPlanId = currentPriceWindow = null;
        priceRequest++;
        $("current-price-status").textContent = id ? "Loading selected plan prices…" : "Loading current price preview…";
        $("price-chart").closest(".chart-wrap").hidden = true;
    }
    renderedPlanId = null;
    planReadPending = false;
    simulationReadPending = false;
    $("overview-status").textContent = id ? "Loading selected plan…" : CONNECTED_MESSAGE;
    $("plan-description").textContent = id ? "Loading selected plan…" : "No plan selected.";
    inspectSteps = [];
    clearLinkedInspection();
    clearBatteryPlan();
    for (const chartId of ["plan-chart", "reference-chart", "self-consumption-chart", "energy-chart", "forecast-chart", "price-chart"]) {
        if (chartId === "price-chart" && !clearPrices) continue;
        if (charts.has(chartId)) chart(chartId, [], {deferRange: true});
    }
    $("reference-issue").textContent = $("self-consumption-issue").textContent = "";
    for (const caption of document.querySelectorAll(".scenario-period")) caption.textContent = id ? "Loading original plan comparison…" : "No plan selected.";
    for (const costId of ["reference-costs", "self-consumption-costs", "ems-costs"]) renderScenarioCosts(costId, null, "", null, costId !== "reference-costs");
    $("expected-benefit").textContent = "";
    planEvidence = null;
    $("inspect-time").disabled = true;
    $("inspect-values").textContent = id ? "Loading original interval values…" : "No plan selected.";
    $("evidence-period").textContent = ""; $("plan-evidence-status").textContent = id ? "Loading original whole-plan evidence…" : "No plan selected.";
    evidenceCards($("plan-evidence"), []);
    $("evidence-time").textContent = ""; $("evidence-issue").textContent = id ? "Loading original plan evidence…" : "No plan selected.";
}

/** Settle independently loaded simulations and evidence for the still-selected plan. */
async function finishPlanSupplements(simulationRead, plan, id, request) {
    const evidenceRead = loadPlanEvidence();
    let simulationsLoaded = false;
    try {
        const result = await simulationRead;
        if (result.error) throw result.error;
        const supplementary = result.value;
        if (request !== selectionRequest) return false;
        if (supplementary.plan_id !== id) throw new Error("Cannot read this view: The plan identity does not match. Please retry.");
        renderPlanComparisons(supplementary, plan);
        simulationReadPending = false;
        simulationsLoaded = true;
        return true;
    } catch {
        if (request !== selectionRequest) return false;
        simulationReadPending = true;
        const message = periodCostPlan ? "Simulation update is unavailable. Showing the previously loaded comparisons. Use Refresh overview to retry." : "Simulations are unavailable. Use Refresh overview to retry.";
        $("overview-status").textContent = `Plan loaded. ${message}`;
        for (const caption of document.querySelectorAll(".scenario-period")) caption.textContent = message;
        return true;
    } finally {
        if (request === selectionRequest && simulationsLoaded && evidenceRequest) $("overview-status").textContent = "Plan loaded. Loading original plan evidence…";
        await evidenceRead;
        // A disclosure can start its shared read while the simulations are still loading.
        if (request === selectionRequest) await evidenceRequest;
        if (request === selectionRequest && simulationsLoaded) $("overview-status").textContent = CONNECTED_MESSAGE;
    }
}

/** Replace interval prices with their original native rates and explicit gaps when available. */
function applyIntervalTariffEvidence(plan, {spot, buy, sell}) {
    const tariffEvidence = plan.input_snapshot?.tariff_evidence || [];
    if (tariffEvidence.length) {
        buy.length = 0; sell.length = 0;
        for (const tariff of tariffEvidence) {
            const start = Date.parse(tariff.start), end = Date.parse(tariff.end);
            if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
            for (const [points, value] of [[spot, tariff.market_price], [buy, tariff.buy_price], [sell, tariff.sell_price]]) {
                const previousEnd = points.at(-1)?.[0];
                if (previousEnd != null && start > previousEnd) points.push([previousEnd, null], [start, null]);
                points.push([start, typeof value === "number" ? value : null, tariff.price_kind], [end, typeof value === "number" ? value : null, tariff.price_kind]);
            }
        }
    }
}

/** Use recorded grid totals or both recorded directional flows. */
function intervalGridTotals(flow) {
    const imported = flow.grid_import ?? (flow.grid_to_load != null && flow.grid_to_battery != null ? flow.grid_to_load + flow.grid_to_battery : null);
    const exported = flow.grid_export ?? (flow.pv_to_grid != null && flow.battery_to_grid != null ? flow.pv_to_grid + flow.battery_to_grid : null);
    return {imported, exported};
}

/** Add solar commands and corresponding inspector cards in their declared order. */
function appendIntervalSolarEvidence(plan, step, command, inverterGroups, {commands, deviceValues}) {
    for (const [index, [source]] of (step.pv_sources || []).entries()) {
        const enabled = command?.devices?.[source]?.enabled;
        const name = plan.comparison_names?.[source] || `Solar ${index + 1}`;
        const production = enabled === false ? "Curtailment requested" : enabled === true ? "Production allowed" : "Fixed production";
        commands.push(`${name}: ${production}`);
        const inverter = plan.input_snapshot?.problem?.pv_sources?.find((unit) => unit.source_id === source)?.inverter_id;
        deviceValues.push(intervalCard(name, [["Solar production", production]], inverterGroups.get(inverter)));
    }
}

/** Add battery commands, stored energy, and attributed routes for one inspected interval. */
function appendIntervalBatteryEvidence({plan, flow, command, intervalIndex, batteryLabels, identities, inverterGroups}, {commands, deviceValues, batteryFlowValues}) {
    for (const [index, battery] of (flow.batteries || []).entries()) {
        const name = batteryLabels.get(battery.device_id) || `Battery ${index + 1}`;
        const target = command?.devices?.[battery.device_id];
        const text = target?.mode === "follow_grid_target" ? "Zero on the meter (local regulation)" : target?.power_w != null ? `${Math.round(target.power_w)} W fixed power (+ discharge)` : "No command";
        commands.push(`${name}: ${text}: Stored: ${number(battery.end_kwh)} kWh`);
        const inverter = identities.find((unit) => unit.device_id === battery.device_id)?.inverter_id;
        deviceValues.push(intervalCard(name, [["Control", text], ["Stored energy", battery.end_kwh, "kWh"]], inverterGroups.get(inverter)));
        const attribution = plan.battery_flow_attribution?.[intervalIndex]?.[battery.device_id];
        for (const {key, title, routes} of BATTERY_FLOW_GROUPS) {
            const values = batteryRouteValues(battery, attribution, key);
            const facts = routes.filter(([route]) => values?.[route] >= BATTERY_FLOW_DISPLAY_MIN)
                .map(([route, label]) => [label, values[route], "kWh"]);
            if (facts.length) batteryFlowValues.push(intervalCard(`${name}: ${title}`, facts));
        }
    }
}

/** Update labels for the original plan and retain a matching comparison. */
function renderOriginalPlanHeader(plan, id) {
    const samePlan = renderedPlanId === id;
    if (!samePlan) clearPlanComparisons();
    const selectedOption = [...$("plan-version").options].find(option => option.value === id);
    if (selectedOption) selectedOption.textContent = planName(plan);
    $("inspect-time").disabled = false;
    const currency = plan.currency || "";
    $("reference-description").textContent = plan.reference_policy === "unoptimized-loads-v1"
        ? "Without battery or price-based load shifting. Both references charge vehicles as soon as permitted, start ready cycles and maintain heating targets within physical limits."
        : "Archived comparison: Without battery storage, retaining the original EV and heating schedule. Ready appliance cycles start at their earliest allowed time.";
    $("plan-description").textContent = "";
    if (plan.consumption_objective) {
        const objective = plan.consumption_objective;
        $("plan-description").textContent = `Primary objective: ${number(objective.grid_import_kwh)} kWh grid import + ${number(objective.terminal_replacement_kwh)} kWh hypothetical stock replacement = ${number(objective.corrected_import_kwh)} kWh: Replacement is an end-of-horizon correction, not measured consumption or a charging command.`;
    }
    return {samePlan, currency};
}

/** Prepare each frozen interval's inspector evidence, series, and physical balance. */
function prepareOriginalPlanEvidence(plan, currency) {
    const buy = [], sell = [], spot = [], bonusSell = [], demandForecast = [], solarForecast = [];
    const bonusRules = plan.input_snapshot?.problem?.export_bonuses || [];
    const batteryLabels = new Map();
    const identities = plan.input_snapshot?.problem?.batteries || plan.expected_flows?.[0]?.batteries || [];
    const names = identities.map((battery, index) => plan.comparison_names?.[battery.device_id] || `Battery ${index + 1}`);
    identities.forEach((battery, index) => {
        // Names are presentation, not identity. Only duplicate names need a readable ordinal.
        const label = names.filter((name) => name === names[index]).length > 1 ? `${names[index]}: ${index + 1}` : names[index];
        batteryLabels.set(battery.device_id, label);
    });
    inspectSteps = [];
    clearLinkedInspection();
    const intervals = plan.input_snapshot?.problem?.intervals || [];
    const inverterGroups = new Map((plan.input_snapshot?.problem?.inverters || []).map((inverter, index) =>
        [inverter.inverter_id, {id: inverter.inverter_id, title: plan.comparison_names?.[inverter.inverter_id] || `Inverter ${index + 1}`} ]));
    intervals.forEach((step, i) => {
        // Keep every declared interval in the inspector. Absent evidence must not shift later values.
        const flow = plan.expected_flows?.[i] || {};
        const {imported, exported} = intervalGridTotals(flow);
        const command = plan.steps.find((entry) => new Date(entry.start) <= new Date(step.start) && new Date(entry.end) > new Date(step.start));
        const time = new Date(step.start).getTime();
        // Use the recorded boundary. Rebuilding microsecond durations can exceed Date's millisecond endpoint.
        const boundary = intervals[i + 1]?.start ?? command?.end;
        const end = boundary ? Date.parse(boundary) : Math.floor(time + step.duration_h * 3600000);
        const prices = plan.input_snapshot?.problem?.intervals?.[i] || step;
        const tariff = plan.input_snapshot?.tariff_evidence?.find((t) => Date.parse(t.start) <= time && Date.parse(t.end) > time);
        const add = (list, value, priceKind) => { if (value != null) list.push([time, value, priceKind], [end, value, priceKind]); };
        add(buy, prices.buy_price, tariff?.price_kind); add(sell, prices.sell_price, tariff?.price_kind);
        const rates = intervalContractRates({plan, bonusRules, index: i, start: time, end, prices, tariff, bonusSell});
        const originalDemand = householdForecastEnergy(plan, prices);
        add(demandForecast, prices.duration_h > 0 && originalDemand != null ? originalDemand / prices.duration_h : null);
        add(solarForecast, prices.duration_h > 0 && typeof prices.pv_kwh === "number" ? prices.pv_kwh / prices.duration_h : null);
        const commands = [], deviceValues = [], applianceValues = [], batteryFlowValues = [];
        for (const task of plan.appliance_schedule || []) {
            const energy = task.interval_kwh[i];
            if (energy > 0) {
                const name = plan.comparison_names?.[task.entity_id] || "Appliance";
                commands.push(`${name}: ${number(energy)} kWh in this interval: Cycle starts: ${when(task.start)}, ends ${when(task.end)}`);
                applianceValues.push(intervalCard(name, [["Energy in this interval", energy, "kWh"], ["Cycle starts", when(task.start)], ["Cycle ends", when(task.end)]]));
            }
        }
        appendIntervalBatteryEvidence(
            {plan, flow, command, intervalIndex: i, batteryLabels, identities, inverterGroups},
            {commands, deviceValues, batteryFlowValues}
        );
        appendIntervalSolarEvidence(plan, step, command, inverterGroups, {commands, deviceValues});
        inspectSteps.push({start: time, end, commands,
            gridImport: imported, gridExport: exported, currency,
            ...rates,
            priceStatus: tariff ? `${tariff.price_kind === "published" ? "Published market rate" : tariff.price_kind === "forecast" ? "Forecast market rate" : "Market rate provenance unclassified"}: Native ${tariff.native_resolution_minutes}-minute resolution` : "Market provenance unavailable",
            values: [
                intervalCard("Energy in this interval", [["Grid import", imported, "kWh"], ["Grid export", exported, "kWh"], ["Household demand: AC", step.load_kwh, "kWh"], ["Available solar: DC", step.pv_kwh, "kWh"]]),
                intervalCard("Electricity rates", [["Import", prices.buy_price, `${currency}/kWh`], ["Export", prices.sell_price, `${currency}/kWh`]]),
                ...applianceValues, ...deviceValues
            ],
            applianceValues, batteryFlowValues,
            priceSource: intervalCard("Price source", [["Market rate", tariff ? optionLabel(tariff.price_kind || "unknown") : null], ["Native resolution", tariff?.native_resolution_minutes, "minutes"]]),
            balances: []});
    });
    const balances = energyBalances(plan, new Map([...Object.entries(plan.comparison_names || {}), ...batteryLabels]));
    inspectSteps.forEach((step, index) => {
        step.balances = balances.map((group) => {
            const result = intervalCard(group.inverter ? inverterGroups.get(group.id)?.title || "Inverter" : group.name,
                group.series.map((series) => [series.name, series.type === "bar" ? series.intervals[index]?.value : series.points[index * 2 + 1]?.[1], series.type === "bar" ? "kW" : "%"]));
            if (group.missing) result.observations.push(`${group.missing} interval(s) have unavailable physical evidence. Missing values are not zero.`);
            return result;
        });
    });
    return {
        balance: balances[0], batteryLabels, bonusRules,
        points: {buy, sell, spot, bonusSell, demandForecast, solarForecast}
    };
}

/** Render the selected plan's original charts together on the active range. */
function renderOriginalPlanCharts(plan, balance, batteryLabels, points, {id, currency, samePlan, bonusRules}) {
    const {spot, buy, sell, bonusSell, demandForecast, solarForecast} = points;
    chart("plan-chart", balance.series, {unit: "kW", balance: true, preserveVisibility: samePlan, deferRange: true, gridSteps: plan.steps});
    pendingBatterySeries = batteryPlanSeries(plan, batteryLabels);
    if (pendingBatterySeries.length) {
        batteryPlanPanel.hidden = false;
        drawBatteryPlan(true);
    } else clearBatteryPlan();
    if (balance.missing) {
        const warning = document.createElement("p"); warning.className = "balance-warning";
        warning.textContent = `${balance.missing} interval(s) have missing or inconsistent physical evidence. Their balance bars are unavailable. No losses or flows are invented.`;
        $("plan-chart-legend").append(warning);
    }
    chart("energy-chart", gridExchangeSeries(plan, periodCostPlan?.projection.comparison || periodCostPlan?.scenarios?.self_consumption?.comparison, periodCostPlan?.scenarios?.without_battery?.comparison), {unit: "W", preserveVisibility: samePlan, deferRange: true});
    chart("forecast-chart", [{name: "Household demand forecast: AC", points: demandForecast, color: "#23965e"}, {name: "Available solar forecast: DC", points: solarForecast, color: "#b58900"}], {unit: "kW", minimum: 0, preserveVisibility: samePlan, deferRange: true});
    applyIntervalTariffEvidence(plan, {spot, buy, sell});
    const priceSeries = [{name: "Spot market rate", points: spot, color: "#d9a300"}, {name: "Import rate", points: buy, color: "#d56788"}, {name: "Export rate", points: sell, color: "#399bc1"}];
    if (bonusRules.length) priceSeries.push({name: "Export rate incl. bonus", points: bonusSell, color: "#9255b7"});
    priceInputPlanId = id;
    currentPriceWindow = null;
    $("price-chart").closest(".chart-wrap").hidden = false;
    $("current-price-status").textContent = "";
    chart("price-chart", priceSeries, {unit: `${currency}/kWh`, deferRange: true});
    if (samePlan && periodCostPlan) renderPlanComparisons(periodCostPlan.projection, plan, {deferRange: true});
    $("inspect-time").max = Math.max(0, inspectSteps.length - 1); $("inspect-time").value = 0; inspect(); applyRange();
}

/**
 * Fetch frozen advice and separate valuations, ignoring stale selection responses.
 * Read plan and simulations concurrently, displaying the original as soon as it arrives. Evidence loads only on disclosure.
 * Preserve frozen reference balances and server-owned costs without optimization. Retain complete evidence during background refresh,
 * but clear it when explicitly selecting another plan. Version
 * requests so late responses cannot overwrite newer selections.
 * Retain every declared interval position even without expected flow. Absent values
 * remain unknown rather than zero. Net watts are (import - export) / duration_h *
 * 1000, positive import and negative export. Missing grid evidence leaves null EMS
 * line gaps. Rebuild tables, inspector and charts without changing active commands.
 * Original-plan success is independent of supplemental availability. Complete the
 * relevant supplemental reads before releasing the caller's shared loading state.
 */
async function showPlan(signal, background = false) {
    const request = ++selectionRequest, id = $("plan-version").value;
    const sourceNamesAtRequest = sourceNamesGeneration;
    const retainDisplayedPlan = renderedPlanId && (background || renderedPlanId === id);
    if (retainDisplayedPlan) {
        $("overview-status").textContent = "Loading plan update. Showing the previously loaded plan until the update is available.";
    } else {
        resetPlanSelection(id);
        if (!id) {
            $("overview-status").textContent = CONNECTED_MESSAGE;
            return true;
        }
    }
    const simulationController = new AbortController();
    try {
        const planRead = api(`v1/plans/${encodeURIComponent(id)}/comparison?view=plan`, signal);
        const simulationRead = api(`v1/plans/${encodeURIComponent(id)}/comparison?view=simulations`, AbortSignal.any([simulationController.signal, AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS)]))
            .then(value => ({value}), error => ({error}));
        const data = await planRead;
        if (request !== selectionRequest) { simulationController.abort(); return false; }
        const plan = data.plan;
        if (plan?.plan_id !== id) throw new Error("Cannot read this view: The plan identity does not match. Please retry.");
        const {samePlan, currency} = renderOriginalPlanHeader(plan, id);
        const {balance, batteryLabels, points, bonusRules} = prepareOriginalPlanEvidence(plan, currency);
        renderOriginalPlanCharts(plan, balance, batteryLabels, points, {id, currency, samePlan, bonusRules});
        renderedPlanId = id;
        planSourceNamesGeneration = sourceNamesAtRequest;
        if (requestedPlan === id) requestedPlan = null;
        planReadPending = false;
        $("overview-status").textContent = "Plan loaded. Loading simulations…";
        return await finishPlanSupplements(simulationRead, plan, id, request);
    } catch (error) {
        simulationController.abort();
        if (request === selectionRequest && retainDisplayedPlan) {
            planReadPending = true;
            $("overview-status").textContent = `${error.message} Showing the previously loaded plan. The update is not available yet.`;
            return false;
        }
        if (request === selectionRequest) {
            for (const caption of document.querySelectorAll(".scenario-period")) caption.textContent = "Original plan comparison unavailable.";
            renderedPlanId = null;
            $("current-price-status").textContent = `Selected plan prices could not be loaded. ${error.message}`;
            $("overview-status").textContent = error.message;
            $("plan-description").textContent = "Selected plan could not be loaded.";
            $("plan-evidence-status").textContent = $("evidence-issue").textContent = `Original evidence could not be loaded. ${error.message}`;
            $("inspect-values").textContent = "Original interval values could not be loaded.";
        }
        return false;
    }
}
/** Page original plan identities without activating them. */
async function plans(older = false, signal) {
    planRefreshEnabled = true;
    const selectionGeneration = selectionRequest;
    const linkedPlan = requestedPlan || (selectedLinkedPlan ? $("plan-version").value : null);
    const data = await api(`v1/plans?limit=20${older && cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`, signal);
    if (selectionGeneration !== selectionRequest) return false;
    if (!older) $("plan-version").replaceChildren();
    for (const plan of data.plans) {
        const option = document.createElement("option"); option.value = plan.plan_id; option.textContent = planName(plan); $("plan-version").append(option);
    }
    cursor = data.next_before; $("older-plans").hidden = !cursor;
    if (!older && linkedPlan) {
        const loading = selectOriginal(linkedPlan), request = selectionRequest;
        const loaded = await loading;
        if (request !== selectionRequest) return false;
        if (loaded) requestedPlan = null;
        else $("overview-status").textContent = "The linked plan could not be loaded. Check the connection and choose Refresh to retry. No other plan has been selected.";
        return loaded;
    }
    if (!older) { selectedLinkedPlan = false; return showPlan(); }
}
/** Follow completed publications while preserving a deliberately selected historical plan. */
async function refreshLatestPlan() {
    if (!planRefreshEnabled || planRefreshBusy || overviewLoads || document.hidden || location.hash === "#savings") return;
    planRefreshBusy = true;
    const selector = $("plan-version"), previousLatest = selector.options[0]?.value || "";
    let priceRead;
    try {
        const data = await api("v1/plans?limit=1", AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS)), latest = data.plans[0];
        if (!latest || overviewLoads || previousLatest !== (selector.options[0]?.value || "")) return;
        const selected = selector.value, followingLatest = !selectedLinkedPlan && selected === previousLatest;
        if (latest.plan_id !== previousLatest) {
            priceRead ??= currentPrices(AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS));
            [...selector.options].find(option => option.value === latest.plan_id)?.remove();
            const option = document.createElement("option");
            option.value = latest.plan_id;
            option.textContent = planName(latest);
            selector.prepend(option);
            selector.value = followingLatest ? latest.plan_id : selected;
        }
        const needsPlan = renderedPlanId !== latest.plan_id || planReadPending || simulationReadPending;
        if (followingLatest && needsPlan) {
            const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
            await Promise.all([showPlan(signal, true), priceRead ??= currentPrices(signal)]);
        }
    } catch { /* Retain the last usable plan and retry after a transient archive read failure. */ }
    finally {
        if (!priceRead && priceRefreshPending && !overviewLoads) priceRead = currentPrices(AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS));
        await Promise.allSettled([priceRead]);
        const priceChart = charts.get("price-chart");
        if (!overviewLoads && priceChart) await loadDailyPrices(priceChart);
        planRefreshBusy = false;
    }
}
/** Read the selected cash comparison without changing Planning state. */
async function savings(signal) { ++periodRequest; return savingsPage.read(signal); }
/** Open original advice only. Do not issue a control or planning request. */
function selectOriginal(id, signal) {
    if (![...$("plan-version").options].some((o) => o.value === id)) {
        const option = document.createElement("option"); option.value = id; option.textContent = "Selected archived plan"; $("plan-version").append(option);
    }
    selectedLinkedPlan = true;
    $("plan-version").value = id; location.hash = "planning"; view();
    const loaded = showPlan(signal); $("plan-heading").scrollIntoView({behavior: "smooth"});
    return loaded;
}
/**
 * Read current saved tariffs only without a selected plan. Late preview reads never replace plan prices.
 * @param {AbortSignal} [signal] Aborting the newest request follows the read failure path.
 * @returns {Promise<boolean|undefined>} True after drawing or when a plan supplies prices, false after hiding the chart and
 * showing a read error, or undefined when superseded. Superseded reads leave chart and status
 * unchanged, including when aborted.
 */
async function currentPrices(signal) {
    if ($("plan-version").value) { priceRefreshPending = false; return true; }
    const request = ++priceRequest;
    try {
        const data = await api("v1/prices/inputs", signal);
        if (request !== priceRequest || $("plan-version").value) return;
        const intervals = data.intervals || [], buy = [], sell = [], market = [], total = [];
        for (const interval of intervals) {
            const start = Date.parse(interval.start), end = Date.parse(interval.end);
            for (const [points, value] of [[buy, interval.buy_price], [sell, interval.sell_price], [market, interval.market_price], [total, interval.total_export_price]]) points.push([start, value, interval.price_kind], [end, value, interval.price_kind]);
        }
        currentPriceWindow = intervals.length ? {start: Math.floor(Date.parse(data.as_of) / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS, end: Date.parse(intervals.at(-1).end)} : null;
        const series = [{name: "Spot market rate", points: market, color: "#d9a300"}, {name: "Import rate", points: buy, color: "#d56788"}, {name: "Export rate", points: sell, color: "#399bc1"}];
        if (data.has_bonus) series.push({name: "Export rate incl. bonus", points: total, color: "#9255b7"});
        $("price-chart").closest(".chart-wrap").hidden = !intervals.length;
        chart("price-chart", series, {unit: `${data.currency || "EUR"}/kWh`});
        $("current-price-status").textContent = data.issue ? `Current price preview unavailable: ${optionLabel(data.issue)}` : !intervals.length ? "Current price preview unavailable: No price intervals available." : `No plan available: Current tariff preview: Saved contract rates as of: ${when(data.as_of)}: ${intervals.some(interval => interval.bonus_issue) ? "Bonus eligibility unavailable: Missing values remain gaps." : data.has_bonus ? "Bonus volume is not applied in this preview." : "No bonus applies in this period."}${data.coverage === "shortened_horizon" ? " Market-price coverage ends early." : ""}`;
        priceRefreshPending = false;
        return true;
    } catch (error) {
        if (request !== priceRequest || $("plan-version").value) return;
        priceRefreshPending = true;
        currentPriceWindow = null;
        $("price-chart").closest(".chart-wrap").hidden = true;
        chart("price-chart", [], {unit: "EUR/kWh"});
        $("current-price-status").textContent = error.message;
        return false;
    }
}
/** Refresh the active view, preserving errors owned by a newer selection or period. */
async function load() {
    if (!overviewSessionReady && !await initializeOverview()) return;
    overviewLoads++;
    try {
        const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
        if (location.hash === "#savings") {
            const selection = selectionRequest, read = savings(signal), period = periodRequest;
            if (await read && period === periodRequest && selection === selectionRequest && location.hash === "#savings") $("overview-status").textContent = CONNECTED_MESSAGE;
            return;
        }
        const planRead = plans(false, signal);
        const previewRead = planRead.catch(() => {}).then(
            () => currentPrices(AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS))
        );
        const results = await Promise.allSettled([planRead, previewRead]);
        const failure = results.find(result => result.status === "rejected");
        if (failure) throw failure.reason;
    }
    catch (error) { $("overview-status").textContent = error.message; }
    finally { overviewLoads--; }
}
let planRefreshTimer, quarterRefreshTimer;
/** Advance live axes at UTC quarter boundaries, independently of solver completion. */
function advancePlanningWindow() {
    if (!document.hidden) applyRange();
    quarterRefreshTimer = setTimeout(advancePlanningWindow, QUARTER_MILLISECONDS - Date.now() % QUARTER_MILLISECONDS);
}
/** Keep one publication poll and one aligned clock after a back-forward cache restore. */
function startPlanRefresh() {
    clearInterval(planRefreshTimer); clearTimeout(quarterRefreshTimer);
    planRefreshTimer = setInterval(refreshLatestPlan, PLAN_REFRESH_MILLISECONDS);
    quarterRefreshTimer = setTimeout(advancePlanningWindow, QUARTER_MILLISECONDS - Date.now() % QUARTER_MILLISECONDS);
}
startPlanRefresh();
document.addEventListener("visibilitychange", () => { if (!document.hidden) { applyRange(); refreshLatestPlan(); } });
window.addEventListener("pagehide", () => { clearInterval(planRefreshTimer); clearTimeout(quarterRefreshTimer); });
window.addEventListener("pageshow", event => {
    if (!event.persisted) return;
    startPlanRefresh(); applyRange(); refreshLatestPlan();
});
$("overview-connect").addEventListener("submit", (event) => { event.preventDefault(); token = $("overview-token").value; $("overview-token").value = ""; load(); });
$("plan-version").addEventListener("change", () => { requestedPlan = null; selectedLinkedPlan = false; showPlan(); });
$("older-plans").addEventListener("click", () => plans(true).catch((error) => { $("overview-status").textContent = error.message; }));
$("overview-refresh").addEventListener("click", load);
/** Initialize once after a confirmed read/render. Explicit Connect/Refresh can retry a failed attempt. */
async function initializeOverview() {
    const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
    try {
        const session = await api("setup/session", signal);
        if (overviewSessionReady) return session;
        useHomeAssistantTimezone(session.timezone);
        savingsPage.initialize();
        watchSourceNames(() => api("setup/session", AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS)), () => {
            sourceNamesGeneration++;
            return location.hash === "#savings" ? savings() : showPlan();
        }, session.source_names_revision);
        overviewIngress = Boolean(session.ingress);
        $("overview-connect").hidden = overviewIngress;
        if (!session.ingress) $("overview-status").textContent = "Connect to view your plans and savings.";
        overviewSessionReady = true;
        return session;
    } catch (error) {
        $("overview-status").textContent = signal.aborted || error?.message === "This view took too long to load. Please retry." ? "This view took too long to load. Please retry." : "Cannot initialize this view: Check the connection and retry.";
        return null;
    }
}
initializeOverview().then(session => { if (session?.ingress) load(); });
