/** Thermal monitoring and daily intent reuse the existing app and HA contracts. */
import {when, useHomeAssistantTimezone} from "./display.js";
import "./chart_theme.js";
import {calendarDays, calendarTime} from "./chart_days.js";
import {bindSettingsActions} from "./settings-actions.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = id => document.getElementById(id);
const EVIDENCE_REFRESH_MILLISECONDS = 30 * 1000;
const QUARTER_MILLISECONDS = 15 * 60 * 1000;
let requestGeneration = 0;
let token = "", current = null, selectedId = null, chart = null, busy = false, minimumDirty = false, minimum = null;
/** Read original evidence or submit one explicit intent through the authorized app. */
async function api(path, body) {
    const response = await fetch(base + path, {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store"});
    if (!response.ok) throw new Error(await response.text());
    return response.json();
}
/** Source names and diagnostics are text, never markup. */
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Unknown measurements remain unavailable. Temperature displays retain one decimal. */
function decimal(value, digits = 1) { return Number.isFinite(value) ? value.toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits}) : "Unavailable"; }
/** Send one daily action, keeping failed input and never claiming physical execution. */
async function intent(field, value) {
    if (busy || !selectedId) return;
    busy = true;
    requestGeneration += 1;
    const controls = [...find("thermal-controls").querySelectorAll("input, button"), find("thermal-store-select")];
    controls.forEach(control => { control.disabled = true; });
    try { await api("v1/thermal/intent", {device_id: selectedId, field, value}); if (field === "minimum_c") minimumDirty = false; await load(); find("status").textContent = "Request saved. Home Assistant determines physical operation."; }
    catch (error) { find("status").textContent = error.message; }
    finally { busy = false; controls.forEach(control => { control.disabled = false; }); }
}
/** Refresh measurements and the retained plan without replacing an unapplied comfort request. */
async function load() {
    const generation = ++requestGeneration;
    const data = await api("v1/thermal");
    if (generation !== requestGeneration) return;
    current = data; useHomeAssistantTimezone(data.timezone || data.catalogue?.timezone);
    if (!data.devices.some(device => device.id === selectedId)) { selectedId = data.devices[0]?.id || null; minimumDirty = false; }
    const selector = find("thermal-store-select"); selector.replaceChildren();
    for (const device of data.devices) { const option = node("option", device.name); option.value = device.id; selector.append(option); }
    selector.value = selectedId || ""; selector.disabled = busy || !data.devices.length;
    find("connect").hidden = true; find("thermal-monitor").hidden = false;
    renderSelection();
}
/** Keep the displayed boiler and intent target aligned before any asynchronous refresh. */
function renderSelection() {
    const device = current.devices.find(device => device.id === selectedId);
    find("thermal-title").textContent = device?.name || "Electric boiler";
    find("thermal-empty").hidden = Boolean(device); find("thermal-controls").hidden = !device;
    find("status").textContent = current.issue || "";
    if (device) {
        find("thermal-toggle").textContent = device.enabled ? "Pause thermal planning" : "Enable thermal planning";
        const target = find("comfort-target"); target.max = device.maximum_c;
        if (!minimumDirty) {
            minimum = device.minimum_c;
            const value = document.activeElement === target ? String(minimum) : minimum.toFixed(1);
            if (target.value !== value) target.value = value;
        }
    }
    renderEvidence();
}
/** Separate measured temperatures and power from requests and predicted comfort. */
function renderEvidence() {
    const parent = find("thermal-evidence"); parent.replaceChildren();
    const device = current?.devices?.find(candidate => candidate.id === selectedId);
    if (!device) { parent.append(node("p", "No configured store is selected.")); renderChart(null); return; }
    if (device.issue) parent.append(node("p", device.issue));
    parent.append(node("p", `Measured temperature: ${decimal(device.actual?.temperature_c)} °C`));
    parent.append(node("p", `Measured heater power: ${decimal(Number.isFinite(device.actual?.power_w) ? device.actual.power_w / 1000 : null, 2)} kW`));
    parent.append(node("p", device.actual?.observed_at ? `HA report: ${when(device.actual.observed_at)}` : "No fresh measurement available."));
    const now = Date.now(), active = (device.planned || []).find(row => Date.parse(row.start) <= now && now < Date.parse(row.end));
    parent.append(node("p", active ? `Planned heating request: ${active.power_w > 0 ? "On" : "Off"}` : "No current heating request."));
    const deficits = (device.planned || []).map(row => row.shortfall_kwh).filter(Number.isFinite);
    parent.append(node("p", deficits.length ? `Largest predicted comfort deficit: ${decimal(Math.max(...deficits), 3)} thermal kWh` : "No current predicted comfort evidence."));
    parent.append(node("p", `Unserved hot-water heat demand: ${decimal(device.unmet_draw_kwh, 3)} thermal kWh`));
    parent.append(node("p", `Ending stock target: ${decimal(device.ending_target_c)} °C`));
    parent.append(node("p", `Predicted ending stock deficit: ${decimal(device.ending_stock_shortfall_kwh, 3)} thermal kWh`));
    renderChart(device);
}

/** Render planned interval power and temperature using the bundled interactive chart engine. */
function renderChart(device) {
    const rows = device?.planned || [], visible = rows.length > 0;
    find("thermal-chart-card").hidden = !visible;
    if (!visible) { if (chart) chart.destroy(); chart = null; return; }
    const points = rows.map(row => ({start: Date.parse(row.start), end: Date.parse(row.end)}));
    const datasets = [
        {type: "bar", label: "Electrical heating power", yAxisID: "y", backgroundColor: "#e2ab35", data: rows.map((row, index) => ({...points[index], x: (points[index].start + points[index].end) / 2, y: row.power_w / 1000}))},
        {type: "line", label: "Predicted water temperature", yAxisID: "temperature", borderColor: "#21836d", pointRadius: 0, stepped: "after", data: rows.map((row, index) => ({x: points[index].end, y: row.end_c}))},
    ];
    const temperatures = rows.map(row => row.end_c).filter(Number.isFinite);
    const x = {type: "linear", min: points[0].start, max: points.at(-1).end, afterBuildTicks: scale => {
        const first = Math.ceil(scale.min / QUARTER_MILLISECONDS), last = Math.floor(scale.max / QUARTER_MILLISECONDS), stride = Math.max(1, Math.ceil((last - first) / 7)); scale.ticks = [];
        for (let quarter = first; quarter <= last; quarter += stride) scale.ticks.push({value: quarter * QUARTER_MILLISECONDS});
    }, ticks: {callback: calendarTime}};
    const options = {responsive: true, maintainAspectRatio: false, parsing: false, animation: false, interaction: {mode: "index", intersect: false}, scales: {x, y: {min: 0, title: {display: true, text: "Electrical power (kW)"}}, temperature: {position: "right", min: Math.max(0, Math.min(device.minimum_c, ...temperatures) - 2), max: Math.max(device.maximum_c, ...temperatures) + 2, grid: {drawOnChartArea: false}, title: {display: true, text: "Water temperature (°C)"}, ticks: {callback: value => decimal(value)}}}, plugins: {tooltip: {bodyFont: {family: "monospace"}, callbacks: {title: items => `${when(rows[items[0].dataIndex].start, {timeZoneName: "short"})} to ${when(rows[items[0].dataIndex].end, {hour: "2-digit", minute: "2-digit", timeZoneName: "short"})}`, label: item => `${item.dataset.label.padEnd(29)} ${decimal(item.parsed.y, item.dataset.yAxisID === "temperature" ? 1 : 2).padStart(8)} ${item.dataset.yAxisID === "temperature" ? "°C" : "kW"}`}}}};
    if (chart) { chart.data.datasets = datasets; chart.options = options; chart.update("none"); }
    else chart = new Chart(find("thermal-plan-chart"), {type: "bar", data: {datasets}, plugins: [calendarDays], options});
    find("thermal-plan-summary").textContent = `${device.name}: ${rows.length} planned intervals: Comfort target: ${decimal(device.minimum_c)} °C: Maximum planning temperature: ${decimal(device.maximum_c)} °C`;
}


find("comfort-target").onfocus = () => { if (minimum !== null) find("comfort-target").value = String(minimum); find("comfort-target").select(); };
find("comfort-target").oninput = event => { minimum = event.target.value === "" ? null : Number(event.target.value); minimumDirty = true; };
find("comfort-target").onblur = () => { if (Number.isFinite(minimum)) find("comfort-target").value = minimum.toFixed(1); };
find("comfort-request").onsubmit = event => { event.preventDefault(); intent("minimum_c", minimum); };
find("thermal-toggle").onclick = () => intent("enabled", !current.devices.find(device => device.id === selectedId).enabled);
find("reset-comfort").onclick = () => { minimumDirty = false; renderSelection(); load().catch(error => { find("status").textContent = error.message; }); };
find("thermal-store-select").onchange = event => { selectedId = event.target.value; minimumDirty = false; renderSelection(); load().catch(error => { find("status").textContent = error.message; }); };
find("refresh-thermal-evidence").onclick = () => !busy && load().catch(error => { find("status").textContent = error.message; });
find("connect").onsubmit = event => { event.preventDefault(); token = find("token").value; load().catch(error => { find("status").textContent = error.message; }); };
fetch(base + "setup/session", {redirect: "error", cache: "no-store"}).then(response => response.json()).then(session => { useHomeAssistantTimezone(session.timezone); if (session.ingress) return load(); }).catch(error => { find("status").textContent = error.message; });
setInterval(() => { if (current && !busy) load().catch(error => { find("status").textContent = error.message; }); }, EVIDENCE_REFRESH_MILLISECONDS);
