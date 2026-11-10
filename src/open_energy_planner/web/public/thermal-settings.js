/** Type-specific configuration reuses versioned app settings and shared save controls. */
import {SaveBar} from "./save-bar.js";
import {confirmAction} from "./confirmation.js";
import {bindSettingsActions} from "./settings-actions.js";
import {useHomeAssistantTimezone, sourceName} from "./display.js";
import {settingsSummary} from "./appliance-settings-detail.js";
import {identity} from "./identity.js";
import {sourceRefresh, retainInputs} from "./source-refresh.js";
import {assignedEnergySources, pickEnergySource} from "./energy-source-picker.js";
import {applianceSettingsApi} from "./appliance-settings-api.js";

const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = id => document.getElementById(id);
let token = "", current = null, energySources, draft = {stores: []}, savedVersion = null;
const api = applianceSettingsApi(base, () => token);
let selectedId = null, editOriginId = null, editing = false, conflict = false, requestGeneration = 0;
bindSettingsActions();
const saveState = new SaveBar({form: "thermal-settings", bar: "thermal-save-bar", save: "save-thermal", discard: "cancel-thermal", status: "thermal-save-state", read: () => draft, canSave: () => editing && !conflict && validDraft()});
find("cancel-thermal").textContent = "Cancel";

/** Treat every provider-supplied name and diagnostic as text, never HTML. */
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Preserve unavailable evidence and format temperature with one decimal by default. */
function decimal(value, digits = 1) { return Number.isFinite(value) ? value.toLocaleString(undefined, {minimumFractionDigits: digits, maximumFractionDigits: digits}) : "Unavailable"; }
/** Locate the selected editor in the full retained configuration draft. */
function selected() { return draft.stores.find(store => store.id === selectedId); }
/** Derive names from Home Assistant instead of introducing another editable identity. */
function displayName(store) { return current?.devices?.find(device => device.id === store.id)?.name || current?.energy_consumers?.find(source => source.entity_id === store.energy_entity)?.name || "New electric boiler"; }

/** Check every retained store, including drafts outside the selected editor. The server validates authoritatively. */
function validDraft() {
    return draft.stores.every(store => {
        const fields = ["volume_litres", "minimum_c", "maximum_c", "ambient_c", "loss_w_per_c", "heater_w"];
        return store.energy_entity && store.temperature_entity && store.power_entity && fields.every(key => Number.isFinite(store[key])) && store.volume_litres > 0 && store.minimum_c >= 0 && store.maximum_c > store.minimum_c && store.maximum_c <= 120 && store.loss_w_per_c >= 0 && store.heater_w > 0 && Number.isInteger(store.heater_w) && Array.isArray(store.draw_profile_kwh) && store.draw_profile_kwh.length === 24 && store.draw_profile_kwh.every(value => Number.isFinite(value) && value >= 0) && (!store.enabled || store.draw_profile_kwh.some(value => value > 0));
    });
}
/** Keep edit mode and the shared save state synchronized after rendering or an asynchronous operation. */
function syncEditor() {
    find("thermal-editor").classList.toggle("settings-readonly", !editing);
    for (const control of find("thermal-editor").querySelectorAll("input, select, button")) control.disabled = !editing || saveState.busy;
    find("thermal-store-select").disabled = saveState.busy || editing || !draft.stores.length;
    find("edit-thermal").disabled = saveState.busy || editing || !selected();
    find("add-thermal").disabled = saveState.busy;
    find("refresh-thermal").disabled = saveState.busy;
    find("refresh-thermal-evidence").disabled = saveState.busy;
    find("thermal-editor").hidden = !editing; find("thermal-summary").hidden = editing;
    find("thermal-description").textContent = editing ? "Edit this appliance's configuration. Changes are applied only after Save." : "Saved configuration. Choose Edit settings to make changes.";
    for (const id of ["save-thermal", "cancel-thermal"]) find(id).hidden = !editing;
    for (const id of ["edit-thermal", "add-thermal", "thermal-more"]) find(id).hidden = editing;
    saveState.update();
    if (editing && !saveState.busy) find("cancel-thermal").disabled = false;
}
/** Keep precise draft values while temperatures use one decimal when their editor is idle. */
function numberInput(parent, store, key, label, help, {min = 0, max, temperature = false, scale = 1, step = "any"} = {}) {
    const wrapper = node("label", label), input = node("input"); input.type = "number"; input.required = true; input.min = min; input.step = step; if (max !== undefined) input.max = max; input.dataset.field = key;
    const precise = () => Number.isFinite(store[key]) ? String(store[key] / scale) : "";
    const show = () => { input.value = temperature && Number.isFinite(store[key]) ? (store[key] / scale).toFixed(1) : precise(); };
    show(); input.onfocus = () => { input.value = precise(); input.select(); };
    input.oninput = () => {
        const scaled = input.value !== "" && input.validity.valid ? Number(input.value) * scale : null;
        // Native kW step validation rejects fractional watts before removing binary floating-point residue.
        store[key] = key === "heater_w" && scaled !== null ? Math.round(scaled) : scaled;
        saveState.update();
    };
    input.onblur = show; wrapper.append(input, node("small", help)); parent.append(wrapper); return input;
}
/** Retain missing configured sources as disabled choices rather than silently substituting another sensor. */
function sourceSelect(parent, store, key, label, help, choices) {
    const wrapper = node("label", label), select = node("select"); select.required = true; select.dataset.field = key;
    const blank = node("option", "Select a source"); blank.value = ""; select.append(blank);
    for (const [value, name] of choices) { const option = node("option", name); option.value = value; select.append(option); }
    if (store[key] && !choices.some(([value]) => value === store[key])) { const unavailable = node("option", `${store[key]}: Source unavailable`); unavailable.value = store[key]; unavailable.disabled = true; select.append(unavailable); }
    select.value = store[key] || ""; select.onchange = () => { store[key] = select.value; saveState.update(); };
    wrapper.append(select, node("small", help)); parent.append(wrapper); return select;
}
/** Offer eligible catalogue entities with their device context to distinguish similar sensors. */
function metadata(predicate) { return (current?.catalogue?.entities || []).filter(predicate).map(source => [source.entity_id, `${sourceName(source, "Sensor")}: ${source.name} (${source.entity_id})`]); }

/** Show a selected store without discarding edits made to any other store. */
function renderEditor() {
    const profileOpen = find("thermal-demand-profile")?.open ?? false;
    const selector = find("thermal-store-select"); selector.replaceChildren();
    for (const store of draft.stores) { const option = node("option", displayName(store)); option.value = store.id; selector.append(option); }
    if (!draft.stores.some(store => store.id === selectedId)) selectedId = draft.stores[0]?.id || null;
    selector.value = selectedId || "";
    const store = selected(), editor = find("thermal-editor"); editor.replaceChildren();
    find("thermal-empty").hidden = Boolean(store); find("thermal-detail").hidden = !store;
    if (!store) { syncEditor(); return; }
    find("thermal-title").textContent = displayName(store);
    const sources = node("div"); sources.className = "settings-fields"; sources.append(node("h3", "Home Assistant sources"));
    const assignedSources = assignedEnergySources(energySources);
    const savedMeter = current.configuration.stores.find(saved => saved.id === store.id)?.energy_entity;
    const meter = sourceSelect(sources, store, "energy_entity", "Boiler consumption meter", "Choose its Home Assistant Energy meter. Individual history is requested automatically. Heating commands wait until the separate consumption baseline is available. Planned heating replaces that baseline.", (current.energy_consumers || []).filter(source => source.entity_id === store.energy_entity || source.entity_id === savedMeter || !assignedSources.has(source.entity_id)).map(source => [source.entity_id, source.name]));
    meter.onchange = () => { if (meter.value === store.energy_entity) return; store.energy_entity = meter.value; store.power_entity = current.energy_consumers.find(source => source.entity_id === meter.value)?.power_entity || ""; retainInputs(renderEditor); saveState.update(); queueMicrotask(syncEditor); };
    sourceSelect(sources, store, "temperature_entity", "Measured water temperature", "Use a temperature sensor or water heater reporting °C or °F. Home Assistant report time determines freshness.", metadata(source => (source.entity_id.startsWith("sensor.") || source.entity_id.startsWith("water_heater.")) && source.device_class === "temperature" && ["°C", "°F"].includes(source.unit)));
    sourceSelect(sources, store, "power_entity", "Measured heater power", "Use the boiler's own electrical power sensor in W or kW.", metadata(source => source.entity_id.startsWith("sensor.") && source.device_class === "power" && ["W", "kW"].includes(source.unit)));
    editor.append(sources);
    const enabledLabel = node("label"), enabled = node("input"); enabled.type = "checkbox"; enabled.dataset.field = "enabled"; enabled.checked = store.enabled; enabled.onchange = () => { store.enabled = enabled.checked; saveState.update(); }; enabledLabel.append(enabled, node("span", "Allow heating schedules")); editor.append(enabledLabel);
    const physics = node("div"); physics.className = "settings-fields"; physics.append(node("h3", "Boiler and limits"));
    numberInput(physics, store, "volume_litres", "Water volume (litres)", "Declare the tank volume. The first model treats the water as one uniform temperature.", {min: 0.1});
    numberInput(physics, store, "heater_w", "Electrical heating power (kW)", "Fixed on/off electrical power. 1 kW equals 1,000 W. This first version is for a resistive electric heater.", {min: 0.001, max: 30, scale: 1000, step: 0.001});
    numberInput(physics, store, "minimum_c", "Minimum comfort temperature (°C)", "Your required comfort level. Unachievable comfort is reported explicitly.", {max: 120, temperature: true});
    numberInput(physics, store, "maximum_c", "Maximum planning temperature (°C)", "Choose a bound appropriate to the boiler. Its own thermostat and physical protection remain authoritative.", {max: 120, temperature: true});
    numberInput(physics, store, "ambient_c", "Tank surroundings temperature (°C)", "The temperature around the insulated tank. This non-freezing water model supports 0 to 60 °C, no higher than the maximum planning temperature.", {min: 0, max: 60, temperature: true});
    numberInput(physics, store, "loss_w_per_c", "Heat-loss coefficient (W/°C)", "Declare the tank's heat loss per degree of difference from its surroundings. This is a model assumption, not a measured value.", {max: 1000});
    editor.append(physics);
    const profile = node("details"); profile.id = "thermal-demand-profile"; profile.open = profileOpen; profile.append(node("summary", "Expected hot-water heat demand by hour"), node("p", "Enter expected thermal kWh drawn during each local clock hour. These declared rates are assumptions, not measured hot-water use. At least one hour must be positive before enabling planning."));
    const hours = node("div"); hours.className = "workspace-grid";
    store.draw_profile_kwh.forEach((energy, hour) => {
        const label = node("label", `${String(hour).padStart(2, "0")}:00 to ${String(hour + 1).padStart(2, "0")}:00 (thermal kWh)`), input = node("input"); input.type = "number"; input.min = 0; input.max = 100; input.step = "any"; input.required = true; input.dataset.hour = String(hour); input.value = energy ?? "";
        input.oninput = () => { store.draw_profile_kwh[hour] = input.value !== "" && input.validity.valid ? Number(input.value) : null; saveState.update(); }; label.append(input); hours.append(label);
    });
    profile.append(hours); editor.append(profile);
    renderSummary(store);
    syncEditor();
}

/** Keep the approved saved view separate from editable assumptions and current Energy requests. */
function renderSummary(store) {
    settingsSummary(find("thermal-summary"), [["Home Assistant sources", [["Consumption meter", store.energy_entity, true], ["Water temperature", store.temperature_entity, true], ["Heater power", store.power_entity, true]]], ["Boiler and limits", [["Water volume", decimal(store.volume_litres) + " litres"], ["Heating power", decimal(store.heater_w / 1000, 3) + " kW"], ["Comfort temperature", decimal(store.minimum_c) + " °C"], ["Maximum temperature", decimal(store.maximum_c) + " °C"], ["Planning", store.enabled ? "Permitted" : "Paused"]]]]);
    const assumptions = node("details"); assumptions.append(node("summary", "Heat-loss and hot-water assumptions"));
    const facts = node("div"); settingsSummary(facts, [["Declared assumptions", [["Surroundings temperature", decimal(store.ambient_c) + " °C"], ["Heat loss", decimal(store.loss_w_per_c, 3) + " W/°C"]]]]);
    const list = node("dl"); list.className = "settings-summary";
    store.draw_profile_kwh.forEach((energy, hour) => { const row = node("div"); row.append(node("dt", `${String(hour).padStart(2, "0")}:00`), node("dd", decimal(energy, 3) + " thermal kWh")); list.append(row); });
    assumptions.append(facts, node("p", "Expected heat drawn during each local clock hour. These are planning assumptions, not measured hot-water usage."), list); find("thermal-summary").append(assumptions);
    const saved = current.configuration.stores.find(row => row.id === store.id);
    settingsSummary(find("thermal-request-summary"), [["Saved request", [["Comfort request", saved ? decimal(saved.minimum_c) + " °C" : "Not saved yet"], ["Planning", saved ? saved.enabled ? "Permitted" : "Paused" : "Not saved yet"]]]]);
}

/** Load settings only on explicit transitions. Background evidence never replaces an edited draft. */
async function load(selection = selectedId) {
    const generation = ++requestGeneration;
    const results = await Promise.allSettled([api("v1/thermal"), api("v1/consumers")]);
    if (generation !== requestGeneration) return false;
    const failed = results.find(result => result.status === "rejected");
    if (failed) throw failed.reason;
    const [data, consumers] = results.map(result => result.value);
    energySources = consumers; data.energy_consumers = consumers.options;
    current = data; useHomeAssistantTimezone(data.timezone || data.catalogue?.timezone);
    draft = structuredClone(data.configuration || {stores: []}); savedVersion = data.configuration_version; selectedId = selection;
    editing = false; conflict = false; find("connect").hidden = true; find("thermal-settings").hidden = false; find("status").textContent = data.issue || "";
    renderEditor(); saveState.loaded(); syncEditor(); return true;
}

/** Retain server revision conflicts and keep input/focus untouched while refreshing only evidence. */
async function refreshEvidence() {
    if (!current || saveState.busy) return;
    const generation = requestGeneration;
    try {
        const data = await api("v1/thermal");
        if (generation !== requestGeneration || saveState.busy) return;
        current = data; useHomeAssistantTimezone(data.timezone || data.catalogue?.timezone);
        conflict = data.configuration_version !== savedVersion;
        if (selected()) renderSummary(selected());
        find("status").textContent = data.issue || ""; 
        saveState.update(conflict ? "Thermal settings changed elsewhere. Cancel and reload before editing or saving." : undefined);
    } catch (error) { find("status").textContent = error.message; }
}
find("thermal-store-select").onchange = event => { selectedId = event.target.value; renderEditor(); };
const refreshSources = sourceRefresh(api, "thermal", find("refresh-thermal-sources"), find("status"), async (catalogue, signal) => {
    const opening = current;
    if (saveState.busy || !opening) return;
    const consumers = await api("v1/consumers", undefined, signal);
    if (saveState.busy || current !== opening) return;
    energySources = consumers; current.energy_consumers = consumers.options;
    current.catalogue = catalogue;
    retainInputs(renderEditor);
    saveState.update();
    syncEditor();
});
find("refresh-thermal-sources").onclick = () => { if (!saveState.busy) return refreshSources(); };
find("edit-thermal").onclick = () => { editOriginId = selectedId; editing = true; syncEditor(); refreshSources(); };
/** Recheck the latest Energy snapshot after the modal closes without replacing existing drafts or saved configuration. */
find("add-thermal").onclick = async () => {
    if (saveState.busy || editing || saveState.dirty) return;
    const opening = current;
    saveState.begin("Loading Home Assistant Energy devices…"); syncEditor();
    try { energySources = await api("v1/consumers"); current.energy_consumers = energySources.options; }
    catch (error) { find("status").textContent = error.message; return; }
    finally { saveState.end(); syncEditor(); }
    if (current !== opening || editing) return;
    find("add-thermal").focus();
    const chosen = await pickEnergySource(find("source-dialog"), energySources, "Add electric boiler");
    if (!chosen || current !== opening || saveState.busy || editing) return;
    const source = energySources.options.find(option => option.entity_id === chosen.entity_id);
    if (!source || assignedEnergySources(energySources).has(source.entity_id)) {
        find("status").textContent = "The selected Energy device is no longer available. Refresh Home Assistant sources and choose an available device."; return;
    }
    const store = {id: identity(), energy_entity: source.entity_id, temperature_entity: "", power_entity: source.power_entity || "", volume_litres: null, minimum_c: null, maximum_c: null, ambient_c: null, loss_w_per_c: null, heater_w: null, draw_profile_kwh: Array(24).fill(0), enabled: false};
    editOriginId = selectedId; draft.stores.push(store); selectedId = store.id; editing = true; renderEditor(); saveState.update(); refreshSources();
};
find("thermal-settings").onsubmit = async event => {
    event.preventDefault(); if (saveState.busy || !editing || conflict || !validDraft()) return;
    ++requestGeneration; saveState.begin(); syncEditor();
    try { await api("v1/thermal/configuration", {configuration: draft, expected_version: savedVersion}); await load(); }
    catch (error) { find("status").textContent = error.message; saveState.update(`${error.message} If the response was interrupted, settings may have been saved. Reload before retrying.`); }
    finally { saveState.end(); syncEditor(); }
};
find("cancel-thermal").onclick = () => saveState.refresh(() => load(editOriginId), {discard: true}).finally(syncEditor);
find("refresh-thermal").onclick = () => { find("thermal-more").open = false; return saveState.refresh(load).finally(syncEditor); };
find("remove-thermal").onclick = async () => {
    find("thermal-more").open = false;
    const store = selected(), openingDraft = draft, openingId = selectedId;
    if (saveState.busy || editing || !store) return;
    if (!await confirmAction({title: "Remove electric boiler?", message: `${displayName(store)} will be removed from these settings.\n\nHome Assistant sources and historical evidence are retained. Choose Save settings to apply the removal. Cancel in the editor restores the saved settings.`, confirmLabel: "Remove boiler", returnFocus: find("thermal-more").querySelector("summary")})) return;
    if (saveState.busy || editing || conflict || draft !== openingDraft || selectedId !== openingId || selected() !== store) return;
    editOriginId = selectedId; draft.stores = draft.stores.filter(store => store.id !== selectedId); selectedId = null; editing = true; renderEditor(); saveState.update();
};
find("refresh-thermal-evidence").onclick = refreshEvidence;
find("connect").onsubmit = async event => { event.preventDefault(); token = find("token").value; try { await load(); } catch (error) { find("status").textContent = error.message; } };
fetch(base + "setup/session", {redirect: "error", cache: "no-store"}).then(response => response.json()).then(session => { useHomeAssistantTimezone(session.timezone); if (session.ingress) return load(); }).catch(error => { find("status").textContent = error.message; });

