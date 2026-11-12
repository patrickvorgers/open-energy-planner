/** EV sessions and daily requests use existing intent endpoints. Physical control remains in HA. */
import {when, percentage, percentageInput, inputNumber, localInstant, localDateTime, useHomeAssistantTimezone} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = id => document.getElementById(id);
let requestGeneration = 0;
let token = "", current = null, busy = false;
/** Keep authorization and redirect protection identical to the configuration editor. */
async function api(path, body) {
    const response = await fetch(base + path, {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store"});
    if (!response.ok) throw new Error(await response.text());
    return response.json();
}
/** Provider names and diagnostics remain text. */
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Render measured sessions separately from expected charging and departure results. */
function renderSessions() {
    find("sessions").replaceChildren();
    for (const point of current.points) {
        const card = node("article"); card.className = "chart-card";
        card.append(node("h3", point.name), node("p", point.status.replaceAll("_", " ").replace(/^./, letter => letter.toUpperCase())));
        const car = (current.vehicles || []).find(vehicle => vehicle.id === point.vehicle_id);
        card.append(node("p", car ? `${car.name}: Measured SoC: ${percentage(car.actual?.soc)}: Target: ${percentage(car.target_soc)}: Departure: ${when(car.next_departure)}` : "No identified connected vehicle."));
        if (point.schedule) card.append(node("p", `Predicted departure SoC: ${percentage(point.schedule.predicted_soc)}: Energy shortfall: ${point.schedule.shortfall_kwh.toFixed(2)} kWh: This is a plan, not confirmed physical charging.`));
        if (point.execution_issue) card.append(node("p", point.execution_issue));
        find("sessions").append(card);
    }
    if (!current.points.length) find("sessions").append(node("p", "No configured charge points."));
}
/** Persist one explicit user request without replacing unrelated vehicle or point configuration. */
async function intent(vehicleId, field, value, form) {
    if (busy) return;
    busy = true;
    requestGeneration += 1;
    const controls = [...find("vehicle-controls").querySelectorAll("input, button")];
    controls.forEach(control => { control.disabled = true; });
    try {
        await api("v1/ev/intent", {vehicle_id: vehicleId, field, value});
        form.dataset.dirty = "";
        if (field === "departure_override" && value === null) form.querySelector("input").value = "";
        await load(); find("status").textContent = "Request saved. A plan does not confirm physical charging.";
    } catch (error) { form.querySelector('[role="status"]').textContent = error.message; }
    finally { busy = false; controls.filter(control => control.isConnected).forEach(control => { control.disabled = false; }); }
}
/** Use one explicit action for each daily preference. Drafts survive evidence refresh and rejected writes. */
function requestForm(parent, vehicle, field, label, type, value, convert = input => input.value) {
    const form = node("form"), wrapper = node("label", label), input = node("input"), status = node("p");
    input.type = type; input.required = true;
    if (field === "target_soc") { input.min = 0; input.max = 100; input.step = "any"; percentageInput(input, value); }
    else input.value = value || "";
    input.oninput = () => { form.dataset.dirty = "true"; };
    wrapper.append(input); status.setAttribute("role", "status");
    const apply = node("button", `Apply ${label.toLowerCase()}`), reset = node("button", "Reset request");
    reset.type = "button"; reset.onclick = () => {
        const saved = current.vehicles.find(car => car.id === vehicle.id)?.[field];
        if (field === "target_soc") percentageInput(input, saved);
        else input.value = field === "departure_override" ? localDateTime(saved) : saved || "";
        form.dataset.dirty = ""; status.textContent = "";
    };
    form.append(wrapper, apply, reset, status);
    form.onsubmit = event => { event.preventDefault(); try { intent(vehicle.id, field, convert(input), form); } catch (error) { status.textContent = error.message; } };
    parent.append(form);
    return form;
}
/** Reuse native vehicle intents instead of a second settings form under Energy. */
function renderControls() {
    if (find("vehicle-controls").querySelector('[data-dirty="true"]')) return;
    find("vehicle-controls").replaceChildren();
    for (const vehicle of current.vehicles || []) {
        const card = node("article"); card.className = "chart-card"; card.append(node("h3", vehicle.name));
        card.append(node("p", `Measured SoC: ${percentage(vehicle.actual?.soc)}: Next departure: ${when(vehicle.next_departure)}`));
        requestForm(card, vehicle, "target_soc", "Target state of charge (%)", "number", vehicle.target_soc, inputNumber);
        requestForm(card, vehicle, "departure_time", "Daily departure time", "time", vehicle.departure_time);
        const override = requestForm(card, vehicle, "departure_override", "One-off departure", "datetime-local", localDateTime(vehicle.departure_override), input => {
            const saved = current.vehicles.find(car => car.id === vehicle.id)?.departure_override;
            return input.value === localDateTime(saved) ? saved : localInstant(input.value);
        });
        const daily = node("button", "Use daily departure"); daily.type = "button";
        daily.onclick = () => intent(vehicle.id, "departure_override", null, override);
        card.append(daily); find("vehicle-controls").append(card);
    }
    if (!(current.vehicles || []).length) find("vehicle-controls").append(node("p", "Add vehicles in Appliances settings before submitting charging requests."));
}
/** Refresh actuals without overwriting pending daily requests. */
async function load() {
    const generation = ++requestGeneration;
    const data = await api("v1/ev");
    if (generation !== requestGeneration) return;
    current = data; useHomeAssistantTimezone(current.timezone);
    find("connect").hidden = true; find("status").textContent = current.issue || "Connected: Only current identified sessions can be planned.";
    renderSessions(); renderControls();
}
find("refresh-evidence").onclick = () => { if (!busy) load().catch(error => { find("status").textContent = error.message; }); };
find("connect").onsubmit = event => { event.preventDefault(); token = find("token").value; load().catch(error => { find("status").textContent = error.message; }); };
fetch(base + "setup/session", {redirect: "error", cache: "no-store"}).then(response => response.json()).then(session => { useHomeAssistantTimezone(session.timezone); if (session.ingress) return load(); }).catch(error => { find("status").textContent = error.message; });
