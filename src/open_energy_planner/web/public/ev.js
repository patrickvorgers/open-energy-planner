/** Complete charging requests share one app-owned lifecycle with native HA controls. */
import {when, percentage, percentageInput, inputNumber, localInstant, localDateTime, useHomeAssistantTimezone} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
import {confirmAction} from "./confirmation.js";
import {identity} from "./identity.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = id => document.getElementById(id);
const drafts = new Map();
const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
let generation = 0, token = "", current = null, busy = false;
/** Keep credentials within the existing authenticated app connection. */
async function api(path, body) {
    const response = await fetch(base + path, {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store"});
    if (!response.ok) throw new Error(await response.text());
    return response.json();
}
/** Provider names and diagnostics remain text. */
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Keep operational actions keyboard accessible. */
function button(label, action, secondary = false) {
    const control = node("button", label); control.type = "button"; if (secondary) control.className = "secondary"; control.onclick = action; return control;
}
/** Repetition is explicit and independent of vehicle defaults. */
function repeatText(days) { return days.length ? `Repeats: ${days.map(day => weekdays[day]).join(", ")}` : "Does not repeat"; }
/** Render real sessions separately from requests and predictions. */
function renderSessions() {
    find("sessions").replaceChildren();
    for (const point of current.points) {
        const card = node("article"); card.className = "chart-card";
        card.append(node("h3", point.name), node("p", point.status.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase())));
        const car = current.vehicles.find(vehicle => vehicle.id === point.vehicle_id), request = car?.charging_request;
        card.append(node("p", car ? `${car.name}: Measured SoC: ${percentage(car.actual?.soc)}${request ? `: Target: ${percentage(request.target_soc)}: Ready by: ${when(request.departure)}` : ": No planned charging request"}` : "No identified connected vehicle."));
        if (point.schedule) card.append(node("p", `Predicted departure SoC: ${percentage(point.schedule.predicted_soc)}: Energy shortfall: ${point.schedule.shortfall_kwh.toFixed(2)} kWh: This is a plan, not confirmed physical charging.`));
        if (point.execution_issue) card.append(node("p", point.execution_issue));
        find("sessions").append(card);
    }
    if (!current.points.length) find("sessions").append(node("p", "No configured charge points."));
}
/** Open one complete draft with a stable retry identity. */
function edit(vehicle) {
    const request = vehicle.charging_request, departure = request?.departure || vehicle.default_departure;
    drafts.set(vehicle.id, {id: request?.id || identity(), version: request?.version, target: request?.target_soc ?? vehicle.target_soc, departure, local: localDateTime(departure), repeat: Boolean(request?.repeat_weekdays.length), days: [...(request?.repeat_weekdays || weekdays.map((_day, index) => index))]});
    renderControls();
}
/** Finish one atomic write and retain rejected drafts. */
async function mutate(path, body, status, afterSave) {
    if (busy) return; busy = true; generation += 1;
    const controls = [...find("vehicle-controls").querySelectorAll("input, button")]; controls.forEach(control => { control.disabled = true; });
    try { await api(path, body); afterSave(); await load(); find("status").textContent = "Charging request saved. A plan does not confirm physical charging."; }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; controls.filter(control => control.isConnected).forEach(control => { control.disabled = false; }); }
}
/** Submit all overrides together without changing configuration defaults. */
function requestForm(card, vehicle, draft) {
    const form = node("form"); form.className = "ev-request-form";
    const targetLabel = node("label", "Target state of charge (%)"), target = node("input");
    target.type = "number"; target.min = 0; target.max = 100; target.step = "any"; target.required = true;
    if (typeof draft.target === "string") target.value = draft.target; else percentageInput(target, draft.target);
    target.oninput = () => { draft.target = target.value; }; targetLabel.append(target);
    const dateLabel = node("label", "Ready by"), date = node("input"); date.type = "datetime-local"; date.required = true; date.value = draft.local; date.oninput = () => { draft.local = date.value; }; dateLabel.append(date);
    const repeatLabel = node("label"), repeat = node("input"); repeat.type = "checkbox"; repeat.checked = draft.repeat; repeatLabel.append(repeat, node("span", "Repeat on selected weekdays"));
    const choices = node("fieldset"); choices.className = "ev-request-weekdays"; choices.hidden = !draft.repeat; choices.append(node("legend", "Repeat weekdays"));
    weekdays.forEach((day, index) => {
        const label = node("label"), check = node("input"); check.type = "checkbox"; check.checked = draft.days.includes(index);
        check.onchange = () => { draft.days = check.checked ? [...draft.days, index].sort() : draft.days.filter(selected => selected !== index); }; label.append(check, node("span", day)); choices.append(label);
    });
    repeat.onchange = () => { draft.repeat = repeat.checked; choices.hidden = !repeat.checked; };
    const status = node("p"); status.setAttribute("role", "status");
    const actions = node("div"); actions.className = "ev-request-actions";
    const save = node("button", draft.version ? "Save changes" : "Plan charging request"); save.type = "submit";
    actions.append(save, button("Cancel changes", () => { drafts.delete(vehicle.id); renderControls(); }, true));
    form.append(targetLabel, dateLabel, repeatLabel, choices, node("p", "These values apply to this charging request. Vehicle defaults remain unchanged."), actions, status);
    form.onsubmit = event => {
        event.preventDefault();
        try {
            if (draft.repeat && !draft.days.length) throw new Error("Choose at least one repeat weekday.");
            const body = {request_id: draft.id, vehicle_id: vehicle.id, target_soc: inputNumber(target), departure: date.value === localDateTime(draft.departure) ? draft.departure : localInstant(date.value), repeat_weekdays: draft.repeat ? draft.days : []};
            if (draft.version) body.expected_version = draft.version;
            mutate("v1/ev/requests", body, status, () => drafts.delete(vehicle.id));
        } catch (error) { status.textContent = error.message; }
    };
    card.append(form);
}
/** Cancel the exact saved identity only after explicit consent. */
async function cancel(vehicle, status) {
    if (busy) return;
    const request = vehicle.charging_request;
    if (await confirmAction({title: "Cancel charging request?", message: `${vehicle.name}: Cancel charging to ${percentage(request.target_soc)} by ${when(request.departure)}${request.repeat_weekdays.length ? " and stop its repeating schedule" : ""}? The request remains in history.`, confirmLabel: "Cancel charging request"}))
        await mutate("v1/ev/cancel", {vehicle_id: vehicle.id, request_id: request.id, expected_version: request.version}, status, () => {});
}
/** Show a coherent request card instead of per-field Apply and Reset actions. */
function renderControls() {
    find("vehicle-controls").replaceChildren();
    for (const vehicle of current.vehicles) {
        const card = node("article"); card.className = "chart-card"; card.dataset.vehicle = vehicle.id;
        card.append(node("h3", vehicle.name), node("p", `Measured SoC: ${percentage(vehicle.actual?.soc)}`));
        const request = vehicle.charging_request, draft = drafts.get(vehicle.id);
        if (draft) requestForm(card, vehicle, draft);
        else if (request) {
            const status = node("p"); status.setAttribute("role", "status"); const actions = node("div"); actions.className = "ev-request-actions";
            actions.append(button("Edit charging request", () => edit(vehicle), true), button("Cancel charging request", () => cancel(vehicle, status), true));
            card.append(node("p", `Planned charging request: Target: ${percentage(request.target_soc)}: Ready by: ${when(request.departure)}`), node("p", repeatText(request.repeat_weekdays)), actions, status);
        } else card.append(node("p", "No planned charging request."), button("Plan charging request", () => edit(vehicle)));
        find("vehicle-controls").append(card);
    }
    if (!current.vehicles.length) find("vehicle-controls").append(node("p", "Add vehicles in Appliances settings before planning a charging request."));
}
/** Page retained outcomes without adding history to the native HA projection. */
async function loadHistory(cursor = null) {
    const result = await api("v1/ev/requests" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
    if (!cursor) find("request-history").replaceChildren();
    for (const request of result.requests) find("request-history").append(node("p", `${request.vehicle_name}: ${request.status.replace(/^./, letter => letter.toUpperCase())}: Target: ${percentage(request.target_soc)}: Ready by: ${when(request.departure)}: ${repeatText(request.repeat_weekdays)}`));
    if (!cursor && !result.requests.length) find("request-history").append(node("p", "No charging requests yet."));
    find("more-history").hidden = !result.next_cursor; find("more-history").onclick = () => loadHistory(result.next_cursor).catch(error => { find("status").textContent = error.message; });
}
/** Refresh measurements without replacing complete request drafts. */
async function load() {
    const requested = ++generation, data = await api("v1/ev"); if (requested !== generation) return;
    current = data; useHomeAssistantTimezone(current.timezone); find("connect").hidden = true;
    find("status").textContent = current.issue || "Connected: Only explicit requests for identified connected vehicles can be planned.";
    renderSessions(); renderControls(); await loadHistory();
}
find("refresh-evidence").onclick = () => { if (!busy) load().catch(error => { find("status").textContent = error.message; }); };
find("connect").onsubmit = event => { event.preventDefault(); token = find("token").value; load().catch(error => { find("status").textContent = error.message; }); };
window.addEventListener("beforeunload", event => { if (drafts.size) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("oep-before-navigation", event => {
    if (!drafts.size) return;
    event.preventDefault();
    if (busy) return;
    confirmAction({title: "Discard charging request changes?", message: "The charging request changes have not been saved. Leave this page and discard them?", confirmLabel: "Discard changes"}).then(accepted => { if (accepted) { drafts.clear(); event.detail.navigate(); } });
});
fetch(base + "setup/session", {redirect: "error", cache: "no-store"}).then(response => response.json()).then(session => { useHomeAssistantTimezone(session.timezone); if (session.ingress) return load(); }).catch(error => { find("status").textContent = error.message; });
