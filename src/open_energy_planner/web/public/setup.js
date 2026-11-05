import {money, when, percentage, percentageInput, inputNumber, useHomeAssistantTimezone, sourceName} from "./display.js";
import {SaveBar} from "./save-bar.js";
import {InstallationLayout} from "./installation-layout.js";
import {PriceSourceSettings} from "./price-sources.js";
import {azimuthLabel, DIRECTION_CHOICES, editableSolarPanels, DEFAULT_PANEL_TILT_DEGREES} from "./solar-geometry.js";
// The browser collects settings. Domain validation, storage and planning stay in the app.
const $ = (id) => document.getElementById(id);
const MAX_SOLAR_PANEL_GROUPS = 16, MAX_PANEL_NAME_LENGTH = 200;
let token = "", ingressSession = false,
    config,
    step = 0,
    busy = false,
    discoveryTimer;
let requestedStrategy = null, installationBaseline = null, installationReady = false;
let pendingSolarSources = [];
/** Include precise drafts and import provenance even when their visible controls are unchanged. */
const installationValues = () => [[...$("wizard").elements]
    .filter((control) => control.id !== "strategy" && !control.hasAttribute("data-view-control") && ["INPUT", "SELECT", "TEXTAREA"].includes(control.tagName))
    .map((control) => [control.id || control.name || control.dataset.field || "", control.type, control.type === "checkbox" ? control.checked : control.dataset.originalPanelName ?? control.dataset.precisePercentage ?? control.value]),
    solarRows.map(editor => [editor.old.entity_id, (editor.old.ha_imports || []).map(provider => [
        provider.adapter, provider.entry_id, provider.inverter_limit_w ?? null,
        provider.panels.map(panel => [panel.panel_id, panel.capacity_w, panel.azimuth, panel.tilt, panel.name || ""]),
    ])])];
/** Imported Energy members need confirmation even when their prefilled controls have not changed. */
const installationDirty = () => installationReady && (pendingSolarSources.length > 0 || JSON.stringify(installationValues()) !== installationBaseline);
const saveState = new SaveBar({form: "wizard", bar: "installation-save-bar", save: "save", discard: "discard-installation", status: "installation-save-state", canSave: () => step === 5 ? priceSettings.dirty : step === 4 ? requestedStrategy !== null && $("strategy").value !== requestedStrategy : !layout.wizard || step === 3, read: () => [
    ...(installationDirty() ? ["installation"] : []),
    ...(requestedStrategy !== null && $("strategy").value !== requestedStrategy ? ["strategy"] : []),
    ...(priceSettings.dirty ? ["price-sources"] : []),
]});
const layout = new InstallationLayout(saveState, value => { if (!busy) showStep(value); });
const priceSettings = new PriceSourceSettings(() => { saveState.update(); layout.sync(); });
const help = {
    energy_entity: "The cumulative battery meter configured in the Energy dashboard. It identifies which battery these measurements and ratings belong to.",
    charge_energy_entity: "Cumulative energy entering the battery. Managed read-only in Home Assistant Settings > Dashboards > Energy.",
    entity_id:
        "Select the cumulative AC solar production meter. Battery discharge must not be included.",
    capacity:
        "Total panel nameplate capacity in kWp. Ten 400 W panels are 4 kWp.",
    orientation:
        "Choose the main panel direction. East-west means panels split across those directions.",
    inverter_id:
        "A stable name for this converter. Reuse it only for sources on the same physical inverter.",
    rating: "Maximum AC power in kW from the inverter specification. A hybrid shares this limit between solar and battery.",
    efficiency:
        "Conversion efficiency as a fraction: 0.96 means 96.0%. Retain the default unless you know a better value.",
    hybrid: "Solar and battery share this inverter and can exchange energy directly through DC.",
    can_curtail:
        "Allow proposed solar on/off commands. Your HA automation must implement them.",
    soc_entity:
        "Battery state of charge in percent. Managed read-only in Home Assistant Energy.",
    power_entity:
        "Normalized battery power from Home Assistant Energy: discharge positive, charging negative. Change the source/direction there.",
    can_follow_grid: "Allow this battery or its inverter to follow the connection-level zero-grid command. Home Assistant automations configure the physical controller. Otherwise the planner publishes fixed power only.",
    power_inverted:
        "Home Assistant Energy normalizes the power direction. No additional inversion is applied.",
    capacity_kwh:
        "Usable battery energy in kWh from Home Assistant Energy. Read-only here. Not current stored energy or inverter power.",
    charge: "Maximum permitted charging power at the battery terminal, in kW.",
    discharge:
        "Maximum permitted discharging power at the battery terminal, in kW.",
    connection:
        "Choose its own AC inverter or the named shared hybrid. This determines losses and shared limits.",
    minimum_soc:
        "Hard planning reserve in percent. Physical battery protection remains local.",
    recover_reserve:
        "When measured energy is below the minimum, restore it as early as the planning intervals and physical limits allow, before optimizing costs. When off, the strategy chooses when to charge. Further discharge below reserve is always forbidden.",
    cycle_fee:
        "Wear cost in your contract currency per full equivalent discharge cycle. Zero excludes it. Partial cycles count proportionally.",
};
const solarRows = [],
    batteryRows = [];
const status = (message) => {
    $("status").textContent = message;
    $("status").hidden = installationReady;
    if (installationReady) saveState.update(message);
};
const identity = (prefix) =>
    `${prefix}_${Array.from(crypto.getRandomValues(new Uint32Array(4)), (n) => n.toString(16)).join("")}`;
async function api(path, method = "GET", body) {
    const response = await fetch(document.querySelector('meta[name="oep-api-base"]').content + path.replace(/^\//, ""), {
        method,
        headers: {
            Authorization: `Bearer ${token}`,
            "Content-Type": "application/json",
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        redirect: "error",
    });
    if (!response.ok)
        throw new Error(
            response.status === 401
                ? "The installation token was rejected."
                : `${response.status}: ${await response.text()}`,
        );
    return response.json();
}
/** Build a validated editor. Minimum SoC rounds to one idle decimal without changing its precise draft. */
function field(parent, title, key, value, options = {}) {
    const label = document.createElement("label");
    const caption = document.createElement("span");
    caption.textContent = title;
    label.append(caption);
    if (options.list) options.choices = sensorChoices(options.list, value);
    const input = document.createElement(options.choices ? "select" : "input");
    input.dataset.key = key;
    input.setAttribute("aria-label", title);
    if (options.choices)
        for (const [value, text] of options.choices) {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = text;
            input.append(option);
        }
    else input.type = options.type || "text";
    if (input.type === "checkbox") input.checked = Boolean(value);
    else input.value = value ?? "";

    if (input.type === "number") {
        input.step = "any";
        input.min = options.min ?? "0";
        input.max = options.max ?? "10000000";
        if (key === "minimum_soc") percentageInput(input, value);
    }
    if (options.required) input.required = true;
    const explanation = document.createElement("small");
    explanation.id = identity("help");
    explanation.textContent = help[key] || "";
    input.setAttribute("aria-describedby", explanation.id);
    label.append(input, explanation);
    parent.append(label);
    if (options.list && value && sensorChoices(options.list).some(([id]) => id === value)) {
        label.hidden = true;
        const detected = document.createElement("p");
        const detectedLabel = document.createElement("span");
        detectedLabel.dataset.sourceEntity = value;
        detectedLabel.dataset.sourceTitle = title;
        detectedLabel.textContent = `${title}: ${input.selectedOptions[0].textContent}: `;
        detected.append(detectedLabel);
        const change = document.createElement("button");
        change.type = "button";
        change.textContent = `Change ${title.toLowerCase()}`;
        change.onclick = () => {
            label.hidden = false;
            detected.hidden = true;
        };
        if (!options.managed) detected.append(change);
        parent.append(detected);
    }
    return input;
}
function sensorChoices(kind, selected) {
    const matches = (config.catalogue.data?.entities || []).filter(
        (e) =>
            e.entity_id.length <= 127 &&
            (kind === "power"
                ? e.device_class === "power" && ["W", "kW"].includes(e.unit)
                : kind === "energy"
                  ? e.device_class === "energy" &&
                    ["Wh", "kWh", "MWh"].includes(e.unit) &&
                    ["total", "total_increasing"].includes(e.state_class)
                  : e.device_class === "battery" && e.unit === "%"),
    );
    const choices = [
        ["", "Select a sensor…"],
        ...matches.map((e) => [e.entity_id, `${sourceName(e, "Sensor")} (${e.entity_id})`]),
    ];
    if (selected && !matches.some((e) => e.entity_id === selected))
        choices.push([
            selected,
            `Configured in Home Assistant. Missing or incompatible sensor: ${selected}`,
        ]);
    return choices;
}
function row(container, collection, title, old = {}) {
    if (collection.length >= 16)
        throw new Error("A maximum of 16 devices of each type is supported.");
    const node = document.createElement("fieldset"),
        legend = document.createElement("legend");
    legend.textContent = title;
    node.append(legend);
    const item = { node, old };
    collection.push(item);
    $(container).append(node);
    return node;
}
/** Read only complete, adapter-normalized metadata for this exact Energy production meter. */
function solarSuggestion(entity) {
    return config.catalogue.data.suggestions.solar_configurations?.find(item => item.entity_id === entity && item.issue === null);
}
/** Seed a new source from complete HA geometry before its physical converter is confirmed. */
function suggestedSolarSource(entity_id) {
    const suggestion = solarSuggestion(entity_id);
    if (!suggestion) return {entity_id};
    const panels = structuredClone(suggestion.providers.flatMap(provider => provider.panels));
    return {entity_id, panels, capacity_w: panels.reduce((total, panel) => total + panel.capacity_w, 0), orientation: "south", ha_imports: structuredClone(suggestion.providers)};
}
/** Build one detected PV editor with HA-owned geometry and independently editable converter settings. */
function addSolar(old = {}) {
    old = { ...old, source_id: old.source_id || config.devices.find((d) => d.kind === "solar" && d.energy_entity === old.entity_id).id };
    const inverter =
        (config.solar?.inverters || []).find(
            (i) => i.inverter_id === old.inverter_id,
        ) || {};
    const node = row("solar-rows", solarRows, "Solar source", old);
    field(node, "Production energy sensor", "entity_id", old.entity_id, {
        choices: sensorChoices("energy", old.entity_id).filter(([id]) => id === old.entity_id),
        required: true,
    }).disabled = true;
    const editor = solarRows.at(-1);
    field(node, "Forecast source", "forecast_source", old.forecast_source || "automatic", {
        choices: [["automatic", "Automatic"], ["pvwatts", "PVWatts"], ["ha", "Home Assistant"], ["ml", "ML model"]],
    });
    const forecastHelp = document.createElement("p");
    forecastHelp.textContent = "Automatic compares the same completed days against measured production. PVWatts remains active while evidence is collected or the selected source is unavailable. HA forecasts require a supported provider and compatible production metering. ML requires sufficient history.";
    node.append(forecastHelp);
    const geometry = document.createElement("fieldset");
    geometry.className = "solar-geometry";
    geometry.setAttribute("aria-label", "Panel geometry");
    node.append(geometry);
    /** Insert one exact plane without deriving any values from its display name. */
    const addPanel = (panel = {}) => {
        if (geometry.querySelectorAll(".solar-panel").length >= MAX_SOLAR_PANEL_GROUPS) throw new Error(`A maximum of ${MAX_SOLAR_PANEL_GROUPS} panel groups is supported.`);
        const group = document.createElement("div"), heading = document.createElement("h3");
        group.className = "solar-panel";
        group.dataset.panelId = panel.panel_id || identity("panel");
        group.setAttribute("role", "group");
        group.setAttribute("aria-label", `Panel group ${geometry.querySelectorAll(".solar-panel").length + 1}`);
        heading.textContent = group.getAttribute("aria-label");
        group.append(heading);
        field(group, "Panel group name", "panel_name", panel.name || "");
        const name = group.querySelector('[data-key="panel_name"]');
        name.maxLength = MAX_PANEL_NAME_LENGTH;
        // Native text inputs remove CR/LF. Keep the original until an explicit name edit.
        name.dataset.originalPanelName = panel.name || "";
        /** Keep the accessible group heading synchronized with its editable display name. */
        const updateName = event => {
            if (event) delete name.dataset.originalPanelName;
            const value = name.dataset.originalPanelName ?? name.value;
            const title = value.trim() ? value : `Panel group ${[...geometry.querySelectorAll(".solar-panel")].indexOf(group) + 1}`;
            heading.textContent = title;
            group.setAttribute("aria-label", title);
        };
        name.addEventListener("input", updateName);
        field(group, "Panel capacity (kWp)", "panel_capacity", panel.capacity_w ? panel.capacity_w / 1000 : "", {type: "number", min: "0.001", required: true});
        field(group, "Tilt (°)", "panel_tilt", panel.tilt ?? "", {type: "number", min: "0", max: "90", required: true});
        const azimuth = field(group, "Azimuth (° clockwise from north)", "panel_azimuth", panel.azimuth ?? "", {type: "number", min: "0", max: "360", required: true});
        const direction = field(group, "Direction shortcut", "panel_direction", "", {choices: DIRECTION_CHOICES});
        direction.setAttribute("data-view-control", "");
        /** A shortcut changes the exact input. Reading an arbitrary angle never rounds that input. */
        const updateDirection = () => {
            const value = azimuth.value === "" ? null : Number(azimuth.value);
            direction.value = value !== null && DIRECTION_CHOICES.some(([key]) => key === String(value % 360)) ? String(value % 360) : "";
            azimuth.closest("label").querySelector("small").textContent = azimuthLabel(value);
        };
        azimuth.addEventListener("input", updateDirection);
        direction.addEventListener("change", () => {
            if (direction.value === "") return;
            azimuth.value = direction.value;
            azimuth.dispatchEvent(new Event("input", {bubbles: true}));
        });
        updateDirection();
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "secondary";
        remove.textContent = "Remove panel group";
        remove.onclick = () => {
            group.remove();
            [...geometry.querySelectorAll(".solar-panel")].forEach((panelGroup, index) => {
                const input = panelGroup.querySelector('[data-key="panel_name"]');
                const name = input.dataset.originalPanelName ?? input.value;
                panelGroup.setAttribute("aria-label", name.trim() ? name : `Panel group ${index + 1}`);
                panelGroup.querySelector("h3").textContent = panelGroup.getAttribute("aria-label");
            });
            node.dispatchEvent(new Event("input", {bubbles: true}));
        };
        group.append(remove);
        geometry.insertBefore(group, geometry.querySelector(":scope > button"));
        updateName();
    };
    /** Use the same named plane editor for imported and manually configured geometry. */
    const renderGeometry = panels => {
        geometry.replaceChildren();
        const explanation = document.createElement("p");
        explanation.setAttribute("data-panel-ownership", "");
        geometry.append(explanation);
        panels.forEach(addPanel);
        const add = document.createElement("button");
        add.type = "button";
        add.className = "secondary";
        add.textContent = "Add panel group";
        add.onclick = () => { addPanel(); node.dispatchEvent(new Event("input", {bubbles: true})); };
        geometry.append(add);
    };
    const ownership = config.panel_settings?.[old.entity_id] || {managed: true, state: "unavailable"};
    let followDraftGeometry = ownership.managed;
    renderGeometry(ownership.managed && !old.capacity_w && !old.panels?.length
        ? [{capacity_w: null, azimuth: null, tilt: null}]
        : editableSolarPanels(old));
    /** Populate unsaved HA-owned geometry on recovery without replacing manual or physical drafts. */
    editor.updateOwnership = (state, changed = false) => {
        const savedSource = config.solar?.sources.some(source => source.entity_id === editor.old.entity_id);
        if (!state.managed) {
            followDraftGeometry = false;
            if (!savedSource && editor.old.ha_imports?.length) editor.old = {...editor.old, ha_imports: []};
        }
        const proposal = state.state === "following" && followDraftGeometry && !savedSource
            ? suggestedSolarSource(editor.old.entity_id) : null;
        const emptyGeometry = [...geometry.querySelectorAll("[data-key]")].every(control => control.value === "");
        if (proposal?.panels?.length && (editor.old.ha_imports?.length || emptyGeometry)
            && JSON.stringify(editor.old.ha_imports || []) !== JSON.stringify(proposal.ha_imports)) {
            editor.old = {...editor.old, ...proposal};
            renderGeometry(proposal.panels);
        }
        changed ||= !savedSource && state.managed && !followDraftGeometry;
        geometry.disabled = state.managed;
        geometry.querySelector("[data-panel-ownership]").textContent = (state.managed
            ? state.state === "unavailable"
                ? editor.old.capacity_w ? "Home Assistant panel settings could not be refreshed. Last accepted settings are retained. Edit the source configuration in Home Assistant."
                    : "Waiting for complete Home Assistant panel settings. No panel geometry has been assumed."
                : "Managed by Home Assistant (Forecast.Solar). Panel settings are followed automatically. Edit them in Home Assistant."
            : "Manual panel settings. No forecast configuration is linked in Home Assistant.")
            + (changed ? " Home Assistant panel settings changed. Your draft is retained. Reload saved settings before saving." : "");
    };
    editor.updateOwnership(ownership);
    const converterDetails = document.createElement("details"), converterSummary = document.createElement("summary");
    converterSummary.textContent = "Shared inverter settings";
    converterDetails.append(converterSummary);
    node.append(converterDetails);
    field(
        converterDetails,
        "Inverter ID",
        "inverter_id",
        old.inverter_id || identity("solar"),
        { required: true },
    );
    field(
        node,
        "Inverter AC rating (kW)",
        "rating",
        inverter.ac_limit_w ? inverter.ac_limit_w / 1000 : "",
        { type: "number", min: "0.001", required: true },
    );
    field(
        node,
        "Inverter efficiency (0–1)",
        "efficiency",
        inverter.efficiency ?? 0.96,
        { type: "number", min: "0.5", max: "1", required: true },
    );
    field(
        node,
        "Hybrid inverter with battery connection",
        "hybrid",
        inverter.hybrid,
        { type: "checkbox" },
    );
    field(
        node,
        "Allow proposed solar on/off commands",
        "can_curtail",
        old.can_curtail,
        { type: "checkbox" },
    );
}
/** Build a battery editor with HA-owned measurements/capacity and app-owned limits. Hybrid labels retain stable converter option values. */
function addBattery(old = {}) {
    old = { ...old, device_id: old.device_id || config.devices.find((d) => d.kind === "battery" && d.energy_entity === old.energy_entity).id };
    const own =
        (config.installation?.inverters || []).find(
            (i) => i.inverter_id === old.inverter_id,
        ) || {};
    const node = row("battery-rows", batteryRows, "Battery", old);
    const reserveLabels = {
        measurement_unavailable: "Waiting for usable measurements",
        reserve_satisfied: "Reserve satisfied",
        recovery_required: "Recovery required",
        below_reserve: "Below reserve: Charging follows the strategy",
    };
    const reserveNote = document.createElement("p");
    const device = config.devices.find((d) => d.id === old.device_id);
    reserveNote.textContent = "Reserve status at last refresh: " + (reserveLabels[device?.reserve_status] || reserveLabels.measurement_unavailable);
    node.append(reserveNote);
    field(node, "Energy dashboard battery", "energy_entity", old.energy_entity, {
        choices: [[old.energy_entity, old.energy_entity]],
        required: true,
    }).disabled = true;
    field(node, "Battery charge energy meter", "charge_energy_entity", old.charge_energy_entity, {
        choices: [[old.charge_energy_entity, old.charge_energy_entity]], required: true,
    }).disabled = true;
    field(node, "State of charge sensor", "soc_entity", old.soc_entity, {
        list: "soc",
        managed: true,
        required: true,
    }).disabled = true;
    field(
        node,
        "Actual battery power sensor",
        "power_entity",
        old.power_entity,
        { list: "power", required: true, managed: true },
    ).disabled = true;
    field(
        node,
        "Invert measured battery power",
        "power_inverted",
        old.power_inverted,
        { type: "checkbox" },
    ).disabled = true;
    field(node, "Usable capacity (kWh)", "capacity_kwh", old.capacity_kwh, {
        type: "number",
        min: "0.001",
        required: true,
    }).disabled = true;
    field(
        node,
        "Maximum charge power (kW)",
        "charge",
        old.charge_limit_w === undefined ? "" : old.charge_limit_w / 1000,
        { type: "number", required: true },
    );
    field(
        node,
        "Maximum discharge power (kW)",
        "discharge",
        old.discharge_limit_w === undefined ? "" : old.discharge_limit_w / 1000,
        { type: "number", required: true },
    );
    field(node, "Can follow zero grid target", "can_follow_grid", old.can_follow_grid ?? false, {
        type: "checkbox",
    });
    const hybrids = solarRows
        .map((item) => values(item.node))
        .filter((i) => i.hybrid)
        .map((source) => [source.inverter_id, solarName(source.entity_id)]);
    field(
        node,
        "Converter connection",
        "connection",
        own.hybrid ? old.inverter_id : "standalone",
        {
            choices: [
                ["standalone", "Own inverter"],
                ...new Map(hybrids).entries(),
            ],
        },
    );
    field(
        node,
        "Own inverter AC rating (kW)",
        "rating",
        own.hybrid ? "" : own.ac_limit_w ? own.ac_limit_w / 1000 : "",
        { type: "number", min: "0.001" },
    );
    field(
        node,
        "Own inverter efficiency (0–1)",
        "efficiency",
        own.efficiency ?? 0.96,
        { type: "number", min: "0.5", max: "1" },
    );
    field(
        node,
        "Minimum state of charge (%)",
        "minimum_soc",
        old.minimum_soc ?? 10,
        { type: "number", max: "100", required: true },
    );
    field(node, "Restore minimum reserve immediately", "recover_reserve", old.recover_reserve ?? false,
          { type: "checkbox" });
    field(
        node,
        "Cost per full cycle (optional)",
        "cycle_fee",
        old.cycle_fee ?? 0,
        { type: "number", required: true },
    );
}
/** Name a converter by its detected solar source while preserving its stable internal reference. */
function solarName(entity) {
    const name = sourceName(config.catalogue.data.entities.find(item => item.entity_id === entity), null);
    return name && name !== entity ? `${name} (${entity})` : entity;
}
/** Read saved definitions from exact numeric drafts rather than rounded minimum-SoC display values. */
function values(node) {
    return Object.fromEntries(
        [...node.querySelectorAll("[data-key]")].map((input) => [
            input.dataset.key,
            input.type === "checkbox"
                ? input.checked
                : input.type === "number"
                  ? inputNumber(input)
                  : input.dataset.key === "panel_name" ? input.dataset.originalPanelName ?? input.value : input.value.trim(),
        ]),
    );
}
function definitions() {
    const inverters = new Map();
    const insert = (i) => {
        if (
            inverters.has(i.inverter_id) &&
            JSON.stringify(inverters.get(i.inverter_id)) !== JSON.stringify(i)
        )
            throw new Error(
                "Sources sharing an inverter must use identical inverter settings.",
            );
        inverters.set(i.inverter_id, i);
    };
    const sources = solarRows.map(({ node, old }) => {
        const v = values(node);
        const originalPanels = editableSolarPanels(old);
        const panels = [...node.querySelectorAll(".solar-panel")].map(group => {
            const plane = values(group);
            const original = originalPanels.find(panel => panel.panel_id === group.dataset.panelId);
            const capacity_w = original && plane.panel_capacity === original.capacity_w / 1000 ? original.capacity_w : plane.panel_capacity * 1000;
            return {panel_id: group.dataset.panelId, name: plane.panel_name, capacity_w, azimuth: plane.panel_azimuth, tilt: plane.panel_tilt};
        });
        if (!panels.length) throw new Error("Add at least one panel group for this production meter.");
        const unchangedManual = !old.panels?.length && JSON.stringify(panels) === JSON.stringify(originalPanels);
        insert({
            inverter_id: v.inverter_id,
            ac_limit_w: v.rating * 1000,
            efficiency: v.efficiency,
            hybrid: v.hybrid,
        });
        return {
            ...old,
            source_id: old.source_id,
            entity_id: v.entity_id,
            inverter_id: v.inverter_id,
            capacity_w: unchangedManual ? old.capacity_w : panels.reduce((total, panel) => total + panel.capacity_w, 0),
            orientation: old.orientation || "south",
            tilt: old.tilt ?? DEFAULT_PANEL_TILT_DEGREES,
            panels: unchangedManual ? [] : panels,
            can_curtail: v.can_curtail,
            forecast_source: v.forecast_source,
        };
    });
    const used = new Map();
    const batteries = batteryRows.map(({ node, old }) => {
        const v = values(node),
            id = old.device_id;
        let converter;
        if (v.connection === "standalone") {
            converter = {
                inverter_id:
                    old.inverter_id &&
                    !(config.installation?.inverters || []).find(
                        (i) => i.inverter_id === old.inverter_id,
                    )?.hybrid
                        ? old.inverter_id
                        : `inv_${id}`,
                ac_limit_w:
                    (v.rating || Math.max(v.charge, v.discharge)) * 1000,
                efficiency: v.efficiency,
                hybrid: false,
            };
            insert(converter);
        } else {
            converter = inverters.get(v.connection);
            if (!converter?.hybrid)
                throw new Error(
                    "Choose an existing hybrid converter or an own inverter.",
                );
        }
        used.set(converter.inverter_id, converter);
        return {
            ...old,
            device_id: id,
            energy_entity: v.energy_entity,
            charge_energy_entity: v.charge_energy_entity,
            inverter_id: converter.inverter_id,
            soc_entity: v.soc_entity,
            power_entity: v.power_entity,
            power_inverted: v.power_inverted,
            can_follow_grid: v.can_follow_grid,
            capacity_kwh: v.capacity_kwh,
            charge_limit_w: v.charge * 1000,
            discharge_limit_w: v.discharge * 1000,
            minimum_soc: v.minimum_soc,
            recover_reserve: v.recover_reserve,
            cycle_fee: v.cycle_fee,
        };
    });
    const terms = config.catalogue.data.suggestions.terms;
    return {
        installation: {
            grid_power_entity: $("grid").value.trim(),
            additional_grid_power_entities: (config.catalogue.data.suggestions.grid_power_entities || []).slice(1),
            grid_power_inverted: $("grid-invert").checked,
            grid_export_entity: $("export-energy").value.trim() || null,
            additional_grid_export_entities: (config.catalogue.data.suggestions.settlement?.exports || []).slice(1),
            import_limit_w: Number($("import").value) * 1000,
            export_limit_w: Number($("export").value) * 1000,
            batteries,
            inverters: [...used.values()],
            expected_version: config.installation?.version ?? null,
        },
        solar: {
            sources,
            inverters: [...inverters.values()].filter((i) =>
                sources.some((s) => s.inverter_id === i.inverter_id),
            ),
            expected_version: config.solar?.version ?? null,
        },
        demand: {
            terms,
            exclusions: config.demand?.exclusions || [],
            annual_kwh: Number($("annual").value),
            timezone: $("timezone").value.trim(),
            expected_version: config.demand?.version ?? null,
        },
    };
}
/** Select an existing setup area and refresh hybrid choices without writing any configuration. */
function showStep(value) {
    step = value;
    if (step === 2) {
        // Sources may have been edited after returning from the battery screen.
        const hybrids = solarRows
            .map(({ node }) => values(node))
            .filter((source) => source.hybrid);
        for (const { node } of batteryRows) {
            const select = node.querySelector('[data-key="connection"]');
            const previous = select.value;
            select.replaceChildren();
            for (const id of [
                "standalone",
                ...new Set(hybrids.map((source) => source.inverter_id)),
            ]) {
                const option = document.createElement("option");
                option.value = id;
                option.textContent = id === "standalone" ? "Own inverter" : solarName(hybrids.find(source => source.inverter_id === id).entity_id);
                select.append(option);
            }
            select.value = [...select.options].some(
                (option) => option.value === previous,
            )
                ? previous
                : "standalone";
            if (select.value !== previous)
                status(
                    "A hybrid connection was removed. Confirm the affected battery's converter connection before saving.",
                );
        }
    }
    document.querySelectorAll("[data-step]").forEach((node) => {
        node.hidden = Number(node.dataset.step) !== step;
    });

    $("back").disabled = step === -1;
    $("next").hidden = step === 3;
    saveState.update();
    layout.show(step);
}
/** Validate the current wizard area and reveal the source containing an invalid field. */
function validateVisible() {
    for (const input of document
        .querySelector(`[data-step="${step}"]`)
        .querySelectorAll("input,select"))
        if (!input.checkValidity()) {
            const container = step === 1 ? "solar-rows" : step === 2 ? "battery-rows" : null;
            if (container) {
                const selector = step === 1 ? "installation-solar" : "installation-battery";
                $(selector).value = String([...$(container).children].findIndex(row => row.contains(input)));
                layout.selectDevice(selector, container);
            }
            input.reportValidity(); return false;
        }
    return true;
}
const PRICE_STATUS_REFRESH_MS = 30000;
const CENTS_PER_EURO = 100;
const PERCENT_SCALE = 100;
let priceTimer, priceRequestVersion = 0;
/** Poll price status and update the source panels without applying stale responses.
 * Clear published coverage and active rows on failed reads. Refresh related strategy
 * status and schedule the next poll after completion. Preserve editable source drafts.
 */
async function refreshPriceSources() {
    clearTimeout(priceTimer);
    const requestVersion = ++priceRequestVersion;
    try {
        const prices = await api("/v1/prices");
        if (requestVersion !== priceRequestVersion) return;
        const policy = prices.selection_policy;
        if (policy) $("price-horizon").textContent = `Blocks count hours ahead from now, not calendar days. Source choices apply only to predictions for unpublished hours. Prices beyond ${policy.planning_horizon_hours} hours value remaining battery energy. They do not extend the plan. Requested price coverage: ${policy.price_coverage_hours} hours.`;
        if (policy) $("price-selection-policy").textContent = `Lower mean absolute error (MAE) is better. A switch requires at least ${percentage(policy.relative_improvement*PERCENT_SCALE)} and ${money(policy.absolute_improvement_eur_per_kwh*CENTS_PER_EURO)} ct/kWh improvement on ${policy.consecutive_days} consecutive daily assessments, using the last ${policy.window_days} days with at least ${policy.minimum_days} settled days and ${policy.minimum_hours} matched hours.`;
        $("price-sources").hidden = false;
        $("price-market").textContent = prices.market_area
            ? `Market area: ${prices.market_area}`
            : "Waiting for market price configuration.";
        priceSettings.update(prices);
        saveState.update();
        layout.sync();
    } catch {
        if (requestVersion !== priceRequestVersion) return;
        $("price-sources").hidden = false;
        $("price-published-coverage").textContent = "Published Nord Pool coverage unavailable. Retrying automatically.";
        $("price-source-list").replaceChildren();
        $("price-source-provenance").replaceChildren();
        $("price-market").textContent = "Price source status unavailable. Retrying automatically.";
    }
    try { await loadStrategy(false); }
    catch { $("strategy-status").textContent = "Strategy status unavailable. Retrying automatically."; }
    try { await refreshSourceNames(); }
    catch { /* Retain the last confirmed names when metadata is temporarily unavailable. */ }
    priceTimer = setTimeout(refreshPriceSources, PRICE_STATUS_REFRESH_MS);
}
/** Poll presentation metadata without reloading saved configuration or overwriting any draft. */
async function refreshSourceNames() {
    if (!installationReady) return;
    const previous = config;
    const state = await api("/v1/setup");
    if (!state.catalogue?.data || !installationReady || saveState.busy || previous !== config) return;
    const panelsChanged = state.solar?.version !== config.solar?.version;
    if (panelsChanged && !layout.editing && !installationDirty()) {
        await load();
        return;
    }
    config.catalogue = state.catalogue;
    config.devices = state.devices;
    config.panel_settings = state.panel_settings;
    for (const editor of solarRows) {
        const ownership = state.panel_settings?.[editor.old.entity_id];
        if (ownership) editor.updateOwnership(ownership, panelsChanged);
    }
    const hybrids = solarRows.map(({node}) => values(node)).filter(source => source.hybrid);
    for (const select of document.querySelectorAll("#wizard select[data-key]")) {
        for (const option of select.options) {
            const converter = select.dataset.key === "connection" && hybrids.find(source => source.inverter_id === option.value);
            if (converter) { option.textContent = solarName(converter.entity_id); continue; }
            const metadata = state.catalogue.data.entities.find(row => row.entity_id === option.value);
            if (metadata) option.textContent = `${sourceName(metadata, "Sensor")} (${option.value})`;
        }
    }
    for (const label of document.querySelectorAll("[data-source-entity]")) {
        const metadata = state.catalogue.data.entities.find(row => row.entity_id === label.dataset.sourceEntity);
        if (metadata) label.textContent = `${label.dataset.sourceTitle}: ${sourceName(metadata, "Sensor")} (${label.dataset.sourceEntity}): `;
    }
    layout.refreshNames(config);
}
/** Reload HA discovery and confirmed settings. Keep source identities and independent revision guards. */
async function load(refresh = false, discard = false) {
    config = await api(refresh ? "/v1/setup?refresh=1" : "/v1/setup");
    installationReady = false;
    try { await loadStrategy(requestedStrategy === null || refresh || discard); }
    catch {
        $("strategy-settings").hidden = false;
        $("installation-save-bar").hidden = false;
        $("strategy-status").textContent = "Strategy status unavailable. Retrying automatically.";
        saveState.update("Strategy status unavailable. Retrying automatically.");
    }
    refreshPriceSources();
    if (config.mode !== "home_assistant")
        throw new Error("Installation setup requires Home Assistant mode.");
    const catalogue = config.catalogue;
    if (
        catalogue.refresh_pending ||
        catalogue.stale ||
        !catalogue.data ||
        catalogue.data.suggestions.energy_status !== "ready"
    ) {
        $("connect").hidden = true;
        $("wizard").hidden = true;
        status(
            !catalogue.data || catalogue.stale
                ? "Waiting for Home Assistant. Install the integration through HACS, then approve its matching connection request here. Check local discovery networking if no request appears."
                : catalogue.refresh_pending
                  ? "Refreshing Home Assistant configuration. Waiting for the integration to upload current sensors and Energy settings."
                  : catalogue.data.suggestions.energy_status === "missing"
                    ? "Configure the Home Assistant Energy dashboard first: Settings > Dashboards > Energy. Add grid import and any solar, grid export and battery charge/discharge meters."
                    : "The Home Assistant Energy dashboard is incomplete or unsupported. Check all energy meters and units, include grid export when solar or storage is present, and use a complete electricity balance (up to 16 meter channels).",
        );
        layout.loaded(config, false, discard);
        showStep(layout.area >= 4 ? layout.area : -1);
        clearTimeout(discoveryTimer);
        discoveryTimer = setTimeout(
            () => load().catch((error) => status(error.message)),
            5000,
        );
        return;
    }
    clearTimeout(discoveryTimer);
    solarRows.splice(0);
    batteryRows.splice(0);
    $("solar-rows").replaceChildren();
    $("battery-rows").replaceChildren();
    const hints = catalogue.data.suggestions;
    const missing = [];
    if (hints.grid_power_entities && !hints.grid_power_entities.length) missing.push("net grid power");
    for (const battery of hints.batteries) {
        for (const key of ["soc_entity", "power_entity", "capacity_kwh", "charge_energy_entity"]) {
            if (Object.hasOwn(battery, "capacity_kwh") && battery[key] == null) missing.push(`${battery.energy_entity}: ${key.replaceAll("_", " ")}`);
        }
    }
    if (missing.length) {
        $("wizard").hidden = true;
        status(`Complete Settings > Dashboards > Energy in Home Assistant: ${missing.join(": ")}: These settings are read-only here. Refresh after correcting them.`);
        layout.loaded(config, false, discard);
        showStep(layout.area >= 4 ? layout.area : -1);
        return;
    }
    $("energy-summary").textContent =
        `Energy dashboard: ${hints.terms.filter((t) => t.sign === 1 && !hints.solar_sources.includes(t.entity_id) && !hints.batteries.some((b) => b.energy_entity === t.entity_id)).map((t) => t.entity_id).join(", ")} (grid import): ` +
        `Solar sources: ${hints.solar_sources.length}: Batteries: ${hints.batteries.length}: Configuration is independent of available history. Devices removed from Energy are excluded automatically.`;
    for (const [id, kind, selected] of [
        [
            "grid",
            "power",
            hints.grid_power_entities?.[0] || hints.grid_power || config.installation?.grid_power_entity,
        ],
        [
            "export-energy",
            "energy",
            hints.grid_export,
        ],
    ]) {
        $(id).replaceChildren();
        for (const [value, text] of sensorChoices(kind, selected)) {
            const option = document.createElement("option");
            option.value = value;
            option.textContent = text;
            $(id).append(option);
        }
        $(id).value = selected || "";
        $(id).disabled = true;
    }
    $("grid-invert").checked =
        false;
    $("grid-invert").disabled = true;
    $("import").value = config.installation
        ? config.installation.import_limit_w / 1000
        : "";
    $("export").value = config.installation
        ? config.installation.export_limit_w / 1000
        : "";
    const estimate = config.annual_estimate;
    $("annual").value = estimate?.annual_kwh ?? config.demand?.annual_kwh ?? "";
    $("annual-field").hidden = Boolean(estimate || config.demand);
    $("household-summary").textContent = estimate
        ? `Whole-home consumption uses the complete Energy dashboard balance. Startup scale: ${estimate.annual_kwh} kWh/year. ${estimate.extrapolated === false
            ? `Measured over ${estimate.days} complete days.`
            : `Extrapolated from ${estimate.days} complete days within the last year. This is an estimate, not a measured annual total. Seasonal differences may affect it.`} Historical load forecasting is initialized after saving.`
        : "Whole-home consumption uses the complete Energy dashboard balance. Fewer than seven complete recent days are available. Confirm an annual estimate for the startup forecast.";
    $("grid-field").hidden = Boolean($("grid").value);
    $("grid-summary").textContent = $("grid-field").hidden
        ? `Grid power from Home Assistant: ${(hints.grid_power_entities || [$("grid").value]).join(", ")}. All connections are summed. Import/export energy meters are reused separately for history and costs.`
        : "Configure net grid power in Settings > Dashboards > Energy in Home Assistant.";
    $("timezone").value = catalogue.data.timezone;
    useHomeAssistantTimezone(catalogue.data.timezone);
    const savedSolar = config.solar?.sources || [];
    pendingSolarSources = hints.solar_sources.filter(entity => !savedSolar.some(source => source.entity_id === entity));
    for (const source of [
        ...hints.solar_sources.map((entity_id) => savedSolar.find((source) => source.entity_id === entity_id) || suggestedSolarSource(entity_id)),
    ])
        addSolar(source);
    const savedBatteries = config.installation?.batteries || [], usedBatteries = new Set();
    const detectedBatteries = hints.batteries.map((hint) => {
        const { energy_entity, soc_entity, power_entity } = hint;
        const saved = savedBatteries.find((battery) => !usedBatteries.has(battery) &&
            (battery.energy_entity === energy_entity || (!battery.energy_entity && ((soc_entity && battery.soc_entity === soc_entity) || (power_entity && battery.power_entity === power_entity)))));
        if (saved) usedBatteries.add(saved);
        return { ...saved, ...hint, power_inverted: false };
    });
    for (const battery of detectedBatteries)
        addBattery(battery);
    if (savedSolar.some((source) => !hints.solar_sources.includes(source.entity_id)) || usedBatteries.size < savedBatteries.length)
        $("energy-summary").textContent += " Device membership changed in Home Assistant. Removed devices are excluded automatically. Confirm missing properties and save to resume planning. Historical plans remain unchanged.";
    $("connect").hidden = true;
    $("wizard").hidden = false;
    $("installation-save-bar").hidden = false;
    $("strategy-settings").hidden = false;
    const keepArea = layout.ready && !layout.wizard;
    layout.loaded(config, true, discard);
    showStep(keepArea || layout.area >= 4 ? layout.area : layout.wizard ? -1 : 0);
    installationBaseline = JSON.stringify(installationValues());
    if (!saveState.ready) saveState.loaded();
    installationReady = true;
    if (!config.installation?.version) installationBaseline = null;
    saveState.update();
    layout.sync();
    status(
        layout.wizard ? "Home Assistant Energy configuration is ready. Confirm your physical ratings before saving." : pendingSolarSources.length ? `Review and save new Home Assistant solar sources: ${pendingSolarSources.map(solarName).join(", ")}.` : "Saved configuration loaded.",
    );
}
$("edit-detected").onclick = () => {
    $("grid-field").hidden = false;
    $("annual-field").hidden = false;
};
$("connect").onsubmit = async (event) => {
    event.preventDefault();
    token = $("token").value;
    try {
        await refreshPairing();
        await load();
        $("token").value = "";
    } catch (error) {
        status(error.message);
    }
};
let pairingView = "", pairingBusy = false;
async function refreshPairing() {
    if ((!token && !ingressSession) || pairingBusy) return;
    pairingBusy = true;
    try {
        const state = await api("/v1/pairing");
        const view = JSON.stringify(state);
        if (view === pairingView) return;
        pairingView = view;
        const container = $("pairing-candidates");
        container.replaceChildren();
        $("pairing").hidden = state.supervisor_managed || (!state.paired && state.pending.length === 0);
        if (state.paired) {
            const connected = document.createElement("p");
            connected.textContent = `Connected to ${state.peer_name}.`;
            const reset = document.createElement("button");
            reset.type = "button";
            reset.textContent = "Forget Home Assistant connection";
            reset.onclick = async () => {
                reset.disabled = true;
                try {
                    await api("/v1/pairing/reset", "POST", {});
                    pairingView = "";
                    await load(true);
                } catch (error) { status(error.message); }
                finally { reset.disabled = false; }
            };
            container.append(connected, reset);
        }
        for (const candidate of state.pending) {
            const row = document.createElement("div");
            const details = document.createElement("p");
            details.textContent = `${candidate.name}. Verification code: ${candidate.verification}`;
            const approve = document.createElement("button");
            approve.type = "button";
            approve.textContent = "Pair this Home Assistant";
            approve.onclick = async () => {
                approve.disabled = true;
                try {
                    await api("/v1/pairing/approve", "POST", {request_id: candidate.request_id});
                    pairingView = "";
                    await load(true);
                } catch (error) { status(error.message); }
                finally { approve.disabled = false; }
            };
            row.append(details, approve);
            container.append(row);
        }
    } finally { pairingBusy = false; }
}
setInterval(() => refreshPairing().catch((error) => status(error.message)), 3000);
/** Refresh read-only data or explicitly discard a draft. Failed reads retain the existing editor. */
async function refreshInstallation(discard) {
    if (saveState.busy) return;
    if (discard && step === 5) {
        priceSettings.discard();
        layout.editing = false;
        saveState.update("Price source edits cancelled. Other drafts retained.");
        layout.sync();
        return;
    }
    if (discard && step === 4) {
        $("strategy").value = requestedStrategy;
        layout.editing = false;
        saveState.update("Planning strategy edits cancelled. Installation draft retained.");
        layout.sync();
        return;
    }
    if (saveState.dirty && !discard) {
        await refreshPriceSources();
        saveState.update("Save or discard your changes before reloading settings.");
        return;
    }
    await saveState.refresh(() => load(!discard, discard), {discard});
    for (const id of ["grid", "export-energy", "grid-invert"]) $(id).disabled = true;
    $("strategy").disabled = requestedStrategy === null;
    showStep(step);
}
$("reload").onclick = () => refreshInstallation(false);
$("discard-installation").onclick = () => refreshInstallation(true);
$("back").onclick = () => {
    if (!busy) showStep(step - 1);
};
$("next").onclick = async () => {
    if (busy || !validateVisible()) return;
    try {
        if (step === 2) {
            const summary = await api(
                "/v1/setup/preview",
                "POST",
                definitions(),
            );
            $("review").textContent =
                `${summary.solar_sources} solar source(s), ${summary.batteries} battery/batteries, ${summary.annual_kwh} kWh annual household consumption.`;
        }
        showStep(step + 1);
    } catch (error) {
        status(error.message);
    }
};
$("wizard").onsubmit = (event) => event.preventDefault();
$("save").onclick = async () => {
    if (busy || !layout.editing || (layout.wizard && ![3, 4, 5].includes(step))) return;
    busy = true;
    saveState.begin();
    layout.sync();
    status("Saving installation…");
    try {
        if (step === 5) {
            ++priceRequestVersion;
            try {
                await priceSettings.save(api);
                layout.editing = false;
                saveState.update("Price source preferences saved. Other drafts retained without saving.");
            } finally { refreshPriceSources(); }
            return;
        }
        let message = "Settings saved.";
        if (step !== 4 && (!layout.wizard || step === 3) && installationDirty()) {
            const body = definitions();
            await api("/v1/setup/preview", "POST", body);
            const demand = await api("/v1/demand", "PUT", body.demand);
            config.demand = {...config.demand, version: demand.configuration.version};
            const solar = await api("/v1/solar", "PUT", body.solar);
            config.solar = solar.configuration;
            const result = await api("/v1/installation", "PUT", body.installation);
            config.installation = result.installation.configuration;
            pendingSolarSources = [];
            installationBaseline = JSON.stringify(installationValues());
            message = `Installation saved. Home Assistant will provision ${(result.installation.devices || []).length} virtual device(s).${result.installation.warnings?.length ? " Fixed PV may exceed the configured export limit. Check the Home Assistant notification." : ""}`;
        }
        if (requestedStrategy !== null && $("strategy").value !== requestedStrategy) {
            await api("/v1/strategy", "PUT", {strategy: $("strategy").value, expected_strategy: requestedStrategy});
            await loadStrategy();
        }
        if (step === 4) {
            layout.editing = false;
            saveState.update(layout.wizard ? "Planning strategy saved. Review the installation draft before saving its settings." : "Planning strategy saved. Installation draft retained without saving.");
            return;
        }
        const savedArea = layout.wizard && step !== 4 ? 0 : step;
        layout.saved(Boolean(config.installation?.version));
        showStep(savedArea);
        status(message); saveState.update(message);
    } catch (error) {
        const message = error.message;
        const detail = `Save could not be confirmed: ${message}. Your edits are retained. Earlier independent saves may already be committed. Discard changes to reload if a version conflict requires it.`;
        status(detail); saveState.update(detail);
    } finally {
        ((busy = false), discoveryTimer);
        saveState.end();
        $("strategy").disabled = requestedStrategy === null;
        showStep(step);
    }
};

async function initializeIngress() {
    try {
        const base = document.querySelector('meta[name="oep-api-base"]').content;
        const response = await fetch(base + "setup/session", {redirect: "error"});
        const session = await response.json();
        if (!session.ingress) return;
        ingressSession = true;
        $("connect").hidden = true;
        await load();
        await refreshPairing();
    } catch (error) {
        status("Waiting for the Home Assistant app connection: " + error.message);
    }
}
initializeIngress();

/** Refresh acceptance status. Synchronize editable intent only on initial load, explicit reload or successful save. */
async function loadStrategy(updateChoice = true) {
    const state = await api("/v1/strategy");
    if (updateChoice || requestedStrategy === null) {
        requestedStrategy = state.requested;
        $("strategy").value = state.requested;
    }
    $("strategy-settings").hidden = false;
    $("installation-save-bar").hidden = false;
    layout.controls.set($("strategy"), false);
    if (!saveState.busy) $("strategy").disabled = false;
    if (!saveState.ready) saveState.loaded();
    saveState.update();
    const label = (value) => ({"minimize cost": "Minimize cost", "minimize consumption": "Minimize consumption"}[value] || "No accepted plan");
    layout.sync();
    const aligned = state.requested === state.published && state.requested === state.active;
    $("strategy-status").textContent = state.issue
        ? "Strategy settings could not be read. Save a supported choice to restore planning."
        : aligned ? "Current goal is published and accepted by Home Assistant. Acceptance does not prove physical execution."
        : `Requested: ${label(state.requested)}: Published plan: ${label(state.published)}: Active in Home Assistant: ${label(state.active)}. A change takes effect with a validated replacement. Existing commands retain their normal validity. Acceptance does not prove physical execution.`;
}
