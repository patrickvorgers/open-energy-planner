import "./chart_theme.js";
import {money, percentage, when, optionLabel, useHomeAssistantTimezone} from "./display.js";
import {profileMetrics} from "./appliance-profile.js";
import {renderRawHistory} from "./appliance-raw-profile.js";
import {bindSettingsActions} from "./settings-actions.js";
import {pollApplianceEvidence} from "./appliance-evidence.js";
import {applianceApi, readApplianceEvidence, followedDevices} from "./appliance-data.js";
bindSettingsActions();
const find = id => document.getElementById(id);
let token = "", chart, energyChart, current, renderedProfile;
let evidenceRefreshing = false, rawLoadingKey, rawLoaded;
let historyPage = 0, historyEntity, renderedHistory, historyTimezone;
const requestedEntity = new URLSearchParams(location.hash.slice(1)).get("entity");
const api = (path, body, method) => applianceApi(token, path, body, method);
const readEvidence = () => readApplianceEvidence(token);
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Replace both learned-cycle charts. Each bar integrates one declared profile phase in kWh.
 * The physical lower bound is zero. Only the upper end receives clearance.
 * The power chart plots AC power in W. The energy chart plots kWh per declared phase, preserving five-minute evidence.
 * With no eligible device, destroy both charts and clear their references.
 */
function profile() {
    if (!current.devices.length) { chart?.destroy(); energyChart?.destroy(); chart = energyChart = renderedProfile = null; renderRawHistory(null); return; }
    const device = current.devices.find((item) => item.entity_id === find("profile-device").value), learned = device?.profile;
    if (rawLoaded?.entity_id === device?.entity_id && rawLoaded?.power_entity === device?.power_entity && rawLoaded?.raw_history?.curve_revision === device?.raw_history?.curve_revision) device.raw_history = rawLoaded.raw_history;
    renderRawHistory(device);
    if (device?.raw_history && !device.raw_history.power_curve) {
        const key = JSON.stringify([device.entity_id, device.raw_history.curve_revision]);
        if (rawLoadingKey !== key) {
            rawLoadingKey = key;
            api(`v1/appliances?raw_entity=${encodeURIComponent(device.entity_id)}`).then(data => {
                const detail = data.devices.find(item => item.entity_id === device.entity_id);
                const retained = current.devices.find(item => item.entity_id === device.entity_id);
                if (data.revision === current.revision && find("profile-device").value === device.entity_id && retained?.power_entity === detail?.power_entity && retained?.raw_history?.curve_revision === detail?.raw_history?.curve_revision) {
                    rawLoaded = detail;
                    retained.raw_history = detail.raw_history;
                    renderRawHistory(retained);
                }
            }).catch(() => { if (find("profile-device").value === device.entity_id) find("recorded-cycle-status").textContent = "Recorded state details unavailable. The learned profile is retained."; })
                .finally(() => { if (rawLoadingKey === key) rawLoadingKey = null; });
        }
    }
    const metrics = learned ? profileMetrics(learned) : null, powers = metrics?.powerW || [], labels = metrics?.labels || powers.map((_, index) => `${index * metrics.phaseMinutes}–${Math.min((index + 1) * metrics.phaseMinutes, metrics.minutes)} min`);
    const profileKey = JSON.stringify([device?.entity_id, learned]);
    find("cycle-charts").hidden = !learned;
    if (profileKey !== renderedProfile) {
        chart?.destroy(); energyChart?.destroy(); chart = energyChart = null;
        if (learned) {
            const timedPower = metrics.variable ? [...powers.map((y, index) => ({x: metrics.edgesMinutes[index], y})), {x: metrics.minutes, y: powers.at(-1)}] : powers;
            chart = new Chart(find("cycle-chart"), {type: "line", data: {labels, datasets: [{label: "Power (W)", data: timedPower, backgroundColor: "#e69f00", borderColor: "#e69f00", stepped: true, pointRadius: 2, fill: false}]}, options: {responsive: true, maintainAspectRatio: false, scales: {...(metrics.variable ? {x: {type: "linear", title: {display: true, text: "Elapsed minutes"}}} : {}), y: {beginAtZero: false, min: 0, title: {display: true, text: "AC power (W)"}}}}});
            energyChart = new Chart(find("cycle-energy-chart"), {type: "bar", data: {labels, datasets: [{label: "Energy (kWh)", data: metrics.phaseEnergy, backgroundColor: "#e69f00"}]}, options: {responsive: true, maintainAspectRatio: false, scales: {y: {beginAtZero: false, min: 0, title: {display: true, text: metrics.variable ? "Energy per actual phase (kWh)" : `Energy per ${metrics.phaseMinutes}-minute interval (kWh)`}}}}});
        }
        renderedProfile = profileKey;
    }
    find("profile-evidence").textContent = learned ? (learned.evidence === "hourly_estimate" ? `Profile: Historical estimate: Estimated cycles: ${learned.samples}: Energy: ${profileMetrics(learned).energyKwh.toFixed(2)} kWh: Cycle time: ${profileMetrics(learned).minutes} minutes: Provisional hourly shape${device.hourly_synthetic ? ": Simulated data" : ""}.` : `Profile: ${learned.evidence === "manual" ? "Manual estimate" : "Learned"}: Completed cycles: ${learned.evidence === "manual" ? 0 : learned.samples}: Evidence: ${optionLabel(learned.evidence)}: Energy: ${(profileMetrics(learned).energyKwh).toFixed(2)} kWh: Cycle time: ${profileMetrics(learned).minutes} minutes: Zero-power phases remain part of the cycle.`) : "Profile not ready. Wait for automatic learning or enter duration and energy in Appliances settings.";
    if (historyEntity !== device?.entity_id) { historyPage = 0; historyEntity = device?.entity_id; }
    const cycles = [...(device?.recent_cycles || [])].reverse();
    historyPage = Math.min(historyPage, Math.max(0, Math.ceil(cycles.length / 25) - 1));
    const historyKey = JSON.stringify([device, historyPage, historyTimezone, when("2000-01-01T00:00:00Z")]);
    if (historyKey === renderedHistory) return;
    renderedHistory = historyKey;
    const history = find("profile-history"); history.replaceChildren();
    history.append(node("h3", "Detected cycles"));
    if (!cycles.length) history.append(node("p", "No completed measured cycles available. Previously learned profiles remain available."));
    else {
        const pagination = node("nav"), previous = node("button", "Previous"), next = node("button", "Next"), page = node("span", `Page ${historyPage + 1}`);
        pagination.className = "activity-pagination"; pagination.setAttribute("aria-label", "Detected cycle pages"); page.setAttribute("role", "status");
        for (const button of [previous, next]) { button.type = "button"; button.className = "secondary"; button.setAttribute("aria-controls", "detected-cycles"); }
        previous.disabled = historyPage === 0; next.disabled = (historyPage + 1) * 25 >= cycles.length;
        previous.onclick = () => { historyPage--; profile(); document.querySelector("#profile-history button:not(:disabled)").focus(); };
        next.onclick = () => { historyPage++; profile(); document.querySelector("#profile-history button:not(:disabled)").focus(); };
        pagination.append(previous, page, next); history.append(pagination);
        const list = node("ul"); list.id = "detected-cycles"; list.className = "activity-list";
        for (const cycle of cycles.slice(historyPage * 25, (historyPage + 1) * 25)) {
            const minutes = Math.round((new Date(cycle.completed_at) - new Date(cycle.started_at)) / 60000);
            const row = node("li"), detail = node("details"), summary = node("summary", `${when(cycle.started_at)}: ${minutes} minutes: ${money(cycle.energy_kwh)} kWh${cycle.synthetic ? ": Simulated" : ""}`);
            detail.className = "activity-row"; detail.append(summary, node("p", `Completed: ${when(cycle.completed_at)}`)); row.append(detail); list.append(row);
        }
        history.append(list);
    }
    if (device?.learning_half_life_days) history.append(node("p", `Recency weighting: ${device.learning_half_life_days}-day half-life. A cycle ${device.learning_half_life_days} days older has half the weight. Fine timing refines hourly evidence. Each detected run counts once.`));
    if (device?.hourly_synthetic) history.append(node("p", "Hourly evidence: Simulated data"));
    const evidenceCard = node("div"), facts = node("dl"); evidenceCard.className = "evidence-card";
    evidenceCard.append(node("h3", "Model and usable history"), facts);
    const addFact = (label, value) => facts.append(node("dt", label), node("dd", value));
    for (const [label, value] of [["Fine cycles in model", device?.learned_profile?.samples ?? 0], ["Hourly estimated cycles", device?.hourly_profile?.samples ?? 0]]) addFact(label, String(value));
    for (const [label, value] of [["Fine cycle weight", device?.learning_weight], ["Hourly cycle weight", device?.hourly_weight]]) if (value != null) addFact(label, `${money(value)} at latest cycle`);
    const recent = device?.learning_history;
    if (recent?.start) {
        addFact("Recent fine history received", `${when(recent.start)} to ${when(recent.end)}`);
        addFact("Usable fine intervals", `${recent.usable_intervals} of ${recent.expected_intervals}: ${percentage(recent.expected_intervals ? 100 * recent.usable_intervals / recent.expected_intervals : null)}`);
    }
    const observation = node("div"); observation.className = "evidence-card";
    observation.append(node("h3", "Recent observations"), node("p", device?.recent_cycles?.length ? `${device.recent_cycles.length} completed measured cycles in the available recent history.` : "No completed measured cycles in the available recent history. This does not discard the retained model."));
    history.prepend(observation, evidenceCard);
    if (metrics?.variable) history.append(node("p", "Recorded state timing: This learned profile preserves actual irregular phase durations. It does not imply a fixed measurement frequency."));
    if (device?.planning_issue) history.append(node("p", device.planning_issue));
    if (learned && learned.evidence !== "manual" && learned.samples === 1) history.append(node("p", "Provisional profile: One complete cycle. Further cycles refine this estimate."));
    if (device?.profile_basis === "statistics" && learned && !["manual", "hourly_estimate"].includes(learned.evidence)) history.append(node("p", "Provisional statistical profile: Reconstructed from historical interval data. The energy counter supplies the total. Historical data cannot prove continuous reporting."));
    if (device?.profile_basis === "observed") history.append(node("p", "Continuously followed profile: Complete cycles with current power reports refine this shape. Historical statistics cannot replace it."));
    const run = device?.observed_run;
    if (run && !["complete", "idle"].includes(run.status)) history.append(node("p", `Measured activity: ${run.status === "observed" ? "Cycle in progress" : "Execution uncertain"}: Started ${when(run.started_at)}: ${money(run.energy_kwh)} kWh measured. Pending starts are blocked. This is separate from execution confirmation.`));
    const fine = device?.fine_import;
    if (fine) history.append(node("p", `Fine history: ${optionLabel(fine.phase)}${fine.cursor ? `: Examined back to ${when(fine.cursor)}` : ""}`));
    const scan = device?.history_import, status = node("p"); status.setAttribute("role", "status");
    const phases = {discovering: "Discovering available hourly history…", processing: "Processing hourly history", complete: "Historical scan complete", unavailable: "No paired hourly energy and power history available", failed: "History processing interrupted. Retained progress will resume automatically"};
    status.textContent = `Historical learning: ${phases[scan?.phase] || (device?.power_entity ? "Waiting for background processing" : "Power sensor required")}`;
    history.append(status);
    if (scan?.start && scan?.end) {
        const total = Date.parse(scan.end) - Date.parse(scan.start), processed = Date.parse(scan.cursor) - Date.parse(scan.start);
        history.append(node("p", `Examined through ${when(scan.cursor)}: Target: ${when(scan.start)} to ${when(scan.end)}`));
        if (scan.phase === "processing") {
            const progress = node("progress"); progress.max = total; progress.value = processed; progress.setAttribute("aria-label", "Hourly history progress");
            history.append(progress, node("span", ` ${percentage(100 * processed / total)}`));
        }
        if (scan.expected_intervals) history.append(node("p", `Usable hourly intervals: ${scan.usable_intervals} of ${scan.expected_intervals}: ${percentage(100 * scan.usable_intervals / scan.expected_intervals)}`));
    }
    if (metrics?.sourcePhaseMinutes > metrics.phaseMinutes) history.append(node("p", learned?.evidence === "hourly_estimate" ? "Five-minute display: Hourly power is spread uniformly. Each energy bar estimates one twelfth of its hour. Fine measurements refine this shape later." : "Five-minute display: Coarser mean power is spread uniformly. These are estimates within each original measurement interval."));
    if (learned?.evidence === "hourly_estimate") history.append(node("p", "Provisional planning profile: Hourly history cannot establish exact start times, separate runs within one hour or short power peaks. The first complete fine cycle refines this estimate automatically."));
}
/** Preserve the selected profile and selector focus when only read-only evidence changes. */
function showProfiles() {
    find("appliance-profiles").hidden = !current.devices.length;
    const selector = find("profile-device"), options = current.devices.map(device => [device.entity_id, device.name]);
    if (JSON.stringify([...selector.options].map(option => [option.value, option.textContent])) !== JSON.stringify(options)) {
        const selected = selector.value || requestedEntity; selector.replaceChildren();
        for (const [id, name] of options) { const option = node("option", name); option.value = id; selector.append(option); }
        if (current.devices.some(device => device.entity_id === selected)) selector.value = selected;
    }
    profile();
}
/** Refresh retained evidence without replacing a selected device or unchanged charts. */
async function load() {
    if (evidenceRefreshing) return;
    evidenceRefreshing = true;
    try {
        const [data, sources, session] = await readEvidence();
        const devices = followedDevices(data, sources);
        useHomeAssistantTimezone(session.timezone);
        historyTimezone = session.timezone || "UTC";
        current = {...data, devices};
        find("connect").hidden = true;
        find("status").textContent = data.issue || "Connected";
        find("cycle-empty").hidden = Boolean(devices.length);
        showProfiles();
    } catch { find("status").textContent = current ? "Learning evidence refresh unavailable. Retained evidence is shown." : "Appliance information could not be loaded. Check the connection and try Refresh."; }
    finally { evidenceRefreshing = false; }
}
pollApplianceEvidence(() => { if (current && !document.hidden) return load(); });
find("profile-device").onchange = () => {
    const address = new URL(location.href); address.hash = new URLSearchParams({entity: find("profile-device").value}).toString();
    location.hash = address.hash; profile();
};
window.addEventListener("hashchange", () => {
    const selected = new URLSearchParams(location.hash.slice(1)).get("entity");
    if (current?.devices.some(device => device.entity_id === selected)) { find("profile-device").value = selected; profile(); }
});
find("refresh").onclick = load;
find("connect").onsubmit = event => { event.preventDefault(); token = find("token").value; load(); };
find("connect").querySelector("button").disabled = false;
api("setup/session").then(session => { if (session.ingress) load(); }).catch(() => { find("status").textContent = "Session information could not be loaded. Refresh this page and try again."; });
