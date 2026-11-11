import {pickEnergySource} from "./energy-source-picker.js";
import {confirmAction} from "./confirmation.js";
import {bindSettingsActions} from "./settings-actions.js";
import {SaveBar} from "./save-bar.js";
import {money, optionLabel} from "./display.js";
import {profileMetrics} from "./appliance-profile.js";
import {pollApplianceEvidence} from "./appliance-evidence.js";
/** Configuration uses the existing consumers and appliance APIs. Source meters remain owned by Home Assistant Energy. */
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = id => document.getElementById(id);
let token = "", consumerVersion, applianceVersion, retainedSpecialized = [], savedConsumers = "";
const consumerChoices = new Map(), pending = new Map();
let energySources;
let selectedId = null, editing = false, evidenceEpoch = 0, evidenceRefreshing = false;
bindSettingsActions();
const saveState = new SaveBar({form: "appliance-settings", bar: "appliance-save-bar", save: "save-settings", discard: "discard-settings", status: "save-state", read: () => [...consumerChoices].map(([id, inputs]) => [id, inputs.enabled.checked, inputs.weather.checked, inputs.permission.checked, inputs.duration.value, inputs.energy.value])});
/** Send versioned configuration requests through the authenticated app boundary. */
async function api(path, body, method = body ? "POST" : "GET") {
    const response = await fetch(base + path, {method, headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store"});
    if (!response.ok) { const error = new Error(await response.text()); error.status = response.status; throw error; }
    return response.json();
}
/** Build labels as text so source names cannot inject markup. */
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Keep all OEP-owned source choices in Settings while preserving EV/thermal-owned selections and unresolved cycles. */
function settings(consumers, specializedMeters, devices) {
    energySources = consumers;
    consumerVersion = consumers.version;
    savedConsumers = JSON.stringify([...consumers.selected].sort((left, right) => left.entity_id.localeCompare(right.entity_id)));
    retainedSpecialized = consumers.selected.filter(item => specializedMeters.has(item.entity_id));
    const selected = new Map(consumers.selected.map(item => [item.entity_id, item]));
    consumerChoices.clear(); find("settings-devices").replaceChildren();
    for (const device of consumers.options.filter(item => !specializedMeters.has(item.entity_id))) {
        const card = node("fieldset"), legend = node("legend", device.name); card.append(legend);
        card.setAttribute("aria-label", device.name);
        const enabled = node("input"), weather = node("input"); enabled.type = "hidden"; weather.type = "checkbox"; enabled.name = device.entity_id + "_selected"; weather.name = device.entity_id + "_weather";
        enabled.checked = selected.has(device.entity_id); weather.checked = selected.get(device.entity_id)?.weather_sensitive || false;
        weather.disabled = !enabled.checked;
        const task = devices.find(item => item.entity_id === device.entity_id)?.task;
        enabled.disabled = Boolean(task && !["completed", "cancelled"].includes(task.status));
        card.append(enabled);
        const weatherLabel = node("label"), weatherText = node("span", "Consumption depends on outdoor temperature");
        weatherLabel.className = "settings-switch"; weatherText.append(node("small", "Use weather sensitivity for heating or cooling.")); weatherLabel.append(weather, weatherText); card.append(weatherLabel);
        const appliance = devices.find(item => item.entity_id === device.entity_id);
        const permission = node("input"); permission.type = "checkbox"; permission.name = device.entity_id + "_shiftable"; permission.checked = Boolean(appliance?.shiftable); permission.disabled = enabled.disabled || !enabled.checked;
        const permissionLabel = node("label"), permissionText = node("span", "Allow shifting complete cycles");
        permissionLabel.className = "settings-switch"; permissionText.append(node("small", "Allow a complete cycle to be scheduled before its deadline.")); permissionLabel.append(permission, permissionText); card.append(permissionLabel);
        weather.setAttribute("aria-label", "Consumption depends on outdoor temperature"); permission.setAttribute("aria-label", "Allow shifting complete cycles");
        permission.onchange = () => { if (permission.checked === Boolean(appliance?.shiftable)) pending.delete(device.entity_id); else pending.set(device.entity_id, permission.checked); saveState.update(""); };
        enabled.onchange = () => {
            weather.disabled = !enabled.checked;
            permission.disabled = enabled.disabled || !enabled.checked;
            if (!enabled.checked && permission.checked) { permission.checked = false; permission.onchange(); }
        };
        card.append(node("h3", "Energy source"), node("p", "Provided by Home Assistant Energy."), node("code", device.entity_id));
        card.append(node("h3", "Power source"), device.power_entity ? node("code", device.power_entity) : node("p", "Automatic cycle learning requires a power sensor. Configure device power consumption in Home Assistant Energy."));
        card.append(node("h3", "Manual cycle profile"), node("p", "Optional fallback until two complete cycles are detected. Energy is spread evenly over the duration. The estimate stays saved when a learned profile becomes available."));
        const duration = node("input"), energy = node("input");
        duration.type = energy.type = "number"; duration.min = "15"; duration.max = "720"; duration.step = "1"; energy.min = "0.001"; energy.step = "any";
        duration.value = appliance?.manual_profile?.duration_minutes ?? ""; energy.value = appliance?.manual_profile?.energy_kwh ?? "";
        for (const [input, label] of [[duration, "Duration (minutes)"], [energy, "Energy consumption (kWh)"]]) {
            const wrapper = node("label", label); wrapper.append(input); card.append(wrapper);
            input.oninput = () => saveState.update("");
        }
        if (device.upstream) card.append(node("small", `Included in ${device.upstream}`));
        find("settings-devices").append(card); consumerChoices.set(device.entity_id, {enabled, weather, permission, duration, energy, card, device, appliance, evidence: appliance, openCycle: enabled.disabled});
    }
}
/** Load confirmed settings under the edit lock. Call only on connection, successful save or explicit refresh/discard. */
async function load() {
    evidenceEpoch += 1;
    const ownsLock = !saveState.busy;
    if (ownsLock) saveState.begin("Loading appliance settings…");
    try {
        const [data, consumers] = await Promise.all([api("v1/appliances"), api("v1/consumers?view=settings")]);
        if (!Array.isArray(consumers.charging_meters) || !Array.isArray(consumers.thermal_meters)) throw new Error("Specialized-meter membership unavailable. Reload before editing appliance settings.");
        settings(consumers, new Set([...consumers.charging_meters, ...consumers.thermal_meters]), data.devices);
        applianceVersion = data.revision;
        pending.clear(); editing = false;
        find("connect").hidden = true;
        find("status").textContent = data.issue || "";
        saveState.loaded(); showAppliance();
        return true;
    } catch (error) { find("status").textContent = error.message; saveState.update(error.message); return false; }
    finally { if (ownsLock) saveState.end(); syncEditor(); }
}
/** Disable shifting before removing consumers, retain partial-write drafts, and reload confirmed settings before releasing the edit lock. */
find("appliance-settings").onsubmit = async (event) => {
    event.preventDefault();
    if (saveState.busy) return;
    const selected = [...retainedSpecialized, ...[...consumerChoices].filter(([, inputs]) => inputs.enabled.checked).map(([entity_id, inputs]) => ({entity_id, weather_sensitive: inputs.weather.checked}))].sort((left, right) => left.entity_id.localeCompare(right.entity_id));
    if ([...pending].some(([id, enabled]) => enabled && !selected.some(item => item.entity_id === id))) { saveState.update("Add the device before enabling cycle shifting."); return; }
    const manualChanges = [...consumerChoices].filter(([, inputs]) => inputs.enabled.checked && (inputs.duration.value !== String(inputs.appliance?.manual_profile?.duration_minutes ?? "") || inputs.energy.value !== String(inputs.appliance?.manual_profile?.energy_kwh ?? "")));
    if (manualChanges.some(([, inputs]) => !inputs.duration.value || !inputs.energy.value)) { saveState.update("Enter both duration and energy for a manual profile."); return; }
    saveState.begin(); syncEditor();
    try {
        for (const [entity_id, shiftable] of pending) if (!shiftable) { const response = await api("v1/appliances/device", {entity_id, shiftable, expected_version: applianceVersion}); applianceVersion = response.appliances.revision; pending.delete(entity_id); }
        if (JSON.stringify(selected) !== savedConsumers) {
            const result = await api("v1/consumers", {expected_version: consumerVersion, selected}, "PUT");
            consumerVersion = result.version; savedConsumers = JSON.stringify(selected);
        }
        for (const [entity_id, shiftable] of pending) { const response = await api("v1/appliances/device", {entity_id, shiftable, expected_version: applianceVersion}); applianceVersion = response.appliances.revision; pending.delete(entity_id); }
        for (const [entity_id, inputs] of manualChanges) {
            const manual = {duration_minutes: Number(inputs.duration.value), energy_kwh: Number(inputs.energy.value)};
            const response = await api("v1/appliances/manual_profile", {entity_id, ...manual, expected_version: applianceVersion});
            applianceVersion = response.appliances.revision;
            inputs.appliance = {...inputs.appliance, manual_profile: manual};
        }
        if (!await load()) throw new Error("Settings were saved but could not be reloaded.");
    } catch (error) { saveState.update(`Some settings may already be saved. Remaining changes are retained. ${error.message}`); }
    finally { saveState.end(); syncEditor(); }
};
find("discard-settings").onclick = async () => { await saveState.refresh(load, {discard: true}); syncEditor(); };
find("refresh").onclick = () => { find("appliance-more").open = false; return saveState.refresh(load); };
find("connect").onsubmit = event => { event.preventDefault(); token = find("token").value; load(); };
fetch(base + "setup/session", {redirect: "error"}).then(response => response.json()).then(session => { if (session.ingress) load(); }).catch(error => { find("status").textContent = error.message; });

/** Select only OEP members while retaining all source controls for versioned API writes. */
function showAppliance() {
    const members = [...consumerChoices].filter(([, inputs]) => inputs.enabled.checked);
    if (!members.some(([id]) => id === selectedId)) selectedId = members[0]?.[0] ?? null;
    find("appliance-select").replaceChildren();
    for (const [id, inputs] of members) { const option = node("option", inputs.device.name); option.value = id; find("appliance-select").append(option); }
    find("appliance-select").value = selectedId || "";
    for (const [id, inputs] of consumerChoices) inputs.card.hidden = id !== selectedId;
    find("appliance-empty").hidden = Boolean(selectedId);
    find("appliance-detail").hidden = !selectedId;
    find("appliance-title").textContent = consumerChoices.get(selectedId)?.device.name || "";
    showEvidence();
    syncEditor();
}
/**
 * Update the membership label and replace only the selected appliance's evidence panel.
 * Show an explicit no-profile state and short planning status. Detailed learning evidence lives on Energy > Appliances.
 * Preserve selectors, controls and the saved edit baseline.
 */
function showEvidence() {
    const selected = consumerChoices.get(selectedId), appliance = selected?.evidence, profile = appliance?.profile;
    find("appliance-membership").textContent = selected?.openCycle ? "Open cycle" : "Added";
    const evidence = find("appliance-evidence"); evidence.replaceChildren();
    if (profile) {
        const metrics = profileMetrics(profile), facts = node("dl"); facts.className = "settings-summary";
        evidence.append(node("p", `Profile: ${appliance.profile_kind || "Learned"}${(profile.evidence === "synthetic" || (profile.evidence === "hourly_estimate" && appliance.hourly_synthetic)) ? ": Simulated data" : ""}`));
        for (const [label, value] of [["Average cycle", `${money(metrics.energyKwh)} kWh`], ["Cycle time", `${metrics.minutes} minutes`], ["Cycles in profile", String(profile.evidence === "manual" ? 0 : profile.samples)]]) { const fact = node("div"); fact.append(node("dt", label), node("dd", value)); facts.append(fact); }
        evidence.append(facts, node("small", "Planning profile: Not a measured run today."));
    } else evidence.textContent = "No profile available yet. Use the appliance normally or enter duration and energy to start planning.";
    evidence.append(node("p", selected?.device.power_entity ? (appliance?.learning_status === "ready" ? "Automatic learning: Ready" : appliance?.learning_status === "provisional" ? "Automatic learning: Provisional hourly profile: Collecting fine cycles" : "Automatic learning: Waiting for sufficient power history and two complete cycles") : "Automatic learning: Power sensor missing in Home Assistant Energy"));
    syncEditor();
}
/** Poll evidence only, retaining previous evidence on failure and the version protecting an unsaved draft. */
async function refreshEvidence() {
    const epoch = evidenceEpoch;
    if (!saveState.ready || saveState.busy || document.hidden || evidenceRefreshing) return;
    evidenceRefreshing = true;
    try {
        const data = await api("v1/appliances");
        if (epoch !== evidenceEpoch || saveState.busy) return;
        for (const inputs of consumerChoices.values()) inputs.evidence = data.devices.find(item => item.entity_id === inputs.device.entity_id);
        showEvidence();
    } catch {
        const evidence = find("appliance-evidence");
        if (epoch === evidenceEpoch && !saveState.busy && !evidence.querySelector("[data-refresh-failed]")) {
            const status = node("p", "Learning evidence refresh unavailable. Retained evidence is shown."); status.dataset.refreshFailed = "true"; status.setAttribute("role", "status"); evidence.append(status);
        }
    } finally { evidenceRefreshing = false; }
}
pollApplianceEvidence(refreshEvidence);
/** Keep SaveBar's locks and retained partial failures authoritative for configuration writes. */
function syncEditor() {
    const locked = saveState.busy || !saveState.ready;
    for (const inputs of consumerChoices.values()) inputs.card.disabled = locked || !editing;
    find("appliance-settings").classList.toggle("settings-readonly", !editing);
    find("appliance-select").disabled = locked || editing || !selectedId;
    for (const id of ["save-settings", "discard-settings"]) find(id).hidden = !editing;
    for (const id of ["add-appliance", "edit-appliance"]) find(id).hidden = editing || (id !== "add-appliance" && !selectedId);
    for (const id of ["add-appliance", "edit-appliance", "remove-appliance"]) find(id).disabled = locked;
    if (editing && !locked) find("discard-settings").disabled = false;
}
find("appliance-select").onchange = event => { if (saveState.busy || saveState.dirty) return; selectedId = event.target.value; showAppliance(); };
find("edit-appliance").onclick = () => { if (saveState.busy) return; editing = true; syncEditor(); };
find("add-appliance").onclick = async () => {
    if (saveState.busy || saveState.dirty) return;
    const opening = energySources;
    const source = await pickEnergySource(find("source-dialog"), opening, "Add appliance");
    if (!source || energySources !== opening || saveState.busy || saveState.dirty) return;
    const inputs = consumerChoices.get(source.entity_id);
    if (!inputs || inputs.enabled.checked) return;
    inputs.enabled.checked = true; inputs.enabled.onchange(); selectedId = source.entity_id; editing = true;
    showAppliance(); saveState.update("Configure the selected source, then Save appliance settings to add it.");
};
find("remove-appliance").onclick = async () => {
    find("appliance-more").open = false;
    if (saveState.busy || saveState.dirty || !selectedId) return;
    const inputs = consumerChoices.get(selectedId);
    if (inputs.openCycle) { saveState.update("Resolve this appliance's open cycle on Energy: Appliances before removing it."); return; }
    const openingId = selectedId;
    if (!await confirmAction({title: "Remove appliance?", message: `${inputs.device.name} will be removed when you confirm.\n\nIts Home Assistant source and history are retained. You can add the same source again later.`, confirmLabel: "Remove appliance", returnFocus: find("appliance-more").querySelector("summary")})) return;
    if (saveState.busy || saveState.dirty || selectedId !== openingId || consumerChoices.get(openingId) !== inputs || inputs.openCycle) return;
    inputs.enabled.checked = false; inputs.enabled.onchange(); editing = true; syncEditor(); saveState.update(); find("appliance-settings").requestSubmit();
};

find("discard-settings").textContent = "Cancel";

syncEditor();
