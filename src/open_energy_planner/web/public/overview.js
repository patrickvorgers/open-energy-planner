// Read-only views never activate an archived plan or reset the reference scenario.
import "./chart_theme.js";
import {calendarDays, calendarTime} from "./chart_days.js";
import {money, when, localDate, localInstant, nextDate, percentage, optionLabel, useHomeAssistantTimezone, watchSourceNames} from "./display.js";
import {energyBalances, quarterBalanceSeries, QUARTER_MILLISECONDS} from "./balance.js";
import {bindSettingsActions} from "./settings-actions.js";
bindSettingsActions();
const $ = (id) => document.getElementById(id);
const CONNECTED_MESSAGE = "Connected: Historical views never change active commands.";
const PLAN_REFRESH_MILLISECONDS = 15000;
const PLAN_READ_TIMEOUT_MILLISECONDS = 5000;
const base = document.querySelector('meta[name="oep-api-base"]').content;
let token = "", cursor = null, evaluationCursor = null, selectionRequest = 0, periodRequest = 0;
let planRefreshEnabled = false, planRefreshBusy = false, overviewLoads = 0, renderedPlanId = null;
let priceRefreshPending = false;
const evaluationVersions = new Map();
const isMoneyUnit = (unit) => /^[A-Z]{3}(\/kWh)?$/.test(unit || "");
const number = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : "-";
/** Share numeric precision across evidence cards, tooltip columns and chart axes. Units remain separate. */
const valueText = (value, unit) => unit === "%" ? percentage(value, false) : unit === "W" ? Math.round(value).toLocaleString() : (isMoneyUnit(unit) ? money : number)(value);
const strategies = {"minimize cost": "Minimize cost", "minimize consumption": "Minimize consumption"};
const statuses = {optimal: "Optimal", feasible: "Feasible"};
/** Read authenticated app JSON. Never persist the standalone administration token. */
async function api(path, signal) {
    const response = await fetch(base + path, {headers: token ? {Authorization: `Bearer ${token}`} : {}, redirect: "error", cache: "no-store", signal});
    if (!response.ok) throw new Error(response.status === 401 ? "Enter the app administration token to continue." : `Cannot read this view (${response.status}).`);
    return response.json();
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
const FORECAST_PRICE_DASH = [2, 4];
let inspectSteps = [], planningHours = 0, planEvidence = null, currentPriceWindow = null, priceRequest = 0;

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
/** Keep labels, right-aligned numbers and units in distinct columns without interpreting provider text as HTML. */
function renderTooltip({chart: instance, tooltip}, node, balance = false) {
    node.hidden = !tooltip.opacity || Boolean(instance.canvas.closest("[hidden]"));
    if (node.hidden) return;
    node.replaceChildren();
    const title = document.createElement("p"); title.className = "chart-tooltip-title";
    title.textContent = tooltip.title.join(" - "); node.append(title);
    const items = [...tooltip.dataPoints];
    const interval = balance && items.find(item => item.raw.start != null);
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
            const swatch = document.createElement("span"); swatch.className = "series-swatch"; swatch.style.backgroundColor = item.dataset.backgroundColor;
            label.append(swatch, document.createTextNode(item.dataset.label));
            value.className = "chart-tooltip-value";
            value.textContent = valueText(item.parsed.y, item.dataset.unit);
            unit.className = "chart-tooltip-unit"; unit.textContent = item.dataset.unit;
            line.append(label, value, unit); table.append(line);
        }
        section.append(table); sections.append(section);
    }
    node.append(sections);
    if (tooltip.afterBody.length) {
        const explanation = document.createElement("p"); explanation.className = "chart-tooltip-explanation";
        explanation.textContent = tooltip.afterBody.join("\n"); node.append(explanation);
    }
    const bounds = instance.canvas.getBoundingClientRect(), margin = TOOLTIP_VIEWPORT_MARGIN_PX;
    node.style.left = `${Math.max(margin, Math.min(bounds.left + tooltip.caretX + margin, window.innerWidth - node.offsetWidth - margin))}px`;
    node.style.top = `${Math.max(margin, Math.min(bounds.top + tooltip.caretY + margin, window.innerHeight - node.offsetHeight - margin))}px`;
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
 * remove their headings on destruction. Clock ticks distinguish repeated hours
 * with timezone names while the headings supply the weekday and date.
 * unit names the default y unit. Minimum fixes a physical y baseline. balance stacks series
 * sharing a stack and includes zero. Rendering applies the selected planning range
 * without category-style end padding. Interval timestamps already determine bar widths.
 */
function chart(id, series, {utcDays = false, unit = "", minimum = null, balance = false} = {}) {
    if (balance) series = quarterBalanceSeries(series);
    charts.get(id)?.destroy();
    $(`${id}-tooltip`)?.remove();
    const tooltip = document.createElement("div"); tooltip.id = `${id}-tooltip`; tooltip.className = "chart-tooltip";
    tooltip.hidden = true; tooltip.setAttribute("role", "tooltip"); document.body.append(tooltip);
    const canvas = $(id), legend = $(`${id}-legend`);
    legend.replaceChildren();
    const axisTime = utcDays ? (value) => new Date(value).toISOString().slice(0, 10) + " UTC" : value => when(value, {timeZoneName: "short"});
    const datasets = series.map((s, index) => {
        const values = [];
        for (const segment of s.segments || [s.points || []]) {
            if (values.length) values.push({x: segment[0][0], y: null});
            values.push(...segment.map(([x, y, priceKind]) => ({x, y, priceKind})));
        }
        if (s.intervals) values.push(...s.intervals.map(({start, end, value, coveredStart, coveredEnd}) => ({x: (start + end) / 2, y: value, start, end, coveredStart, coveredEnd})));
        const color = s.color || palette[index % palette.length];
        return {label: s.name, data: values, hidden: id === "current-price-chart" && hiddenPriceSeries.has(s.name), type: s.type || "line", stack: s.stack, yAxisID: s.axis || "y", unit: s.unit || unit, tooltipGroup: s.tooltipGroup, borderColor: color, backgroundColor: color, borderWidth: s.type === "bar" ? 0 : 2, borderDash: s.reference ? [6, 4] : [], segment: {borderDash: ({p0}) => p0.raw.priceKind === "forecast" ? FORECAST_PRICE_DASH : undefined}, pointRadius: 0, pointHitRadius: 10, spanGaps: false, order: s.axis === "soc" ? 0 : 1};
    });
    const result = new Chart(canvas, {
        type: "line", data: {datasets}, plugins: [intervalWidths, calendarDays],
        options: {responsive: true, maintainAspectRatio: false, animation: false, parsing: false,
            interaction: {mode: "nearest", axis: "x", intersect: false},
            plugins: {calendarDays: {utc: utcDays}, valueAxisHeadroom: {group: ["plan-chart", "reference-chart", "self-consumption-chart"].includes(id) ? "planning-comparison" : null}, legend: {display: false}, tooltip: {enabled: false, external: (context) => renderTooltip(context, tooltip, balance), callbacks: {
                title: (items) => !items.length ? "" : items[0].raw.start != null ? `From ${axisTime(items[0].raw.start)} to ${axisTime(items[0].raw.end)}` : axisTime(items[0].parsed.x),
                label: (item) => `${item.dataset.label}: ${valueText(item.parsed.y, item.dataset.unit)} ${item.dataset.unit}`,
                afterBody: (items) => {
                    if (balance && items.length) {
                        const bar = items.find(item => item.raw.start != null)?.raw;
                        if (!bar) return [];
                        const notes = bar.coveredStart !== bar.start || bar.coveredEnd !== bar.end
                            ? [`Partial quarter: Mean power covers ${axisTime(bar.coveredStart)} to ${axisTime(bar.coveredEnd)} only.`] : [];
                        if (!["reference-chart", "self-consumption-chart"].includes(id)) {
                            notes.push(...new Set(inspectSteps.filter(step => step.end > bar.coveredStart && step.start < bar.coveredEnd).flatMap(step => step.commands || [])));
                        }
                        return notes;
                    }
                    if (!items.length || ["savings-chart", "realization-chart", "forecast-chart", "reference-chart", "self-consumption-chart"].includes(id)) return "";
                    const step = inspectSteps.find((step) => step.start <= items[0].parsed.x && items[0].parsed.x < step.end);
                    return id === "current-price-chart" ? [] : id === "price-chart" ? step?.priceStatus || "" : step?.commands || [];
                }
            }}},
            scales: {x: {type: "linear", offset: false, grid: {display: false},
                afterBuildTicks: utcDays ? undefined : (scale) => {
                    const first = Math.ceil(scale.min / QUARTER_MILLISECONDS);
                    const last = Math.floor(scale.max / QUARTER_MILLISECONDS);
                    const stride = Math.max(1, Math.ceil((last - first) / (MAX_TIME_AXIS_LABELS - 1)));
                    scale.ticks = [];
                    for (let quarter = first; quarter <= last; quarter += stride) scale.ticks.push({value: quarter * QUARTER_MILLISECONDS});
                },
                ticks: {maxTicksLimit: MAX_TIME_AXIS_LABELS, maxRotation: 0, callback: (value) => utcDays ? axisTime(value) : calendarTime(value)},
                title: {display: utcDays, text: "UTC calendar date"}},
                y: {min: minimum ?? undefined, suggestedMin: balance ? 0 : undefined, stacked: balance, beginAtZero: false, title: {display: Boolean(unit), text: unit}, grid: {color: (context) => context.tick.value === 0 ? "#455d70" : "#edf1f5", lineWidth: (context) => context.tick.value === 0 ? 2 : 1}, ticks: {maxTicksLimit: 6, callback: (value) => unit === "W" ? number(value) : valueText(value, unit)}},
                ...(series.some((entry) => entry.axis === "soc") ? {soc: {position: "right", min: 0, max: 100, stacked: false, title: {display: true, text: "SoC (%)"}, ticks: {callback: (value) => percentage(value, false)}, grid: {drawOnChartArea: false}}} : {})},
            onClick: (_event, elements) => {
                if (["savings-chart", "realization-chart", "forecast-chart", "reference-chart", "self-consumption-chart", "current-price-chart"].includes(id) || !elements.length) return;
                const time = datasets[elements[0].datasetIndex].data[elements[0].index].x;
                const index = inspectSteps.findIndex((step) => step.start <= time && time < step.end);
                if (index >= 0) { $("inspect-time").value = index; inspect(); $("plan-evidence-panel").open = true; $("interval-evidence-panel").open = true; $("evidence-time").scrollIntoView({block: "nearest"}); }
            }
        }
    });
    charts.set(id, result);
    series.forEach((s, index) => {
        const label = document.createElement("label"), input = document.createElement("input");
        input.type = "checkbox"; input.checked = result.isDatasetVisible(index);
        input.addEventListener("change", () => {
            if (id === "current-price-chart") {
                if (input.checked) hiddenPriceSeries.delete(s.name); else hiddenPriceSeries.add(s.name);
            }
            result.setDatasetVisibility(index, input.checked); applyRange();
        });
        const swatch = document.createElement("span"); swatch.className = "series-swatch"; swatch.style.backgroundColor = datasets[index].backgroundColor; swatch.setAttribute("aria-hidden", "true");
        label.append(input, swatch, document.createTextNode(s.name)); legend.append(label);
    });
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
    const currentPlan = $("plan-version").selectedIndex === 0 && first <= now;
    const start = first == null ? undefined : currentPlan ? currentQuarter : Math.floor(first / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS;
    for (const [id, instance] of charts) {
        if (["savings-chart", "realization-chart"].includes(id)) continue;
        if (id === "current-price-chart") {
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
/** Emphasize the existing incremental comparison without inventing a value or hiding a loss. */
function renderSmartBenefit(value, currency, period = "Whole original plan. Expected, not measured.") {
    const known = typeof value === "number" && Number.isFinite(value);
    const zero = known && money(Math.abs(value)) === money(0);
    const state = !known ? "unavailable" : zero ? "zero" : value > 0 ? "positive" : "negative";
    for (const region of document.querySelectorAll(".planning-benefit")) region.dataset.state = state;
    for (const label of document.querySelectorAll('[data-smart-benefit="label"]')) label.textContent = state === "negative" ? "Extra expected cost from smart planning" : state === "zero" ? "No extra expected savings from smart planning" : "Extra expected savings from smart planning";
    for (const amount of document.querySelectorAll('[data-smart-benefit="value"]')) amount.textContent = known ? money(zero ? 0 : Math.abs(value)) : "-";
    for (const unit of document.querySelectorAll('[data-smart-benefit="currency"]')) unit.textContent = currency;
    for (const caption of document.querySelectorAll(".scenario-period")) caption.textContent = period;
}

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
    if (showBenefit) metric(totals, "Expected benefit versus without battery", benefit);
    const details = document.createElement("details"), summary = document.createElement("summary"), breakdown = document.createElement("dl");
    summary.textContent = "Cost breakdown"; details.open = expanded;
    const cash = costs && [costs.purchase, costs.sale, costs.bonus].every(value => typeof value === "number" && Number.isFinite(value)) ? costs.purchase - costs.sale - costs.bonus : null;
    metric(breakdown, "Net grid cash cost", cash);
    metric(breakdown, "Wear", costs?.wear);
    metric(breakdown, "Battery value change (not cash)", costs?.inventory_change);
    const explanation = document.createElement("p");
    explanation.textContent = "Economic cost = net grid cash cost + wear - battery value change. A positive value change reduces economic cost. Battery value change is not cash.";
    details.append(summary, breakdown, explanation);
    host.replaceChildren(totals, details);
    if (!showBenefit) {
        const note = document.createElement("p"); note.textContent = "Baseline without storage"; host.append(note);
    }
}

/**
 * Fetch frozen advice and separate valuations, ignoring stale selection responses.
 * Render both frozen reference balances, original whole-plan costs and factual
 * evidence without optimization. Clear/disable evidence during loading. Version
 * requests so late responses cannot overwrite newer selections.
 * Retain every declared interval position even without expected flow. Absent values
 * remain unknown rather than zero. Net watts are (import - export) / duration_h *
 * 1000, positive import and negative export. Missing grid evidence leaves null EMS
 * line gaps. Rebuild tables, inspector and charts without changing active commands.
 */
async function showPlan(signal) {
    const request = ++selectionRequest, id = $("plan-version").value;
    renderSmartBenefit(null, "", id ? "Loading original plan comparison…" : "No plan selected.");
    for (const costId of ["reference-costs", "self-consumption-costs", "ems-costs", "optimizer-total-costs"]) renderScenarioCosts(costId, null, "", null, costId !== "reference-costs");
    $("expected-benefit").textContent = "";
    planEvidence = null;
    $("inspect-time").disabled = true;
    $("inspect-values").textContent = id ? "Loading original interval values…" : "No plan selected.";
    $("evidence-period").textContent = ""; $("plan-evidence-status").textContent = id ? "Loading original whole-plan evidence…" : "No plan selected.";
    evidenceCards($("plan-evidence"), []); evidenceCards($("interval-evidence"), []);
    $("evidence-time").textContent = ""; $("evidence-issue").textContent = id ? "Loading original plan evidence…" : "No plan selected.";
    if (!id) { $("overview-status").textContent = CONNECTED_MESSAGE; return true; }
    try {
        const data = await api(`v1/plans/${encodeURIComponent(id)}/comparison`, signal);
        if (request !== selectionRequest) return false;
        const plan = data.plan;
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
        $("plan-description").textContent = `Generated: ${when(plan.generated_at)}: Strategy: ${strategies[plan.strategy] || "Unknown"}: Status: ${statuses[plan.solution_status] || "Unknown"}: Advice does not prove execution`;
        if (plan.consumption_objective) {
            const objective = plan.consumption_objective;
            $("plan-description").textContent += `: Primary objective: ${number(objective.grid_import_kwh)} kWh grid import + ${number(objective.terminal_replacement_kwh)} kWh hypothetical stock replacement = ${number(objective.corrected_import_kwh)} kWh: Replacement is an end-of-horizon correction, not measured consumption or a charging command.`;
        }
        for (const name of ["plan-rows", "plan-battery-rows", "plan-solar-rows", "expected-cost-rows"]) $(name).replaceChildren();
        const baseline = withoutBattery?.comparison;
        const self = selfConsumption?.comparison;
        renderScenarioCosts("reference-costs", baseline?.reference_costs, currency);
        renderScenarioCosts("self-consumption-costs", self?.reference_costs, currency, selfConsumption?.expected_savings_vs_without_battery, true);
        for (const costId of ["ems-costs", "optimizer-total-costs"]) renderScenarioCosts(costId, baseline?.ems_costs || self?.ems_costs, currency, baseline?.expected_savings, true);
        const period = planEvidence?.period;
        renderSmartBenefit(self?.expected_savings, currency, period ? `From ${when(period.start)} to ${when(period.end)}: Whole original plan. Expected, not measured.` : undefined);
        let message = "Benefits versus without battery include storage and OEP. Excludes investment costs.";
        if (self?.reference_start?.status === "measured_common_start") message = "Self-consumption and OEP start from the same measured SoC and opening bonus budgets. The extra expected saving includes the difference in remaining battery value. " + message;
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
            add(demandForecast, prices.duration_h > 0 && typeof prices.load_kwh === "number" ? prices.load_kwh / prices.duration_h : null);
            add(solarForecast, prices.duration_h > 0 && typeof prices.pv_kwh === "number" ? prices.pv_kwh / prices.duration_h : null);
            const commands = [];
            for (const task of plan.appliance_schedule || []) {
                const energy = task.interval_kwh[i];
                if (energy > 0) commands.push(`Appliance ${task.entity_id}: ${number(energy)} kWh in this interval: Cycle starts: ${when(task.start)}, ends ${when(task.end)}`);
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
        const series = [{name: "EMS power (W)", points: ems}];
        if (bareReference.length) series.push({name: "Without battery (W)", points: bareReference, reference: true});
        if (reference.length) series.push({name: "Battery self-consumption (W)", points: reference, reference: true});
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
        const priceSeries = [{name: "Spot market rate", points: spot, color: "#85919c"}, {name: "Import rate", points: buy, color: "#2586c4"}, {name: "Export rate", points: sell, color: "#23965e"}];
        if (bonusRules.length) priceSeries.push({name: "Export rate incl. bonus", points: bonusSell, color: "#9255b7"});
        chart("price-chart", priceSeries, {unit: `${currency}/kWh`});
        const hours = intervals.reduce((total, interval) => total + interval.duration_h, 0);
        $("planning-window").textContent = `${number(hours)}-hour planning window`;
        $("inspect-time").max = Math.max(0, inspectSteps.length - 1); $("inspect-time").value = 0; inspect(); applyRange();
        $("overview-status").textContent = CONNECTED_MESSAGE;
        renderedPlanId = id;
        return true;
    } catch (error) {
        if (request === selectionRequest) renderSmartBenefit(null, "", "Original plan comparison unavailable.");
        if (request === selectionRequest) {
            renderedPlanId = null;
            $("overview-status").textContent = error.message;
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
        const option = document.createElement("option"); option.value = plan.plan_id; option.textContent = `${when(plan.generated_at)}: ${strategies[plan.strategy] || "Unknown"}`; $("plan-version").append(option);
    }
    cursor = data.next_before; $("older-plans").hidden = !cursor;
    if (!older) return showPlan(signal);
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
        const selected = selector.value, followingLatest = selected === previousLatest;
        if (latest.plan_id !== previousLatest) {
            priceRead ??= currentPrices(AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS));
            [...selector.options].find(option => option.value === latest.plan_id)?.remove();
            const option = document.createElement("option");
            option.value = latest.plan_id;
            option.textContent = `${when(latest.generated_at)}: ${strategies[latest.strategy] || "Unknown"}`;
            selector.prepend(option);
            selector.value = followingLatest ? latest.plan_id : selected;
        }
        const needsPlan = renderedPlanId !== latest.plan_id;
        if (followingLatest && needsPlan) {
            const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
            await Promise.all([showPlan(signal), priceRead ??= currentPrices(signal)]);
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
        const coverage = data.coverage;
        for (const revision of data.versions || []) evaluationVersions.set(revision.revision, revision);
        renderVersions(selectedRevision);
        evaluationCursor = data.next_evaluation;
        $("older-evaluations").hidden = !evaluationCursor;
        $("evaluation-version-info").textContent = data.revision ? `${data.corrected ? "Corrected evaluation" : "Original evaluation"}: ${data.revision.slice(0, 8)}: Original plans remain unchanged.` : "An evaluation version appears when settlement can begin.";
        $("coverage").textContent = `Quarters evaluated: ${number(coverage.evaluated)} of ${coverage.expected}: Reconstructed intervals: ${coverage.estimated}: Missing coverage is not zero savings.${data.details_truncated ? " Detail table shows the first day. Select a day to inspect later intervals. Period totals and chart cover the full selection." : ""}${data.issue ? ` ${data.issue}.` : ""}`;
        const currency = data.intervals[0]?.currency || "";
        $("savings-total").textContent = coverage.evaluated ? `Evaluated benefit: ${money(data.savings)} ${currency}: Avoided gross import: ${number(data.avoided_import_kwh)} kWh: Negative results are retained.` : "No evaluated savings yet. Comparison starts when a published plan, completed metering and published prices are available.";
        $("cost-rows").replaceChildren();
        for (const [key, label] of [["purchase", "Import cost"], ["sale", "Export income"], ["bonus", "Bonus income"], ["grid_cash", "Net grid cash cost"], ["wear", "Battery wear"], ["inventory_change", "Battery value change (estimated, not cash)"], ["economic_cost", "Economic cost"], ["import_kwh", "Gross import (kWh)"]]) row($("cost-rows"), [label, coverage.evaluated ? (key.endsWith("_kwh") ? number : money)(data.totals.actual[key]) : "-", coverage.evaluated ? (key.endsWith("_kwh") ? number : money)(data.totals.reference[key]) : "-"]);
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
function selectOriginal(id) {
    if (![...$("plan-version").options].some((o) => o.value === id)) {
        const option = document.createElement("option"); option.value = id; option.textContent = `Original plan ${id}`; $("plan-version").append(option);
    }
    location.hash = "planning"; view(); $("plan-version").value = id; showPlan(); $("plan-heading").scrollIntoView({behavior: "smooth"});
}
/**
 * Read current saved tariffs without changing frozen plan evidence.
 * @param {AbortSignal} [signal] Aborting the newest request follows the read failure path.
 * @returns {Promise<boolean|undefined>} True after drawing, false after hiding the chart and
 * showing a read error, or undefined when superseded. Superseded reads leave chart and status
 * unchanged, including when aborted.
 */
async function currentPrices(signal) {
    const request = ++priceRequest;
    try {
        const data = await api("v1/prices/inputs", signal);
        if (request !== priceRequest) return;
        const intervals = data.intervals || [], buy = [], sell = [], market = [], total = [];
        for (const interval of intervals) {
            const start = Date.parse(interval.start), end = Date.parse(interval.end);
            for (const [points, value] of [[buy, interval.buy_price], [sell, interval.sell_price], [market, interval.market_price], [total, interval.total_export_price]]) points.push([start, value, interval.price_kind], [end, value, interval.price_kind]);
        }
        currentPriceWindow = intervals.length ? {start: Math.floor(Date.parse(data.as_of) / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS, end: Date.parse(intervals.at(-1).end)} : null;
        const series = [{name: "Spot market rate", points: market, color: "#d9a300"}, {name: "Import rate", points: buy, color: "#d56788"}, {name: "Export rate", points: sell, color: "#399bc1"}];
        if (data.has_bonus) series.push({name: "Export rate incl. bonus (before volume cap)", points: total, color: "#9255b7"});
        $("current-price-chart").closest(".chart-wrap").hidden = !intervals.length;
        chart("current-price-chart", series, {unit: `${data.currency || "EUR"}/kWh`});
        $("current-price-status").textContent = data.issue ? `Current prices unavailable: ${optionLabel(data.issue)}` : `Saved contract rates as of: ${when(data.as_of)}: ${intervals.some(interval => interval.bonus_issue) ? "Bonus eligibility unavailable: Missing values remain gaps." : data.has_bonus ? "Bonus volume is not applied in this preview." : "No bonus applies in this period."}${data.coverage === "shortened_horizon" ? " Market-price coverage ends early." : ""}`;
        priceRefreshPending = false;
        return true;
    } catch (error) {
        if (request !== priceRequest) return;
        priceRefreshPending = true;
        currentPriceWindow = null;
        $("current-price-chart").closest(".chart-wrap").hidden = true;
        chart("current-price-chart", [], {unit: "EUR/kWh"});
        $("current-price-status").textContent = error.message;
        return false;
    }
}
/** Refresh independent views. Only the plan reader reports success, preserving newer selection errors. */
async function load() {
    overviewLoads++;
    try {
        const signal = AbortSignal.timeout(PLAN_READ_TIMEOUT_MILLISECONDS);
        const results = await Promise.allSettled([plans(false, signal), savings(signal), currentPrices(signal)]);
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
$("plan-version").addEventListener("change", () => showPlan());
$("older-plans").addEventListener("click", () => plans(true).catch((error) => { $("overview-status").textContent = error.message; }));
$("period").addEventListener("submit", (event) => { event.preventDefault(); savings(); });
$("evaluation-version").addEventListener("change", () => savings());
$("overview-refresh").addEventListener("click", load);
$("older-evaluations").addEventListener("click", async () => {
    try {
        const data = await api(`v1/evaluations?before=${encodeURIComponent(evaluationCursor)}`);
        for (const revision of data.versions) evaluationVersions.set(revision.revision, revision);
        renderVersions($("evaluation-version").value);
        evaluationCursor = data.next_evaluation; $("older-evaluations").hidden = !evaluationCursor;
    } catch (error) { $("overview-status").textContent = error.message; }
});
api("setup/session").then((session) => { useHomeAssistantTimezone(session.timezone); $("period-start").value = localDate(); $("period-end").value = localDate(); watchSourceNames(() => api("setup/session"), async () => (await Promise.all([showPlan(), savings()])).every(result => result === true), session.source_names_revision); if (session.ingress) { $("overview-connect").hidden = true; load(); } else $("overview-status").textContent = "Connect to view your plans and savings."; }).catch((error) => { $("overview-status").textContent = error.message; });
