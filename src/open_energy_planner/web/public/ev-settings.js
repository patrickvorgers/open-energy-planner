/** Type-specific configuration reuses versioned app settings and shared save controls. */
import {percentage, percentageInput, inputNumber, optionLabel, useHomeAssistantTimezone, sourceName} from "./display.js";
import {settingsSummary} from "./appliance-settings-detail.js";
import {identity} from "./identity.js";
import {SaveBar} from "./save-bar.js";
import {confirmAction} from "./confirmation.js";
import {bindApplianceSettingsNavigation} from "./appliance-settings-navigation.js";
import {bindSettingsActions} from "./settings-actions.js";
import {sourceRefresh, retainInputs} from "./source-refresh.js";
import {applianceSettingsApi} from "./appliance-settings-api.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = (id) => document.getElementById(id);
const VEHICLE_FIELDS = ["id", "soc_entity", "soc_timestamp_attribute", "capacity_kwh", "maximum_w", "target_soc", "departure_time"];
const POINT_FIELDS = ["id", "energy_entity", "power_entity", "connected_entity", "minimum_w", "maximum_w", "control", "vehicle_ids"];
const WATTS_PER_KW = 1000;
let token = "", current, draft, editing = false, selectedId = null, editOriginId = null;
const api = applianceSettingsApi(base, () => token);
const integrationSelections = new Map();
const saveState = new SaveBar({form: "settings", bar: "ev-save-bar", save: "save", discard: "refresh", status: "dirty", read: () => draft, canSave: () => editing});
bindApplianceSettingsNavigation(saveState);
find("refresh").textContent = "Cancel";
find("settings").hidden = true;
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
function changed() { saveState.update(); }
/** Select one existing identity without merging vehicle and charge-point configuration models. */
function selected() { return draft?.vehicles.find(row => row.id === selectedId) || draft?.points.find(row => row.id === selectedId); }
/** Show precise display kW while physical configuration retains whole watts. */
function power(value) { return Number.isFinite(value) ? (value / WATTS_PER_KW).toLocaleString(undefined, {minimumFractionDigits: 1, maximumFractionDigits: 3}) + " kW" : "Unavailable"; }
/** Keep source-derived names separate from immutable OEP identities. */
function displayName(row, vehicle) { return (vehicle ? current.vehicles : current.points).find(saved => saved.id === row.id)?.name || (vehicle ? current.catalogue.entities.find(entity => entity.entity_id === row.soc_entity)?.integration_name || "New vehicle" : "New charge point"); }
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
    if (row[key] && !options.some(([value]) => value === row[key]) && ["soc_entity", "power_entity", "connected_entity"].includes(key)) {
        const source = current.catalogue.entities.find(entity => entity.entity_id === row[key]);
        const option = node("option", `${source?.name || row[key]}: Current selection outside this list`); option.value = row[key]; control.append(option);
    }
    control.value = row[key] || ""; control.onchange = () => { row[key] = control.value; changed(); };
    wrapper.append(control, node("small", help)); card.append(wrapper);
    if (!options.length) wrapper.append(node("p", emptyMessage));
    return control;
}
function metadata(predicate, integration) { return current.catalogue.entities.filter(row => predicate(row) && (integration === undefined || (row.integration_id || "other") === integration)).map((row) => { const name = sourceName(row, "Sensor"); return [row.entity_id, `${name}${row.name === name ? "" : ": " + row.name} (${row.entity_id})`]; }); }
/** Ask for an actual HA config entry before narrowing source roles, retaining unregistered templates. */
function integrationPicker(card, row, vehicle) {
    const candidates = current.catalogue.entities.filter(entity => vehicle ? isVehicleSoc(entity) : entity.entity_id.startsWith("binary_sensor.") || entity.device_class === "power" && ["W", "kW"].includes(entity.unit));
    if (!candidates.some(entity => entity.integration_id)) return undefined;
    const groups = new Map(candidates.map(entity => [entity.integration_id || "other", entity.integration_name || "Other Home Assistant sources"]));
    const source = current.catalogue.entities.find(entity => entity.entity_id === (vehicle ? row.soc_entity : row.power_entity || row.connected_entity || row.energy_entity));
    const selectedIntegration = integrationSelections.get(row.id) ?? (source ? source.integration_id || "other" : "");
    const selection = {integration: selectedIntegration};
    const control = select(card, selection, "integration", "Home Assistant integration", vehicle ? "Choose the integration that manages this car. Its title supplies the vehicle name." : "Choose the integration that reports the charge point's power and physical connection. Its Energy meter is selected separately.", [...groups].sort((left, right) => left[1].localeCompare(right[1])));
    control.required = false;
    control.parentElement.style.gridColumn = "1 / -1";
    control.onchange = () => {
        integrationSelections.set(row.id, control.value);
        if (vehicle && control.value) {
            const entity = current.catalogue.entities.find(source => source.entity_id === row.soc_entity);
            if ((entity?.integration_id || "other") !== control.value) row.soc_entity = "";
        }
        render(); changed();
    };
    return selectedIntegration;
}
/** Percentage SoC may omit HA's optional battery class, but incompatible classes remain excluded. */
function isVehicleSoc(entity) { return entity.entity_id.startsWith("sensor.") && entity.unit === "%" && ["battery", ""].includes(entity.device_class); }
/** Filter connection candidates by visible names without guessing that any binary sensor means plugged in. */
function connectedSource(card, row, integration) {
    const group = node("fieldset"); group.className = "settings-fields"; group.append(node("legend", "Physical connection")); card.append(group);
    const candidates = metadata(entity => entity.entity_id.startsWith("binary_sensor."), integration);
    const searchLabel = node("label"), search = node("input"); searchLabel.className = "ev-field"; searchLabel.append(node("span", "Find connection sensor")); search.type = "search";
    search.setAttribute("aria-label", "Find connection sensor");
    searchLabel.append(search, node("small", "Search the device, sensor name or entity ID.")); group.append(searchLabel);
    const control = select(group, row, "connected_entity", "Vehicle connected", "Choose a sensor that stays On while plugged in, even when charging is paused. Charging, door and fault sensors do not establish a connection.", candidates, integration === "" ? "Choose the Home Assistant integration first." : undefined);
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
        const integration = integrationPicker(card, row, true);
        select(card, row, "soc_entity", "Vehicle state of charge", "Select this car's measured battery percentage, not the stationary home battery.", metadata(isVehicleSoc, integration), integration === "" ? "Choose the Home Assistant integration first." : undefined);
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
        const integration = integrationPicker(card, row, false);
        select(card, row, "energy_entity", "Charging consumption meter", "Configured in Home Assistant Energy. Separate from instantaneous power.", current.energy_consumers.map((entity) => [entity.entity_id, entity.name]));
        select(card, row, "power_entity", "Measured AC charging power", "Positive W or kW consumed by this charge point.", metadata((entity) => entity.device_class === "power" && ["W", "kW"].includes(entity.unit), integration), integration === "" ? "Choose the Home Assistant integration first." : undefined);
        connectedSource(card, row, integration);
        card.append(node("h3", "Charging limits"));
        const control = select(card, row, "control", "Control capability", "Physical actions remain in your HA automation. Measurement only never produces a charging request.", [["read_only", "Measurement only"], ["on_off", "On/off at fixed power"], ["variable", "Variable power"]]);
        control.parentElement.style.gridColumn = "1 / -1";
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
        integrationSelections.clear();
        draft = {vehicles: current.vehicles.map((row) => Object.fromEntries(VEHICLE_FIELDS.map((key) => [key, row[key]]))), points: current.points.map((row) => Object.fromEntries(POINT_FIELDS.map((key) => [key, row[key]])))};
        selectedId = selection; editing = false; find("connect").hidden = true; find("settings").hidden = false; find("status").textContent = current.issue || ""; render(); saveState.loaded(); syncEditor();
    } catch (error) { find("status").textContent = error.message; saveState.update(error.message); }
}
const refreshSources = sourceRefresh(api, "ev", find("refresh-ev-sources"), find("status"), catalogue => {
    if (saveState.busy || !current) return;
    current.catalogue = catalogue;
    retainInputs(render);
    saveState.update();
});
find("refresh-ev-sources").onclick = () => { if (!saveState.busy) return refreshSources(); };
find("edit-ev").onclick = () => { if (!saveState.busy && selected()) { editOriginId = selectedId; editing = true; syncEditor(); refreshSources(); } };
find("ev-device-select").onchange = event => { if (saveState.busy || editing) return; selectedId = event.target.value; render(); };
find("add-ev").onclick = () => { if (!saveState.busy && !editing && !saveState.dirty) find("ev-add-dialog").showModal(); };
find("continue-add-ev").onclick = () => {
    if (saveState.busy || editing) return;
    const vehicle = document.querySelector('input[name="ev-add-kind"]:checked').value === "vehicle";
    const row = vehicle ? {id: identity(), soc_entity: "", soc_timestamp_attribute: null, capacity_kwh: "", maximum_w: "", target_soc: 80, departure_time: "07:00"} : {id: identity(), energy_entity: "", power_entity: "", connected_entity: "", minimum_w: "", maximum_w: "", control: "read_only", vehicle_ids: []};
    editOriginId = selectedId;
    (vehicle ? draft.vehicles : draft.points).push(row); selectedId = row.id; editing = true;
    find("ev-add-dialog").close(); render(); changed();
    refreshSources();
};
find("remove-ev").onclick = removeSelected;
find("reload-ev").onclick = () => { find("ev-more").open = false; return saveState.refresh(load); };
find("settings").onsubmit = async (event) => { event.preventDefault(); if (saveState.busy || !editing) return; saveState.begin(); syncEditor(); try { await api("v1/ev/configuration", {configuration: draft, expected_version: current.configuration_version}); await load(); } catch (error) { find("status").textContent = error.message; saveState.update(error.message); } finally { saveState.end(); syncEditor(); } };
find("refresh").onclick = () => saveState.refresh(() => load(editOriginId), {discard: true}).finally(syncEditor);
find("refresh-evidence").onclick = () => saveState.refresh(load);
find("connect").onsubmit = (event) => { event.preventDefault(); token = find("token").value; load(); };
fetch(base + "setup/session", {redirect: "error"}).then((response) => response.json()).then((session) => { useHomeAssistantTimezone(session.timezone); if (session.ingress) load(); }).catch((error) => { find("status").textContent = error.message; });
