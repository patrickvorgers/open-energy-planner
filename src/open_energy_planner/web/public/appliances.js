import {applianceApi, readApplianceEvidence, followedDevices} from "./appliance-data.js";
import {confirmAction} from "./confirmation.js";
import {when, localInstant, optionLabel, useHomeAssistantTimezone} from "./display.js";
import {profileMetrics} from "./appliance-profile.js";
import {bindSettingsActions} from "./settings-actions.js";
import {identity} from "./identity.js";
import {pollApplianceEvidence} from "./appliance-evidence.js";
bindSettingsActions();
/** Task UI uses the existing authenticated app boundary. Physical execution remains in HA. */
const find = (id) => document.getElementById(id);
let token = "", current, consumers;
let evidenceRefreshing = false;
const api = (path, body, method) => applianceApi(token, path, body, method);
const readEvidence = () => readApplianceEvidence(token);
function node(tag, text) { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; }
/** Refresh names and evidence without discarding planning drafts. Retain previous evidence on failure. */
async function refreshEvidence() {
    if (!current || evidenceRefreshing || document.hidden) return;
    evidenceRefreshing = true;
    const previous = current;
    try {
        const [data, sources, session] = await readEvidence();
        if (current !== previous) return;
        useHomeAssistantTimezone(session.timezone);
        consumers = sources;
        current = {...data, devices: followedDevices(data, consumers)};
        for (const card of document.querySelectorAll("[data-appliance-entity]")) {
            const source = consumers.options.find(row => row.entity_id === card.dataset.applianceEntity);
            if (source) card.querySelector("h2").textContent = source.name;
        }
        if (find("status").textContent === "Learning evidence refresh unavailable. Retained evidence is shown.") find("status").textContent = data.issue || "Connected: A schedule never proves physical execution.";
    } catch { find("status").textContent = "Learning evidence refresh unavailable. Retained evidence is shown."; }
    finally { evidenceRefreshing = false; }
}
pollApplianceEvidence(refreshEvidence);
/**
 * Show selected cycle appliances, excluding persisted charging and thermal meters.
 * Read failures leave existing evidence visible and report status.
 */
async function load() {
    try {
        const [data, sources, session] = await readEvidence();
        useHomeAssistantTimezone(session.timezone);
        consumers = sources;
        current = {...data, devices: followedDevices(data, consumers)};
        find("connect").hidden = true; find("status").textContent = data.issue || "Connected: A schedule never proves physical execution."; find("appliance-list").replaceChildren();
        const permitted = current.devices.filter(device => device.shiftable);
        find("appliance-empty").hidden = Boolean(permitted.length);
        for (const option of consumers.options.filter((item) => permitted.some(device => device.entity_id === item.entity_id))) {
            const device = current.devices.find((item) => item.entity_id === option.entity_id), task = device?.task;
            const card = node("section"); card.className = "chart-card"; card.dataset.applianceEntity = option.entity_id; card.append(node("h2", option.name));
            card.append(node("p", device?.profile ? `Average cycle: ${(profileMetrics(device.profile).energyKwh).toFixed(2)} kWh, ${profileMetrics(device.profile).minutes} minutes.` : "Waiting for a completed measured cycle."));
            const insights = node("a", "View cycle insights");
            const destination = new URL("appliance-cycles", new URL(document.querySelector('meta[name="oep-api-base"]').content, location.href));
            destination.hash = new URLSearchParams({entity: device.entity_id}).toString();
            if (new URL(location.href).searchParams.get("embedded") === "1") destination.searchParams.set("embedded", "1");
            insights.href = destination.href; card.append(insights);
            card.append(node("p", `Task: ${task ? task.status.replaceAll("_", " ") : "None"}.`));
            if (task) {
                card.append(node("p", `Finish before: ${when(task.deadline)}: Planned start: ${task.scheduled_start ? when(task.scheduled_start) : "Not scheduled"}.`));
                if (task.attempt) card.append(node("p", `Execution: ${optionLabel(task.attempt.status)}: Start request expires: ${when(task.attempt.expires_at)}.`));
            }
            const form = node("form"), deadline = node("input"), deadlineLabel = node("label", "Finish before (Home Assistant time)"); deadline.type = "datetime-local"; deadline.required = true; deadlineLabel.append(deadline); form.append(deadlineLabel);
            const submit = node("button", "Appliance is ready"); submit.disabled = !device?.shiftable || !device?.profile || Boolean(task && !["completed", "cancelled"].includes(task.status)); form.append(submit); card.append(form);
            form.onsubmit = async (event) => { event.preventDefault(); submit.disabled = true; try { await api("v1/appliances/tasks", {entity_id: option.entity_id, request_id: identity(), ready_at: new Date().toISOString(), deadline: localInstant(deadline.value)}); await load(); } catch (error) { find("status").textContent = error.message; submit.disabled = false; } };
            if (task && !["completed", "cancelled"].includes(task.status)) {
                const cancel = node("button", "Cancel planning"); cancel.type = "button"; cancel.className = "secondary";
                cancel.onclick = async () => {
                    if (cancel.disabled || !await confirmAction({title: "Cancel appliance planning?", message: `${option.name || option.entity_id} will no longer be scheduled for this request.\n\nThis does not stop the physical appliance or report that its cycle has completed. Home Assistant and the appliance remain responsible for physical control.`, confirmLabel: "Cancel planning"})) return;
                    if (!cancel.isConnected) {
                        find("status").textContent = `${option.name || option.entity_id}: Information was refreshed while confirmation was open. Review the current task, then choose Cancel planning again if needed.`;
                        return;
                    }
                    if (cancel.disabled) return;
                    cancel.disabled = true;
                    try { await api("v1/appliances/cancel", {entity_id: option.entity_id, task_id: task.id}); await load(); }
                    catch { find("status").textContent = `${option.name || option.entity_id}: Cancellation could not be confirmed. Choose Refresh task evidence to check the current task before trying again. Physical control remains with Home Assistant.`; }
                    finally { if (cancel.isConnected) cancel.disabled = false; }
                };
                card.append(cancel);
            }
            if (task?.events?.length) { const details = node("details"), list = node("ol"); details.append(node("summary", "Execution evidence")); for (const event of task.events) list.append(node("li", `${when(event.at)}: ${optionLabel(event.event)}: Attempt: ${event.attempt_id}`)); details.append(list); card.append(details); }
            find("appliance-list").append(card);
        }
    } catch { find("status").textContent = "Appliance information could not be loaded. Check the connection and try Refresh."; }
}
find("refresh").onclick = load;
find("connect").onsubmit = (event) => { event.preventDefault(); token = find("token").value; load(); };
find("connect").querySelector("button").disabled = false;
api("setup/session").then(session => { if (session.ingress) load(); }).catch(() => { find("status").textContent = "Session information could not be loaded. Refresh this page and try again."; });
