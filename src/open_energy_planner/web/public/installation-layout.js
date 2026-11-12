import {configuredRate, percentage, optionLabel, sourceName} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
import {azimuthLabel} from "./solar-geometry.js";

const find = id => document.getElementById(id);
const areas = [[-1, "Home Assistant"], [0, "Connection and household"], [1, "Solar production"], [2, "Batteries"], [3, "Review"], [4, "Planning strategy"], [5, "Price sources"]];
const wizardAreas = [-1, 0, 1, 2, 3];

/** Append one text-only fact. Provider names and sensor identifiers remain unmodified. */
function summaryFact(list, title, value) {
    const row = document.createElement("div"), term = document.createElement("dt"), detail = document.createElement("dd");
    term.textContent = title;
    if (Array.isArray(value) && value.length) {
        const sources = document.createElement("ul");
        for (const name of value) { const item = document.createElement("li"); item.textContent = name; sources.append(item); }
        detail.append(sources);
    } else detail.textContent = Array.isArray(value) ? "None configured" : value;
    row.append(term, detail); list.append(row);
}

/** Present the existing setup draft without owning validation, writes or HA discovery. */
export class InstallationLayout {
    /** Reuse SaveBar locks and the setup module's navigation and refresh boundaries. */
    constructor(saveState, select) {
        this.saveState = saveState;
        this.select = select;
        this.ready = false;
        this.wizard = true;
        this.editing = false;
        this.area = -1;
        this.controls = new Map();
        this.config = null;
        bindSettingsActions();
        find("installation-area").onchange = event => this.select(Number(event.target.value));
        for (const button of document.querySelectorAll("[data-review-area]")) button.onclick = () => this.select(Number(button.dataset.reviewArea));
        find("installation-return").onclick = () => this.select(-1);
        find("installation-strategy-shortcut").onclick = () => this.select(4);
        find("installation-sources-shortcut").onclick = () => this.select(5);
        find("edit-installation").onclick = () => {
            this.editing = true;
            this.sync();
            const control = document.querySelector(`[data-step="${this.area}"] input:not(:disabled), [data-step="${this.area}"] select:not(:disabled)`);
            control?.focus({preventScroll: this.area === 5});
            if (this.area === 5) control?.scrollIntoView({block:"center"});
        };
        for (const [selector, container] of [["installation-solar", "solar-rows"], ["installation-battery", "battery-rows"]]) {
            find(selector).onchange = () => this.selectDevice(selector, container);
        }
        document.addEventListener("input", event => {
            if (event.target.form === find("wizard")) { this.summary(); this.capacities(); }
        });
        document.addEventListener("change", event => {
            if (event.target.form === find("wizard")) this.sync();
        });
    }

    /** Capture control ownership before read-only presentation, including controls built during a locked reload. */
    loaded(config, ready = true, discard = false) {
        const keepStrategy = !discard && this.config !== null && this.area >= 4;
        const previousEditing = this.editing;
        this.config = config;
        this.ready = ready;
        this.wizard = !config.installation?.version;
        this.editing = keepStrategy ? previousEditing : ready && this.wizard;
        this.refreshNames(config);
    }

    /** Retain control ownership while registering fields created by metadata recovery. */
    captureControls() {
        const previous = this.controls;
        this.controls = new Map();
        for (const control of find("wizard").elements) {
            if (["INPUT", "SELECT", "TEXTAREA"].includes(control.tagName) && !control.hasAttribute("data-view-control")) {
                this.controls.set(control, previous.get(control) ?? this.saveState.locked.get(control) ?? control.disabled);
            }
        }
    }

    /** Refresh labels and facts only, retaining selected devices, editor values and saved revisions. */
    refreshNames(config) {
        if (!this.config) return;
        this.captureControls();
        this.config = {...this.config, catalogue: config.catalogue, devices: config.devices};
        for (const [selector, container, kind] of [["installation-solar", "solar-rows", "solar"], ["installation-battery", "battery-rows", "battery"]]) {
            const selectedEntity = find(selector).selectedOptions[0]?.dataset.entity;
            const choices = [...find(container).children].map((row, index) => {
                const entity = row.querySelector(`[data-key="${kind === "solar" ? "entity_id" : "energy_entity"}"]`)?.value;
                const option = document.createElement("option");
                option.value = String(index);
                option.dataset.entity = entity || "";
                const name = sourceName(config.catalogue?.data?.entities?.find(item => item.entity_id === entity), `${optionLabel(kind)} ${index + 1}`);
                option.dataset.name = name;
                option.textContent = entity && name !== entity ? `${name} (${entity})` : name;
                return option;
            });
            find(selector).replaceChildren(...choices);
            const selected = choices.find(option => option.dataset.entity === selectedEntity);
            if (selected) find(selector).value = selected.value;
            find(selector).closest("label").hidden = choices.length === 0;
            this.selectDevice(selector, container);
        }
        find("ha-installation-state").textContent = this.ready ? "Connected" : "Waiting for configuration";
        const catalogue = config.catalogue?.data;
        const facts = find("ha-installation-facts"); facts.replaceChildren();
        const hints = catalogue?.suggestions;
        const name = entity => {
            const label = sourceName(catalogue?.entities?.find(item => item.entity_id === entity), null);
            return label && label !== entity ? `${label} (${entity})` : entity;
        };
        const storageMeters = new Set(hints?.batteries?.flatMap(battery => [battery.energy_entity, battery.charge_energy_entity]) || []);
        const meters = sign => (hints?.terms || []).filter(term => term.sign === sign && !hints?.solar_sources?.includes(term.entity_id) && !storageMeters.has(term.entity_id)).map(term => term.entity_id);
        this.gridSources = [
            ["Grid power sensors", (hints?.grid_power_entities || (hints?.grid_power ? [hints.grid_power] : [])).map(name)],
            ["Grid import energy meters", (hints?.settlement?.imports || meters(1)).map(name)],
            ["Grid export energy meters", (hints?.settlement?.exports || meters(-1)).map(name)],
        ];
        for (const [title, value] of [["Energy configuration", this.ready ? "Ready" : "Waiting for complete configuration"], ...this.gridSources, ["Solar sources", hints?.solar_sources?.map(name) || []], ["Batteries", hints?.batteries?.map(battery => name(battery.energy_entity)) || []], ["Timezone", catalogue?.timezone || "Unavailable"]]) summaryFact(facts, title, value);
        const estimate = this.config.annual_estimate;
        if (estimate) summaryFact(facts, "Annual household consumption", `${estimate.annual_kwh} kWh/year. ${estimate.extrapolated === false ? `Measured over ${estimate.days} complete days.` : `Extrapolated from ${estimate.days} complete days within the last year. Seasonal differences may affect it.`}`);
        const connectionSources = find("installation-grid-sources"); connectionSources.replaceChildren();
        for (const [title, value] of this.gridSources) summaryFact(connectionSources, title, value);
        find("ha-installation-summary").textContent = catalogue
            ? "Manage source membership, meters and battery capacity in Home Assistant Settings > Dashboards > Energy. All configured grid connections contribute to the balance. Devices removed from Energy are excluded automatically."
            : "Waiting for the Home Assistant integration to upload its Energy configuration.";
        find("installation-shell").hidden = false;
        find("wizard").hidden = false;
        this.sync();
    }

    /** Show one detected source while retaining every draft and stable source identity. */
    selectDevice(selector, container) {
        [...find(container).children].forEach((row, index) => { row.hidden = index !== Number(find(selector).value); });
        this.summary();
    }

    /** Navigation is local presentation. Draft values never become persisted by selection. */
    show(area) {
        if (this.wizard && area >= 4 && area !== this.area) this.editing = false;
        this.area = area;
        find("installation-area").value = String(area);
        this.sync();
    }

    /** Successful persistence ends the wizard and explicit editing. Partial failures never call this. */
    saved(configured) {
        this.wizard = !configured;
        this.editing = false;
        this.sync();
    }

    /** Apply mode and locks without overriding Home Assistant ownership or SaveBar's in-flight lock. */
    sync() {
        const locked = this.saveState.busy;
        if (this.wizard && this.area < 4) this.editing = this.ready;
        if (!locked) for (const [control, managed] of this.controls) if (control.isConnected) control.disabled = managed || !this.editing;
        if (!locked) find("export").disabled = !this.editing || !find("allow-feed-in").checked;
        const strategyPanel = find("strategy-settings");
        const strategyParent = document.querySelector(`[data-step="${this.wizard && this.area < 4 ? 3 : 4}"]`);
        if (strategyPanel.parentElement !== strategyParent) strategyParent.append(strategyPanel);
        const strategyValue = find("installation-strategy-value");
        strategyValue.hidden = this.editing;
        strategyValue.replaceChildren();
        summaryFact(strategyValue, "Optimization goal", find("strategy").selectedOptions[0]?.textContent || "Unavailable");
        find("strategy-explanation").textContent = find("strategy").value === "minimize consumption"
            ? "Reduce grid import plus the grid energy needed to replace depleted starting battery stock."
            : find("strategy").value === "minimize cost" ? "Choose the plan with the lowest expected economic cost."
            : "Choose a supported optimization goal.";
        const priceControls = [...document.querySelectorAll("[data-price-preference]")];
        for (const control of priceControls) control.disabled = !this.editing || locked;
        const canEdit = this.area === 5 ? priceControls.length > 0 : this.area === 4 ? this.controls.get(find("strategy")) === false : this.ready && [0, 1, 2].includes(this.area);
        const wizardArea = this.wizard && this.area < 4;
        find("installation-heading").textContent = this.wizard ? "Set up Open Energy Planner" : "Installation settings";
        find("installation-intro").textContent = this.wizard ? "Connect your Energy dashboard and confirm your installation." : "Your connection, energy sources and planning preferences.";
        document.body.classList.toggle("installation-readonly", !this.editing);
        find("installation-toolbar").hidden = wizardArea;
        find("installation-area").closest("label").hidden = this.wizard;
        find("installation-return").hidden = !this.wizard || this.area < 4;
        find("installation-return").disabled = locked;
        find("installation-steps").hidden = !wizardArea;
        find("installation-after-setup").hidden = !this.wizard;
        find("installation-wizard-actions").hidden = !wizardArea;
        find("installation-area").disabled = locked || this.editing;
        find("edit-installation").hidden = wizardArea || this.editing || !canEdit;
        find("edit-installation").disabled = locked;
        find("save").hidden = !this.editing || (wizardArea && this.area !== 3);
        find("save").textContent = this.area === 5 ? "Save price sources" : this.area === 4 ? "Save planning strategy" : "Save installation";
        find("discard-installation").hidden = !this.editing || wizardArea;
        find("discard-installation").textContent = "Cancel";
        if (this.editing && !locked) find("discard-installation").disabled = false;
        find("installation-save-bar").hidden = !this.ready && this.area !== 4 && !(this.area === 5 && priceControls.length);
        // The same SaveBar moves with the wizard actions. There is still one saved/unsaved status.
        const actions = wizardArea ? find("installation-wizard-actions") : find("installation-toolbar").querySelector(".settings-actions");
        if (find("installation-save-bar").parentElement !== actions) actions.append(find("installation-save-bar"));
        for (const panel of document.querySelectorAll("#wizard > [data-step]")) {
            panel.hidden = Number(panel.dataset.step) !== this.area || (!this.editing && [0, 1, 2].includes(this.area));
        }
        find("grid-field").hidden = false;
        find("annual-field").hidden = false;
        find("next").disabled = locked || !this.ready;
        find("next").hidden = this.area === 3 || this.area > 3;
        find("back").disabled = locked || this.area === -1;
        find("progress").textContent = this.area === 3 ? "Nothing is saved until you finish." : `Stage ${wizardAreas.indexOf(this.area) + 1} of ${wizardAreas.length}`;
        const steps = find("installation-steps");
        if (!steps.children.length) steps.replaceChildren(...wizardAreas.map((area, index) => {
            const item = document.createElement("li"), button = document.createElement("button");
            button.type = "button"; button.textContent = ["Home Assistant", "Connection", "Solar", "Battery", "Review"][index];
            button.onclick = () => this.select(area);
            item.append(button); return item;
        }));
        wizardAreas.forEach((area, index) => {
            const button = steps.children[index].firstElementChild;
            button.disabled = locked || index > wizardAreas.indexOf(this.area);
            if (area === this.area) button.setAttribute("aria-current", "step");
            else button.removeAttribute("aria-current");
        });
        for (const [area, selector, container] of [[1, "installation-solar", "solar-rows"], [2, "installation-battery", "battery-rows"]]) {
            const label = find(selector).closest("label");
            const parent = !this.editing && this.area === area ? find("installation-device-toolbar") : find(container).parentElement;
            if (label.parentElement !== parent) parent.insertBefore(label, parent === find("installation-device-toolbar") ? null : find(container));
        }
        this.summary(); this.capacities();
    }

    /** Render a text-only summary of the selected saved editor. Unknown numeric values stay unknown. */
    summary() {
        const exportLimit = find("export").value;
        const feedInDisabled = !find("allow-feed-in").checked || (exportLimit !== "" && Number(exportLimit) === 0);
        find("feed-in-warning").hidden = !feedInDisabled;
        const panel = find("installation-summary");
        const summary = find("installation-summary-content");
        const visible = this.ready && !this.editing && [0, 1, 2].includes(this.area);
        panel.hidden = !visible;
        if (!visible) return;
        summary.replaceChildren();
        const tag = document.createElement("span"), heading = document.createElement("h2"), list = document.createElement("dl");
        tag.className = "settings-tag"; tag.textContent = "Saved settings";
        heading.textContent = areas.find(([key]) => key === this.area)[1];
        let root = document.querySelector(`[data-step="${this.area}"]`);
        if (this.area === 1 || this.area === 2) {
            const container = this.area === 1 ? "solar-rows" : "battery-rows";
            const selector = this.area === 1 ? "installation-solar" : "installation-battery";
            root = find(container).children[Number(find(selector).value)];
            heading.textContent = find(selector).selectedOptions[0]?.dataset.name || heading.textContent;
        }
        list.className = "settings-summary";
        const ownership = root?.querySelector("[data-panel-ownership]");
        if (ownership) summaryFact(list, "Panel settings", ownership.textContent);
        const panelLists = new Map(), panelSections = [];
        if (this.area === 1) for (const group of root?.querySelectorAll(".solar-panel") || []) {
            const section = document.createElement("section"), title = document.createElement("h3"), facts = document.createElement("dl");
            section.className = "solar-panel-summary";
            title.textContent = group.getAttribute("aria-label");
            facts.className = "settings-summary";
            section.append(title, facts);
            panelLists.set(group, facts);
            panelSections.push(section);
        }
        for (const input of root?.querySelectorAll("input,select") || []) {
            if (input.closest(".calibration-fields")?.hidden) continue;
            if (input.type === "hidden" || input.id === "grid-invert" || input.dataset.key === "panel_name" || input.hasAttribute("data-view-control")) continue;
            const title = input.getAttribute("aria-label") || [...(input.labels?.[0]?.childNodes || [])].filter(node => node.nodeType === Node.TEXT_NODE).map(node => node.textContent).join("").trim();
            if (["grid", "export-energy"].includes(input.id) || input.dataset.key === "inverter_id") continue;
            const checked = input.checked && (input.id !== "allow-feed-in" || !feedInDisabled);
            const value = input.type === "checkbox" ? (checked ? "Enabled" : "Off") : input.dataset.key === "minimum_soc" ? (input.value === "" ? "Not configured" : percentage(Number(input.value))) : !input.value ? "Not configured" : input.dataset.key === "wear_cost_per_kwh" ? configuredRate(Number(input.value)) : input.selectedOptions?.[0]?.textContent || input.value;
            summaryFact(panelLists.get(input.closest(".solar-panel")) || list, title, input.dataset.key === "panel_azimuth" ? azimuthLabel(input.value === "" ? null : Number(input.value)) : value);
        }
        if (this.area === 0) for (const [title, value] of this.gridSources || []) summaryFact(list, title, value);
        if (this.area === 2 && root) summaryFact(list, "Usable capacity source", "Home Assistant Energy: Read-only");
        summary.append(tag, heading, list, ...panelSections);
        for (const status of root?.querySelectorAll(".calibration-status") || []) summary.append(status.cloneNode(true));
        if (!root) { const empty = document.createElement("p"); empty.textContent = `No ${this.area === 1 ? "solar sources" : "batteries"} configured in Home Assistant Energy.`; summary.append(empty); }
        if (this.area === 0 && feedInDisabled) {
            const warning = find("feed-in-warning").cloneNode(true); warning.removeAttribute("id"); summary.append(warning);
        }
    }

    /** Show configuration capacity totals, never live measurements or inferred missing zeroes. */
    capacities() {
        const container = find("installation-capacities"); container.replaceChildren();
        const number = value => value === "" || value == null ? "Not configured" : Number(value).toLocaleString();
        const solar = [...find("solar-rows").querySelectorAll('[data-key="capacity"],[data-key="panel_capacity"]')];
        const batteries = [...find("battery-rows").querySelectorAll('[data-key="capacity_kwh"]')];
        const totalCapacity = inputs => inputs.length && inputs.every(input => input.value !== "") ? inputs.reduce((total, input) => total + Number(input.value), 0) : null;
        const solarCapacity = totalCapacity(solar), batteryCapacity = totalCapacity(batteries);
        const facts = [["home", "Grid import limit", find("import").value, "kW"], ["home", "Grid export limit", find("export").value, "kW"], ["solar", "Panel capacity", solarCapacity, "kWp"], ["battery", "Usable storage", batteryCapacity, "kWh"]];
        const reserves = [...find("battery-rows").querySelectorAll('[data-key="minimum_soc"]')];
        find("review-connection").textContent = `${number(find("import").value)} kW import: ${number(find("export").value)} kW export`;
        find("review-solar").textContent = `${find("solar-rows").children.length} sources: ${solarCapacity == null ? "Not configured" : `${number(solarCapacity)} kWp`}`;
        find("review-batteries").textContent = `${batteries.length} batteries: ${batteryCapacity == null ? "Not configured" : `${number(batteryCapacity)} kWh usable capacity from Home Assistant`}`;
        if (reserves.length === 1) facts.push(["settings", "Planning reserve", reserves[0].value === "" ? null : percentage(Number(reserves[0].value)), ""]);
        else if (reserves.length > 1) facts.push(["settings", "Planning reserves", "Per battery", ""]);
        for (const [icon, label, value, unit] of facts) {
            const card = document.createElement("div"), image = document.createElement("img"), figure = document.createElement("strong"), caption = document.createElement("small");
            image.src = new URL(`icons/${icon}.svg`, import.meta.url).href; image.alt = "";
            figure.textContent = !this.ready ? "Unavailable" : unit ? `${number(value)}${value === "" || value == null ? "" : ` ${unit}`}` : value ?? "Not configured"; caption.textContent = label;
            card.append(image, figure, caption); container.append(card);
        }
    }
}
