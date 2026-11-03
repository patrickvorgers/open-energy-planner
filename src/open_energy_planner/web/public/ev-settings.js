/** Type-specific configuration reuses versioned app settings and shared save controls. */
import {percentage, percentageInput, inputNumber, optionLabel, useHomeAssistantTimezone, sourceName} from "./display.js";
import {settingsSummary} from "./appliance-settings-detail.js";
import {identity} from "./identity.js";
import {SaveBar} from "./save-bar.js";
import {bindApplianceSettingsNavigation} from "./appliance-settings-navigation.js";
import {bindSettingsActions} from "./settings-actions.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = (id) => document.getElementById(id);
const VEHICLE_FIELDS = ["id", "soc_entity", "soc_timestamp_attribute", "capacity_kwh", "maximum_w", "target_soc", "departure_time"];
const POINT_FIELDS = ["id", "energy_entity", "power_entity", "connected_entity", "minimum_w", "maximum_w", "control", "vehicle_ids"];
const WATTS_PER_KW = 1000;
let token = "", current, draft, editing = false, selectedId = null, editOriginId = null;
const saveState = new SaveBar({form: "settings", bar: "ev-save-bar", save: "save", discard: "refresh", status: "dirty", read: () => draft, canSave: () => editing});
bindApplianceSettingsNavigation(saveState);
find("refresh").textContent = "Cancel";
find("settings").hidden = true;
async function api(path, body) {
    const response = await fetch(base + path, {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store"});
    if (!response.ok) throw new Error(await response.text());
    return response.json();
}
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
function changed() { saveState.update(); }
/** Select one existing identity without merging vehicle and charge-point configuration models. */
function selected() { return draft?.vehicles.find(row => row.id === selectedId) || draft?.points.find(row => row.id === selectedId); }
/** Show precise display kW while physical configuration retains whole watts. */
function power(value) { return Number.isFinite(value) ? (value / WATTS_PER_KW).toLocaleString(undefined, {minimumFractionDigits: 1, maximumFractionDigits: 3}) + " kW" : "Unavailable"; }
/** Keep source-derived names separate from immutable OEP identities. */
function displayName(row, vehicle) { return (vehicle ? current.vehicles : current.points).find(saved => saved.id === row.id)?.name || (vehicle ? "New vehicle" : "New charge point"); }
/** Use the same read-only, Edit and Cancel boundary as the other Appliances editors. */
function syncEditor() {
    const locked = saveState.busy || !saveState.ready;
    for (const section of document.querySelectorAll("#vehicles > section, #points > section")) for (const control of section.querySelectorAll("input, select, button")) control.disabled = locked || !editing || section.hidden;
    find("ev-device-select").disabled = locked || editing || !selected();
    find("edit-ev").disabled = locked || !selected(); find("add-ev").disabled = locked;
    find("ev-editor").hidden = !editing; find("ev-summary").hidden = editing;
    find("ev-description").textContent = editing ? "Edit this appliance's configuration. Changes are applied only after Save." : "Saved configuration. Choose Edit settings to make changes.";
    for (const id of ["save", "refresh"]) find(id).hidden = !editing;
    for (const id of ["edit-ev", "add-ev", "ev-more"]) find(id).hidden = editing;
    if (editing && !saveState.busy) find("refresh").disabled = false;
}
/** Retain precise numeric drafts while target SoC shows one decimal when idle and exact values during editing. */
function input(card, row, key, label, help, type = "number", limits = {}) {
    const wrapper = node("label", label), control = node("input"); control.type = type; control.required = true;
    const watts = key === "maximum_w" || key === "minimum_w";
    Object.assign(control, limits); if (key === "target_soc") percentageInput(control, row[key]); else control.value = row[key] === null || row[key] === undefined || row[key] === "" ? "" : watts ? row[key] / WATTS_PER_KW : row[key]; control.dataset.field = key;
    control.oninput = () => { const value = type === "number" ? inputNumber(control) : control.value; row[key] = watts && value !== null ? Math.round(value * WATTS_PER_KW) : value; changed(); };
    wrapper.append(control, node("small", help)); card.append(wrapper); return control;
}
function select(card, row, key, label, help, options) {
    const wrapper = node("label", label), control = node("select"); control.required = true; control.dataset.field = key;
    const blank = node("option", "Select a source"); blank.value = ""; control.append(blank);
    for (const [value, text] of options) { const option = node("option", text); option.value = value; control.append(option); }
    control.value = row[key] || ""; control.onchange = () => { row[key] = control.value; changed(); };
    wrapper.append(control, node("small", help)); card.append(wrapper);
}
function metadata(predicate) { return current.catalogue.entities.filter(predicate).map((row) => [row.entity_id, `${sourceName(row, "Sensor")}: ${row.name} (${row.entity_id})`]); }
/** Stage a confirmed removal through versioned Save, keeping Cancel and archived history intact. */
function removeSelected() {
    find("ev-more").open = false;
    const row = selected();
    if (saveState.busy || saveState.dirty || !row) return;
    const dependent = draft.vehicles.includes(row) ? draft.points.filter(point => point.vehicle_ids.includes(row.id) && point.vehicle_ids.every(id => id === row.id)) : [];
    if (dependent.length) {
        saveState.update(`This vehicle is the only allowed vehicle for: ${dependent.map(point => displayName(point, false)).join(", ")}. Edit or remove the listed charge points first.`);
        return;
    }
    if (!confirm("Remove this appliance from OEP? Home Assistant sources and historical evidence are retained. Save settings to apply the removal.")) return;
    editOriginId = selectedId;
    const rows = draft.vehicles.includes(row) ? draft.vehicles : draft.points;
    rows.splice(rows.indexOf(row), 1);
    if (rows === draft.vehicles) for (const point of draft.points) point.vehicle_ids = point.vehicle_ids.filter(id => id !== row.id);
    editing = true; render(); changed();
}
/** Render the existing vehicle and charge-point configuration without operational controls. */
function render() {
    const all = [...draft.vehicles, ...draft.points];
    if (!all.some(row => row.id === selectedId)) selectedId = all[0]?.id || null;
    const selector = find("ev-device-select"); selector.replaceChildren();
    for (const [rows, label, vehicle] of [[draft.vehicles, "Vehicles", true], [draft.points, "Charge points", false]]) {
        if (!rows.length) continue;
        const group = node("optgroup"); group.label = label;
        for (const row of rows) { const option = node("option", displayName(row, vehicle) + (vehicle ? ": Vehicle" : ": Charge point")); option.value = row.id; group.append(option); }
        selector.append(group);
    }
    selector.value = selectedId || ""; find("ev-empty").hidden = Boolean(selectedId); find("ev-detail").hidden = !selectedId;
    find("vehicles").replaceChildren(); find("points").replaceChildren();
    for (const row of draft.vehicles) {
        const card = node("section"); card.className = "settings-fields"; card.hidden = row.id !== selectedId; card.append(node("h3", "Home Assistant sources"));
        select(card, row, "soc_entity", "Vehicle state of charge", "Select this car's measured battery percentage, not the stationary home battery.", metadata((entity) => entity.entity_id.startsWith("sensor.") && entity.unit === "%" && entity.device_class === "battery"));
        const advanced = node("details"); advanced.append(node("summary", "Optional original sample timestamp"));
        const stamp = input(advanced, row, "soc_timestamp_attribute", "SoC timestamp attribute", "If the SoC entity exposes its original sample time as an ISO timestamp attribute, enter that exact attribute name. Otherwise freshness uses HA report time and is labelled accordingly.", "text"); stamp.required = false; stamp.oninput = () => { row.soc_timestamp_attribute = stamp.value || null; changed(); }; card.append(advanced);
        card.append(node("h3", "Vehicle limits"));
        input(card, row, "capacity_kwh", "Usable battery capacity (kWh)", "Usable energy, not the manufacturer's gross pack size.", "number", {min: 0.1, max: 1000, step: 0.1});
        input(card, row, "maximum_w", "Maximum vehicle AC charging power (kW)", "The car's own AC limit. The lower of this and the charge point limit applies.", "number", {min: 0.001, max: 100, step: 0.001});
        card.append(node("h3", "Charging preferences"));
        input(card, row, "target_soc", "Target state of charge (%)", "The desired battery percentage before departure.", "number", {min: 0, max: 100, step: "any"});
        input(card, row, "departure_time", "Daily departure time", "Local Home Assistant time. Set a one-off date/time using the native HA vehicle controls.", "time");
        find("vehicles").append(card);
    }
    for (const row of draft.points) {
        const card = node("section"); card.className = "settings-fields"; card.hidden = row.id !== selectedId; card.append(node("h3", "Home Assistant sources"));
        select(card, row, "energy_entity", "Charging consumption meter", "Configured in Home Assistant Energy. Separate from instantaneous power.", current.energy_consumers.map((entity) => [entity.entity_id, entity.name]));
        select(card, row, "power_entity", "Measured AC charging power", "Positive W or kW consumed by this charge point.", metadata((entity) => entity.device_class === "power" && ["W", "kW"].includes(entity.unit)));
        select(card, row, "connected_entity", "Vehicle connected", "A binary sensor that is On only while a vehicle is physically connected.", metadata((entity) => entity.entity_id.startsWith("binary_sensor.")));
        select(card, row, "control", "Control capability", "Physical actions remain in your HA automation. Measurement only never produces a charging request.", [["read_only", "Measurement only"], ["on_off", "On/off at fixed power"], ["variable", "Variable power"]]);
        card.append(node("h3", "Charging limits"));
        input(card, row, "minimum_w", "Minimum active charging power (kW)", "The lowest supported charging power. Off remains available.", "number", {min: 0, max: 100, step: 0.001});
        input(card, row, "maximum_w", "Maximum charging power (kW)", "Point/cable limit. On/off charging uses this fixed level, capped by the vehicle.", "number", {min: 0.001, max: 100, step: 0.001});
        const group = node("fieldset"); group.append(node("legend", "Allowed vehicles"));
        for (const car of draft.vehicles) { const label = node("label"), check = node("input"); check.type = "checkbox"; check.checked = row.vehicle_ids.includes(car.id); check.onchange = () => { row.vehicle_ids = check.checked ? [...row.vehicle_ids, car.id] : row.vehicle_ids.filter((id) => id !== car.id); changed(); }; label.append(check, node("span", current.vehicles.find((vehicle) => vehicle.id === car.id)?.name || car.soc_entity || "New vehicle")); group.append(label); }
        card.append(group); find("points").append(card);
    }
    renderSummary(); saveState.update();
    syncEditor();
}
/** Show only saved type-specific configuration, with operational preferences kept in the side panel. */
function renderSummary() {
    const row = selected();
    if (!row) { find("ev-summary").replaceChildren(); find("ev-request-summary").replaceChildren(); return; }
    const vehicle = draft.vehicles.includes(row);
    find("ev-title").textContent = displayName(row, vehicle); find("ev-kind").textContent = vehicle ? "Vehicle" : "Charge point";
    const sourceFacts = vehicle ? [["State of charge", row.soc_entity, true]] : [["Consumption meter", row.energy_entity, true], ["Measured power", row.power_entity, true], ["Connection status", row.connected_entity, true]];
    const limits = vehicle ? [["Usable battery", Number.isFinite(row.capacity_kwh) ? row.capacity_kwh.toLocaleString(undefined, {maximumFractionDigits: 3}) + " kWh" : "Unavailable"], ["Maximum AC power", power(row.maximum_w)], ["Physical control", "Through the assigned charge point"]] : [["Control", {read_only: "Measurement only", on_off: "On/off at fixed power", variable: "Variable power"}[row.control]], ["Minimum active power", power(row.minimum_w)], ["Maximum charging power", power(row.maximum_w)], ["Allowed vehicles", row.vehicle_ids.map(id => { const car = draft.vehicles.find(item => item.id === id); return car ? displayName(car, true) : id; }).join(", ") || "None"]];
    settingsSummary(find("ev-summary"), [[vehicle ? "Vehicle and source" : "Home Assistant sources", sourceFacts], [vehicle ? "Vehicle limits" : "Charging capability", limits]]);
    find("ev-request-title").textContent = vehicle ? "Charging request" : "Charging sessions";
    find("ev-request-description").textContent = vehicle ? "Target state of charge and departure remain available under Energy and through the HA integration." : "Connection, vehicle assignment and charging evidence remain under Energy. Vehicle identity must be reported explicitly per session.";
    const saved = (vehicle ? current.vehicles : current.points).find(item => item.id === row.id);
    settingsSummary(find("ev-request-summary"), [["Saved request", vehicle ? [["Target SoC", percentage(saved?.target_soc)], ["Daily departure", saved?.departure_time ?? "Unavailable"]] : [["Status", saved?.status ? optionLabel(saved.status) : "Not saved yet"]]]]);
}
/** Reload confirmed configuration only through an explicit SaveBar transition. */
async function load(selection = selectedId) {
    try {
        current = await api("v1/ev"); useHomeAssistantTimezone(current.timezone);
        draft = {vehicles: current.vehicles.map((row) => Object.fromEntries(VEHICLE_FIELDS.map((key) => [key, row[key]]))), points: current.points.map((row) => Object.fromEntries(POINT_FIELDS.map((key) => [key, row[key]])))};
        selectedId = selection; editing = false; find("connect").hidden = true; find("settings").hidden = false; find("status").textContent = current.issue || ""; render(); saveState.loaded(); syncEditor();
    } catch (error) { find("status").textContent = error.message; saveState.update(error.message); }
}
find("edit-ev").onclick = () => { if (!saveState.busy && selected()) { editOriginId = selectedId; editing = true; syncEditor(); } };
find("ev-device-select").onchange = event => { if (saveState.busy || editing) return; selectedId = event.target.value; render(); };
find("add-ev").onclick = () => { if (!saveState.busy && !editing && !saveState.dirty) find("ev-add-dialog").showModal(); };
find("continue-add-ev").onclick = () => {
    if (saveState.busy || editing) return;
    const vehicle = document.querySelector('input[name="ev-add-kind"]:checked').value === "vehicle";
    const row = vehicle ? {id: identity(), soc_entity: "", soc_timestamp_attribute: null, capacity_kwh: "", maximum_w: "", target_soc: 80, departure_time: "07:00"} : {id: identity(), energy_entity: "", power_entity: "", connected_entity: "", minimum_w: "", maximum_w: "", control: "read_only", vehicle_ids: []};
    editOriginId = selectedId;
    (vehicle ? draft.vehicles : draft.points).push(row); selectedId = row.id; editing = true;
    find("ev-add-dialog").close(); render(); changed();
};
find("remove-ev").onclick = removeSelected;
find("reload-ev").onclick = () => { find("ev-more").open = false; return saveState.refresh(load); };
find("settings").onsubmit = async (event) => { event.preventDefault(); if (saveState.busy || !editing) return; saveState.begin(); syncEditor(); try { await api("v1/ev/configuration", {configuration: draft, expected_version: current.configuration_version}); await load(); } catch (error) { find("status").textContent = error.message; saveState.update(error.message); } finally { saveState.end(); syncEditor(); } };
find("refresh").onclick = () => saveState.refresh(() => load(editOriginId), {discard: true}).finally(syncEditor);
find("refresh-evidence").onclick = () => saveState.refresh(load);
find("connect").onsubmit = (event) => { event.preventDefault(); token = find("token").value; load(); };
fetch(base + "setup/session", {redirect: "error"}).then((response) => response.json()).then((session) => { useHomeAssistantTimezone(session.timezone); if (session.ingress) load(); }).catch((error) => { find("status").textContent = error.message; });
