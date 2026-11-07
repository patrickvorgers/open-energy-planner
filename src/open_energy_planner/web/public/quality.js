import "./chart_theme.js";
import {qualityChart} from "./quality-chart.js";
import {when, dateLabel, percentage, useHomeAssistantTimezone, watchSourceNames} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
import {currentModelRows, renderCurrentModel} from "./quality-models.js";
bindSettingsActions();

const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = (id) => document.getElementById(id);
let token = "", cursor = null, requestVersion = 0, historyVersion = 0, charts = [], displayedDay = null;
const quantity = (value) => typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {maximumFractionDigits: 3}) : "Unavailable";
let currentRows = new Map(), indexData = null;
const expandedModels = new Set();
const selectionKey = `forecast-quality-selection:${base}`;
let subject = "household", selections = {}, solarIds = new Set();
try {const saved = JSON.parse(localStorage.getItem(selectionKey)); if (["household", "solar", "devices"].includes(saved?.subject)) subject = saved.subject; if (saved?.selections && typeof saved.selections === "object") selections = saved.selections;} catch { /* Browsers may disable local preferences. */ }
/** Store presentation preferences only, never credentials or source evidence. */
function remember() {try {localStorage.setItem(selectionKey, JSON.stringify({subject, selections}));} catch { /* Selection remains usable without persistence. */ }}

/** Never persist credentials or let an API error display stale evidence as current. */
async function api(path) {
    const response = await fetch(base + path, {headers: token ? {Authorization: `Bearer ${token}`} : {}, redirect: "error", cache: "no-store"});
    if (!response.ok) throw new Error(`Cannot read forecast quality (${response.status}).`);
    return response.json();
}
/** Render all HA/model text through textContent, never HTML. */
function paragraph(parent, value, className = "") {
    const node = document.createElement("p"); node.textContent = value; node.className = className; parent.append(node); return node;
}
/** Dispose previous graphs before presenting a new day or device. */
function clearGraphs() { for (const detail of document.querySelectorAll(".quality-current-model")) { if (detail.open) expandedModels.add(detail.dataset.modelSource); else expandedModels.delete(detail.dataset.modelSource); } for (const chart of charts) chart.destroy(); charts = []; find("quality-charts").replaceChildren(); }
/** Remove stale evidence while a new request is pending or failed. */
function clear() { clearGraphs(); displayedDay = null; find("quality-source").disabled = true; currentRows = new Map(); for (const id of ["quality-models", "quality-policy", "quality-model-issue"]) find(id).replaceChildren(); }

/** Filter the same frozen daily evidence without changing the date or fetching a newer prediction. */
function renderSources() {
    clearGraphs();
    find("quality-models").replaceChildren();
    const selected = find("quality-source").value;
    const sources = (displayedDay?.series || []).filter(source => subjectOf(source) === subject && source.id === selected);
    const inProgress = displayedDay && Date.parse(displayedDay.as_of) < Date.parse(displayedDay.end);
    sources.forEach((source, index) => graph(source, index, inProgress));
    for (const row of currentRows.values()) {
        if (subjectOf(row) !== subject || row.id !== selected || sources.some(source => source.id === row.id)) continue;
        const article = document.createElement("article"), heading = document.createElement("h2");
        article.className = "chart-card"; heading.textContent = row.label; article.append(heading);
        paragraph(article, "Current model status: No midnight graph is available for this device on the selected day.");
        renderCurrentModel(article, row, expandedModels.has(row.id)); find("quality-models").append(article);
    }
    if (!sources.length && displayedDay) paragraph(find("quality-charts"), "No midnight forecast was recorded for this day. New forecasts are captured automatically at the next midnight.");
}

/** Resolve stable source families independently of display labels. */
function subjectOf(source) {return source.scope || (source.id === "household" ? "household" : source.measurement_boundary || source.solarSelection || solarIds.has(source.id) ? "solar" : "devices");}

/** Keep one selection per subject while the calendar day remains shared. */
function devices(data) {
    const input = find("quality-source");
    const available = new Map([...currentRows.values(), ...(data.series || [])].filter(source => subjectOf(source) === subject).map(source => [source.id, source.title || source.label]));
    input.replaceChildren();
    for (const [value, label] of available) {const option = document.createElement("option"); option.value = value; option.textContent = label; input.append(option);}
    input.value = available.has(selections[subject]) ? selections[subject] : available.keys().next().value || "";
    selections[subject] = input.value;
    input.disabled = !available.size;
    find("quality-source-label").hidden = subject === "household";
    find("quality-source-title").textContent = subject === "solar" ? "Solar source" : "Device";
    input.setAttribute("aria-label", find("quality-source-title").textContent);
    for (const button of document.querySelectorAll("#quality-subjects button")) button.setAttribute("aria-pressed", String(button.dataset.subject === subject));
}

/** Plot archived numeric evidence. Null actuals remain gaps instead of measured zeros.
 * Measured bars and forecast lines retain their hourly kWh values. Lines never bridge missing hours.
 * Accessible responsive controls toggle each series independently. Tooltips list each series once.
 * The physical lower bound is zero. Only the upper end receives clearance.
 */
function graph(source, index, inProgress) {
    // Unique instants share occurrence labels across table rows and tooltip endpoints.
    const localHours = new Map(), hourOccurrences = new Map();
    const instants = [...new Set(source.rows.flatMap(row => [Date.parse(row.start), Date.parse(row.end)]))].sort((left, right) => left - right);
    for (const instant of instants) {
        const label = when(instant), hours = localHours.get(label) || [];
        hours.push(instant); localHours.set(label, hours);
    }
    for (const hours of localHours.values()) {
        if (hours.length > 1) hours.forEach((instant, occurrence) => hourOccurrences.set(instant, ` (${occurrence + 1} of ${hours.length})`));
    }
    /** Keep endpoint formatting while identifying repeated local wall times without timezone names. */
    const evidenceTime = (value, options = {}) => when(value, options) + (hourOccurrences.get(Date.parse(value)) || "");
    const article = document.createElement("article"), heading = document.createElement("h2"), wrap = document.createElement("div"), canvas = document.createElement("canvas");
    article.className = "chart-card"; heading.textContent = source.title; article.append(heading);
    const daily = source.daily;
    const totals = document.createElement("dl"); totals.className = "quality-daily quality-summary";
    const complete = daily.measured_kwh != null && !daily.evaluation_issue;
    for (const [label, value] of [
        ["Day forecast", `${quantity(daily.predicted_kwh)} kWh`],
        ["Measured", daily.measured_kwh == null ? "Awaiting complete day" : `${quantity(daily.measured_kwh)} kWh`],
        ["Difference", complete ? `${quantity(daily.error_kwh)} kWh` : "Not scored yet"],
    ]) {
        const item = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd");
        term.textContent = label; description.textContent = value; item.append(term, description); totals.append(item);
    }
    article.append(totals);
    const measuredDay = source.coverage.hours > 0 && source.coverage.measured_hours === source.coverage.hours;
    paragraph(article, !inProgress && measuredDay ? `Complete day: ${source.coverage.measured_hours} of ${source.coverage.hours} hours measured.` : `${inProgress ? "Day in progress" : "Incomplete measurements"}: ${source.coverage.measured_hours} of ${source.coverage.hours} hours measured. Daily accuracy waits for complete comparable evidence.`, "quality-coverage");
    if (daily.evaluation_issue) paragraph(article, daily.evaluation_issue, "balance-warning");
    canvas.id = `quality-chart-${index}`; canvas.setAttribute("role", "img"); canvas.setAttribute("aria-label", `${source.title}: Original predicted and measured hourly energy`);
    const plot = document.createElement("div"); plot.className = "chart-plot";
    wrap.className = "chart-wrap"; wrap.append(canvas); plot.append(wrap); article.append(plot); find("quality-charts").append(article);
    charts.push(qualityChart(canvas, plot, source, displayedDay, evidenceTime));
    const details = document.createElement("details"), summary = document.createElement("summary"), scroll = document.createElement("div"), table = document.createElement("table"), header = document.createElement("thead"), body = document.createElement("tbody");
    summary.textContent = "Hourly evidence and provenance"; details.append(summary);
    paragraph(details, `Original model: ${source.model_label}: Issued: ${when(source.issued_at)}${source.measurement_boundary ? `: Measurement boundary: ${source.measurement_boundary}` : ""}`);
    paragraph(details, `Measured hours: ${source.coverage.measured_hours} of ${source.coverage.hours}: Comparable pairs: ${source.coverage.paired_hours}: MAE: ${quantity(source.metrics.mae_kwh)} kWh/hour: Bias: ${quantity(source.metrics.bias_kwh)} kWh/hour: RMSE: ${quantity(source.metrics.rmse_kwh)} kWh/hour: WAPE: ${percentage(source.metrics.wape_percent)}`, "quality-metrics");
    if (source.comparison_issue && source.comparison_issue !== daily.evaluation_issue) paragraph(details, source.comparison_issue, "balance-warning");
    if (source.weather_sensitive != null) paragraph(details, `Weather influence: ${source.weather_sensitive ? "Requested" : "Not requested"}: Temperature features: ${source.weather_features_used ? "Used" : "Not used"}: Individual forecast: ${source.composition_selected ? "Included in the active household composition" : "Retained as a shadow component"}`);
    if (daily.relative_error_percent != null) paragraph(details, `Relative daily error: ${percentage(daily.relative_error_percent)}`);

    const headerRow = document.createElement("tr"); for (const title of ["Hour starts", "Predicted kWh", "Measured kWh", "Error kWh", "Evaluation"]) { const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = title; headerRow.append(cell); } header.append(headerRow);
    for (const row of source.rows) { const line = document.createElement("tr"); for (const value of [evidenceTime(row.start), quantity(row.predicted_kwh), quantity(row.measured_kwh), quantity(row.error_kwh), row.evaluation_issue || "Eligible"]) { const cell = document.createElement("td"); cell.textContent = value; line.append(cell); } body.append(line); }
    table.append(header, body); scroll.className = "table-scroll"; scroll.append(table); details.append(scroll);
    paragraph(details, `Original forecast version: ${source.forecast_version}`, "quality-version");
    paragraph(details, `Latest measurement receipt: ${source.last_received_at ? when(source.last_received_at) : "Unavailable"}`);
    article.append(details);
    const comparison = document.createElement("div"); comparison.className = "table-scroll quality-comparison"; comparison.tabIndex = 0; comparison.setAttribute("role", "region"); comparison.setAttribute("aria-label", "Original model comparison");
    const modelsTable = document.createElement("table"), caption = document.createElement("caption"); caption.textContent = "Original model comparison"; modelsTable.append(caption);
    const modelHead = document.createElement("tr");
    for (const label of ["Model", "Day forecast (kWh)", "Difference (kWh)", "MAE (kWh/hour)"]) {const cell = document.createElement("th"); cell.scope = "col"; cell.textContent = label; modelHead.append(cell);}
    const modelHeader = document.createElement("thead"); modelHeader.append(modelHead); modelsTable.append(modelHeader);
    const modelBody = document.createElement("tbody");
    for (const [index, model] of [source, ...(source.alternatives || [])].entries()) {const line = document.createElement("tr"); for (const value of [model.model_label + (index ? "" : ": Original choice"), quantity(model.daily?.predicted_kwh), quantity(model.daily?.error_kwh), quantity(model.metrics.mae_kwh)]) {const cell = document.createElement("td"); cell.textContent = value; line.append(cell);} modelBody.append(line);}
    modelsTable.append(modelBody); comparison.append(modelsTable); article.insertBefore(comparison, details);
    const current = currentRows.get(source.id);
    if (current) renderCurrentModel(article, current, expandedModels.has(source.id));
}

async function show() {
    const version = ++requestVersion, day = find("quality-day").value; clear();
    find("quality-status").textContent = "Loading daily forecast evidence…";
    try {
        const [data, selection] = await Promise.all([day ? api(`v1/forecast-quality/days/${encodeURIComponent(day)}`) : Promise.resolve(indexData), api("v1/models")]);
        if (version !== requestVersion) return false;
        useHomeAssistantTimezone(data.timezone); currentRows = currentModelRows(data.current_models, selection);
        find("quality-policy").textContent = selection.explanation;
        find("quality-model-issue").textContent = selection.issue === "model_selection_archive_unreadable" ? "Model selection archive is unreadable. Baseline retained. Inspect application logs." : selection.issue || "";
        solarIds = new Set((data.current_models?.solar?.configuration?.sources || []).map(source => source.source_id));
        displayedDay = day ? data : null; devices({...data, series: data.series || []}); renderSources();
        find("quality-status").textContent = day ? `Day ${dateLabel(data.day)}: Midnight forecast: ${data.captured_at ? "Recorded" : "Unavailable"}: Evidence checked: ${when(data.as_of)}${data.issues.length ? `: ${data.issues.join(": ")}` : ""}` : currentRows.size ? "No daily forecasts available yet. Current model status is shown below." : "No daily forecasts or current model status available yet.";
        return true;
    } catch (error) { if (version === requestVersion) { clear(); find("quality-status").textContent = error.message; } return false; }
}
async function load(older = false) {
    const version = ++historyVersion;
    ++requestVersion;
    clear();
    find("quality-status").textContent = "Loading forecast days…";
    try {
        const page = await api("v1/forecast-quality/days" + (older && cursor ? `?before=${encodeURIComponent(cursor)}` : ""));
        if (version !== historyVersion) return;
        const previous = find("quality-day").value;
        const days = new Set([...page.days, ...(older ? [...find("quality-day").options].map((option) => option.value) : []), ...(previous ? [previous] : [])]);
        find("quality-day").replaceChildren();
        for (const day of [...days].sort().reverse()) { const option = document.createElement("option"); option.value = day; option.textContent = dateLabel(day); find("quality-day").append(option); }
        indexData = page;
        cursor = page.next_before; find("quality-older").disabled = !cursor; find("quality-older").hidden = !cursor; find("quality-day").disabled = !find("quality-day").options.length;
        if ([...find("quality-day").options].some((option) => option.value === previous)) find("quality-day").value = previous;
        await show();
    } catch (error) { if (version === historyVersion) { clear(); find("quality-status").textContent = error.message; } }
}
find("connect").onsubmit = (event) => { event.preventDefault(); token = find("token").value; load(); };
find("quality-source").onchange = () => {selections[subject] = find("quality-source").value; remember(); renderSources();};
for (const button of document.querySelectorAll("#quality-subjects button")) button.onclick = () => {subject = button.dataset.subject; devices(displayedDay || {}); remember(); renderSources();};
find("quality-day").onchange = show; find("quality-refresh").onclick = () => load(); find("quality-older").onclick = () => load(true);
fetch(base + "setup/session", {redirect: "error"}).then((response) => response.json()).then((session) => { useHomeAssistantTimezone(session.timezone); watchSourceNames(() => api("setup/session"), show, session.source_names_revision); find("connect").hidden = Boolean(session.ingress); if (session.ingress) load(); }).catch((error) => { find("quality-status").textContent = error.message; });
