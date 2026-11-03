import {money, when, localDate, localInstant, optionLabel, useHomeAssistantTimezone} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
import {SaveBar} from "./save-bar.js";
import {refreshNotifications} from "./notifications.js";
/** Edit app-owned dated rules. Arithmetic and money validation stay on the server. */
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = (id) => document.getElementById(id);
let token = "", version = null, timezone = "UTC", revision = 0, previewed = -1, busy = false;
const AUTO_PREVIEW_DELAY_MS = 600;
const BONUS_HISTORY_REFRESH_MS = 3000;
let previewTimer = null, loading = false;
let contractId = null, selectedPeriod = 0, editing = false;
bindSettingsActions();
const periods = [];
const bonusEditors = new WeakMap();
const conditionEditors = new WeakMap();
const monthlyEditors = new WeakMap();
const activeStarts = {};
const saveState = new SaveBar({form: "contract-editor", bar: "contract-save-bar", save: "save-contract", discard: "reload-contract", status: "contract-save-state", canSave: () => editing && periods.length > 0 && previewed === revision, read: () => [...document.querySelectorAll("#contract-identity input, #contract-identity select, #contract-periods input, #contract-periods select")].map(input => [input.type, input.type === "checkbox" ? input.checked : input.value])});
const say = (message) => { find("contract-status").textContent = message; saveState.update(message); };

async function api(path, method = "GET", body) {
    const response = await fetch(base + path, {
        method, headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"},
        body: body === undefined ? undefined : JSON.stringify(body), redirect: "error", cache: "no-store",
    });
    if (!response.ok) throw new Error(response.status === 401 ? "The installation token was rejected." : `${response.status}: ${await response.text()}`);
    return response.json();
}

function changed(event) {
    if (event?.target && (!event.target.closest("#contract-editor") || event.target.closest("#contract-budget"))) return;
    if (event?.target && periods.some(inputs => inputs.effective_from === event.target) && event.target.value) sortPeriods();
    revision += 1; previewed = -1; find("save-contract").disabled = true;
    find("contract-preview").replaceChildren();
    document.querySelectorAll(".rule-preview").forEach((element) => element.remove());
    clearTimeout(previewTimer);
    if (!loading) previewTimer = setTimeout(preview, AUTO_PREVIEW_DELAY_MS);
    say("Unsaved changes. Rates are checked automatically after typing. Use Save contract to apply them.");
}

function field(parent, title, value, help, choices) {
    const label = document.createElement("label");
    label.append(document.createTextNode(title));
    const input = document.createElement(choices ? "select" : "input");
    input.setAttribute("aria-label", title);
    if (choices) for (const choice of choices) {
        const option = document.createElement("option"); option.value = choice; option.textContent = optionLabel(choice); input.append(option);
    }
    input.value = value ?? "";
    const hint = document.createElement("small"); hint.textContent = help;
    label.append(input, hint); parent.append(label); input.addEventListener("input", changed);
    return input;
}

/** Group related editors without changing stored values or input event ownership. */
function fieldGroup(parent, title) {
    const heading = document.createElement("h3"), group = document.createElement("div");
    heading.textContent = title; group.className = "settings-fields";
    parent.append(heading, group); return group;
}

/** Native disclosure rows keep saved activation separate from unsaved form edits. */
function row(parent, collection, title, savedStart) {
    const details = document.createElement("details"), summary = document.createElement("summary");
    details.className = "contract-rule"; details.open = true; details.dataset.savedStart = savedStart || "";
    details.append(summary);
    const box = document.createElement("fieldset");
    const legend = document.createElement("legend"); legend.textContent = title; box.append(legend);
    const inputs = {};
    const remove = document.createElement("button"); remove.type = "button"; remove.className = "secondary"; remove.textContent = `Remove ${title.toLowerCase()}`;
    remove.onclick = () => removeRule(collection, inputs, title);
    box.append(remove); details.append(box); parent.append(details); collection.push(inputs);
    details.addEventListener("input", () => { details.dataset.dirty = "true"; refreshRows(collection, title); });
    return [box, inputs];
}

/** Update saved-state summaries. Typing never closes a row the user opened. */
function refreshRows(collection, title, collapse = false) {
    for (const inputs of collection) {
        const details = inputs.effective_from.closest("details"), start = details.dataset.savedStart;
        const status = !start ? "New rule" : start === activeStarts[title] ? "Active" : Date.parse(start) > Date.now() ? "Scheduled" : "Superseded";
        details.querySelector("summary").textContent = `${title}: ${inputs.effective_from.value || "Start date required"}: ${status}${details.dataset.dirty ? ": Unsaved changes" : ""}`;
        if (collapse) details.open = status === "Active" || !start;
    }
}

/** Refresh the saved-state snapshot only after load or an unchanged successful save. */
function showSavedRules(result) {
    const values = result.periods || [], title = "Price rule";
    activeStarts[title] = values.filter((value) => Date.parse(value.effective_from) <= Date.now()).sort((left, right) => Date.parse(left.effective_from) - Date.parse(right.effective_from)).at(-1)?.effective_from;
    const nextStart = values.filter(value => Date.parse(value.effective_from) > Date.now()).sort((left, right) => Date.parse(left.effective_from) - Date.parse(right.effective_from))[0]?.effective_from;
    find("rule-context").textContent = `In effect: ${activeStarts[title] ? localDate(activeStarts[title]) : "No rule"}${nextStart ? `: Next change: ${localDate(nextStart)}` : ""}`;
    periods.forEach((inputs, index) => {
        const details = inputs.effective_from.closest("details"), savedStart = values[index]?.effective_from || "";
        details.dataset.savedStart = savedStart; delete details.dataset.dirty;
        inputs.effective_from.dataset.original = savedStart;
        inputs.effective_from.dataset.originalDate = savedStart ? localDate(savedStart) : "";
        const contractStart = values[index]?.contract_start;
        if (contractStart) {
            inputs.contract_start.dataset.original = contractStart;
            inputs.contract_start.dataset.originalDate = localDate(contractStart);
        }
        const bonus = (result.bonuses || []).find((rule) => rule.effective_from === savedStart);
        if (bonus) {
            const identity = bonusEditors.get(inputs).inputs.rule_id;
            identity.value = bonus.rule_id; identity.dataset.contractStart = inputs.contract_start.value;
        }
    });
    refreshRows(periods, title);
    for (const inputs of periods) inputs.effective_from.closest("details").open = true;
}

function dateField(parent, title, value, help) {
    if (value?.includes("T") && localInstant(localDate(value)) !== new Date(value).toISOString()) help += ` Existing time ${when(value)} is retained until you change this date.`;
    const input = field(parent, title, value ? (value.length === 10 ? value : localDate(value)) : "", help);
    input.type = "date";
    input.dataset.original = value || "";
    input.dataset.originalDate = input.value;
    return input;
}
function dateValue(input) { return input.value === input.dataset.originalDate && input.dataset.original ? input.dataset.original : input.value; }
/** Native dialogs handle Escape, focus containment and return focus to the information button. */
function information(parent, dialogId, label) {
    const button = document.createElement("button"); button.type = "button"; button.className = "info-button";
    button.textContent = "i"; button.setAttribute("aria-label", label); button.setAttribute("aria-haspopup", "dialog"); button.setAttribute("aria-controls", dialogId);
    button.onclick = () => find(dialogId).showModal(); parent.append(button);
}
/** Use the ARIA tabs pattern with one tab stop, arrow keys and explicit panel ownership. */
for (const list of document.querySelectorAll('.contract-help [role="tablist"]')) {
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    const select = (selected, focus = false) => {
        for (const tab of tabs) {
            const active = tab === selected; tab.setAttribute("aria-selected", String(active)); tab.tabIndex = active ? 0 : -1;
            find(tab.getAttribute("aria-controls")).hidden = !active;
        }
        if (focus) selected.focus();
    };
    for (const tab of tabs) {
        tab.onclick = () => select(tab);
        tab.onkeydown = (event) => {
            const current = tabs.indexOf(tab);
            const index = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : event.key === "ArrowRight" ? (current + 1) % tabs.length : event.key === "ArrowLeft" ? (current + tabs.length - 1) % tabs.length : null;
            if (index !== null) { event.preventDefault(); select(tabs[index], true); }
        };
    }
}
/**
 * Append a dated draft editor from a server rule or the previous rule's formulas.
 * Undefined bonusValue inherits the previous bonus. Null explicitly disables it.
 * Original instants remain attached to date inputs until the user changes a date.
 * This mutates the editor and preview revision only. Saving validates on the server.
 */
function addPeriod(value = {}, bonusValue, previous = periods.at(-1)) {
    const inherited = previous && bonusEditors.get(previous);
    const source = value.effective_from ? value : previous ? {import_formula: previous.import_formula.value, export_formula: previous.export_formula.value} : value;
    const [box, inputs] = row(find("contract-periods"), periods, "Price rule", value.effective_from);
    const dates = fieldGroup(box, "Effective dates");
    inputs.contract_start = dateField(dates, "Contract starts", value.contract_start ?? (previous && dateValue(previous.contract_start)) ?? (value.effective_from ? localDate(value.effective_from) : localDate()), "Original start with this supplier. Keep it for changed terms within the same contract. Change it when switching supplier. Confirm the prefilled date for older rules.");
    inputs.contract_start.required = true;
    inputs.effective_from = dateField(dates, "Price rule starts", value.effective_from ?? (previous ? "" : localDate()), `The whole rule starts at 00:00 in ${timezone}, independently of the contract start. Leave existing timestamps unchanged to retain their original instant.`);
    inputs.effective_from.required = true;
    const updateMinimum = () => { inputs.effective_from.min = inputs.contract_start.value; };
    inputs.contract_start.addEventListener("input", updateMinimum); updateMinimum();
    const rates = fieldGroup(box, "Import and export"); information(rates.previousElementSibling, "price-rule-help", "Price rule example");
    inputs.import_formula = field(rates, "Import formula", source.import_formula ?? "market", "Total import cost per kWh, including applicable taxes and additions.");
    inputs.export_formula = field(rates, "Export formula", source.export_formula ?? "market", "Export income per kWh before this rule's optional bonus.");
    const inheritedValue = inherited ? readBonus(inherited.inputs, false) : null;
    const bonus = bonusValue === undefined ? inheritedValue : bonusValue;
    const enabled = bonusValue === undefined ? Boolean(inherited?.enabled.checked) : Boolean(bonus && bonus.rate_formula !== "0");
    bonusEditors.set(inputs, addBonusEditor(box, inputs, bonus || {}, enabled));
    refreshRows(periods, "Price rule"); changed();
}

const comparisonChoices = [
    ["greater_than", "Greater than"], ["greater_than_or_equal", "Greater than or equal to"],
    ["less_than", "Less than"], ["less_than_or_equal", "Less than or equal to"],
    ["equal", "Equal to"], ["not_equal", "Not equal to"],
];
/** Collect a comparison draft. The server alone validates and evaluates its arithmetic. */
function comparisonEditor(parent, value) {
    const enabled = document.createElement("input"), label = document.createElement("label");
    enabled.type = "checkbox"; enabled.checked = Boolean(value);
    label.append(enabled, document.createTextNode("Enable price condition")); parent.append(label);
    const group = document.createElement("fieldset"); group.className = "settings-fields";
    const legend = document.createElement("legend"); legend.textContent = "Price condition"; group.append(legend); parent.append(group);
    const expression = field(group, "Price expression", value?.expression ?? "market", "Arithmetic expression in EUR/kWh, using the same market formula syntax as prices.");
    const comparison = field(group, "Comparison", value?.operator ?? "greater_than", "Choose how the expression must compare with the threshold.", comparisonChoices.map(([key]) => key));
    for (const option of comparison.options) option.textContent = comparisonChoices.find(([key]) => key === option.value)[1];
    const threshold = field(group, "Threshold (€/kWh)", value?.threshold ?? "0", "Exact decimal EUR/kWh boundary. Configuration precision is preserved. No tolerance is added.");
    threshold.type = "number"; threshold.step = "any";
    const preview = document.createElement("p"); preview.setAttribute("role", "status"); group.append(preview);
    const update = () => {
        group.hidden = !enabled.checked;
        for (const input of [expression, comparison, threshold]) input.disabled = !enabled.checked;
        preview.textContent = `Bonus applies when ${expression.value || "the expression"} is ${comparison.selectedOptions[0].textContent.toLowerCase()} ${threshold.value || "0"} €/kWh. The draft is validated automatically after typing.`;
    };
    enabled.addEventListener("change", () => { update(); changed(); });
    for (const input of [expression, comparison, threshold]) input.addEventListener("input", update);
    update();
    return () => enabled.checked ? {expression: expression.value.trim(), operator: comparison.value, threshold: threshold.value.trim()} : null;
}

/** Twelve compact local ranges. Disabled months remain explicit null entries. */
function monthlyEditor(parent, schedule) {
    const details = document.createElement("details"); details.className = "monthly-window";
    const summary = document.createElement("summary"); summary.textContent = "Monthly hours"; details.append(summary);
    const help = document.createElement("p"); help.textContent = "Start is included, end is excluded. Unchecked months pay no bonus. Night windows continue into the following day, including the next month. The opening date selects the month. Both repeated autumn hours count. Missing spring hours do not exist."; details.append(help);
    const grid = document.createElement("div"); grid.className = "monthly-window-grid"; details.append(grid);
    const headings = document.createElement("div"); headings.className = "monthly-window-row";
    for (const title of ["Month", "Start", "End"]) { const heading = document.createElement("strong"); heading.textContent = title; headings.append(heading); } grid.append(headings);
    const months = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"];
    const rows = months.map((month, index) => {
        const row = document.createElement("div"); row.className = "monthly-window-row"; grid.append(row);
        const entry = schedule ? schedule[index] : {start_time: "06:00", end_time: "22:00"};
        const label = document.createElement("label"), enabled = document.createElement("input");
        enabled.type = "checkbox"; enabled.checked = entry !== null; enabled.setAttribute("aria-label", `${month} enabled`);
        label.append(enabled, document.createTextNode(month)); row.append(label);
        const start = field(row, `${month} start`, entry?.start_time ?? "06:00", "");
        const end = field(row, `${month} end`, entry?.end_time ?? "22:00", "");
        for (const input of [start, end]) { input.parentElement.firstChild.remove(); input.placeholder = "HH:MM"; input.required = true; input.pattern = "[0-9]{2}:[0-9]{2}"; }
        const update = () => { start.disabled = end.disabled = !enabled.checked; };
        enabled.addEventListener("change", () => { update(); changed(); }); update();
        return {enabled, start, end};
    });
    parent.append(details);
    return {
        // Hidden ranges must not participate in native form validation.
        setActive: active => {
            details.hidden = !active;
            for (const {enabled, start, end} of rows) {
                enabled.disabled = !active;
                start.disabled = end.disabled = !active || !enabled.checked;
            }
        },
        read: () => rows.map(({enabled, start, end}) => enabled.checked ? {start_time: start.value, end_time: end.value} : null),
    };
}

/**
 * Append optional bonus controls for a period and return their inputs and toggle.
 * The bonus shares its period's dates. Rates use EUR/kWh and caps use exported kWh.
 * Preserve saved identity and opening evidence. Browser changes remain drafts until
 * the server validates and saves the contract. No allowance is reset here.
 */
function addBonusEditor(parent, period, value, isEnabled) {
    const label = document.createElement("label"), enabled = document.createElement("input");
    enabled.type = "checkbox"; enabled.checked = isEnabled;
    label.append(enabled, document.createTextNode("Include export bonus")); parent.append(label);
    const box = document.createElement("fieldset"); box.className = "bonus-editor";
    const legend = document.createElement("legend"); legend.textContent = "Export bonus"; box.append(legend); parent.append(box);
    information(legend, "export-bonus-help", "Export bonus example");
    const inputs = {effective_from: period.effective_from, contract_start: period.contract_start};
    const identity = document.createElement("input"); identity.type = "hidden"; identity.value = value.rule_id || ""; identity.dataset.contractStart = period.contract_start.value; box.append(identity); inputs.rule_id = identity;
    const rates = document.createElement("div"); rates.className = "settings-fields"; box.append(rates);
    inputs.rate_formula = field(rates, "Extra export rate", value.rate_formula ?? "0", "Extra income per kWh. Set eligibility separately with Price condition.");
    inputs.cap_kwh = field(rates, "Annual export cap (kWh)", value.cap_kwh, "All exported kWh count towards this cap, including unpaid export. A new price rule does not reset used volume.");
    conditionEditors.set(inputs, comparisonEditor(box, value.price_condition));

    const zone = document.createElement("p"); zone.textContent = `Timezone: ${timezone}: Change it in Home Assistant installation settings.`; box.append(zone);
    const contractDay = period.contract_start.value.slice(5);
    const savedDay = value.reset_month ? `${String(value.reset_month).padStart(2, "0")}-${String(value.reset_day).padStart(2, "0")}` : "01-01";
    const timing = document.createElement("div"); timing.className = "settings-fields"; box.append(timing);
    inputs.reset_basis = field(timing, "Annual reset", savedDay === "01-01" ? "calendar_year" : "contract_anniversary", "Calendar year starts on 1 January. Contract anniversary uses Contract starts, never this price rule's effective date.", ["calendar_year", "contract_anniversary"]);
    inputs.reset_basis.options[0].textContent = "Calendar year (1 January)"; inputs.reset_basis.options[1].textContent = "Contract anniversary";
    if (savedDay !== "01-01" && savedDay !== contractDay) {
        const existing = document.createElement("option"); existing.value = "existing"; existing.textContent = `Existing reset (${savedDay})`; inputs.reset_basis.append(existing); inputs.reset_basis.value = "existing";
    }
    inputs.reset_basis.dataset.savedDay = savedDay;
    inputs.window = field(timing, "Paid window", value.window ?? "always", "Always, fixed local hours, sunrise to sunset, or different clock hours per month.", ["always", "clock", "daylight", "monthly_schedule"]);
    inputs.start_time = field(box, "Window start", value.start_time, "HH:MM, included. Required only for clock windows.");
    inputs.end_time = field(box, "Window end", value.end_time, "HH:MM, excluded. Clock windows may cross midnight.");
    inputs.start_time.type = inputs.end_time.type = "time";
    const monthly = monthlyEditor(box, value.monthly_schedule); monthlyEditors.set(inputs, monthly);
    const update = () => {
        monthly.setActive(inputs.window.value === "monthly_schedule");
        box.hidden = !enabled.checked; box.disabled = !enabled.checked;
        for (const key of ["start_time", "end_time"]) { inputs[key].disabled = inputs.window.value !== "clock"; inputs[key].parentElement.hidden = inputs[key].disabled; }
    };
    enabled.addEventListener("change", () => { update(); changed(); });
    inputs.window.addEventListener("change", update); update();
    return {inputs, enabled, box};
}

function readBonus(inputs, validate = true) {
    const value = Object.fromEntries(Object.entries(inputs).map(([key, input]) => [key, input.value.trim()]));
    value.price_condition = conditionEditors.get(inputs)(); value.effective_from = dateValue(inputs.effective_from); value.timezone = timezone;
    const reset = value.reset_basis === "calendar_year" ? "01-01" : value.reset_basis === "existing" ? inputs.reset_basis.dataset.savedDay : inputs.contract_start.value.slice(5);
    if (!value.rule_id || inputs.rule_id.dataset.contractStart !== inputs.contract_start.value) delete value.rule_id;
    delete value.contract_start;
    if (validate && !/^\d{2}-\d{2}$/.test(reset || "")) throw new Error("Enter the original contract start before choosing its anniversary.");
    [value.reset_month, value.reset_day] = (reset || "01-01").split("-").map(Number); delete value.reset_basis;
    if (validate && !value.cap_kwh) throw new Error("Enter the bonus export cap.");
    value.cap_kwh = value.cap_kwh === "" && !validate ? undefined : Number(value.cap_kwh);
    if (value.window !== "clock") value.start_time = value.end_time = null;
    if (value.window === "monthly_schedule") value.monthly_schedule = monthlyEditors.get(inputs).read();
    return value;
}

function payload() {
    for (const inputs of periods) {
        if (!dateValue(inputs.contract_start)) throw new Error("Enter Contract starts for every price rule.");
        if (!dateValue(inputs.effective_from)) throw new Error("Enter Price rule starts for every price rule.");
        if (inputs.effective_from.value < inputs.contract_start.value) throw new Error("Price rule starts must be on or after Contract starts.");
    }
    const bonusRules = []; let previous = null, previousContract = null;
    for (const period of periods) {
        const editor = bonusEditors.get(period);
        if (editor.enabled.checked) previous = readBonus(editor.inputs);
        else if (previous) previous = {...previous, effective_from: dateValue(period.effective_from), rate_formula: "0", price_condition: null, window: "always", start_time: null, end_time: null};
        if (previous && previous.window !== "monthly_schedule") delete previous.monthly_schedule;
        if (!editor.enabled.checked && previous && previousContract !== period.contract_start.value) delete previous.rule_id;
        if (previous) bonusRules.push(previous);
        previousContract = period.contract_start.value;
    }
    return {
        contract_id: contractId, market_area: find("market-area").value.trim().toUpperCase(), currency: "EUR", expected_version: version,
        periods: periods.map((inputs, index) => ({contract_start: dateValue(inputs.contract_start), effective_from: dateValue(inputs.effective_from), effective_until: periods[index + 1] ? dateValue(periods[index + 1].effective_from) : null, import_formula: inputs.import_formula.value.trim(), export_formula: inputs.export_formula.value.trim()})),
        bonuses: bonusRules,
    };
}

function table(title, headings, rows) {
    const section = document.createElement("div"); section.className = "table-scroll";
    const grid = document.createElement("table"); const caption = document.createElement("caption"); caption.textContent = title; grid.append(caption);
    const header = document.createElement("tr");
    for (const heading of headings) { const cell = document.createElement("th"); cell.textContent = heading; header.append(cell); }
    const head = document.createElement("thead"); head.append(header); grid.append(head);
    const body = document.createElement("tbody");
    for (const values of rows) { const line = document.createElement("tr"); for (const value of values) { const cell = document.createElement("td"); cell.textContent = value; line.append(cell); } body.append(line); }
    grid.append(body); section.append(grid); return section;
}

/**
 * Request server-evaluated EUR/kWh samples and draft annual-volume evidence.
 * Render five examples for the selected rule without altering the saved allowance.
 * A failed or stale response cannot authorize saving a newer draft revision.
 */
async function preview() {
    clearTimeout(previewTimer);
    if (busy || loading) return;
    const requestRevision = revision;
    try {
        const proposed = payload();
        const result = await api("v1/contracts/preview", "POST", proposed);
        if (revision !== requestRevision) return;
        document.querySelectorAll(".rule-preview").forEach((element) => element.remove());
        const show = (inputs, block) => { block.classList.add("rule-preview", "price-preview"); block.dataset.periodIndex = String(periods.indexOf(inputs)); block.hidden = periods.indexOf(inputs) !== selectedPeriod; find("contract-preview").append(block); };
        result.preview.forEach((samples, index) => {
            const inputs = periods[index], enabled = bonusEditors.get(inputs).enabled.checked;
            const bonus = (result.bonus_preview || []).find(item => Date.parse(item.effective_from) === Date.parse(proposed.periods[index].effective_from.includes("T") ? proposed.periods[index].effective_from : localInstant(proposed.periods[index].effective_from)));
            const headings = ["Spot", "Import cost", "Base export", ...(enabled ? ["Extra bonus"] : [])];
            show(inputs, table(`Price rule ${index + 1}: EUR/kWh`, headings, samples.map(item => [money(item.market), money(item.buy_price), money(item.sell_price), ...(enabled ? [money(bonus?.samples.find(sample => sample.market === item.market)?.extra_rate)] : [])])));
        });
        (result.bonus_budget_preview || []).forEach((budget, index) => {
            const owner = periods.find(inputs => dateValue(inputs.effective_from) === proposed.bonuses[index].effective_from);
            const note = document.createElement("p"); note.className = "rule-preview bonus-preview draft-budget-preview";
            note.dataset.periodIndex = String(periods.indexOf(owner)); note.hidden = !editing || periods.indexOf(owner) !== selectedPeriod;
            note.textContent = `Draft annual budget: Current period since ${when(budget.period_start)}: Maximum: ${budget.cap_kwh} kWh: ` + (budget.issue ? (result.bonus_history_pending ? "Reading Home Assistant history for all configured export meters…" : `Measured budget unavailable: ${optionLabel(budget.issue)}. Complete history or a confirmed starting volume is required after saving.`) : `Measured export: ${budget.used_kwh.toFixed(2)} kWh: Remaining: ${budget.remaining_kwh.toFixed(2)} kWh: Measured through: ${when(budget.measured_at)}: Future exports are not included.`);
            find("contract-preview").append(note);
        });

        previewed = revision; find("save-contract").disabled = false;
        say(saveState.dirty ? "Rates validated. Check the examples, then Save contract." : "Saved rates validated.");
        if (result.bonus_history_pending) previewTimer = setTimeout(preview, BONUS_HISTORY_REFRESH_MS);
    } catch (error) { if (revision === requestRevision) { previewed = -1; find("save-contract").disabled = true; say(`${error.message}. Changes remain unsaved.`); } }
}

/** Append one text-only label/value fact to saved contract or budget evidence. */
function contractFact(parent, label, value) {
    const item = document.createElement("div"), title = document.createElement("dt"), detail = document.createElement("dd");
    title.textContent = label; detail.textContent = value; item.append(title, detail); parent.append(item);
}

const budgetDrafts = new Map();
let budgetReadVersion = 0, budgetConfirmationPending = false;

/** Render annual allowances and preserved drafts, reporting read success for confirmation recovery. */
async function budgets(refreshed = false) {
    if (budgetConfirmationPending) return false;
    const readVersion = ++budgetReadVersion;
    const refresh = find("refresh-budget");
    refresh.disabled = true;
    find("budget-status").textContent = "Reading the latest bonus budget...";
    try {
        const result = await api("v1/contracts/budgets");
        if (readVersion !== budgetReadVersion) return false;
        find("contract-budget").hidden = false; find("bonus-budgets").replaceChildren();
        const active = result.budgets.filter(budget => budget.active);
        find("budget-status").textContent = active.length ? "Measured export volumes are independent of proposed plans." : "No active saved bonus. Save a price rule with a bonus to establish its budget automatically.";
        if (refreshed && active.length) find("budget-status").textContent = active.some(budget => budget.issue) ? "Budget refreshed. History or opening evidence still needs attention below. Refresh reads the latest result. Home Assistant history is requested automatically." : "Budget refreshed. Measured export volumes are independent of proposed plans.";
        if (!active.length) {
            const inactive = document.createElement("p"); inactive.className = "settings-tag"; inactive.textContent = "Inactive"; find("bonus-budgets").append(inactive);
        }
        for (const budget of active) {
            const panel = document.createElement("div"), facts = document.createElement("dl"); facts.className = "settings-summary";
            const period = document.createElement("p"); period.className = "budget-period";
            period.textContent = `Current annual period: ${budget.period_start ? localDate(budget.period_start) : "Unavailable"}`; panel.append(period);
            const remaining = document.createElement("p"); remaining.className = "budget-remaining";
            const amount = document.createElement("strong"), unit = document.createElement("small"); amount.textContent = money(budget.remaining_kwh); unit.textContent = " kWh remaining this annual period"; remaining.append(amount, unit); panel.append(remaining);
            if (budget.used_kwh != null && budget.cap_kwh > 0) {
                const meter = document.createElement("meter"); meter.min = 0; meter.max = budget.cap_kwh; meter.value = Math.min(budget.used_kwh, budget.cap_kwh); meter.setAttribute("aria-label", "Export counted towards annual cap"); panel.append(meter);
            }
            contractFact(facts, "Counter since", budget.period_start ? when(budget.period_start) : "Unavailable");
            contractFact(facts, "Annual cap", `${money(budget.cap_kwh)} kWh`);
            const opening = budget.opening;

            contractFact(facts, "Used export", budget.used_kwh == null ? "Unavailable" : `${money(budget.used_kwh)} kWh`);
            contractFact(facts, "Measured through", budget.measured_at ? when(budget.measured_at) : "Unavailable");
            panel.append(facts);
            const details = document.createElement("details"), summary = document.createElement("summary"); summary.textContent = "Opening evidence and grid export meters"; details.append(summary);
            const openingFacts = document.createElement("dl"); openingFacts.className = "settings-summary";
            if (opening) {
                const origins = {home_assistant_history: "Automatically derived from Home Assistant history", manual_confirmation: "Manually confirmed", unknown: "Origin unknown"};
                contractFact(openingFacts, "Export before reference", opening.used_kwh == null ? "Confirmation required" : `${money(opening.used_kwh)} kWh`);
                contractFact(openingFacts, "Opening as of", when(opening.measured_at));
                contractFact(openingFacts, "Opening origin", origins[opening.source] || origins.unknown);
                contractFact(openingFacts, "Export since opening", `${money(budget.export_since_opening_kwh)} kWh`);
            }
            details.append(openingFacts);
            if (opening) {
                const explanation = document.createElement("p");
                explanation.textContent = budget.statistics ? `Annual export uses Home Assistant long-term statistics.sum. The reference endpoint is ${when(budget.statistics.reference_at)}. Only export before that endpoint needs manual confirmation when the period boundary is missing. Physical meter resets do not reset statistics.sum.` : "Historical opening evidence is retained with its original calculation basis.";
                details.append(explanation);
                const calculation = document.createElement("p"); calculation.hidden = budget.used_kwh == null; calculation.textContent = `Total used = opening ${money(opening.used_kwh)} + measured export since opening ${money(budget.export_since_opening_kwh)} = ${money(budget.used_kwh)} kWh.`; details.append(calculation);
                if (opening.recorded_at) { const recorded = document.createElement("p"); recorded.textContent = `Origin recorded: ${when(opening.recorded_at)}`; details.append(recorded); }
                if (opening.history_digest) { const digest = document.createElement("p"); digest.textContent = `History digest: ${opening.history_digest}`; digest.className = "evidence-reference"; details.append(digest); }
            }
            if (budget.statistics) {
                for (const series of budget.statistics.series) {
                    contractFact(openingFacts, series.statistic_id, `Reference sum: ${money(series.reference_sum)} kWh: Latest sum: ${money(series.latest_sum)} kWh`);
                }
                contractFact(openingFacts, "Statistics measured through", when(budget.statistics.latest_at));
            }
            for (const meter of budget.meters || []) {
                const item = document.createElement("p"), before = opening?.meters?.find(value => value.entity_id === meter.entity_id);
                item.textContent = `${meter.entity_id}: ${money(meter.meter_kwh)} kWh at ${when(meter.measured_at)}` + (before ? `: Opening meter ${money(before.meter_kwh)} kWh` : ""); details.append(item);
            }
            const initialization = document.createElement("details"), initializationTitle = document.createElement("summary"), initializationText = document.createElement("p");
            initializationTitle.textContent = "How is the starting value set?";
            initializationText.textContent = "OEP subtracts the annual boundary sum from the latest completed Home Assistant statistics sum for every export meter. When the boundary is missing, enter only the total exported between the annual period start and the displayed reference endpoint. OEP adds the measured sum difference after that reference. Enter zero only if that missing period had no export. Each annual period gets its own reference.";
            initialization.append(initializationTitle, initializationText); panel.append(initialization, details);
            if (budget.issue) {
                const issue = document.createElement("p"); issue.setAttribute("role", "status");
                const guidance = budget.confirmation ? "Enter the export before the validated reference below. Export after that reference is already measured." : "Opening confirmation needs a validated history reference first. Check the selected grid export meters and Home Assistant statistics for missing hours or corrected readings. Other contract settings remain editable. Refresh reads the latest result and does not repair statistics.";
                issue.textContent = `Budget unavailable: ${optionLabel(budget.issue)}. ${guidance}`; panel.append(issue);
                if (budget.confirmation) {
                    const anchor = budget.confirmation;
                    const used = field(panel, "Export before reference (kWh)", "", `Enter only export from ${when(budget.period_start)} until ${when(budget.statistics.reference_at)}. Export after this reference is already measured by Home Assistant statistics.sum.`);
                    used.type = "number"; used.min = "0"; used.step = "any";
                    used.setAttribute("form", "bonus-budget-actions");
                    const draftKey = JSON.stringify({rule_id: budget.rule_id, ...anchor});
                    used.value = budgetDrafts.get(draftKey) ?? "";
                    used.oninput = () => budgetDrafts.set(draftKey, used.value);
                    const confirm = document.createElement("button"); confirm.type = "button"; confirm.textContent = "Confirm opening volume";
                    confirm.setAttribute("form", "bonus-budget-actions");
                    confirm.onclick = async () => {
                        if (budgetConfirmationPending) return;
                        if (!used.value.trim() || !used.validity.valid) {
                            find("budget-status").textContent = "Enter a nonnegative confirmed used volume.";
                            used.focus();
                            return;
                        }
                        confirm.disabled = true;
                        budgetConfirmationPending = true;
                        ++budgetReadVersion;
                        refresh.disabled = true;
                        try {
                            await api("v1/contracts/budgets", "POST", {rule_id: budget.rule_id, used_kwh: Number(used.value), contract_version: result.contract_version, ...anchor});
                            budgetDrafts.delete(draftKey);
                            panel.remove();
                            budgetConfirmationPending = false;
                            void refreshNotifications(undefined, true);
                            const followUpReadVersion = budgetReadVersion + 1;
                            if (!(await budgets()) && budgetReadVersion === followUpReadVersion) find("budget-status").textContent = `Opening volume saved. ${find("budget-status").textContent}`;
                        } catch (error) {
                            budgetConfirmationPending = false;
                            const recoveryReadVersion = budgetReadVersion + 1;
                            const reloaded = await budgets();
                            if (budgetReadVersion === recoveryReadVersion) find("budget-status").textContent = reloaded ? error.message : `${error.message}. ${find("budget-status").textContent}`;
                            confirm.disabled = false;
                        }
                    };
                    panel.append(confirm);
                }
            }
            find("bonus-budgets").append(panel);
        }
        return true;
    } catch (error) { if (readVersion === budgetReadVersion) find("budget-status").textContent = `Budget refresh failed: ${error.message}. Your unsaved edits are retained. Try Refresh budget again.`; return false; }
    finally { if (readVersion === budgetReadVersion && !budgetConfirmationPending) refresh.disabled = false; }
}

/**
 * Load confirmed contract rules in the Home Assistant installation timezone.
 * Join historical price and bonus boundaries into dated editors, then capture the
 * saved baseline and active selection. Cancel calls this to restore server values.
 * Errors are reported by the shared save state. No contract is written here.
 */
async function load() {
    loading = true; clearTimeout(previewTimer);
    try {
        const result = await api("v1/contracts");
        if (result.issue) throw new Error(`Recover stored contract evidence before editing: ${result.issue}`);
        const setup = await api("v1/setup"); timezone = setup.catalogue?.data?.timezone;
        if (!timezone) throw new Error("Connect Home Assistant before editing contract dates: its installation timezone is required.");
        useHomeAssistantTimezone(timezone);
        if (!result.contract_id) throw new Error("The app contract identity is unavailable. Reload before editing.");
        const options = setup.market_area_options || [], automatic = setup.market_area_hint;
        if (!options.length) throw new Error("No supported bidding areas are known for the Home Assistant country. Check the country in Home Assistant. No area is guessed.");
        if (result.market_area && !options.includes(result.market_area)) throw new Error(`Saved bidding area ${result.market_area} does not belong to the Home Assistant country. Check the installation location and contact your grid operator before changing the contract.`);
        contractId = result.contract_id; version = result.version; periods.length = 0;
        for (const id of ["contract-identity", "contract-periods"]) find(id).replaceChildren();
        if (automatic) {
            const area = document.createElement("input"); area.type = "hidden"; area.id = "market-area"; area.value = automatic; find("contract-identity").append(area);
            const info = document.createElement("p"); info.textContent = `Bidding area: ${automatic}: Determined automatically from Home Assistant.`; find("contract-identity").append(info);
        } else {
            const area = field(find("contract-identity"), "Bidding area", result.market_area || "", "Choose the area for this connection. Check your grid operator or energy supplier, contract, invoice or customer portal if unsure. Only areas belonging to the Home Assistant country are offered. Source coverage is checked separately.", ["", ...options]);
            area.id = "market-area"; area.options[0].textContent = "Choose bidding area"; area.required = true;
        }
        find("contract-market-area").textContent = result.market_area || automatic || "Choose area"; find("contract-timezone").textContent = timezone;
        const savedPeriods = result.periods || [], savedBonuses = result.bonuses || [];
        const starts = [...new Set([...savedPeriods.map((rule) => rule.effective_from), ...savedBonuses.filter((rule) => Date.parse(rule.effective_from) >= Date.parse(savedPeriods[0]?.effective_from)).map((rule) => rule.effective_from)])].sort((left, right) => Date.parse(left) - Date.parse(right));
        const joined = starts.map((start) => ({...savedPeriods.filter((rule) => Date.parse(rule.effective_from) <= Date.parse(start)).at(-1), contract_start: savedPeriods.filter((rule) => Date.parse(rule.effective_from) <= Date.parse(start)).at(-1)?.contract_start || (savedPeriods[0]?.effective_from ? localDate(savedPeriods[0].effective_from) : localDate()), effective_from: start}));
        for (const period of joined) {
            const bonus = savedBonuses.filter((rule) => Date.parse(rule.effective_from) <= Date.parse(period.effective_from)).sort((left, right) => Date.parse(left.effective_from) - Date.parse(right.effective_from)).at(-1);
            addPeriod(period, bonus || null);
        }
        if (!joined.length) addPeriod();
        showSavedRules({...result, periods: joined});
        find("contract-editor").hidden = false; find("contract-connect").hidden = true; find("contract-status").hidden = true;
        changed(); say(version ? "Saved contract loaded. Preview changes before saving." : "Enter your contract rates and bidding area. No supplier settings are assumed.");
        saveState.loaded(Boolean(version)); editing = !version; selectedPeriod = Math.max(0, periods.findIndex(inputs => inputs.effective_from.closest("details").dataset.savedStart === activeStarts["Price rule"])); selectPeriod(selectedPeriod); loading = false; previewTimer = setTimeout(preview, AUTO_PREVIEW_DELAY_MS); await budgets();
    } catch (error) { say(error.message); }
    finally { loading = false; }
}

find("contract-connect").onsubmit = (event) => { event.preventDefault(); token = find("contract-token").value; find("contract-token").value = ""; load(); };
find("add-period").onclick = () => {
    if (busy || saveState.busy || saveState.dirty) { say("Save or cancel your changes before adding a price rule."); return; }
    const previous = periods[selectedPeriod]; addPeriod({}, undefined, previous); editing = true; selectPeriod(periods.length - 1); syncEditor();
};
find("price-rule-select").onchange = event => {
    if (busy || saveState.dirty) { event.target.value = String(selectedPeriod); say("Save or cancel your changes before selecting another rule."); return; }
    editing = false; selectPeriod(Number(event.target.value));
};
find("edit-period").onclick = () => { editing = true; syncEditor(); };
find("delete-period").onclick = () => {
    find("contract-more").open = false;
    if (busy || saveState.busy || saveState.dirty) { say("Save or cancel your changes before deleting a rule."); return; }
    if (periods.length <= 1) { say("Keep at least one price rule. Edit it or add a replacement first."); return; }
    const selected = periods[selectedPeriod];
    if (!confirm(`Delete the price rule starting ${selected.effective_from.value}? The preceding rule will extend until the next rule. Deleting the first rule removes coverage before the next rule. Save contract applies this change. Original plans remain unchanged.`)) return;
    removeRule(periods, selected, "Price rule"); editing = true; syncEditor();
};
find("preview-contract").onclick = preview;
find("reload-contract").onclick = async () => { await saveState.refresh(load, {discard: true}); syncEditor(); };
find("refresh-budget").onclick = () => budgets(true);
find("bonus-budget-actions").onsubmit = event => event.preventDefault();
find("contract-editor").onsubmit = async (event) => {
    event.preventDefault();
    if (busy || previewed !== revision) return;
    busy = true; saveState.begin(); syncEditor();
    const savedRevision = revision;
    try {
        const result = await api("v1/contracts", "PUT", payload()); version = result.version;
        if (revision === savedRevision) { previewed = -1; showSavedRules(result); editing = false; saveState.loaded(); selectPeriod(selectedPeriod); say("Contract saved. New planning and affected cost evaluations use this revision. Original plans are preserved."); }
        else say("Previous draft saved. Your newer edits remain unsaved. Preview them before saving.");
        await budgets();
    } catch (error) { say(`${error.message}. Your edits are retained.`); }
    finally { busy = false; saveState.end(); syncEditor(); if (revision !== savedRevision) previewTimer = setTimeout(preview, AUTO_PREVIEW_DELAY_MS); }
};

try {
    const response = await fetch(base + "setup/session", {redirect: "error", cache: "no-store"});
    if (response.ok && (await response.json()).ingress) await load();
    else say("Connect with the app administration token.");
} catch (error) { say(error.message); }

/** Keep selection out of the persisted draft and show only the selected rule and its examples. */
function selectPeriod(index) {
    selectedPeriod = index;
    find("price-rule-select").replaceChildren();
    [...periods.keys()].reverse().forEach(periodIndex => {
        const inputs = periods[periodIndex], details = inputs.effective_from.closest("details");
        const option = document.createElement("option"); option.value = String(periodIndex); option.textContent = details.querySelector("summary").textContent; find("price-rule-select").append(option);
        details.hidden = periodIndex !== index; details.open = true;
    });
    find("price-rule-select").value = String(index);
    for (const block of document.querySelectorAll(".rule-preview")) block.hidden = Number(block.dataset.periodIndex) !== index;
    syncEditor();
}
/** Gate explicit editing without changing values or SaveBar's confirmed baseline. */
function syncEditor() {
    const locked = busy || saveState.busy || !saveState.ready;
    for (const inputs of periods) inputs.effective_from.closest("details").querySelector("fieldset").disabled = locked || !editing;
    find("contract-identity").querySelectorAll("input,select").forEach(input => { input.disabled = locked || !editing; });
    find("contract-editor").classList.toggle("settings-readonly", !editing);
    find("contract-periods").hidden = !editing;
    find("contract-identity").hidden = !editing || find("market-area")?.type === "hidden";
    find("contract-summary").hidden = editing; find("contract-example-links").hidden = editing;
    find("contract-terms-state").textContent = editing ? "Draft terms" : "Saved terms";
    find("preview-contract").hidden = !editing;
    for (const block of document.querySelectorAll(".draft-budget-preview")) block.hidden = !editing || Number(block.dataset.periodIndex) !== selectedPeriod;
    renderContractSummary();
    for (const id of ["save-contract", "reload-contract"]) find(id).hidden = !editing;
    for (const id of ["edit-period", "add-period", "contract-more"]) find(id).hidden = editing;
    for (const id of ["edit-period", "add-period", "price-rule-select", "delete-period"]) find(id).disabled = locked || (id === "price-rule-select" && editing);
    // Cancel also exits explicit edit mode when no values changed.
    if (editing && !locked) find("reload-contract").disabled = false;
}

find("reload-contract").textContent = "Cancel";

/** Delete a draft rule directly because read-only fieldsets cannot dispatch button clicks. */
function removeRule(collection, inputs, title) {
    collection.splice(collection.indexOf(inputs), 1); inputs.effective_from.closest("details").remove(); changed(); refreshRows(collection, title); selectPeriod(Math.min(selectedPeriod, collection.length - 1));
}

/** Maintain chronological coverage after date edits and retain the selected rule and input focus. */
function sortPeriods() {
    const selected = periods[selectedPeriod], focused = document.activeElement;
    const instant = inputs => {
        const value = dateValue(inputs.effective_from);
        if (!value) return Infinity;
        try { return Date.parse(value.includes("T") ? value : localInstant(value)); }
        catch { return Infinity; }
    };
    periods.sort((left, right) => instant(left) - instant(right));
    for (const inputs of periods) find("contract-periods").append(inputs.effective_from.closest("details"));
    refreshRows(periods, "Price rule"); selectPeriod(periods.indexOf(selected));
    if (focused?.isConnected) focused.focus({preventScroll:true});
}

/** Present saved terms as the approved key/value summary, never as disabled form controls. */
function renderContractSummary() {
    const inputs = periods[selectedPeriod], summary = find("contract-summary"); summary.replaceChildren();
    if (!inputs) return;
    const editor = bonusEditors.get(inputs), bonus = editor.inputs;
    for (const [label, value] of [["Contract starts", inputs.contract_start.value], ["Rule starts", inputs.effective_from.value || "Not set"], ["Import formula", inputs.import_formula.value], ["Export formula", inputs.export_formula.value], ["Export bonus", editor.enabled.checked ? "Included" : "Off"]]) contractFact(summary, label, value);
    if (editor.enabled.checked) {
        const condition = conditionEditors.get(bonus)();
        const comparison = comparisonChoices.find(([key]) => key === condition?.operator)?.[1];
        for (const [label, value] of [["Extra rate", bonus.rate_formula.value], ["Price condition", condition ? `${condition.expression}: ${comparison} ${condition.threshold}` : "None"], ["Paid window", bonus.window.value === "clock" ? `${bonus.start_time.value}–${bonus.end_time.value}` : optionLabel(bonus.window.value)], ["Annual cap", bonus.cap_kwh.value ? `${money(Number(bonus.cap_kwh.value))} kWh` : "Not configured"], ["Annual reset", bonus.reset_basis.selectedOptions[0].textContent]]) contractFact(summary, label, value);
    }
    const selectedEnd = periods[selectedPeriod + 1]?.effective_from.value;
    find("selected-rule-until").textContent = selectedEnd ? `Until ${selectedEnd} (exclusive)` : "No end date";

}
find("show-price-examples").onclick = () => find("price-rule-help").showModal();
find("show-bonus-examples").onclick = () => find("export-bonus-help").showModal();
