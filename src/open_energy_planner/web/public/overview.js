// Read-only views never activate an archived plan or reset the reference scenario.
import "./chart_theme.js";
import {calendarDays, calendarTime, calendarDayStart} from "./chart_days.js";
import {money, when, dateLabel, localDate, localInstant, nextDate, percentage, optionLabel, planName, planStrategy, useHomeAssistantTimezone, watchSourceNames} from "./display.js";
import {energyBalances, householdForecastEnergy, quarterBalanceSeries, balancePeriodEnergy, QUARTER_MILLISECONDS} from "./balance.js";
import {bindSettingsActions} from "./settings-actions.js";
bindSettingsActions();
const $ = (id) => document.getElementById(id);
const CONNECTED_MESSAGE = "Connected: Historical views never change active commands.";
const PLAN_REFRESH_MILLISECONDS = 15000;
const PLAN_READ_TIMEOUT_MILLISECONDS = 5000;
const base = document.querySelector('meta[name="oep-api-base"]').content;
let requestedPlan = new URL(location.href).searchParams.get("plan");
let selectedLinkedPlan = false;
let token = "", cursor = null, evaluationCursor = null, selectionRequest = 0, periodRequest = 0;
let planRefreshEnabled = false, planRefreshBusy = false, overviewLoads = 0, renderedPlanId = null;
let planReadPending = false;
let priceRefreshPending = false;
let periodCostPlan = null, periodCostKey = "", periodCostRequest = 0;
const evaluationVersions = new Map();
const DAILY_RESULTS_PAGE_SIZE = 25;
let dailySavingsData = null, dailySavingsPage = 0;
let overviewSessionReady = false;
const isMoneyUnit = (unit) => /^[A-Z]{3}(\/kWh)?$/.test(unit || "");
const number = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : "-";
/** Share numeric precision across evidence cards, tooltip columns and chart axes. Units remain separate. */
const valueText = (value, unit) => unit === "%" ? percentage(value, false) : unit === "W" ? Math.round(value).toLocaleString() : (isMoneyUnit(unit) ? money : number)(value);
const statuses = {optimal: "Optimal", feasible: "Feasible"};
/** Read JSON with owned guidance and a default deadline. An explicit signal owns its request group. */
async function api(path, signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS)) {
    let response;
    try {
        response = await fetch(base + path, {headers: token ? {Authorization: `Bearer ${token}`} : {}, redirect: "error", cache: "no-store", signal});
    } catch {
        throw new Error(signal?.aborted ? "This view took too long to load. Please retry." : "Cannot read this view: Check the connection and retry.");
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
const hiddenPriceSeries = new Set();
const palette = ["#078b80", "#e49b38", "#5d70bc", "#d56788", "#399bc1", "#8a65ad"];
const INTERVAL_BAR_FILL = 0.9;
const TOOLTIP_VIEWPORT_MARGIN_PX = 12;
const MILLISECONDS_PER_MINUTE = 60000;
const MILLISECONDS_PER_HOUR = 60 * MILLISECONDS_PER_MINUTE;
const MAX_TIME_AXIS_LABELS = 6;
const DAILY_DATE_LABEL_MIN_WIDTH_PX = 120;
const FORECAST_PRICE_DASH = [2, 4];
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
    for (const card of cards) {
        const article = document.createElement("article"), heading = document.createElement("h4"), list = document.createElement("dl");
        article.className = "evidence-card"; heading.textContent = card.title; article.append(heading);
        for (const fact of card.facts) {
            const term = document.createElement("dt"), value = document.createElement("dd");
            term.textContent = fact.label;
            value.textContent = fact.value == null ? "Not recorded" : typeof fact.value === "number" ? `${valueText(fact.value, fact.unit)} ${fact.unit}`.trim() : fact.value;
            list.append(term, value);
        }
        article.append(list);
        for (const observation of card.observations) {
            const text = document.createElement("p"); text.className = "evidence-observation"; text.textContent = observation; article.append(text);
        }
        parent.append(article);
    }
}
/** Explain actual bar coverage and retain recorded commands only for the active optimizer view. */
function balanceNotes(bar, id, axisTime) {
    const notes = bar.coveredStart !== bar.start || bar.coveredEnd !== bar.end
        ? [`Partial quarter: Mean power covers ${axisTime(bar.coveredStart)} to ${axisTime(bar.coveredEnd)} only.`] : [];
    if (!["reference-chart", "self-consumption-chart"].includes(id)) {
        notes.push(...new Set(inspectSteps.filter(step => step.end > bar.coveredStart && step.start < bar.coveredEnd).flatMap(step => step.commands || [])));
    }
    return notes;
}
/** Keep labels, right-aligned numbers and units in distinct columns without interpreting provider text as HTML. */
function renderTooltip({chart: instance, tooltip}, node, balance = false, balanceTime = null) {
    node.classList.remove("chart-tooltip-day"); node.style.maxHeight = "";
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
    if (interval) {
        // Flow bars use midpoints. Attach only recorded start states, never interpolate a missing SoC.
        instance.data.datasets.forEach((dataset, index) => {
            if (dataset.tooltipGroup !== "Battery state" || !instance.isDatasetVisible(index)) return;
            const point = dataset.data.find(point => point.x === (interval.raw.coveredStart ?? interval.raw.start) && point.y != null);
            if (point && !items.some(item => item.dataset === dataset)) items.push({dataset: {...dataset, label: `${dataset.label} at start`}, parsed: point});
        });
    }
    const groups = balance ? ["Production", "Consumption", "Battery state"].map(name => [name, items.filter(item => item.dataset.tooltipGroup === name)]).filter(([, items]) => items.length) : [[null, items]];
    const sections = document.createElement("div"); sections.className = balance ? "balance-tooltip-groups" : "";
    node.classList.toggle("chart-tooltip-balance", balance);
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
        section.append(table); sections.append(section);
    }
    node.append(sections);
    const notes = interval ? balanceNotes(interval.raw, instance.canvas.id, when) : tooltip.afterBody;
    if (notes.length) {
        const explanation = document.createElement("p"); explanation.className = "chart-tooltip-explanation";
        explanation.textContent = notes.join("\n"); node.append(explanation);
    }
    const bounds = instance.canvas.getBoundingClientRect(), margin = TOOLTIP_VIEWPORT_MARGIN_PX;
    node.style.left = `${Math.max(margin, Math.min(bounds.left + tooltip.caretX + margin, window.innerWidth - node.offsetWidth - margin))}px`;
    node.style.top = `${Math.max(margin, Math.min(bounds.top + tooltip.caretY + margin, window.innerHeight - node.offsetHeight - margin))}px`;
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
    button.setAttribute("aria-label", `Energy totals for ${full}`);
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
        const items = series.flatMap((entry, index) => entry.type !== "bar" || !instance.isDatasetVisible(index) ? [] : [{
            dataset: {...instance.data.datasets[index], unit: "kWh"}, raw: {},
            parsed: {y: balancePeriodEnergy(entry.intervals, from, to)}
        }]);
        const partial = from > calendarDayStart(date) || to < calendarDayStart(nextDate(date));
        const notes = [partial ? "Partial day. Totals cover only the shown planned period." : "Totals cover this complete calendar day.",
            `From ${when(from)} to ${when(to)}.`, "Supply is positive. Use is negative."];
        if (items.some(item => item.parsed.y == null)) notes.push("A dash means that energy is unavailable because interval evidence is missing or inconsistent.");
        if (!items.length) notes.push("All energy series are hidden. Show a series in the legend to see its total.");
        if (summary.owner !== button) {
            summary.owner?.removeAttribute("aria-describedby");
            summary.owner = button; summary.pinned = false;
        }
        const bounds = button.getBoundingClientRect(), canvas = instance.canvas.getBoundingClientRect();
        renderTooltip({chart: instance, tooltip: {opacity: 1, dataPoints: items, title: [`Energy totals: ${full}`],
            afterBody: notes, caretX: bounds.left + bounds.width / 2 - canvas.left, caretY: bounds.bottom - canvas.top}}, tooltip, true);
        tooltip.classList.add("chart-tooltip-day");
        button.setAttribute("aria-describedby", tooltip.id);
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
/**
 * Render series into canvas id, destroying its previous chart, removing its tooltip
 * and rebuilding its accessible legend. Current-price series retain their visibility.
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
function chart(id, series, {utcDays = false, calendarDates = false, unit = "", minimum = null, balance = false} = {}) {
    let balanceTime = null;
    if (balance) series = quarterBalanceSeries(series);
    charts.get(id)?.destroy();
    $(`${id}-tooltip`)?.remove();
    const tooltip = document.createElement("div"); tooltip.id = `${id}-tooltip`; tooltip.className = "chart-tooltip";
    tooltip.hidden = true; tooltip.setAttribute("role", "tooltip"); document.body.append(tooltip);
    const daySummary = {owner: null, pinned: false};
    /** Shared dismissal also revokes date ownership when scrolling or interval inspection replaces it. */
    const hideTooltip = () => {
        tooltip.hidden = true;
        daySummary.owner?.removeAttribute("aria-describedby");
        daySummary.owner = null; daySummary.pinned = false;
    };
    const tooltipEvents = new AbortController();
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") hideTooltip();
    }, {signal: tooltipEvents.signal});
    window.addEventListener("scroll", event => {
        if (event.target instanceof Node && tooltip.contains(event.target)) return;
        const owner = daySummary.owner, bounds = owner?.getBoundingClientRect();
        const movesOwner = event.target === document || event.target === window
            || event.target instanceof Element && event.target.contains(owner);
        if (owner && owner === document.activeElement && movesOwner && !tooltip.hidden
            && bounds.bottom > 0 && bounds.top < innerHeight && bounds.right > 0 && bounds.left < innerWidth) {
            positionDayTooltip(tooltip, owner);
        } else hideTooltip();
    }, {capture: true, signal: tooltipEvents.signal});
    const canvas = $(id), legend = $(`${id}-legend`);
    legend.replaceChildren();
    const axisTime = calendarDates ? value => dateLabel(localDate(value)) : utcDays ? (value) => dateLabel(new Date(value).toISOString().slice(0, 10)) : value => when(value);
    const datasets = series.map((s, index) => {
        const values = [];
        for (const segment of s.segments || [s.points || []]) {
            if (values.length) values.push({x: segment[0][0], y: null});
            values.push(...segment.map(([x, y, priceKind]) => ({x, y, priceKind})));
        }
        if (s.intervals) values.push(...s.intervals.map(({start, end, value, coveredStart, coveredEnd}) => ({x: (start + end) / 2, y: value, start, end, coveredStart, coveredEnd})));
        const color = s.color || palette[index % palette.length];
        return {label: s.name, data: values, hidden: id === "price-chart" && hiddenPriceSeries.has(s.name), type: s.type || "line", stack: s.stack, yAxisID: s.axis || "y", unit: s.unit || unit, tooltipGroup: s.tooltipGroup, deviceState: s.deviceState, swatchColor: color, borderColor: color, backgroundColor: deviceFill(color, s.deviceState), borderWidth: s.type === "bar" ? 0 : 2, borderDash: s.reference ? [6, 4] : [], segment: {borderDash: ({p0}) => p0.raw.priceKind === "forecast" ? FORECAST_PRICE_DASH : undefined}, pointRadius: 0, pointHitRadius: 10, spanGaps: false, order: s.axis === "soc" ? 0 : 1};
    });
    const result = new Chart(canvas, {
        type: "line", data: {datasets}, plugins: [intervalWidths, ...(calendarDates ? [] : [calendarDays]), {id: "ownedTooltip", afterDestroy() { hideTooltip(); tooltipEvents.abort(); tooltip.remove(); }}],
        options: {responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
            interaction: {mode: "nearest", axis: "x", intersect: false},
            onHover: (event, _elements, instance) => {
                if (balance) {
                    const hits = instance.getElementsAtEventForMode(event, "nearest", {axis: "xy", intersect: true}, false);
                    balanceTime = hits.some(hit => instance.data.datasets[hit.datasetIndex].tooltipGroup === "Battery state")
                        ? null : instance.scales.x.getValueForPixel(event.x);
                }
            },
            plugins: {calendarDays: {utc: utcDays, onHeading: balance ? (instance, heading, day) => { hideTooltip(); planningDayHeading(instance, series, tooltip, daySummary, heading, day); } : null}, valueAxisHeadroom: {group: ["plan-chart", "reference-chart", "self-consumption-chart"].includes(id) ? "planning-comparison" : null}, legend: {display: false}, tooltip: {enabled: false, external: (context) => {
                hideTooltip();
                const time = balanceTime; balanceTime = null;
                renderTooltip(context, tooltip, balance, time);
            }, callbacks: {
                title: (items) => !items.length ? "" : items[0].raw.start != null ? `From ${axisTime(items[0].raw.start)} to ${axisTime(items[0].raw.end)}` : axisTime(items[0].parsed.x),
                label: (item) => `${item.dataset.label}: ${valueText(item.parsed.y, item.dataset.unit)} ${item.dataset.unit}`,
                afterBody: (items) => {
                    if (balance && items.length) {
                        const bar = items.find(item => item.raw.start != null)?.raw;
                        if (!bar) return [];
                        return balanceNotes(bar, id, axisTime);
                    }
                    if (!items.length || ["savings-chart", "realization-chart", "forecast-chart", "reference-chart", "self-consumption-chart"].includes(id)) return "";
                    const step = inspectSteps.find((step) => step.start <= items[0].parsed.x && items[0].parsed.x < step.end);
                    return id === "price-chart" ? priceInputPlanId ? step?.priceStatus || "" : [] : step?.commands || [];
                }
            }}},
            scales: {x: {type: "linear", offset: false, grid: {display: false},
                afterBuildTicks: utcDays ? undefined : (scale) => {
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
                },
                ticks: {maxTicksLimit: MAX_TIME_AXIS_LABELS, maxRotation: 0, callback: (value) => utcDays || calendarDates ? axisTime(value) : calendarTime(value)},
                title: {display: utcDays || calendarDates, text: "Calendar date"}},
                y: {min: minimum ?? undefined, suggestedMin: balance ? 0 : undefined, stacked: balance, beginAtZero: id === "price-chart", title: {display: Boolean(unit), text: unit}, grid: {color: (context) => context.tick.value === 0 ? "#455d70" : "#edf1f5", lineWidth: (context) => context.tick.value === 0 ? 2 : 1}, ticks: {maxTicksLimit: 6, callback: (value) => unit === "W" ? number(value) : valueText(value, unit)}},
                ...(series.some((entry) => entry.axis === "soc") ? {soc: {position: "right", min: 0, max: 100, stacked: false, title: {display: true, text: "SoC (%)"}, ticks: {callback: (value) => percentage(value, false)}, grid: {drawOnChartArea: false}}} : {})},
            onClick: (_event, elements) => {
                if (["savings-chart", "realization-chart", "forecast-chart", "reference-chart", "self-consumption-chart"].includes(id) || id === "price-chart" && !priceInputPlanId || !elements.length) return;
                const time = datasets[elements[0].datasetIndex].data[elements[0].index].x;
                const index = inspectSteps.findIndex((step) => step.start <= time && time < step.end);
                if (index >= 0) { $("inspect-time").value = index; inspect(); $("plan-evidence-panel").open = true; $("interval-evidence-panel").open = true; $("evidence-time").scrollIntoView({block: "nearest"}); }
            }
        }
    });
    charts.set(id, result);
    const legendGroups = new Map();
    series.forEach((s, index) => {
        const identity = s.legendId ?? index;
        if (!legendGroups.has(identity)) legendGroups.set(identity, {series: s, indexes: []});
        legendGroups.get(identity).indexes.push(index);
    });
    for (const {series: s, indexes} of legendGroups.values()) {
        const label = document.createElement("label"), input = document.createElement("input");
        input.type = "checkbox"; input.checked = indexes.every(index => result.isDatasetVisible(index));
        if (s.legendId != null) input.setAttribute("aria-describedby", `${id}-device-key`);
        input.addEventListener("change", () => {
            if (id === "price-chart") {
                if (input.checked) hiddenPriceSeries.delete(s.name); else hiddenPriceSeries.add(s.name);
            }
            for (const index of indexes) result.setDatasetVisibility(index, input.checked);
            applyRange();
        });
        const swatch = document.createElement("span"); swatch.className = "series-swatch"; swatch.style.backgroundColor = datasets[indexes[0]].swatchColor; swatch.setAttribute("aria-hidden", "true");
        label.append(input, swatch, document.createTextNode(s.legendName || s.name)); legend.append(label);
    }
    if (series.some(s => s.legendId != null)) {
        const key = document.createElement("p"); key.id = `${id}-device-key`;
        key.className = "chart-device-key";
        key.textContent = "Striped bars show expected use. Solid bars show scheduled use. Expected use does not authorize a start.";
        legend.append(key);
    }
    if (!datasets.some((d) => d.data.length)) {
        const empty = document.createElement("p"); empty.textContent = "No covered data available."; legend.append(empty);
    }
    if (id === "savings-chart") {
        const points = series.flatMap((item) => item.points);
        $("savings-chart-range").textContent = points.length ? `From ${axisTime(Math.min(...points.map((p) => p[0])))} to ${axisTime(Math.max(...points.map((p) => p[0])))}` : "";
    }
    applyRange();
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
        if (["savings-chart", "realization-chart"].includes(id)) continue;
        if (id === "price-chart" && !priceInputPlanId) {
            const priceStart = currentPriceWindow ? Math.max(currentPriceWindow.start, currentQuarter) : undefined;
            const priceEnd = currentPriceWindow ? Math.max(currentPriceWindow.end, priceStart + QUARTER_MILLISECONDS) : undefined;
            instance.options.scales.x.min = priceStart;
            instance.options.scales.x.max = planningHours && priceStart != null ? priceStart + planningHours * MILLISECONDS_PER_HOUR : priceEnd;
            instance.update(); continue;
        }
        const bars = instance.data.datasets.find((dataset) => dataset.type === "bar")?.data;
        const fullEnd = end == null ? bars?.at(-1)?.end : Math.max(Math.ceil(end / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS, start + QUARTER_MILLISECONDS);
        instance.options.scales.x.min = start ?? bars?.[0]?.start;
        instance.options.scales.x.max = planningHours && start != null ? start + planningHours * MILLISECONDS_PER_HOUR : fullEnd;
    }
    // Set all windows before synchronizing comparison axes from their joint visible data.
    for (const instance of charts.values()) instance.update();
    if (first != null && end != null && start != null) {
        document.querySelector('[data-hours="0"]').textContent = `Full period: ${number(Math.max(0, end - Math.max(first, start)) / MILLISECONDS_PER_HOUR)} hours`;
        showPeriodCosts(start, planningHours ? start + planningHours * MILLISECONDS_PER_HOUR : Math.max(end, start + QUARTER_MILLISECONDS));
    }
}
/** Render server-owned period amounts, clearing old totals while another period is pending. */
function renderPeriodCosts(period, scenarios, currency, message) {
    for (const [name, host] of [["optimizer", "ems-costs"], ["self_consumption", "self-consumption-costs"], ["without_battery", "reference-costs"]]) {
        const entry = scenarios?.[name];
        renderScenarioCosts(host, entry?.costs, currency, entry?.benefit_vs_without_battery, name !== "without_battery");
    }
    for (const caption of document.querySelectorAll(".scenario-period")) caption.textContent = message || (period ? `From ${when(period.start)} to ${when(period.end)}: ${number(period.duration_h)} hours: Expected, not measured. Battery value uses the original plan's valuation basis.` : "No planned intervals in this period. Select an earlier plan or wait for a new plan.");
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
        const baseline = plan.scenarios?.without_battery?.comparison, self = plan.scenarios?.self_consumption?.comparison;
        renderPeriodCosts(plan.period, {
            optimizer: {costs: baseline?.ems_costs || self?.ems_costs, benefit_vs_without_battery: baseline?.expected_savings},
            self_consumption: {costs: self?.reference_costs, benefit_vs_without_battery: plan.scenarios?.self_consumption?.expected_savings_vs_without_battery},
            without_battery: {costs: baseline?.reference_costs}
        }, plan.currency);
        return;
    }
    renderPeriodCosts(null, null, plan.currency, "Loading expected costs for the selected period…");
    try {
        const query = new URLSearchParams({start: new Date(start).toISOString(), end: new Date(end).toISOString()});
        const data = await api(`v1/plans/${encodeURIComponent(plan.id)}/comparison?${query}`, AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS));
        if (request !== periodCostRequest || periodCostPlan !== plan) return;
        renderPeriodCosts(data.period, data.scenarios, plan.currency);
        for (const [name, host] of [["optimizer", "ems-costs"], ["self_consumption", "self-consumption-costs"], ["without_battery", "reference-costs"]]) {
            const issue = data.scenarios?.[name]?.issue;
            if (issue) document.getElementById(host).closest("figcaption").querySelector(".scenario-period").textContent += ` ${issue}`;
        }
    } catch (error) {
        if (request !== periodCostRequest || periodCostPlan !== plan) return;
        periodCostKey = "";
        renderPeriodCosts(null, null, plan.currency, `Expected costs for the selected period are unavailable. ${error.message} Use Refresh overview to retry.`);
    }
}
/** Explain exact interval advice with keyboard/touch access independent of canvas tooltips. */
function inspect() {
    if (!planEvidence) return;
    const step = inspectSteps[Number($("inspect-time").value)];
    $("inspect-values").textContent = step ? step.text : "No planned intervals available.";
    const evidence = planEvidence?.intervals?.find((interval) => Date.parse(interval.start) === step?.start);
    $("evidence-time").textContent = evidence ? `From ${when(evidence.start)} to ${when(evidence.end)}: ${number((Date.parse(evidence.end) - Date.parse(evidence.start)) / MILLISECONDS_PER_MINUTE)} minutes: Energy quantities apply to this interval. Prices are rates per kWh.` : "";
    $("evidence-issue").textContent = evidence?.issue || planEvidence?.issue || (!evidence ? "Original interval evidence is unavailable." : "");
    evidenceCards($("interval-evidence"), evidence?.cards || []);
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
    for (const instance of charts.values()) instance.resize();
    for (const tooltip of document.querySelectorAll(".chart-tooltip")) tooltip.hidden = true;
}
window.addEventListener("hashchange", view);
$("inspect-time").addEventListener("input", inspect);
for (const button of document.querySelectorAll("[data-hours]")) button.addEventListener("click", () => {
    planningHours = Number(button.dataset.hours);
    for (const other of document.querySelectorAll("[data-hours]")) other.setAttribute("aria-pressed", String(other === button));
    applyRange();
});
view();
/** Keep the original cost and optional benefit readable, with non-cash components in native disclosure. */
function renderScenarioCosts(id, costs, currency, benefit, showBenefit) {
    const host = $(id), expanded = host.querySelector("details")?.open || false;
    const totals = document.createElement("dl");
    const metric = (parent, label, value) => {
        const field = document.createElement("div"), name = document.createElement("dt"), amount = document.createElement("dd");
        name.textContent = label; amount.textContent = `${money(value)} ${currency}`;
        field.append(name, amount); parent.append(field);
    };
    metric(totals, costs?.economic_cost == null ? "Expected costs unavailable" : "Expected economic cost", costs?.economic_cost);
    if (showBenefit) {
        const known = typeof benefit === "number" && Number.isFinite(benefit);
        const zero = known && money(Math.abs(benefit)) === money(0);
        metric(totals, "Expected benefit versus without battery", zero ? 0 : benefit);
        totals.lastElementChild.dataset.benefitState = !known ? "unavailable" : zero ? "zero" : benefit > 0 ? "positive" : "negative";
    }
    const details = document.createElement("details"), summary = document.createElement("summary"), breakdown = document.createElement("dl");
    summary.textContent = "Cost breakdown"; details.open = expanded;
    metric(breakdown, "Import cost", costs?.purchase);
    metric(breakdown, "Export income", costs?.sale);
    metric(breakdown, "Bonus income", costs?.bonus);
    metric(breakdown, "Net electricity cost", costs?.electricity_cost);
    metric(breakdown, "Wear", costs?.wear);
    metric(breakdown, "Battery value change (not cash)", costs?.inventory_change);
    const cashExplanation = document.createElement("p"), explanation = document.createElement("p");
    cashExplanation.textContent = "Net electricity cost = import cost - export income - bonus income. Amounts are rounded for display. Totals use full precision.";
    explanation.textContent = "Economic cost = net electricity cost + wear - battery value change. A positive value change reduces economic cost. Battery value change is not cash.";
    details.append(summary, breakdown, cashExplanation, explanation);
    host.replaceChildren(totals, details);
    if (!showBenefit) {
        const note = document.createElement("p"); note.textContent = "Baseline without storage"; host.append(note);
    }
}

/**
 * Fetch frozen advice and separate valuations, ignoring stale selection responses.
 * Render both frozen reference balances, original whole-plan costs and factual
 * evidence without optimization. Retain complete evidence during background refresh,
 * but clear it when explicitly selecting another plan. Version
 * requests so late responses cannot overwrite newer selections.
 * Retain every declared interval position even without expected flow. Absent values
 * remain unknown rather than zero. Net watts are (import - export) / duration_h *
 * 1000, positive import and negative export. Missing grid evidence leaves null EMS
 * line gaps. Rebuild tables, inspector and charts without changing active commands.
 */
async function showPlan(signal, background = false) {
    const request = ++selectionRequest, id = $("plan-version").value;
    const retainDisplayedPlan = renderedPlanId && (background || renderedPlanId === id);
    if (retainDisplayedPlan) {
        $("overview-status").textContent = "Loading plan update. Showing the previously loaded plan until the update is available.";
    } else {
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
        $("overview-status").textContent = id ? "Loading selected plan…" : CONNECTED_MESSAGE;
        $("plan-description").textContent = id ? "Loading selected plan…" : "No plan selected.";
        $("planning-window").textContent = "";
        for (const name of ["plan-rows", "plan-battery-rows", "plan-solar-rows", "expected-cost-rows"]) $(name).replaceChildren();
        inspectSteps = [];
        for (const chartId of ["plan-chart", "reference-chart", "self-consumption-chart", "energy-chart", "forecast-chart", "price-chart"]) {
            if (chartId === "price-chart" && !clearPrices) continue;
            if (charts.has(chartId)) chart(chartId, []);
        }
        $("reference-issue").textContent = $("self-consumption-issue").textContent = "";
        for (const caption of document.querySelectorAll(".scenario-period")) caption.textContent = id ? "Loading original plan comparison…" : "No plan selected.";
        for (const costId of ["reference-costs", "self-consumption-costs", "ems-costs"]) renderScenarioCosts(costId, null, "", null, costId !== "reference-costs");
        $("expected-benefit").textContent = "";
        planEvidence = null;
        $("inspect-time").disabled = true;
        $("inspect-values").textContent = id ? "Loading original interval values…" : "No plan selected.";
        $("evidence-period").textContent = ""; $("plan-evidence-status").textContent = id ? "Loading original whole-plan evidence…" : "No plan selected.";
        evidenceCards($("plan-evidence"), []); evidenceCards($("interval-evidence"), []);
        $("evidence-time").textContent = ""; $("evidence-issue").textContent = id ? "Loading original plan evidence…" : "No plan selected.";
        if (!id) {
            $("overview-status").textContent = CONNECTED_MESSAGE;
            return true;
        }
    }
    try {
        const data = await api(`v1/plans/${encodeURIComponent(id)}/comparison`, signal);
        if (request !== selectionRequest) return false;
        periodCostPlan = null; periodCostRequest++;
        const plan = data.plan;
        const selectedOption = [...$("plan-version").options].find(option => option.value === id);
        if (selectedOption) selectedOption.textContent = planName(plan);
        const comparison = data.comparison, valuation = data.valuation;
        const withoutBattery = data.scenarios?.without_battery;
        const selfConsumption = data.scenarios?.self_consumption;
        for (const [chartId, entry, issueId] of [["reference-chart", withoutBattery, "reference-issue"], ["self-consumption-chart", selfConsumption, "self-consumption-issue"]]) {
            const balance = entry?.chart ? energyBalances(entry.chart, new Map(Object.entries(plan.comparison_names || {})))[0] : null;
            $(chartId).closest(".chart-wrap").hidden = !balance;
            chart(chartId, balance?.series || [], {unit: "kW", balance: true});
            $(issueId).textContent = entry?.issue || (balance?.missing ? `${balance.missing} interval(s) have incomplete physical evidence.` : "");
        }
        planEvidence = data.evidence;
        $("inspect-time").disabled = false;
        $("evidence-period").textContent = planEvidence?.period ? `From ${when(planEvidence.period.start)} to ${when(planEvidence.period.end)}: ${number(planEvidence.period.duration_h)} hours: All totals below cover this whole period.` : "";
        $("plan-evidence-status").textContent = planEvidence?.issue || "";
        evidenceCards($("plan-evidence"), planEvidence?.summary || []);
        const currency = plan.currency || "";
        $("plan-description").textContent = `Generated: ${when(plan.generated_at)}: Strategy: ${planStrategy(plan.strategy)}: Status: ${statuses[plan.solution_status] || "Unknown"}: Advice does not prove execution`;
        if (plan.consumption_objective) {
            const objective = plan.consumption_objective;
            $("plan-description").textContent += `: Primary objective: ${number(objective.grid_import_kwh)} kWh grid import + ${number(objective.terminal_replacement_kwh)} kWh hypothetical stock replacement = ${number(objective.corrected_import_kwh)} kWh: Replacement is an end-of-horizon correction, not measured consumption or a charging command.`;
        }
        for (const name of ["plan-rows", "plan-battery-rows", "plan-solar-rows", "expected-cost-rows"]) $(name).replaceChildren();
        const baseline = withoutBattery?.comparison;
        const self = selfConsumption?.comparison;
        const period = planEvidence?.period;
        let message = "Benefits versus no battery include storage and smart planning. Excludes investment costs.";
        if (self?.reference_start?.status === "measured_common_start") message = "Self-consumption and smart planning start from the same measured SoC and opening bonus budgets. Expected benefits include the difference in remaining battery value. " + message;
        if (data.valuation) message += " Contract updated. Self-consumption costs were repriced without re-optimization. See the separate cost table.";
        if (data.valuation_status?.issue) message += ` Repricing unavailable: ${data.valuation_status.issue}.`;
        if (comparison?.reference_start?.status === "provisional_common_start") message += " Self-consumption reference initialization is provisional.";
        if (comparison?.reference_start?.estimated) message += " Self-consumption reference start is estimated.";
        $("expected-benefit").textContent = message + " Not evaluated lifetime savings. See Savings for evaluated results.";
        const original = comparison?.ems_costs || {purchase: plan.independent_costs?.import_cost, sale: plan.independent_costs?.export_income, bonus: plan.independent_costs?.bonus_income, wear: plan.independent_costs?.wear_cost};
        for (const [key, label] of [["purchase", "Import cost"], ["sale", "Export income"], ["bonus", "Bonus income"], ["wear", "Wear"], ["inventory_change", "Battery value change"], ["economic_cost", "Economic cost"]]) row($("expected-cost-rows"), [label, money(original[key]), money(comparison?.reference_costs?.[key]), money(valuation?.ems_costs?.[key]), money(valuation?.reference_costs?.[key])]);
        const ems = [], reference = [], bareReference = [], buy = [], sell = [], spot = [], bonusSell = [], demandForecast = [], solarForecast = [];
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
        const intervals = plan.input_snapshot?.problem?.intervals || comparison?.intervals || [];
        intervals.forEach((step, i) => {
            // Keep every declared interval in the inspector. Absent evidence must not shift later values.
            const flow = plan.expected_flows?.[i] || {};
            const ref = comparison?.reference?.[i];
            const bare = baseline?.reference?.[i];
            const imported = flow.grid_import ?? (flow.grid_to_load != null && flow.grid_to_battery != null ? flow.grid_to_load + flow.grid_to_battery : null);
            const exported = flow.grid_export ?? (flow.pv_to_grid != null && flow.battery_to_grid != null ? flow.pv_to_grid + flow.battery_to_grid : null);
            const actualStored = flow.batteries ? flow.batteries.reduce((total, battery) => total + battery.end_kwh, 0) : flow.battery_end_kwh;
            const refStored = ref ? Object.values(ref.stored).reduce((a, b) => a + b, 0) : null;
            row($("plan-rows"), [when(step.start), number(step.load_kwh), number(step.pv_kwh), number(imported), number(exported), number(ref?.import_kwh), number(ref?.export_kwh), number(actualStored), number(refStored)]);
            const command = plan.steps.find((entry) => new Date(entry.start) <= new Date(step.start) && new Date(entry.end) > new Date(step.start));
            (flow.batteries || []).forEach((battery, index) => {
                const target = command?.devices?.[battery.device_id];
                const mode = target?.mode === "follow_grid_target" ? "Zero on the meter" : target?.mode === "fixed_power" ? "Fixed power" : "No command";
                row($("plan-battery-rows"), [when(step.start), batteryLabels.get(battery.device_id) || `Battery ${index + 1}`, mode, target?.power_w == null ? "-" : Math.round(target.power_w), number(battery.end_kwh), number(ref?.stored[battery.device_id])]);
            });
            (step.pv_sources || []).forEach(([source, available], index) => {
                const used = flow.solar?.find((value) => value.source_id === source)?.used_kwh;
                const enabled = command?.devices?.[source]?.enabled;
                row($("plan-solar-rows"), [when(step.start), plan.comparison_names?.[source] || `Solar ${index + 1}`, number(available), number(used), enabled === false ? "Curtailment requested" : enabled === true ? "Production allowed" : "Fixed production"]);
            });
            const time = new Date(step.start).getTime();
            // Use the recorded boundary. Rebuilding microsecond durations can exceed Date's millisecond endpoint.
            const boundary = intervals[i + 1]?.start ?? command?.end;
            const end = boundary ? Date.parse(boundary) : Math.floor(time + step.duration_h * 3600000);
            const prices = plan.input_snapshot?.problem?.intervals?.[i] || step;
            const tariff = plan.input_snapshot?.tariff_evidence?.find((t) => Date.parse(t.start) <= time && Date.parse(t.end) > time);
            const add = (list, value, priceKind) => { if (value != null) list.push([time, value, priceKind], [end, value, priceKind]); };
            add(buy, prices.buy_price, tariff?.price_kind); add(sell, prices.sell_price, tariff?.price_kind);
            if (bonusRules.length) {
                // Frozen compiled rates already include rule/window eligibility. Intentionally ignore quota.
                const rates = bonusRules.map((rule) => rule.rates?.[i]);
                const tariffs = plan.input_snapshot?.tariff_evidence || [];
                const covered = !tariffs.length || tariffs.some((tariff) => Date.parse(tariff.start) <= time && Date.parse(tariff.end) >= end);
                const known = covered && Number.isFinite(prices.sell_price) && rates.every((rate) => Number.isFinite(rate) && rate >= 0);
                const total = known ? prices.sell_price + rates.reduce((sum, rate) => sum + rate, 0) : null;
                bonusSell.push([time, total, tariff?.price_kind], [end, total, tariff?.price_kind]);
            }
            const originalDemand = householdForecastEnergy(plan, prices);
            add(demandForecast, prices.duration_h > 0 && originalDemand != null ? originalDemand / prices.duration_h : null);
            add(solarForecast, prices.duration_h > 0 && typeof prices.pv_kwh === "number" ? prices.pv_kwh / prices.duration_h : null);
            const commands = [];
            for (const task of plan.appliance_schedule || []) {
                const energy = task.interval_kwh[i];
                if (energy > 0) commands.push(`${plan.comparison_names?.[task.entity_id] || "Appliance"}: ${number(energy)} kWh in this interval: Cycle starts: ${when(task.start)}, ends ${when(task.end)}`);
            }
            for (const [index, battery] of (flow.batteries || []).entries()) {
                const name = batteryLabels.get(battery.device_id) || `Battery ${index + 1}`;
                const target = command?.devices?.[battery.device_id];
                const text = target?.mode === "follow_grid_target" ? "Zero on the meter (local regulation)" : target?.power_w != null ? `${Math.round(target.power_w)} W fixed power (+ discharge)` : "No command";
                commands.push(`${name}: ${text}: Stored: ${number(battery.end_kwh)} kWh`);

            }
            for (const [index, [source]] of (step.pv_sources || []).entries()) {
                const enabled = command?.devices?.[source]?.enabled;
                commands.push(`${plan.comparison_names?.[source] || `Solar ${index + 1}`}: ${enabled === false ? "Curtailment requested" : enabled === true ? "Production allowed" : "Fixed production"}`);
            }
            inspectSteps.push({start: time, end, commands, priceStatus: tariff ? `${tariff.price_kind === "published" ? "Published market rate" : tariff.price_kind === "forecast" ? "Forecast market rate" : "Market rate provenance unclassified"}: Native ${tariff.native_resolution_minutes}-minute resolution` : "Market provenance unavailable", text: `From ${when(step.start)} to ${when(end)}\nGrid: ${number(imported)} kWh import / ${number(exported)} kWh export: Household: ${number(step.load_kwh)} kWh: Available PV DC: ${number(step.pv_kwh)} kWh\nImport: ${money(prices.buy_price)} ${currency}/kWh: Export: ${money(prices.sell_price)} ${currency}/kWh${tariff ? `: Market rate: ${optionLabel(tariff.price_kind || "unknown")}: Native resolution: ${tariff.native_resolution_minutes} minutes` : ""}\n${commands.join("\n")}`});
            if (imported != null && exported != null) {
                const power = (imported - exported) / step.duration_h * 1000;
                ems.push([time, power], [end, power]);
            } else ems.push([time, null], [end, null]);
            if (bare) {
                const power = (bare.import_kwh - bare.export_kwh) / step.duration_h * 1000;
                bareReference.push([time, power], [end, power]);
            }
            if (ref) {
                const power = (ref.import_kwh - ref.export_kwh) / step.duration_h * 1000;
                reference.push([time, power], [end, power]);
            }
        });
        const series = [{name: "Optimizer", points: ems}];
        if (reference.length) series.push({name: "Battery self-consumption", points: reference, color: palette[2]});
        if (bareReference.length) series.push({name: "Without battery: Baseline", points: bareReference, color: palette[1]});
        const balances = energyBalances(plan, new Map([...Object.entries(plan.comparison_names || {}), ...batteryLabels]));
        inspectSteps.forEach((step, index) => {
            const rows = balances.map((group) => `${group.name}${group.missing ? ` (${group.missing} interval(s) with unavailable physical evidence)` : ""}: ${group.series.map((series) => series.type === "bar" ? `${series.name} ${number(series.intervals[index]?.value)} kW` : `${series.name} ${percentage(series.points[index * 2 + 1]?.[1])}`).join(": ")}`);
            step.text += `\n${rows.join("\n")}`;
        });
        const balance = balances[0];
        chart("plan-chart", balance.series, {unit: "kW", balance: true});
        if (balance.missing) {
            const warning = document.createElement("p"); warning.className = "balance-warning";
            warning.textContent = `${balance.missing} interval(s) have missing or inconsistent physical evidence. Their balance bars are unavailable. No losses or flows are invented.`;
            $("plan-chart-legend").append(warning);
        }
        chart("energy-chart", series, {unit: "W"});
        chart("forecast-chart", [{name: "Household demand forecast: AC", points: demandForecast, color: "#23965e"}, {name: "Available solar forecast: DC", points: solarForecast, color: "#b58900"}], {unit: "kW", minimum: 0});
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
        const priceSeries = [{name: "Spot market rate", points: spot, color: "#d9a300"}, {name: "Import rate", points: buy, color: "#d56788"}, {name: "Export rate", points: sell, color: "#399bc1"}];
        if (bonusRules.length) priceSeries.push({name: "Export rate incl. bonus", points: bonusSell, color: "#9255b7"});
        priceInputPlanId = id;
        currentPriceWindow = null;
        $("price-chart").closest(".chart-wrap").hidden = false;
        $("current-price-status").textContent = `Selected plan: ${when(plan.generated_at)}: Original contract rates. ${bonusRules.length ? "Potential export bonus is shown before the volume cap." : "No bonus applies in this plan."}`;
        chart("price-chart", priceSeries, {unit: `${currency}/kWh`});
        const hours = intervals.reduce((total, interval) => total + interval.duration_h, 0);
        $("planning-window").textContent = `${number(hours)}-hour planning window`;
        periodCostPlan = {id, currency, scenarios: data.scenarios, period, start: inspectSteps[0]?.start, end: inspectSteps.at(-1)?.end};
        periodCostKey = ""; periodCostRequest++;
        $("inspect-time").max = Math.max(0, inspectSteps.length - 1); $("inspect-time").value = 0; inspect(); applyRange();
        $("overview-status").textContent = CONNECTED_MESSAGE;
        renderedPlanId = id;
        planReadPending = false;
        return true;
    } catch (error) {
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
    const data = await api(`v1/plans?limit=20${older && cursor ? `&before=${encodeURIComponent(cursor)}` : ""}`, signal);
    if (!older) $("plan-version").replaceChildren();
    for (const plan of data.plans) {
        const option = document.createElement("option"); option.value = plan.plan_id; option.textContent = planName(plan); $("plan-version").append(option);
    }
    cursor = data.next_before; $("older-plans").hidden = !cursor;
    if (!older && requestedPlan) {
        const loaded = await selectOriginal(requestedPlan, signal);
        if (loaded) requestedPlan = null;
        else $("overview-status").textContent = "The linked plan could not be loaded. Check the connection and choose Refresh to retry. No other plan has been selected.";
        return loaded;
    }
    if (!older) { selectedLinkedPlan = false; return showPlan(signal); }
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
        const needsPlan = renderedPlanId !== latest.plan_id || planReadPending;
        if (followingLatest && needsPlan) {
            const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
            await Promise.all([showPlan(signal, true), priceRead ??= currentPrices(signal)]);
        }
    } catch { /* Retain the last usable plan and retry after a transient archive read failure. */ }
    finally {
        if (!priceRead && priceRefreshPending && !overviewLoads) priceRead = currentPrices(AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS));
        await Promise.allSettled([priceRead]); planRefreshBusy = false;
    }
}
/** Use local calendar boundaries as aware API instants. Absent coverage never becomes zero savings. */
async function savings(signal) {
    const request = ++periodRequest;
    try {
        const start = $("period-start").value, last = $("period-end").value;
        if (!start || !last || last < start) throw new Error("Choose a valid date range.");
        const query = {start: localInstant(start), end: localInstant(nextDate(last))};
        const selectedRevision = $("evaluation-version").value;
        if (selectedRevision) query.revision = selectedRevision;
        const data = await api(`v1/savings?${new URLSearchParams(query)}`, signal);
        if (request !== periodRequest) return false;
        if (data.daily) { renderDailySavings(data); return true; }
        const coverage = data.coverage;
        for (const revision of data.versions || []) evaluationVersions.set(revision.revision, revision);
        renderVersions(selectedRevision);
        evaluationCursor = data.next_evaluation;
        $("older-evaluations").hidden = !evaluationCursor;
        $("evaluation-version-info").textContent = data.revision ? `${data.corrected ? "Corrected evaluation" : "Original evaluation"}: ${data.revision.slice(0, 8)}: Original plans remain unchanged.` : "An evaluation version appears when settlement can begin.";
        $("coverage").textContent = `Quarters evaluated: ${number(coverage.evaluated)} of ${coverage.expected}: Reconstructed intervals: ${coverage.estimated}: Missing coverage is not zero savings.${data.details_truncated ? " Detail table shows the first day. Select a day to inspect later intervals. Period totals and chart cover the full selection." : ""}${data.issue ? ` ${data.issue}.` : ""}`;
        const currency = data.intervals[0]?.currency || "";
        $("savings-chart").setAttribute("aria-label", "Evaluated savings over the selected period");
        $("realization-chart").setAttribute("aria-label", "Recorded import compared with advice and simulated reference");
        $("realization-chart").closest("figure").querySelector("figcaption h3").textContent = "Import: advice and realization";
        $("savings-total").textContent = coverage.evaluated ? `Evaluated benefit: ${money(data.savings)} ${currency}: Avoided gross import: ${number(data.avoided_import_kwh)} kWh: Negative results are retained.` : "No evaluated savings yet. Comparison starts when a published plan, completed metering and published prices are available.";
        $("cost-rows").replaceChildren();
        for (const [key, label] of [["purchase", "Import cost"], ["sale", "Export income"], ["bonus", "Bonus income"], ["grid_cash", "Net electricity cost"], ["wear", "Battery wear"], ["inventory_change", "Battery value change (estimated, not cash)"], ["economic_cost", "Economic cost"], ["import_kwh", "Gross import (kWh)"]]) row($("cost-rows"), [label, coverage.evaluated ? (key.endsWith("_kwh") ? number : money)(data.totals.actual[key]) : "-", coverage.evaluated ? (key.endsWith("_kwh") ? number : money)(data.totals.reference[key]) : "-"]);
        $("evaluation-rows").replaceChildren();
        $("realized-battery-rows").replaceChildren();
        $("inventory-rows").replaceChildren();
        for (const r of data.intervals) {
            row($("evaluation-rows"), [when(r.start), r.advice?.complete ? "Advice available" : "No complete active advice", number(r.load_kwh), number(r.measured_pv_kwh), r.advice?.complete ? number(r.advice.import_kwh) : "-", number(r.actual.import_kwh), number(r.reference.import_kwh), money(r.savings), r.quality === "recorded" && !r.available_pv_estimated && !r.subinterval_estimated ? "Measurements were recorded. The reference was simulated." : "Contains estimated reconstruction", when(r.evaluated_at)]);
            const button = document.createElement("button"); button.type = "button"; button.textContent = "View original plan";
            button.addEventListener("click", () => selectOriginal(r.plan_id));
            $("evaluation-rows").lastElementChild.children[1].append(button);
            Object.entries(r.actual.stored).forEach(([id, value], index) => row($("realized-battery-rows"), [when(r.start), r.device_names?.[id] || `Battery ${index + 1}`, number(value), number(r.reference.stored[id])]));
            for (const movement of r.inventory_movements) row($("inventory-rows"), [when(r.start), r.device_names?.[movement.device_id] || "Changed battery", movement.scenario === "reference" ? "Simulated reference" : "Actual", number(movement.kwh), money(movement.value), "Installation change"]);
        }
        const bins = data.details_truncated ? data.chart : data.intervals;
        const segments = [];
        let previousEnd = null;
        for (const bin of bins) {
            const start = new Date(data.details_truncated ? `${bin.start}T00:00:00Z` : bin.start).getTime();
            const end = new Date(data.details_truncated ? `${bin.end}T00:00:00Z` : bin.end).getTime();
            if (start !== previousEnd) segments.push([]);
            segments.at(-1).push([start, bin.savings], [end, bin.savings]);
            previousEnd = end;
        }
        chart("savings-chart", [{name: `Benefit (${currency})`, points: segments.flat(), segments}], {unit: currency, utcDays: data.details_truncated});
        // Detailed measured/advised energy uses interval duration. Never plot truncated totals as complete period power.
        const realized = [], advised = [], simulated = [];
        let previous = null;
        for (const interval of data.intervals) {
            const start = new Date(interval.start).getTime(), end = new Date(interval.end).getTime();
            const hours = (end - start) / 3600000;
            if (previous !== null && start !== previous) {
                for (const points of [realized, advised, simulated]) points.push([start, null]);
            }
            realized.push([start, interval.actual.import_kwh / hours * 1000], [end, interval.actual.import_kwh / hours * 1000]);
            simulated.push([start, interval.reference.import_kwh / hours * 1000], [end, interval.reference.import_kwh / hours * 1000]);
            const value = interval.advice?.complete ? interval.advice.import_kwh / hours * 1000 : null;
            advised.push([start, value], [end, value]); previous = end;
        }
        chart("realization-chart", [{name: "Recorded EMS", points: realized}, {name: "Simulated reference", points: simulated, reference: true}, {name: "Original advice", points: advised}], {unit: "W", minimum: 0});
        $("realization-chart").closest("figure").querySelector("figcaption p").textContent = data.details_truncated ? "First day of detailed intervals only. Select a day to compare later advice and recorded import. Period benefit above covers the entire selection." : "Recorded EMS import versus simulated reference and original advice. Missing advice stays absent.";

        return true;
    } catch (error) { if (request === periodRequest) $("coverage").textContent = error.message; return false; }
}
/** Present compact daily evidence with independent coverage, including unavailable scenarios. */
function renderDailySavings(data) {
    const samePeriod = dailySavingsData?.start === data.start && dailySavingsData?.end === data.end;
    if (!samePeriod) dailySavingsPage = 0;
    dailySavingsData = data;
    $("daily-savings-details").hidden = false;
    for (const id of ["savings-chart", "realization-chart"]) $(id).closest("figure").classList.add("daily-savings-chart");
    $("savings-chart").setAttribute("aria-label", "Daily smart planning benefit on matching measured coverage");
    $("realization-chart").setAttribute("aria-label", "Daily gross import: Actual, battery self-consumption and without battery");
    $("evaluation-version").closest("label").hidden = true;
    $("older-evaluations").hidden = true;
    $("evaluation-version-info").textContent = "Finalized days retain their original calculation method, contract, installation and quality. Changes apply only to provisional days.";
    for (const id of ["evaluation-rows", "realized-battery-rows", "inventory-rows"]) $(id).closest("details").hidden = true;
    const currency = data.currency || "";
    $("coverage").textContent = `${data.day_count} evaluated days: Actual coverage: ${percentage(data.coverage.actual.seconds / data.coverage.actual.expected_seconds * 100)}: Self-consumption coverage: ${percentage(data.coverage.reference.seconds / data.coverage.reference.expected_seconds * 100)}: Without battery coverage: ${percentage(data.coverage.without_battery.seconds / data.coverage.without_battery.expected_seconds * 100)}.${data.details_truncated ? " Select a shorter period to inspect later daily details. Totals and charts cover the full selection." : ""}${data.issue ? ` ${data.issue}.` : ""}`;
    $("savings-total").textContent = [
        data.savings === null ? "No matching measured coverage for a smart planning comparison. Missing evidence is not zero savings." : `Smart planning benefit: ${money(data.savings)} ${currency}.`,
        data.battery_savings === null ? "No matching measured coverage for a comparison versus without battery. Missing evidence is not zero savings." : `Benefit versus without battery: ${money(data.battery_savings)} ${currency}.`,
        "Negative results are retained."
    ].join(" ");
    const table = $("cost-rows").closest("table");
    table.querySelector("caption").textContent = "Costs on each scenario's own measured coverage. Compare benefits only on matching coverage.";
    table.querySelector("thead tr").replaceChildren(...["Component", "Actual", "Battery self-consumption", "Without battery"].map(label => { const cell = document.createElement("th"); cell.textContent = label; return cell; }));
    $("cost-rows").replaceChildren();
    for (const [key, label] of [["purchase", "Import cost"], ["sale", "Export income"], ["bonus", "Bonus income"], ["grid_cash", "Net electricity cost"], ["wear", "Battery wear"], ["inventory_change", "Battery value change (estimated, not cash)"], ["economic_cost", "Economic cost"], ["import_kwh", "Gross import (kWh)"], ["export_kwh", "Gross export (kWh)"]]) {
        row($("cost-rows"), [label, ...["actual", "reference", "without_battery"].map(kind => data.totals[kind][key] === null ? "Unavailable" : (key.endsWith("_kwh") ? number : money)(data.totals[kind][key]))]);
    }
    const segments = [];
    let previousEnd = null;
    for (const bin of data.chart) {
        const start = Date.parse(bin.start), end = Date.parse(bin.end);
        if (bin.savings === null) { previousEnd = null; continue; }
        if (start !== previousEnd) segments.push([]);
        segments.at(-1).push([start, bin.savings], [end, bin.savings]); previousEnd = end;
    }
    chart("savings-chart", [{name: `Smart planning benefit (${currency})`, points: segments.flat(), segments}], {unit: currency, calendarDates: true});
    $("savings-chart").setAttribute("aria-label", "Smart planning benefit on matching measured coverage");
    $("savings-chart").closest("figure").querySelector("figcaption p").textContent = "Benefit totals for each plotted interval on matching measured coverage. Partial coverage remains partial. Gaps indicate unavailable comparisons.";
    const series = [["actual", "Actual"], ["reference", "Battery self-consumption"], ["without_battery", "Without battery"]].map(([kind, name]) => ({name, points: data.chart.flatMap(bin => [[Date.parse(bin.start), bin[`${kind}_import_kwh`] ?? null], [Date.parse(bin.end), bin[`${kind}_import_kwh`] ?? null]])}));
    chart("realization-chart", series, {unit: "kWh", minimum: 0, calendarDates: true});
    const caption = $("realization-chart").closest("figure").querySelector("figcaption");
    $("realization-chart").setAttribute("aria-label", "Gross import: Actual, battery self-consumption and without battery");
    caption.querySelector("h3").textContent = "Gross import";
    caption.querySelector("p").textContent = "Measured actual import and both simulated scenarios on their own available coverage. Inspect daily details before comparing partial days.";
    renderDailySavingsRows(!samePeriod);
}
/** Keep a compact, accessible 25-row list and disclose missing ranges only on request. */
function renderDailySavingsRows(resetScroll = false) {
    const data = dailySavingsData;
    if (!data) return;
    const pages = Math.max(1, Math.ceil(data.days.length / DAILY_RESULTS_PAGE_SIZE));
    dailySavingsPage = Math.max(0, Math.min(dailySavingsPage, pages - 1));
    const expanded = new Set([...$("daily-savings-rows").querySelectorAll("tr:has(details[open])")].map(row => row.dataset.day));
    const focusedDay = document.activeElement?.closest("#daily-savings-rows tr")?.dataset.day;
    const focusedTag = document.activeElement?.tagName?.toLowerCase();
    let restoreFocus = null;
    $("daily-savings-rows").replaceChildren();
    for (const day of data.days.slice(dailySavingsPage * DAILY_RESULTS_PAGE_SIZE, (dailySavingsPage + 1) * DAILY_RESULTS_PAGE_SIZE)) {
        const amount = kind => { const value = day.scenarios[kind]; return value.totals ? `${money(value.totals.economic_cost)}: ${percentage(value.coverage_seconds / day.expected_seconds * 100)}` : "Unavailable"; };
        const quality = Object.values(day.scenarios).every(scenario => scenario.status === "complete") ? "Complete" : Object.values(day.scenarios).some(scenario => scenario.status !== "unavailable") ? "Partial" : "Unavailable";
        row($("daily-savings-rows"), [dateLabel(day.day), `${day.finalized_at ? "Finalized" : "Provisional"}: ${quality}`, amount("actual"), amount("reference"), amount("without_battery"), day.savings === null ? "Unavailable" : money(day.savings), ""]);
        const current = $("daily-savings-rows").lastElementChild;
        current.dataset.day = day.day;
        const details = document.createElement("details"), summary = document.createElement("summary");
        summary.textContent = "Coverage and calculation"; details.append(summary);
        const metadata = document.createElement("p"); metadata.textContent = `Evaluated: ${when(day.evaluated_at)}: ${day.finalized_at ? `Finalized: ${when(day.finalized_at)}` : `Recovery until: ${when(day.finalizes_after)}`}: Method: ${day.policy}`; details.append(metadata);
        if (day.day_plan_id) { const original = document.createElement("button"); original.type = "button"; original.textContent = "View retained day plan"; original.addEventListener("click", () => selectOriginal(day.day_plan_id)); details.append(original); }
        for (const [kind, label] of [["actual", "Actual"], ["reference", "Battery self-consumption"], ["without_battery", "Without battery"]]) {
            const scenario = day.scenarios[kind], text = document.createElement("p");
            text.textContent = `${label}: ${optionLabel(scenario.status)}: Reconstructed coverage: ${percentage(scenario.estimated_seconds / day.expected_seconds * 100)}${scenario.opening ? `: Opening battery value: ${money(scenario.opening.value)}: Closing battery value: ${money(scenario.closing.value)}` : ""}`; details.append(text);
            for (const gap of scenario.missing) { const missing = document.createElement("p"); missing.textContent = `${when(gap.start)} to ${when(gap.end)}: ${gap.reason}`; details.append(missing); }
        }
        for (const movement of day.inventory_movements || []) {
            const changed = document.createElement("p");
            changed.textContent = `Installation change: ${when(movement.at)}: ${day.device_names?.[movement.device_id] || "Battery"}: ${movement.scenario === "actual" ? "Actual" : "Battery self-consumption"}: ${number(movement.kwh)} kWh: Value: ${money(movement.value)}: Excluded from operating benefit.`;
            details.append(changed);
        }
        $("daily-savings-rows").lastElementChild.lastElementChild.append(details);
        details.open = !resetScroll && expanded.has(day.day);
        if (!resetScroll && focusedDay === day.day && ["summary", "button"].includes(focusedTag)) restoreFocus = current.querySelector(focusedTag);
    }
    $("daily-savings-page").textContent = `Page ${dailySavingsPage + 1} of ${pages}`;
    $("daily-savings-previous").disabled = dailySavingsPage === 0;
    $("daily-savings-next").disabled = dailySavingsPage >= pages - 1;
    const scroll = $("daily-savings-rows").closest(".table-scroll");
    if (resetScroll) { scroll.scrollTop = 0; scroll.scrollLeft = 0; }
    restoreFocus?.focus({preventScroll: true});
}
/** Keep the selected immutable evaluation available while paging descriptors. */
function renderVersions(selected) {
    $("evaluation-version").replaceChildren();
    const latest = document.createElement("option"); latest.value = ""; latest.textContent = "Latest evaluated costs"; $("evaluation-version").append(latest);
    for (const revision of [...evaluationVersions.values()].sort((a, b) => b.created_at.localeCompare(a.created_at))) {
        const option = document.createElement("option"); option.value = revision.revision; option.textContent = `${when(revision.created_at)}: Version: ${revision.revision.slice(0, 8)}`; $("evaluation-version").append(option);
    }
    $("evaluation-version").value = selected;
}
/** Open original advice only. Do not issue a control or planning request. */
function selectOriginal(id, signal) {
    if (![...$("plan-version").options].some((o) => o.value === id)) {
        const option = document.createElement("option"); option.value = id; option.textContent = "Selected archived plan"; $("plan-version").append(option);
    }
    selectedLinkedPlan = true;
    location.hash = "planning"; view(); $("plan-version").value = id;
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
/** Refresh independent views. Only the plan reader reports success, preserving newer selection errors. */
async function load() {
    if (!overviewSessionReady && !await initializeOverview()) return;
    overviewLoads++;
    try {
        const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
        const planRead = plans(false, signal);
        const previewRead = planRead.catch(() => {}).then(
            () => currentPrices(AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS))
        );
        const results = await Promise.allSettled([planRead, savings(signal), previewRead]);
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
$("plan-version").addEventListener("change", () => { selectedLinkedPlan = false; showPlan(); });
$("older-plans").addEventListener("click", () => plans(true).catch((error) => { $("overview-status").textContent = error.message; }));
$("period").addEventListener("submit", (event) => { event.preventDefault(); savings(); });
$("evaluation-version").addEventListener("change", () => savings());
$("daily-savings-previous").addEventListener("click", () => { dailySavingsPage--; renderDailySavingsRows(true); });
$("daily-savings-next").addEventListener("click", () => { dailySavingsPage++; renderDailySavingsRows(true); });
$("overview-refresh").addEventListener("click", load);
$("older-evaluations").addEventListener("click", async () => {
    try {
        const data = await api(`v1/evaluations?before=${encodeURIComponent(evaluationCursor)}`);
        for (const revision of data.versions) evaluationVersions.set(revision.revision, revision);
        renderVersions($("evaluation-version").value);
        evaluationCursor = data.next_evaluation; $("older-evaluations").hidden = !evaluationCursor;
    } catch (error) { $("overview-status").textContent = error.message; }
});
/** Initialize once after a confirmed read/render. Explicit Connect/Refresh can retry a failed attempt. */
async function initializeOverview() {
    const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
    try {
        const session = await api("setup/session", signal);
        if (overviewSessionReady) return session;
        useHomeAssistantTimezone(session.timezone);
        const completed = new Date(`${localDate()}T00:00:00Z`);
        completed.setUTCDate(completed.getUTCDate() - 1);
        if (!$("period-start").value) $("period-start").value = completed.toISOString().slice(0, 10);
        if (!$("period-end").value) $("period-end").value = $("period-start").value;
        watchSourceNames(() => api("setup/session", AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS)), async () => (await Promise.all([showPlan(), savings()])).every(result => result === true), session.source_names_revision);
        $("overview-connect").hidden = Boolean(session.ingress);
        if (!session.ingress) $("overview-status").textContent = "Connect to view your plans and savings.";
        overviewSessionReady = true;
        return session;
    } catch {
        $("overview-status").textContent = signal.aborted ? "This view took too long to load. Please retry." : "Cannot initialize this view: Check the connection and retry.";
        return null;
    }
}
initializeOverview().then(session => { if (session?.ingress) load(); });
