/** Read-only activity history with installation-local dates and cursor-based live updates. */
import {when, localDate, useHomeAssistantTimezone} from "./display.js";
import {bindSettingsActions} from "./settings-actions.js";
import {connectEvents, observeEvents} from "./events.js";
import {priceSourceName} from "./price-sources.js";
bindSettingsActions();
const find = id => document.getElementById(id);
const base = document.querySelector('meta[name="oep-api-base"]').content;
let token = "", rows = new Map(), running = [], scheduled = [], health = [], next, generation = 0, loading = false;
let historyProblem = "", loadProblem = "", liveProblem = "", historyLimit = 100;
const historyWarning = (available, lost) => lost ? "Some activity updates could not be recorded. History may be incomplete. Energy planning continues independently." : available === false ? "Activity history cannot currently be saved. Energy planning continues independently." : "";
const label = value => value.charAt(0).toUpperCase() + value.slice(1).replaceAll(/[_-]/g, " ");
const moduleName = value => value.split(":").map(label).join(": ");
const clock = value => when(value, {hour:"2-digit", minute:"2-digit"});
const matches = row => localDate(new Date(row.started_at)) === find("activity-day").value && (!find("activity-type").value || row.kind === find("activity-type").value);
const newestFirst = (left, right) => right.started_at.localeCompare(left.started_at) || right.id.localeCompare(left.id);

/** Never expose raw response bodies or save credentials. */
async function api(path) {
    try {
        const response = await fetch(base + path, {headers: token ? {Authorization:`Bearer ${token}`} : {}, cache:"no-store", redirect:"error", signal:AbortSignal.timeout(10000)});
        if ([401,403].includes(response.status)) find("connect").hidden = false;
        if (!response.ok) throw new Error();
        return await response.json();
    } catch {
        throw new Error("Activity history could not be loaded. Check the connection and try Refresh.");
    }
}

/** Use inert text and native disclosure so every outcome remains keyboard accessible. */
function activityRow(row) {
    const item = document.createElement("details"), summary = document.createElement("summary"), time = document.createElement("time"), title = document.createElement("strong"), type = document.createElement("span"), state = document.createElement("span");
    item.className = "activity-row";
    item.dataset.activityId = row.id;
    time.dateTime = row.started_at;
    time.textContent = clock(row.started_at);
    title.textContent = row.title;
    type.className = "activity-type"; type.textContent = label(row.kind);
    state.className = "activity-state"; state.dataset.state = row.state;
    state.textContent = row.recovered ? "Recovered" : label(row.state);
    summary.append(time, title, type, state); item.append(summary);
    for (const text of [row.detail, `Module: ${moduleName(row.owner)}`, row.finished_at ? `Finished: ${when(row.finished_at)}` : "In progress"]) {
        const paragraph = document.createElement("p"); paragraph.textContent = text; item.append(paragraph);
    }
    return item;
}

/** Keep each source's own state and safe explanation available inside its group. */
function moduleStatus(row, name) {
    const item = document.createElement("details"), title = document.createElement("summary"), detail = document.createElement("p");
    item.className = "activity-row"; title.textContent = `${name}: ${label(row.state)}`; detail.textContent = row.detail; item.append(title, detail); return item;
}

/** Keep existing weather owner identifiers stable while showing their shared responsibility. */
function modulePath(owner) {
    if (owner === "solar-weather") return ["weather", "solar"];
    if (owner === "temperature-weather") return ["weather", "temperature"];
    return owner.split(":");
}

/** Nest producer namespaces without inventing an aggregate health state. */
function moduleBranch(node, path) {
    const part = path.at(-1), name = label(path.length === 2 && path[0] === "prices" ? priceSourceName(part) : part);
    if (!node.children.size) return moduleStatus(node.row, name);
    const group = document.createElement("section"), heading = document.createElement(`h${Math.min(path.length + 2, 6)}`), children = document.createElement("ul");
    group.className = "activity-health-group"; group.setAttribute("aria-label", name); heading.textContent = name; children.className = "activity-list";
    if (node.row) {
        const overview = document.createElement("li"); overview.append(moduleStatus(node.row, "Overview")); children.append(overview);
    }
    for (const [key, child] of [...node.children].sort(([left], [right]) => left.localeCompare(right))) {
        const item = document.createElement("li"); item.append(moduleBranch(child, [...path, key])); children.append(item);
    }
    group.append(heading, children); return group;
}

/** Show current work only on its actual local start day. */
function render() {
    find("activity-status").textContent = loadProblem || liveProblem || historyProblem || (loading ? "Loading activity history." : "");
    const kind = find("activity-type").value;
    if (!loading) {
        const history = [...rows.values()].filter(matches).sort(newestFirst);
        if (history.length > historyLimit) {
            const retained = history.slice(0, historyLimit), oldest = retained.at(-1);
            rows = new Map(retained.map(row => [row.id, row]));
            next = `${oldest.started_at}|${oldest.id}`;
        }
    }
    find("activity-refresh").disabled = loading;
    find("activity-more").disabled = loading;
    const opened = new Set([...find("activity-list").querySelectorAll("details[open]")].map(item => item.dataset.activityId));
    const focused = document.activeElement?.tagName === "SUMMARY" ? document.activeElement.parentElement.dataset.activityId : undefined;
    const merged = new Map([...rows, ...running.map(row => [row.id, row])]);
    const visible = [...merged.values()].filter(matches).sort(newestFirst);
    let restoreFocus;
    find("activity-list").replaceChildren(...visible.map(row => {
        const entry = document.createElement("li"), details = activityRow(row);
        details.open = opened.has(row.id);
        if (focused === row.id) restoreFocus = details.querySelector("summary");
        entry.append(details); return entry;
    }));
    restoreFocus?.focus({preventScroll:true});
    if (!visible.length) {
        const empty = document.createElement("li"); empty.className = "activity-empty";
        empty.textContent = loading ? "Loading activities." : "No activities recorded for this selection.";
        find("activity-list").append(empty);
    }
    find("activity-more").hidden = !next;
    const upcoming = scheduled.filter(row => Date.parse(row.due_at) >= Date.now() && Date.parse(row.due_at) <= Date.now() + 6*60*60*1000 && (!kind || row.kind === kind)).sort((left,right) => left.due_at.localeCompare(right.due_at));
    find("activity-upcoming").replaceChildren(...upcoming.map(row => {
        const item = document.createElement("p"); item.className = "activity-row"; item.textContent = `${clock(row.due_at)}: ${row.title}`; return item;
    }));
    if (!upcoming.length) find("activity-upcoming").textContent = "No known scheduled work in this window.";
    const modules = new Map();
    for (const row of health) {
        let children = modules, node;
        for (const part of modulePath(row.owner)) {
            if (!children.has(part)) children.set(part, {children: new Map()});
            node = children.get(part); children = node.children;
        }
        node.row = row;
    }
    find("activity-health").replaceChildren(...[...modules].sort(([left], [right]) => left.localeCompare(right)).map(([key, node]) => moduleBranch(node, [key])));
}

/** A late page response must not replace newer live outcomes for the same execution. */
async function load(append = false) {
    const version = ++generation;
    loading = true;
    loadProblem = "";
    if (!append) {rows.clear(); next = undefined; historyLimit = 100;}
    render();
    find("activity-status").textContent = "Loading activity history.";
    try {
        const day = find("activity-day").value, before = append && next ? `&before=${encodeURIComponent(next)}` : "";
        const kind = find("activity-type").value;
        const result = await api(`v1/activities?day=${encodeURIComponent(day)}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}${before}`);
        if (version !== generation) return;
        useHomeAssistantTimezone(result.timezone);
        for (const row of result.activities) if (!rows.has(row.id)) rows.set(row.id, row);
        next = result.next;
        if (append) historyLimit += 100;
        loadProblem = "";
    } catch (error) {if (version === generation) loadProblem = error.message;}
    finally {if (version === generation) {loading = false; render();}}
}

observeEvents(message => {
    if (message.unavailable) {liveProblem = "Live updates unavailable. Displayed information may be out of date."; if (message.unauthorized) find("connect").hidden = false; render(); return;}
    liveProblem = "";
    if (message.reset) {
        running = message.snapshot.running; scheduled = message.snapshot.scheduled; health = message.snapshot.health;
        historyProblem = historyWarning(message.snapshot.history_available, message.snapshot.lost_updates);
        load();
    }
    for (const event of message.events || []) {
        const row = event.value;
        if (event.topic === "activity") {running = running.filter(item => item.id !== row.id); if (row.state === "running") running.push(row); else if (matches(row)) rows.set(row.id,row);}
        if (event.topic === "schedule") scheduled = [...scheduled.filter(item => item.id !== row.id), ...(row.activity ? [row.activity] : [])];
        if (event.topic === "health") health = [...health.filter(item => item.owner !== row.owner), ...(row.removed ? [] : [row])];
        if (event.topic === "history") historyProblem = historyWarning(row.available, row.lost_updates);
    }
    render();
});
find("activity-type").addEventListener("change", () => load());
find("activity-day").addEventListener("change", () => load());
find("activity-today").addEventListener("click", () => {find("activity-day").value = localDate(); load();});
find("activity-refresh").addEventListener("click", () => {if (!loading) load();});
find("activity-more").addEventListener("click", () => {if (!loading) load(true);});
find("connect").addEventListener("submit", event => {event.preventDefault(); token = find("token").value; find("token").value = ""; find("connect").hidden = true; connectEvents(base, token); load();});
api("setup/session").then(session => {useHomeAssistantTimezone(session.timezone); find("activity-day").value = localDate(); find("connect").hidden = Boolean(session.ingress); if (session.ingress) {connectEvents(base); load();} else {loadProblem = "Connect to read activity history."; render();}}).catch(error => {loadProblem = error.message; render();});
setInterval(render, 60000);
