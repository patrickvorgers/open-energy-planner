/** Complete charging requests share one app-owned lifecycle with native HA controls. */
import {when, percentage, percentageInput, inputNumber, localInstant, localDateTime, useHomeAssistantTimezone} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
import {confirmAction} from "./confirmation.js";
import {identity} from "./identity.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = id => document.getElementById(id);
const drafts = new Map();
const historyPages = new Set();
const activeReads = new Set();
const EV_READ_TIMEOUT_MILLISECONDS = 30000;
const READ_FAILURE = "Charging evidence could not be read. Check the connection and try Refresh.";
const WRITE_UNCERTAIN = "Charging request outcome could not be confirmed. Your edits are retained. Refresh charging evidence before retrying.";
const validationMessages = new Map([
    "Charging request belongs to a different vehicle", "Invalid charging request fields",
    "Charging request ID was already used", "Charging departure must be in the next thirty days",
    "Charging departure must be after this request's readiness",
    "Choose distinct repeat weekdays between zero and six", "Only a planned charging request can be edited",
    "Charging request changed. Refresh before saving", "Charging request no longer exists",
    "This vehicle already has a planned charging request", "Charging request changed. Refresh before cancelling",
    "Invalid EV physical quantity", "Unknown vehicle",
].map(message => [message, message]));
const preparationMessages = new Map([
    "Choose at least one repeat weekday.", "Choose a valid date and time.",
    "This time does not exist in the Home Assistant installation timezone.",
].map(message => [message, message]));
/** Only explicit app-owned messages may enter visible request diagnostics. */
class EVRequestError extends Error {
    constructor(publicMessage) { super(); this.publicMessage = publicMessage; }
}
/** Keep arbitrary transport and preparation exception values outside the page. */
function requestMessage(error) { return error instanceof EVRequestError ? error.publicMessage : READ_FAILURE; }
/** Cancel superseded read bodies without cancelling a potentially durable write. */
function abortReads() { for (const controller of activeReads) controller.abort(); }
const weekdays = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
let generation = 0, token = "", current = null, busy = false, loading = null;
/** Bound reads through body consumption. Unconfirmed writes retain their exact retry identity. */
async function api(path, body) {
    const controller = body ? null : new AbortController();
    if (controller) activeReads.add(controller);
    const deadline = controller ? setTimeout(() => controller.abort(), EV_READ_TIMEOUT_MILLISECONDS) : null;
    try {
        const response = await fetch(base + path, {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store", signal: controller?.signal});
        if (response.status === 401) throw new EVRequestError("The installation token was rejected.");
        if (!response.ok) {
            if (body && [400, 409].includes(response.status)) {
                const canonical = validationMessages.get(await response.text());
                if (canonical) throw new EVRequestError(canonical);
            } else await response.body?.cancel();
            throw new EVRequestError(body ? WRITE_UNCERTAIN : READ_FAILURE);
        }
        const data = await response.json(); controller?.signal.throwIfAborted(); return data;
    } catch (error) {
        if (error instanceof EVRequestError) throw error;
        throw new EVRequestError(body ? WRITE_UNCERTAIN : READ_FAILURE);
    } finally { if (controller) { clearTimeout(deadline); activeReads.delete(controller); } }
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
/** Keep durable acceptance separate from optional follow-up reads. */
async function mutate(path, body, status, afterSave) {
    if (busy) return; busy = true; generation += 1; abortReads(); find("refresh-evidence").disabled = true;
    find("vehicle-controls").querySelectorAll("input, button").forEach(control => { control.disabled = true; });
    let accepted = false;
    const confirmation = path.endsWith("/cancel") ? "Charging request cancelled." : "Charging request saved. A plan does not confirm physical charging.";
    try {
        const result = await api(path, body); accepted = true; afterSave();
        current = result; useHomeAssistantTimezone(current.timezone); renderSessions(); renderControls();
        find("status").textContent = confirmation;
        await load(true); find("status").textContent = confirmation;
    } catch (error) {
        if (accepted) find("status").textContent = `${confirmation} Refresh charging evidence to reload the latest status and history.`;
        else status.textContent = requestMessage(error);
    } finally {
        busy = false; find("refresh-evidence").disabled = Boolean(loading); find("vehicle-controls").querySelectorAll("input, button").forEach(control => { control.disabled = false; });
    }
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
        } catch (error) { status.textContent = preparationMessages.get(error?.message) || "Charging request could not be prepared. Check target, departure and repeat weekdays."; }
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
    if (busy) find("vehicle-controls").querySelectorAll("input, button").forEach(control => { control.disabled = true; });
}
/** Bind history to its state generation and request each cursor only once. */
async function loadHistory(cursor = null, requested = generation) {
    const key = cursor || "";
    if (requested !== generation || historyPages.has(key)) return;
    historyPages.add(key); find("more-history").disabled = true;
    try {
        const result = await api("v1/ev/requests" + (cursor ? "?cursor=" + encodeURIComponent(cursor) : ""));
        if (requested !== generation) return;
        if (!cursor) find("request-history").replaceChildren();
        for (const request of result.requests) find("request-history").append(node("p", `${request.vehicle_name}: ${request.status.replace(/^./, letter => letter.toUpperCase())}: Target: ${percentage(request.target_soc)}: Ready by: ${when(request.departure)}: ${repeatText(request.repeat_weekdays)}`));
        if (!cursor && !result.requests.length) find("request-history").append(node("p", "No charging requests yet."));
        find("more-history").hidden = !result.next_cursor;
        find("more-history").onclick = () => loadHistory(result.next_cursor, requested).catch(error => { if (requested === generation) find("status").textContent = requestMessage(error); });
    } catch (error) {
        if (requested !== generation) return;
        historyPages.delete(key); throw error;
    } finally { if (requested === generation) find("more-history").disabled = false; }
}
/** Share manual refreshes and settle cancelled predecessors before reading a new evidence cohort. */
function load(replace = false) {
    if (loading && !replace) return loading.promise;
    const previous = loading, requested = ++generation, owner = {};
    abortReads(); historyPages.clear(); loading = owner; find("refresh-evidence").disabled = true;
    owner.promise = (async () => {
        if (previous) await previous.promise.catch(() => {});
        if (requested !== generation) return;
        const data = await api("v1/ev"); if (requested !== generation) return;
        current = data; useHomeAssistantTimezone(current.timezone); find("connect").hidden = true;
        find("status").textContent = current.issue || "Connected: Only explicit requests for identified connected vehicles can be planned.";
        renderSessions(); renderControls(); await loadHistory(null, requested);
    })().catch(error => { if (requested === generation) throw error; }).finally(() => {
        if (loading === owner) { loading = null; find("refresh-evidence").disabled = busy; }
    });
    return owner.promise;
}
find("refresh-evidence").onclick = () => { if (!busy) load().catch(error => { find("status").textContent = requestMessage(error); }); };
find("connect").onsubmit = event => { event.preventDefault(); token = find("token").value; load(true).catch(error => { find("status").textContent = requestMessage(error); }); };
window.addEventListener("beforeunload", event => { if (drafts.size) { event.preventDefault(); event.returnValue = ""; } });
document.addEventListener("oep-before-navigation", event => {
    if (!drafts.size) return;
    event.preventDefault();
    if (busy) return;
    confirmAction({title: "Discard charging request changes?", message: "The charging request changes have not been saved. Leave this page and discard them?", confirmLabel: "Discard changes"}).then(accepted => { if (accepted) { drafts.clear(); event.detail.navigate(); } });
});
api("setup/session").then(session => { useHomeAssistantTimezone(session.timezone); if (session.ingress) return load(); }).catch(error => { if (!current) find("status").textContent = requestMessage(error); });
