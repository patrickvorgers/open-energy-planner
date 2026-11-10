/** Type-specific configuration reuses versioned app settings and shared save controls. */
import {percentage, percentageInput, inputNumber, optionLabel, useHomeAssistantTimezone, sourceName} from "./display.js";
import {settingsSummary} from "./appliance-settings-detail.js";
import {identity} from "./identity.js";
import {SaveBar} from "./save-bar.js";
import {confirmAction} from "./confirmation.js";
import {bindSettingsActions} from "./settings-actions.js";
import {sourceRefresh, retainInputs} from "./source-refresh.js";
import {assignedEnergySources, pickEnergySource} from "./energy-source-picker.js";
import {applianceSettingsApi} from "./appliance-settings-api.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = (id) => document.getElementById(id);
const VEHICLE_FIELDS = ["id", "soc_entity", "soc_timestamp_attribute", "capacity_kwh", "maximum_w", "target_soc", "departure_time"];
const POINT_FIELDS = ["id", "energy_entity", "power_entity", "connected_entity", "minimum_w", "maximum_w", "control", "vehicle_ids"];
const WATTS_PER_KW = 1000;
const NO_DEVICE = "sources-without-device";
let token = "", current, energySources, draft, editing = false, selectedId = null, editOriginId = null;
const api = applianceSettingsApi(base, () => token);
const integrationSelections = new Map();
const integrationTypeSelections = new Map();
const deviceSelections = new Map();
const saveState = new SaveBar({form: "settings", bar: "ev-save-bar", save: "save", discard: "refresh", status: "dirty", read: () => draft, canSave: () => editing});
find("refresh").textContent = "Cancel";
find("settings").hidden = true;
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Restore editor actions after SaveBar processes the same bubbling change event. */
function changed() { saveState.update(); queueMicrotask(syncEditor); }
/** Select one existing identity without merging vehicle and charge-point configuration models. */
function selected() { return draft?.vehicles.find(row => row.id === selectedId) || draft?.points.find(row => row.id === selectedId); }
/** Show precise display kW while physical configuration retains whole watts. */
function power(value) { return Number.isFinite(value) ? (value / WATTS_PER_KW).toLocaleString(undefined, {minimumFractionDigits: 1, maximumFractionDigits: 3}) + " kW" : "Unavailable"; }
/** Keep source-derived names separate from immutable OEP identities. */
function displayName(row, vehicle) {
    if (!vehicle) return current.energy_consumers.find(source => source.entity_id === row.energy_entity)?.name || current.points.find(saved => saved.id === row.id)?.name || "New charge point";
    const source = current.catalogue.entities.find(entity => entity.entity_id === row.soc_entity);
    return (vehicle ? current.vehicles : current.points).find(saved => saved.id === row.id)?.name || (vehicle ? sourceName({device_name: source?.device_name, entity_id: source?.entity_id}, source?.integration_name || "New vehicle") : "New charge point");
}
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
    const wrapper = node("label"), control = node("input"); wrapper.className = "ev-field"; wrapper.append(node("span", label)); control.type = type; control.required = true;
    control.setAttribute("aria-label", label);
    const watts = key === "maximum_w" || key === "minimum_w";
    Object.assign(control, limits); if (key === "target_soc") percentageInput(control, row[key]); else control.value = row[key] === null || row[key] === undefined || row[key] === "" ? "" : watts ? row[key] / WATTS_PER_KW : row[key]; control.dataset.field = key;
    control.oninput = () => { const value = type === "number" ? inputNumber(control) : control.value; row[key] = watts && value !== null ? Math.round(value * WATTS_PER_KW) : value; changed(); };
    wrapper.append(control, node("small", help)); card.append(wrapper); return control;
}
function select(card, row, key, label, help, options, emptyMessage = "No matching Home Assistant sources were discovered. Check that the integration exposes this sensor, then choose Refresh Home Assistant sources.") {
    const wrapper = node("label"), control = node("select"); wrapper.className = "ev-field"; wrapper.append(node("span", label)); control.required = true; control.dataset.field = key;
    control.setAttribute("aria-label", label);
    const blank = node("option", "Select a source"); blank.value = ""; control.append(blank);
    for (const [value, text] of options) { const option = node("option", text); option.value = value; control.append(option); }
    if (row[key] && !options.some(([value]) => value === row[key]) && ["soc_entity", "energy_entity", "power_entity", "connected_entity"].includes(key)) {
        const source = current.catalogue.entities.find(entity => entity.entity_id === row[key]);
        const option = node("option", `${source?.name || row[key]}: ${key === "energy_entity" ? "Current selection unavailable in Home Assistant Energy" : "Current selection outside this list"}`); option.value = row[key]; control.append(option);
    }
    control.value = row[key] || ""; control.onchange = () => { row[key] = control.value; changed(); };
    wrapper.append(control, node("small", help)); card.append(wrapper);
    if (!options.length) wrapper.append(node("p", emptyMessage));
    return control;
}
function metadata(predicate, selection) { return (selection?.entities ?? current.catalogue.entities).filter(row => predicate(row) && (!selection?.device || row.device_id === selection.device) && (selection?.integration === undefined || (row.integration_id || "other") === selection.integration)).map((row) => { const name = sourceName(row, "Sensor"); return [row.entity_id, `${name}${row.name === name ? "" : ": " + row.name} (${row.entity_id})`]; }); }
/** Keep integration types distinct from configured account instances and physical device identities. */
function vehicleIntegration(entity) { return entity.integration_domain || entity.integration_id || "other"; }
/** Give identical visible names distinct choices without changing the underlying identities. */
function pickerOptions(groups) {
    const options = [...groups].sort((left, right) => left[1].localeCompare(right[1]) || left[0].localeCompare(right[0]));
    const totals = new Map(), positions = new Map();
    for (const [, label] of options) totals.set(label, (totals.get(label) || 0) + 1);
    return options.map(([id, label]) => {
        if (totals.get(label) === 1) return [id, label];
        const position = (positions.get(label) || 0) + 1;
        positions.set(label, position);
        return [id, `${label}: ${position}`];
    });
}
/** Select integration, optional account, device and SoC in order. Unique compatible choices remain drafts until Save. */
function vehicleSourcePicker(card, row) {
    const candidates = current.catalogue.entities.filter(isVehicleSoc);
    const source = candidates.find(entity => entity.entity_id === row.soc_entity);
    const groups = new Map(candidates.map(entity => [vehicleIntegration(entity), entity.integration_display_name || entity.integration_name || "Other Home Assistant sources"]));
    const type = integrationTypeSelections.get(row.id) ?? (source ? vehicleIntegration(source) : "");
    integrationTypeSelections.set(row.id, type);
    if (type && !groups.has(type)) groups.set(type, "Previously selected integration (unavailable)");
    const integrationControl = select(card, {type}, "type", "Home Assistant integration", "Choose the integration that supplies your car's measured battery percentage. Its account is shown only when several instances need a choice.", pickerOptions(groups));
    integrationControl.required = false;
    integrationControl.parentElement.style.gridColumn = "1 / -1";
    integrationControl.onchange = () => {
        integrationTypeSelections.set(row.id, integrationControl.value);
        integrationSelections.delete(row.id); deviceSelections.delete(row.id);
        const selectedSource = candidates.find(entity => entity.entity_id === row.soc_entity);
        if (integrationControl.value && (!selectedSource || vehicleIntegration(selectedSource) !== integrationControl.value)) row.soc_entity = "";
        render(); changed();
    };
    const typed = candidates.filter(entity => vehicleIntegration(entity) === type);
    const entries = new Map(typed.map(entity => [entity.integration_id || "other", entity.integration_name || "Other Home Assistant sources"]));
    const sameType = source && vehicleIntegration(source) === type;
    const integration = integrationSelections.get(row.id) ?? (sameType ? source.integration_id || "other" : entries.size === 1 ? entries.keys().next().value : "");
    integrationSelections.set(row.id, integration);
    const missingIntegration = integration && !entries.has(integration);
    if (missingIntegration) entries.set(integration, "Previously selected instance (unavailable)");
    if (entries.size > 1 || missingIntegration) {
        const entryControl = select(card, {integration}, "integration", "Home Assistant integration instance", missingIntegration ? "The previously selected instance is unavailable. Refresh sources or explicitly choose the account that owns this car. The existing sensor is retained until you choose a replacement." : "Several accounts or instances supply this integration. Choose the one that owns the car.", pickerOptions(entries));
        entryControl.required = false;
        entryControl.parentElement.style.gridColumn = "1 / -1";
        entryControl.onchange = () => {
            integrationTypeSelections.set(row.id, type);
            integrationSelections.set(row.id, entryControl.value); deviceSelections.delete(row.id);
            const selectedSource = typed.find(entity => entity.entity_id === row.soc_entity);
            if (!selectedSource || (selectedSource.integration_id || "other") !== entryControl.value) row.soc_entity = "";
            render(); changed();
        };
    }
    const scoped = typed.filter(entity => (entity.integration_id || "other") === integration);
    const devices = new Map(scoped.map(entity => [entity.device_id || NO_DEVICE, entity.device_id ? entity.device_name || "Home Assistant device" : "Sources without a Home Assistant device"]));
    const sameEntry = sameType && (source.integration_id || "other") === integration;
    const device = deviceSelections.get(row.id) ?? (sameEntry ? source.device_id || NO_DEVICE : !row.soc_entity && devices.size === 1 ? devices.keys().next().value : "");
    deviceSelections.set(row.id, device);
    if (device && !devices.has(device)) devices.set(device, "Previously selected device (unavailable)");
    const deviceControl = select(card, {device}, "device", "Home Assistant device", "Only devices with a compatible SoC source are listed. One suitable device is selected automatically. Service devices are excluded.", pickerOptions(devices));
    deviceControl.required = false;
    deviceControl.parentElement.style.gridColumn = "1 / -1";
    deviceControl.onchange = () => {
        integrationTypeSelections.set(row.id, type);
        integrationSelections.set(row.id, integration);
        deviceSelections.set(row.id, deviceControl.value);
        const selectedSource = scoped.find(entity => entity.entity_id === row.soc_entity);
        if (deviceControl.value && (!selectedSource || (selectedSource.device_id || NO_DEVICE) !== deviceControl.value)) row.soc_entity = "";
        render(); changed();
    };
    const entities = scoped.filter(entity => (entity.device_id || NO_DEVICE) === device);
    if (editing && !row.soc_entity && entities.length === 1) row.soc_entity = entities[0].entity_id;
    return {entities};
}
/** Percentage SoC may omit HA's optional battery class, but incompatible classes remain excluded. */
function isVehicleSoc(entity) { return entity.device_entry_type !== "service" && entity.entity_id.startsWith("sensor.") && entity.unit === "%" && ["battery", ""].includes(entity.device_class); }
/** Filter connection candidates by visible names without guessing that any binary sensor means plugged in. */
function connectedSource(card, row) {
    const group = node("fieldset"); group.className = "settings-fields"; group.append(node("legend", "Physical connection")); card.append(group);
    const candidates = metadata(entity => entity.entity_id.startsWith("binary_sensor."));
    const searchLabel = node("label"), search = node("input"); searchLabel.className = "ev-field"; searchLabel.append(node("span", "Find connection sensor")); search.type = "search";
    search.setAttribute("aria-label", "Find connection sensor");
    searchLabel.append(search, node("small", "Search the device, sensor name or entity ID.")); group.append(searchLabel);
    const control = select(group, row, "connected_entity", "Vehicle connected", "Choose a sensor that stays On while plugged in, even when charging is paused. Charging, door and fault sensors do not establish a connection.", candidates);
    const searchable = [...control.options].filter(option => option.value).map(option => [option.value, option.textContent]);
    const result = node("small"); result.setAttribute("role", "status"); result.style.gridColumn = "1 / -1"; group.append(result);
    search.oninput = () => {
        const query = search.value.trim().toLocaleLowerCase();
        control.replaceChildren(); const blank = node("option", "Select a source"); blank.value = ""; control.append(blank);
        let matches = 0;
        for (const [value, text] of searchable) { const matched = text.toLocaleLowerCase().includes(query); if (matched) matches++; if (value === row.connected_entity || matched) { const option = node("option", text); option.value = value; control.append(option); } }
        control.value = row.connected_entity || "";
        result.textContent = matches ? `${matches} matching connection sensor${matches === 1 ? "" : "s"}.` : "No sensors match this search.";
    };
}
/** Stage a confirmed removal through versioned Save, keeping Cancel and archived history intact. */
async function removeSelected() {
    find("ev-more").open = false;
    const row = selected();
    if (saveState.busy || saveState.dirty || !row) return;
    const dependent = draft.vehicles.includes(row) ? draft.points.filter(point => point.vehicle_ids.includes(row.id) && point.vehicle_ids.every(id => id === row.id)) : [];
    if (dependent.length) {
        saveState.update(`This vehicle is the only allowed vehicle for: ${dependent.map(point => displayName(point, false)).join(", ")}. Edit or remove the listed charge points first.`);
        return;
    }
    const openingDraft = draft, openingId = selectedId;
    const kind = draft.vehicles.includes(row) ? "vehicle" : "charge point";
    if (!await confirmAction({title: `Remove ${kind}?`, message: `${displayName(row, kind === "vehicle")} will be removed from these settings.\n\nHome Assistant sources and historical evidence are retained. Choose Save settings to apply the removal. Cancel in the editor restores the saved settings.`, confirmLabel: `Remove ${kind}`, returnFocus: find("ev-more").querySelector("summary")})) return;
    if (saveState.busy || saveState.dirty || draft !== openingDraft || selectedId !== openingId || selected() !== row) return;
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
    find("ev-empty").hidden = Boolean(selectedId); find("ev-detail").hidden = !selectedId;
    find("vehicles").replaceChildren(); find("points").replaceChildren();
    for (const row of draft.vehicles) {
        const card = node("section"); card.className = "settings-fields"; card.hidden = row.id !== selectedId; card.append(node("h3", "Home Assistant sources"));
        const selection = vehicleSourcePicker(card, row);
        const sources = selection.entities.map(entity => {
            const label = entity.name || entity.entity_id;
            const duplicate = selection.entities.some(other => other.entity_id !== entity.entity_id && (other.name || other.entity_id) === label);
            return [entity.entity_id, duplicate ? `${label} (${entity.entity_id})` : label];
        });
        select(card, row, "soc_entity", "Vehicle state of charge", "One compatible sensor is selected automatically. Choose explicitly when several remain. Check this car's measured battery percentage before saving.", sources, "Choose an integration, any required account and a device with a compatible SoC source. Refresh Home Assistant sources if it is missing.");
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
        const assignedSources = assignedEnergySources(energySources);
        const savedMeter = current.points.find(saved => saved.id === row.id)?.energy_entity;
        const meter = select(card, row, "energy_entity", "Charging consumption meter", "Choose the device consumption meter assigned to this charge point in the Home Assistant Energy dashboard. A registered device or integration is not required.", current.energy_consumers.filter(source => source.entity_id === row.energy_entity || source.entity_id === savedMeter || !assignedSources.has(source.entity_id)).map((entity) => [entity.entity_id, entity.name]), "No device consumption meters are configured in Home Assistant Energy. Add the charge point under Individual devices, then refresh Home Assistant sources.");
        meter.onchange = () => { if (meter.value === row.energy_entity) return; row.energy_entity = meter.value; row.power_entity = current.energy_consumers.find(source => source.entity_id === meter.value)?.power_entity || ""; retainInputs(render); changed(); };
        select(card, row, "power_entity", "Measured AC charging power", "Selected with the Energy device when Home Assistant defines device power consumption. Otherwise choose its W or kW sensor. You can explicitly override this source.", metadata((entity) => entity.device_class === "power" && ["W", "kW"].includes(entity.unit)));
        connectedSource(card, row);
        card.append(node("h3", "Charging limits"));
        const control = select(card, row, "control", "Control capability", "Physical actions remain in your HA automation. Measurement only never produces a charging request.", [["read_only", "Measurement only"], ["on_off", "On/off at fixed power"], ["variable", "Variable power"]]);
        control.parentElement.style.gridColumn = "1 / -1";
        input(card, row, "minimum_w", "Minimum active charging power (kW)", "The lowest supported charging power. Off remains available.", "number", {min: 0, max: 100, step: 0.001});
        input(card, row, "maximum_w", "Maximum charging power (kW)", "Point/cable limit. On/off charging uses this fixed level, capped by the vehicle.", "number", {min: 0.001, max: 100, step: 0.001});
        const group = node("fieldset"); group.append(node("legend", "Allowed vehicles"));
        for (const car of draft.vehicles) { const label = node("label"), check = node("input"); check.type = "checkbox"; check.checked = row.vehicle_ids.includes(car.id); check.onchange = () => { row.vehicle_ids = check.checked ? [...row.vehicle_ids, car.id] : row.vehicle_ids.filter((id) => id !== car.id); changed(); }; label.append(check, node("span", current.vehicles.find((vehicle) => vehicle.id === car.id)?.name || car.soc_entity || "New vehicle")); group.append(label); }
        card.append(group); find("points").append(card);
    }
    const selector = find("ev-device-select"); selector.replaceChildren();
    for (const [rows, label, vehicle] of [[draft.vehicles, "Vehicles", true], [draft.points, "Charge points", false]]) {
        if (!rows.length) continue;
        const group = node("optgroup"); group.label = label;
        for (const row of rows) { const option = node("option", displayName(row, vehicle) + (vehicle ? ": Vehicle" : ": Charge point")); option.value = row.id; group.append(option); }
        selector.append(group);
    }
    selector.value = selectedId || "";
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
        const results = await Promise.allSettled([api("v1/ev"), api("v1/consumers")]);
        const failed = results.find(result => result.status === "rejected");
        if (failed) throw failed.reason;
        [current, energySources] = results.map(result => result.value); useHomeAssistantTimezone(current.timezone);
        current.energy_consumers = energySources.options;
        integrationSelections.clear();
        integrationTypeSelections.clear();
        deviceSelections.clear();
        draft = {vehicles: current.vehicles.map((row) => Object.fromEntries(VEHICLE_FIELDS.map((key) => [key, row[key]]))), points: current.points.map((row) => Object.fromEntries(POINT_FIELDS.map((key) => [key, row[key]])))};
        selectedId = selection; editing = false; find("connect").hidden = true; find("settings").hidden = false; find("status").textContent = current.issue || ""; render(); saveState.loaded(); syncEditor();
    } catch (error) { find("status").textContent = error.message; saveState.update(error.message); }
}
/** Refresh energySources ownership and current.energy_consumers choices without replacing draft overrides.
 * Discard results while settings are busy or another configuration has replaced the read's owner.
 */
const refreshSources = sourceRefresh(api, "ev", find("refresh-ev-sources"), find("status"), async (catalogue, signal) => {
    const opening = current;
    if (saveState.busy || !opening) return;
    const refreshed = await api("v1/consumers", undefined, signal);
    if (saveState.busy || current !== opening) return;
    current.catalogue = catalogue;
    energySources = refreshed;
    current.energy_consumers = refreshed.options;
    retainInputs(render);
    saveState.update();
    syncEditor();
});
find("refresh-ev-sources").onclick = () => { if (!saveState.busy) return refreshSources(); };
find("edit-ev").onclick = () => { if (!saveState.busy && selected()) { editOriginId = selectedId; editing = true; syncEditor(); refreshSources(); } };
find("ev-device-select").onchange = event => { if (saveState.busy || editing) return; selectedId = event.target.value; render(); };
find("add-ev").onclick = () => { if (!saveState.busy && !editing && !saveState.dirty) find("ev-add-dialog").showModal(); };
/** Lock the Energy read and leave drafts untouched on cancellation or changed configuration/edit state.
 * Recheck energySources after the modal closes before initializing a new charge point's meter and power.
 */
find("continue-add-ev").onclick = async () => {
    if (saveState.busy || editing) return;
    const vehicle = document.querySelector('input[name="ev-add-kind"]:checked').value === "vehicle";
    find("ev-add-dialog").close();
    let source = null;
    if (!vehicle) {
        const opening = current;
        saveState.begin("Loading Home Assistant Energy devices…"); syncEditor();
        try { energySources = await api("v1/consumers"); current.energy_consumers = energySources.options; }
        catch (error) { find("status").textContent = error.message; return; }
        finally { saveState.end(); syncEditor(); }
        if (current !== opening || editing) return;
        find("add-ev").focus();
        source = await pickEnergySource(find("source-dialog"), energySources, "Add charge point");
        if (!source || current !== opening || saveState.busy || editing) return;
        source = energySources.options.find(option => option.entity_id === source.entity_id);
        if (!source || assignedEnergySources(energySources).has(source.entity_id)) {
            find("status").textContent = "The selected Energy device is no longer available. Refresh Home Assistant sources and choose an available device."; return;
        }
    }
    const row = vehicle ? {id: identity(), soc_entity: "", soc_timestamp_attribute: null, capacity_kwh: "", maximum_w: "", target_soc: 80, departure_time: "07:00"} : {id: identity(), energy_entity: source.entity_id, power_entity: source.power_entity || "", connected_entity: "", minimum_w: "", maximum_w: "", control: "read_only", vehicle_ids: []};
    editOriginId = selectedId;
    (vehicle ? draft.vehicles : draft.points).push(row); selectedId = row.id; editing = true;
    render(); changed();
    refreshSources();
};
find("remove-ev").onclick = removeSelected;
find("reload-ev").onclick = () => { find("ev-more").open = false; return saveState.refresh(load); };
find("settings").onsubmit = async (event) => { event.preventDefault(); if (saveState.busy || !editing) return; saveState.begin(); syncEditor(); try { await api("v1/ev/configuration", {configuration: draft, expected_version: current.configuration_version}); await load(); } catch (error) { find("status").textContent = error.message; saveState.update(error.message); } finally { saveState.end(); syncEditor(); } };
find("refresh").onclick = () => saveState.refresh(() => load(editOriginId), {discard: true}).finally(syncEditor);
find("refresh-evidence").onclick = () => saveState.refresh(load);
find("connect").onsubmit = (event) => { event.preventDefault(); token = find("token").value; load(); };
fetch(base + "setup/session", {redirect: "error"}).then((response) => response.json()).then((session) => { useHomeAssistantTimezone(session.timezone); if (session.ingress) load(); }).catch((error) => { find("status").textContent = error.message; });
