import "./chart_theme.js";
import {confirmAction} from "./confirmation.js";
import {money, percentage, when, localInstant, optionLabel, useHomeAssistantTimezone} from "./display.js";
import {PROFILE_MINUTES, WATT_PHASES_PER_KWH, profileMetrics} from "./appliance-profile.js";
import {bindSettingsActions} from "./settings-actions.js";
import {identity} from "./identity.js";
import {pollApplianceEvidence} from "./appliance-evidence.js";
bindSettingsActions();
/** Task UI uses the existing authenticated app boundary. Physical execution remains in HA. */
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = (id) => document.getElementById(id);
let token = "", chart, energyChart, current, consumers, renderedProfile;
let evidenceRefreshing = false;
const READ_TIMEOUT_MILLISECONDS = 30000;
/** Bound authenticated GETs through response decoding. Preserve write acknowledgement and HTTP status handling. */
async function api(path, body, method = body ? "POST" : "GET") {
    const response = await fetch(base + path, {method, headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store", signal: method === "GET" ? AbortSignal.timeout(READ_TIMEOUT_MILLISECONDS) : undefined});
    if (!response.ok) { const error = new Error(await response.text()); error.status = response.status; throw error; }
    return response.json();
}
/** Settle every related read before callers release their refresh guard or report a failure. */
async function readEvidence() {
    const results = await Promise.allSettled([api("v1/appliances"), api("v1/consumers"), api("setup/session")]);
    if (results.some(result => result.status === "rejected")) throw new Error();
    return results.map(result => result.value);
}
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Replace both learned-cycle charts. Each bar integrates one PROFILE_MINUTES phase in kWh.
 * The physical lower bound is zero. Only the upper end receives clearance.
 * The power chart plots AC power in W. The energy chart plots kWh per quarter-hour phase.
 * With no eligible device, destroy both charts and clear their references.
 */
function profile() {
    if (!current.devices.length) { chart?.destroy(); energyChart?.destroy(); chart = energyChart = renderedProfile = null; return; }
    const device = current.devices.find((item) => item.entity_id === find("profile-device").value), learned = device?.profile;
    const powers = learned?.power_w || [], labels = powers.map((_, index) => `${index * PROFILE_MINUTES}–${(index + 1) * PROFILE_MINUTES} min`);
    const profileKey = JSON.stringify([device?.entity_id, learned]);
    find("cycle-charts").hidden = !learned;
    if (profileKey !== renderedProfile) {
        chart?.destroy(); energyChart?.destroy(); chart = energyChart = null;
        if (learned) {
            chart = new Chart(find("cycle-chart"), {type: "line", data: {labels, datasets: [{label: "Power (W)", data: powers, backgroundColor: "#e69f00", borderColor: "#e69f00", cubicInterpolationMode: "monotone", pointRadius: 2, fill: false}]}, options: {responsive: true, maintainAspectRatio: false, scales: {y: {beginAtZero: false, min: 0, title: {display: true, text: "AC power (W)"}}}}});
            energyChart = new Chart(find("cycle-energy-chart"), {type: "bar", data: {labels, datasets: [{label: "Energy (kWh)", data: powers.map(power => power / WATT_PHASES_PER_KWH), backgroundColor: "#e69f00"}]}, options: {responsive: true, maintainAspectRatio: false, scales: {y: {beginAtZero: false, min: 0, title: {display: true, text: "Energy per quarter-hour (kWh)"}}}}});
        }
        renderedProfile = profileKey;
    }
    find("profile-evidence").textContent = learned ? (learned.evidence === "hourly_estimate" ? `Profile: Historical estimate: Estimated cycles: ${learned.samples}: Energy: ${profileMetrics(learned).energyKwh.toFixed(2)} kWh: Cycle time: ${profileMetrics(learned).minutes} minutes: Provisional hourly shape${device.hourly_synthetic ? ": Simulated data" : ""}.` : `Profile: ${learned.evidence === "manual" ? "Manual estimate" : "Learned"}: Completed cycles: ${learned.evidence === "manual" ? 0 : learned.samples}: Evidence: ${optionLabel(learned.evidence)}: Energy: ${(profileMetrics(learned).energyKwh).toFixed(2)} kWh: Cycle time: ${profileMetrics(learned).minutes} minutes: Zero-power phases remain part of the cycle.`) : "Profile not ready. Wait for automatic learning or enter duration and energy in Appliances settings.";
    const history = find("profile-history"); history.replaceChildren();
    history.append(node("h3", "Detected fine cycles"));
    if (!device?.recent_cycles?.length) history.append(node("p", "No completed measured cycles available."));
    else {
        const list = node("ul");
        for (const cycle of [...device.recent_cycles].reverse()) {
            const minutes = Math.round((new Date(cycle.completed_at) - new Date(cycle.started_at)) / 60000);
            list.append(node("li", `${when(cycle.started_at)}: ${minutes} minutes: ${money(cycle.energy_kwh)} kWh${cycle.synthetic ? ": Simulated" : ""}`));
        }
        history.append(list);
    }
    if (device?.learning_half_life_days) history.append(node("p", `Recency weighting: ${device.learning_half_life_days}-day half-life. A cycle ${device.learning_half_life_days} days older has half the weight. Fine and hourly models stay separate.`));
    if (device?.hourly_synthetic) history.append(node("p", "Hourly evidence: Simulated data"));
    const evidenceCard = node("div"), facts = node("dl"); evidenceCard.className = "evidence-card";
    evidenceCard.append(node("h4", "Model and usable history"), facts);
    const addFact = (label, value) => facts.append(node("dt", label), node("dd", value));
    for (const [label, value] of [["Fine cycles in model", device?.learned_profile?.samples ?? 0], ["Hourly estimated cycles", device?.hourly_profile?.samples ?? 0]]) addFact(label, String(value));
    for (const [label, value] of [["Fine cycle weight", device?.learning_weight], ["Hourly cycle weight", device?.hourly_weight]]) if (value != null) addFact(label, `${money(value)} at latest cycle`);
    const recent = device?.learning_history;
    if (recent?.start) {
        addFact("Recent fine history received", `${when(recent.start)} to ${when(recent.end)}`);
        addFact("Usable fine intervals", `${recent.usable_intervals} of ${recent.expected_intervals}: ${percentage(recent.expected_intervals ? 100 * recent.usable_intervals / recent.expected_intervals : null)}`);
    }
    history.append(evidenceCard);
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
    if (learned?.evidence === "hourly_estimate") history.append(node("p", "Provisional planning profile: Hourly history cannot establish exact start times, separate runs within one hour or short power peaks. Two complete fine cycles replace this estimate automatically."));
}
/** Limit profile inspection to followed cycle devices, including those still scanning without a profile or shift permission. */
function followedDevices(data) {
    const specialized = new Set([...consumers.charging_meters, ...consumers.thermal_meters]);
    const selected = new Set(consumers.selected.map(item => item.entity_id));
    return data.devices.filter(device => (device.shiftable || device.profile || device.recent_cycles?.length || device.history_import || device.learning_history) && selected.has(device.entity_id) && !specialized.has(device.entity_id));
}
/** Preserve the selected profile and selector focus when only read-only evidence changes. */
function showProfiles() {
    find("appliance-profiles").hidden = !current.devices.length;
    const selector = find("profile-device"), options = current.devices.map(device => [device.entity_id, device.name]);
    if (JSON.stringify([...selector.options].map(option => [option.value, option.textContent])) !== JSON.stringify(options)) {
        const selected = selector.value; selector.replaceChildren();
        for (const [id, name] of options) { const option = node("option", name); option.value = id; selector.append(option); }
        if (current.devices.some(device => device.entity_id === selected)) selector.value = selected;
    }
    profile();
}
/** Refresh learning evidence and its HA timezone. Keep drafts and unchanged charts, and retain previous evidence on failure. */
async function refreshEvidence() {
    if (!current || evidenceRefreshing || document.hidden) return;
    evidenceRefreshing = true;
    const previous = current;
    try {
        const [data, sources, session] = await readEvidence();
        if (current !== previous) return;
        useHomeAssistantTimezone(session.timezone);
        consumers = sources;
        current = {...data, devices: followedDevices(data)};
        for (const card of document.querySelectorAll("[data-appliance-entity]")) {
            const source = consumers.options.find(row => row.entity_id === card.dataset.applianceEntity);
            if (source) card.querySelector("h2").textContent = source.name;
        }
        showProfiles();
        if (find("status").textContent === "Learning evidence refresh unavailable. Retained evidence is shown.") find("status").textContent = data.issue || "Connected: A schedule never proves physical execution.";
    } catch { find("status").textContent = "Learning evidence refresh unavailable. Retained evidence is shown."; }
    finally { evidenceRefreshing = false; }
}
pollApplianceEvidence(refreshEvidence);
/**
 * Show selected cycle appliances, excluding persisted charging and thermal meters.
 * Retain the profile selection only while available. Read failures leave existing evidence visible and report status.
 */
async function load() {
    try {
        const [data, sources, session] = await readEvidence();
        if (!Array.isArray(sources.charging_meters) || !Array.isArray(sources.thermal_meters)) throw new Error("Specialized-meter membership unavailable. Refresh before inspecting appliances.");
        useHomeAssistantTimezone(session.timezone);
        consumers = sources;
        current = {...data, devices: followedDevices(data)};
        find("connect").hidden = true; find("status").textContent = data.issue || "Connected: A schedule never proves physical execution."; find("appliance-list").replaceChildren();
        const permitted = current.devices.filter(device => device.shiftable);
        find("appliance-empty").hidden = Boolean(permitted.length); find("appliance-profiles").hidden = !current.devices.length;
        for (const option of consumers.options.filter((item) => permitted.some(device => device.entity_id === item.entity_id))) {
            const device = current.devices.find((item) => item.entity_id === option.entity_id), task = device?.task;
            const card = node("section"); card.className = "chart-card"; card.dataset.applianceEntity = option.entity_id; card.append(node("h2", option.name));
            card.append(node("p", device?.profile ? `Average cycle: ${(profileMetrics(device.profile).energyKwh).toFixed(2)} kWh, ${profileMetrics(device.profile).minutes} minutes.` : "Waiting for a completed measured cycle."));
            card.append(node("p", `Task: ${task ? task.status.replaceAll("_", " ") : "None"}.`));
            if (task) {
                card.append(node("p", `Finish before: ${when(task.deadline)}: Planned start: ${task.scheduled_start ? when(task.scheduled_start) : "Not scheduled"}.`));
                if (task.attempt) card.append(node("p", `Execution: ${optionLabel(task.attempt.status)}: Start request expires: ${when(task.attempt.expires_at)}.`));
            }
            const form = node("form"), deadline = node("input"), deadlineLabel = node("label", "Finish before (Home Assistant time)"); deadline.type = "datetime-local"; deadline.required = true; deadlineLabel.append(deadline); form.append(deadlineLabel);
            const submit = node("button", "Appliance is ready"); submit.disabled = !device?.shiftable || !device?.profile || Boolean(task && !["completed", "cancelled"].includes(task.status)); form.append(submit); card.append(form);
            form.onsubmit = async (event) => { event.preventDefault(); submit.disabled = true; try { await api("v1/appliances/tasks", {entity_id: option.entity_id, request_id: identity(), ready_at: new Date().toISOString(), deadline: localInstant(deadline.value)}); await load(); } catch (error) { find("status").textContent = error.message; submit.disabled = false; } };
            if (task && !["completed", "cancelled"].includes(task.status)) {
                const cancel = node("button", "Cancel planning"); cancel.type = "button"; cancel.className = "secondary";
                cancel.onclick = async () => {
                    if (cancel.disabled || !await confirmAction({title: "Cancel appliance planning?", message: `${option.name || option.entity_id} will no longer be scheduled for this request.\n\nThis does not stop the physical appliance or report that its cycle has completed. Home Assistant and the appliance remain responsible for physical control.`, confirmLabel: "Cancel planning"})) return;
                    if (!cancel.isConnected) {
                        find("status").textContent = `${option.name || option.entity_id}: Information was refreshed while confirmation was open. Review the current task, then choose Cancel planning again if needed.`;
                        return;
                    }
                    if (cancel.disabled) return;
                    cancel.disabled = true;
                    try { await api("v1/appliances/cancel", {entity_id: option.entity_id, task_id: task.id}); await load(); }
                    catch (error) { find("status").textContent = error.message; }
                    finally { if (cancel.isConnected) cancel.disabled = false; }
                };
                card.append(cancel);
            }
            if (task?.events?.length) { const details = node("details"), list = node("ol"); details.append(node("summary", "Execution evidence")); for (const event of task.events) list.append(node("li", `${when(event.at)}: ${optionLabel(event.event)}: Attempt: ${event.attempt_id}`)); details.append(list); card.append(details); }
            find("appliance-list").append(card);
        }
        showProfiles();
    } catch { find("status").textContent = "Appliance information could not be loaded. Check the connection and try Refresh."; }
}
find("profile-device").onchange = profile;
find("refresh").onclick = load;
find("connect").onsubmit = (event) => { event.preventDefault(); token = find("token").value; load(); };
api("setup/session").then(session => { if (session.ingress) load(); }).catch(() => { find("status").textContent = "Session information could not be loaded. Refresh this page and try again."; });
