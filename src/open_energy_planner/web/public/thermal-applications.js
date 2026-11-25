/** Configure area-based and named applications using the existing thermal settings draft. */
import {settingsSummary} from "./appliance-settings-detail.js";
import {when} from "./display.js";

export const SPACE_HEATING = "space_heating";
export const HOT_WATER = "hot_water";
const MINIMUM_FULL_LOAD_POWER_W = 100, MAXIMUM_FULL_LOAD_POWER_W = 12000;

/** Return the saved application type label. */
export function applicationType(application) {
    return application.kind === SPACE_HEATING ? "Space heating" : "Domestic hot water";
}

/** Validate the preheating margin against the same limits as the saved settings. */
function validMargin(margin) {
    return Number.isFinite(margin) && margin >= 0 && margin <= 3;
}

/** Accept an omitted rating or a whole number of watts in the supported range. */
function validFullLoadPower(power) {
    return power === undefined || (Number.isInteger(power) && power >= MINIMUM_FULL_LOAD_POWER_W && power <= MAXIMUM_FULL_LOAD_POWER_W);
}

/** Reject incomplete edits before submitting the combined thermal draft. */
export function validApplication(application) {
    if (application.kind === HOT_WATER) return Boolean(application.name.trim());
    if (!application.area_id) return false;
    if (!application.sources.every(source => validFullLoadPower(source.full_load_power_w))) return false;
    if (!application.sources.some(source => source.allow_scheduling)) return true;
    return Boolean(application.climate_entity) && validMargin(application.maximum_preheat_c);
}

/** Create one text-only form or display element. */
function node(tag, value) {
    const element = document.createElement(tag);
    if (value !== undefined) element.textContent = value;
    return element;
}

/** Wrap a control with a visible label. */
function field(label, control) {
    const wrapper = node("label", label);
    control.setAttribute("aria-label", label);
    wrapper.append(control);
    return wrapper;
}

/** Create an explicitly owned settings action. */
function action(label, onClick) {
    const button = node("button", label);
    button.type = "button";
    button.className = "secondary";
    button.onclick = onClick;
    return button;
}

/** Group climate entities by selected HA area and retain the explicit existing choice. */
function climateOptions(select, app, catalogue, metadata) {
    const groups = new Map((catalogue.areas || []).map(area => {
        const group = node("optgroup");
        group.label = "In " + area.name;
        return [area.id, group];
    }));
    const local = groups.get(app.area_id), unassigned = node("optgroup");
    unassigned.label = "No Home Assistant area";
    for (const source of metadata.values()) {
        if (!source.entity_id.startsWith("climate.")) continue;
        const group = groups.get(source.area_id) || unassigned;
        group.append(new Option(source.name + " (" + source.entity_id + ")", source.entity_id));
    }
    const ordered = [local, ...[...groups.values()].filter(group => group !== local), unassigned];
    select.replaceChildren(new Option("No thermostat linked", ""),
        ...ordered.filter(group => group?.childElementCount));
    if (app.climate_entity && !metadata.has(app.climate_entity)) {
        select.add(new Option(app.climate_entity + ": Source unavailable", app.climate_entity));
    }
    select.value = app.climate_entity || "";
}

/** Offer every eligible Energy source through one modal and an explicit area filter. */
function chooseSource(dialog, app, options, metadata, add) {
    const form = node("form"), title = node("h2", "Add heat source");
    title.id = "thermal-source-title";
    const select = node("select");
    select.required = true;
    const all = node("input");
    all.type = "checkbox";
    const allLabel = node("label");
    allLabel.className = "settings-switch";
    allLabel.append(all, node("span", "Show devices from all areas"));
    allLabel.hidden = app.kind !== SPACE_HEATING;
    const status = node("p");
    status.setAttribute("role", "status");
    const actions = node("div"), confirm = node("button", "Add source");
    actions.className = "settings-actions";
    actions.append(confirm, action("Cancel", () => dialog.close()));
    form.append(title, field("Home Assistant Energy device", select), allLabel, status, actions);
    const available = options.filter(option => !app.sources.some(source => source.energy_entity === option.entity_id));
    const update = () => {
        const choices = allLabel.hidden || all.checked ? available :
            available.filter(option => metadata.get(option.entity_id)?.area_id === app.area_id);
        select.replaceChildren(new Option("Choose an Energy device", ""),
            ...choices.map(option => new Option(option.name || option.entity_id, option.entity_id)));
        confirm.disabled = !choices.length;
        status.textContent = choices.length ? "" : available.length
            ? "No available devices in this area. Show devices from all areas to see other options."
            : "No additional heat sources are available.";
    };
    all.onchange = update;
    form.onsubmit = event => {
        event.preventDefault();
        if (!select.value) return;
        app.sources.push({energy_entity: select.value, allow_scheduling: false});
        dialog.close();
        add();
    };
    dialog.replaceChildren(form);
    update();
    dialog.showModal();
}

/** Render the saved application facts and the draft editor with shared OEP controls. */
export function renderApplication({app, current, draft, editor, summary, dialog, editing, rerender, changed}) {
    const catalogue = current.catalogue || {};
    const metadata = new Map((catalogue.entities || []).map(source => [source.entity_id, source]));
    const options = current.energy_consumers || [];
    const optionById = new Map(options.map(option => [option.entity_id, option]));
    const projected = current.applications?.find(application => application.id === app.id);
    const space = app.kind === SPACE_HEATING;
    const sourceName = source => optionById.get(source.energy_entity)?.name || source.energy_entity;
    const boilerFor = source => draft.stores.find(store => store.energy_entity === source.energy_entity);
    const renderSource = source => {
        const meter = source.energy_entity;
        const power = optionById.get(meter)?.power_entity;
        const section = node("section");
        section.className = "thermal-heat-source settings-facts-group";
        section.append(node("h4", sourceName(source)), node("h5", "Energy source"), node("code", meter));
        if (power) section.append(node("h5", "Power source"), node("code", power));
        const facts = editing ? [] : [["Planning permission", source.allow_scheduling ? "Allowed" : "Forecast only"]];
        if (!editing && space && source.allow_scheduling && source.full_load_power_w !== undefined) {
            facts.push(["Electrical input at full load", `${source.full_load_power_w.toLocaleString()} W`]);
        }
        if (!space) facts.push(["Scheduling model", boilerFor(source) ? "Electric boiler" : "Forecast only"]);
        const mode = current.source_modes?.[meter]?.application_id;
        const uses = draft.applications.filter(application => application.sources.some(link => link.energy_entity === meter));
        if (uses.length > 1) {
            const modeApplication = uses.find(application => application.id === mode);
            facts.push(["Reported operating mode", current.applications?.find(row => row.id === mode)?.name || modeApplication?.name || "Not reported"]);
            const attribution = current.mode_attribution?.[meter];
            if (attribution) {
                const energy = value => value.toLocaleString(undefined, {maximumFractionDigits: 2}) + " kWh";
                const period = attribution.retained_from ? `Since ${when(attribution.retained_from)}` : "Last 30 days";
                facts.push(["Attribution period", period],
                    ["Attributed electricity", energy(attribution.applications_kwh[app.id] ?? 0)],
                    ["Unattributed electricity at source", energy(attribution.unknown_kwh)]);
            }
        }
        if (facts.length) {
            const detail = node("div");
            settingsSummary(detail, [["", facts]]);
            section.append(detail);
        }
        return section;
    };
    const hasScheduling = space && app.sources.some(source => source.allow_scheduling);
    const facts = space ? [["Heating area", projected?.name || app.name],
        ["Temperature and target", metadata.get(app.climate_entity)?.name || app.climate_entity || "No thermostat linked"],
        ["Outdoor temperature", "Automatic from weather data"]] : [];
    if (hasScheduling) facts.push(["Maximum preheating", `+${app.maximum_preheat_c} °C`]);
    if (projected?.actual) facts.push(["Current thermostat target", `${projected.actual.target_c} °C`]);
    if (projected?.preheat_limit_c != null) facts.push(["Current preheating limit", `${projected.preheat_limit_c} °C`]);
    if (projected?.planning_status) facts.push(["Planning status", projected.planning_status]);
    settingsSummary(summary, space ? [["Home Assistant sources", facts]] : []);
    const heatSources = node("div");
    if (!editing) heatSources.append(...app.sources.map(renderSource));
    summary.append(node("h3", "Heat sources"), heatSources);
    if (!app.sources.length) heatSources.append(node("p", "No heat sources linked."));
    if (!editing) return;

    const fields = node("div");
    fields.className = "settings-fields";
    if (space) {
        const area = node("select"), climate = node("select");
        area.dataset.field = "heating-area";
        climate.dataset.field = "climate-source";
        area.required = true;
        const occupied = new Set(draft.applications.filter(row => row !== app).map(row => row.area_id));
        area.append(new Option("Choose a Home Assistant area", ""),
            ...(catalogue.areas || []).filter(row => !occupied.has(row.id)).map(row => new Option(row.name, row.id)));
        if (app.area_id && ![...area.options].some(option => option.value === app.area_id)) {
            area.add(new Option(app.name + ": Area unavailable", app.area_id));
        }
        area.value = app.area_id || "";
        climateOptions(climate, app, catalogue, metadata);
        area.onchange = () => {
            app.area_id = area.value || null;
            app.name = area.value ? area.selectedOptions[0].text : "";
            rerender();
        };
        climate.onchange = () => { app.climate_entity = climate.value || null; changed(); };
        fields.append(field("Heating area", area), field("Temperature and target", climate));
        if (hasScheduling) {
            const margin = node("input");
            margin.type = "number";
            margin.required = true;
            margin.min = "0";
            margin.max = "3";
            margin.step = "any";
            margin.value = app.maximum_preheat_c ?? "";
            margin.oninput = () => { app.maximum_preheat_c = margin.value === "" ? null : margin.valueAsNumber; changed(); };
            const label = field("Maximum preheating (°C)", margin);
            label.append(node("small", "Degrees above the current thermostat target."));
            fields.append(label);
        }
    } else {
        const name = node("input");
        name.required = true;
        name.maxLength = 160;
        name.value = app.name;
        name.oninput = () => { app.name = name.value; changed(); };
        fields.append(field("Application name", name));
    }
    editor.append(fields, node("h3", "Heat sources"));
    if (!app.sources.length) editor.append(node("p", "No heat sources linked."));
    const updateSources = () => {
        if (space && !app.sources.some(source => source.allow_scheduling) && !validMargin(app.maximum_preheat_c)) {
            app.maximum_preheat_c = null;
        }
        rerender();
    };
    for (const source of app.sources) {
        const section = renderSource(source);
        const permit = node("input"), label = node("label");
        permit.type = "checkbox";
        permit.dataset.field = "allow-scheduling:" + source.energy_entity;
        permit.checked = source.allow_scheduling;
        permit.onchange = () => {
            source.allow_scheduling = permit.checked;
            if (!permit.checked && !validFullLoadPower(source.full_load_power_w)) delete source.full_load_power_w;
            updateSources();
        };
        label.className = "settings-switch";
        const permission = node("span", "Allow scheduling");
        permit.setAttribute("aria-label", "Allow scheduling");
        permission.append(node("small", space
            ? "Uses a validated model and a current thermostat target to plan preheating."
            : "Saves your permission for future scheduling."));
        label.append(permit, permission);
        section.append(label);
        if (space && source.allow_scheduling) {
            const power = node("input");
            power.type = "number";
            power.dataset.field = "full-load-power:" + source.energy_entity;
            power.min = MINIMUM_FULL_LOAD_POWER_W;
            power.max = MAXIMUM_FULL_LOAD_POWER_W;
            power.step = "1";
            power.value = source.full_load_power_w ?? "";
            power.oninput = () => {
                if (power.value === "" && !power.validity.badInput) delete source.full_load_power_w;
                else source.full_load_power_w = power.valueAsNumber;
                changed();
            };
            const powerLabel = field("Electrical input at full load (W)", power);
            powerLabel.append(node("small", "Use the electrical power drawn at full load in heating mode. Leave blank for forecast only."));
            section.append(powerLabel);
        }
        section.append(action("Remove " + sourceName(source), () => {
            app.sources = app.sources.filter(row => row !== source);
            updateSources();
        }));
        editor.append(section);
    }
    const occupiedMeters = new Set(current.occupied_meters || []);
    const add = action("Add heat source", () => chooseSource(dialog, app,
        options.filter(option => !occupiedMeters.has(option.entity_id)), metadata, rerender));
    if (space && !app.area_id) add.dataset.requiresArea = "";
    editor.append(add);
}
