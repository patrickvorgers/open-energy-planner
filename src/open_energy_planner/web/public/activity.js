import {createPeriodSelector} from "./period-selector.js";
/** Read-only activity history with installation-local dates and cursor-based live updates. */
import {when, localDate, useHomeAssistantTimezone, planName, planStrategy} from "./display.js";
import {bindSettingsActions, sessionResponse} from "./settings-actions.js";
import {connectEvents, observeEvents} from "./events.js";
import {priceSourceName} from "./price-sources.js";
bindSettingsActions();
const find = id => document.getElementById(id);
const activityDay = createPeriodSelector(find("activity-period"), {dayOnly: true, label: "Activity day", onChange: resetHistory});
const base = document.querySelector('meta[name="oep-api-base"]').content;
let token = "", rows = new Map(), running = [], scheduled = [], health = [], next, generation = 0, loading = false;
const pageSize = 25;
const historyFailure = "Activity history could not be loaded. Check the connection and try Refresh.";
let historyProblem = "", loadProblem = "", liveProblem = "", pageIndex = 0, requestedPage = 0, pageCursors = [undefined], pendingUpdates = new Map(), resetPending = false;
const historyWarning = (available, lost) => lost ? "Some activity updates could not be recorded. History may be incomplete. Energy planning continues independently." : available === false ? "Activity history cannot currently be saved. Energy planning continues independently." : "";
const label = value => value.charAt(0).toUpperCase() + value.slice(1).replaceAll(/[_-]/g, " ");
const moduleName = value => value.split(":").map(label).join(": ");
const clock = value => when(value, {hour:"2-digit", minute:"2-digit"});
const matches = row => localDate(new Date(row.started_at)) === activityDay.value.start && (!find("activity-type").value || row.kind === find("activity-type").value);
const newestFirst = (left, right) => right.started_at.localeCompare(left.started_at) || right.id.localeCompare(left.id);

/** Never expose raw response bodies or save credentials. */
async function api(path) {
    try {
        const response = path === "setup/session" ? await sessionResponse(base) : await fetch(base + path, {headers: token ? {Authorization:`Bearer ${token}`} : {}, cache:"no-store", redirect:"error", signal:AbortSignal.timeout(10000)});
        if ([401,403].includes(response.status)) find("connect").hidden = false;
        if (!response.ok) {
            await response.body?.cancel();
            throw new Error();
        }
        return await response.json();
    } catch {
        throw new Error(historyFailure);
    }
}

/** Use only known provider display names as a fallback when the activity has no subject. */
function activitySubject(context, module, source) {
    if (context.plan_generated_at) return `Plan ${planName({generated_at:context.plan_generated_at, strategy:context.plan_strategy})}`;
    if (context.label) return context.label;
    if (module !== "prices" || !source) return "";
    const provider = priceSourceName(source);
    return provider !== source ? provider : "";
}

/** Show plan strategy and the work period without duplicating the plan creation time. */
function activityTitle(row) {
    const [module, source] = row.owner.split(":");
    const context = row.context || {};
    const period = context.start && context.end ? `${when(context.start)} to ${when(context.end)}` : "";
    if (module === "planning" && context.plan_generated_at && period) {
        return `${row.title}: Plan ${period}: ${planStrategy(context.plan_strategy)}`;
    }
    const parts = [activitySubject(context, module, source), period].filter(Boolean);
    return parts.length ? `${row.title}: ${parts.join(", ")}` : row.title;
}

/** Offer owned read-only destinations, never a URL supplied by an event or provider. */
function activityLink(row) {
    const owner = row.owner.split(":")[0];
    const destinations = {
        prices:["overview#planning", "View planning and prices"],
        planning:["overview#planning", "View planning"],
        settlement:["overview#savings", "View savings"],
        "contract-valuations":["contract", "View contract"],
        appliances:["appliance-cycles", "View appliance profiles"],
        demand:["quality", "View forecast quality"],
        solar:["quality", "View forecast quality"],
        models:["quality", "View forecast quality"],
        "forecast-quality":["quality", "View forecast quality"],
        "solar-weather":["quality", "View forecast quality"],
        "temperature-weather":["quality", "View forecast quality"],
    };
    const destination = row.context?.plan_id ? ["overview#planning", "View this plan"] : Object.hasOwn(destinations, owner) ? destinations[owner] : null;
    if (!destination) return null;
    const link = document.createElement("a"), target = new URL(base + destination[0], location.href);
    if (row.context?.plan_id) target.searchParams.set("plan", row.context.plan_id);
    if (new URL(location.href).searchParams.get("embedded") === "1") target.searchParams.set("embedded", "1");
    link.href = target.href; link.textContent = destination[1];
    return link;
}

/** Use inert text and native disclosure so every outcome remains keyboard accessible. */
function activityRow(row) {
    const item = document.createElement("details"), summary = document.createElement("summary"), time = document.createElement("time"), title = document.createElement("strong"), type = document.createElement("span"), state = document.createElement("span");
    item.className = "activity-row";
    item.dataset.activityId = row.id;
    time.dateTime = row.started_at;
    time.textContent = clock(row.started_at);
    title.textContent = activityTitle(row);
    title.title = title.textContent;
    type.className = "activity-type"; type.textContent = label(row.kind);
    state.className = "activity-state"; state.dataset.state = row.state;
    state.textContent = row.recovered ? "Recovered" : label(row.state);
    summary.append(time, title, type, state); item.append(summary);
    for (const text of [title.textContent, row.detail, `Module: ${moduleName(row.owner)}`, row.finished_at ? `Finished: ${when(row.finished_at)}` : "In progress"]) {
        const paragraph = document.createElement("p"); paragraph.textContent = text; item.append(paragraph);
    }
    if (row.context?.waiting_start && row.context.waiting_end) {
        const paragraph = document.createElement("p"); paragraph.textContent = `Processing stopped at: ${when(row.context.waiting_start)} to ${when(row.context.waiting_end)}`; item.append(paragraph);
    }
    const link = activityLink(row);
    if (link) { const paragraph = document.createElement("p"); paragraph.append(link); item.append(paragraph); }
    return item;
}

/** Keep each source's own state and safe explanation available inside its group. */
function moduleStatus(row, name) {
    const item = document.createElement("details"), title = document.createElement("summary"), detail = document.createElement("p");
    item.className = "activity-row"; item.dataset.owner = row.owner; title.textContent = `${name}: ${label(row.state)}`; detail.textContent = row.detail; item.append(title, detail); return item;
}

/** Keep existing weather owner identifiers stable while showing their shared responsibility. */
function modulePath(owner) {
    if (owner === "solar-weather") return ["weather", "solar"];
    if (owner === "temperature-weather") return ["weather", "temperature"];
    const parts = owner.split(":");
    return parts[0] === "prices" ? parts.slice(0, 2) : parts;
}

/** Nest producer namespaces without inventing an aggregate health state. */
function moduleBranch(node, path, expanded) {
    const part = path.at(-1), name = label(path.length === 2 && path[0] === "prices" ? priceSourceName(part) : part);
    if (!node.children.size) return moduleStatus(node.row, name);
    const group = document.createElement("section"), heading = document.createElement(`h${Math.min(path.length + 2, 6)}`), children = document.createElement("ul");
    group.className = "activity-health-group"; group.setAttribute("aria-label", name); heading.textContent = name; children.className = "activity-list";
    if (node.row) {
        const overview = document.createElement("li"); overview.append(moduleStatus(node.row, "Overview")); children.append(overview);
    }
    for (const [key, child] of [...node.children].sort(([left], [right]) => left.localeCompare(right))) {
        const item = document.createElement("li"); item.append(moduleBranch(child, [...path, key], expanded)); children.append(item);
    }
    if (path.length === 1) {
        const disclosure = document.createElement("details"), summary = document.createElement("summary");
        disclosure.className = "activity-row activity-group-toggle"; disclosure.dataset.moduleGroup = part; disclosure.open = expanded.get(part) ?? true;
        summary.append(heading); disclosure.append(summary, children); group.append(disclosure); return group;
    }
    group.append(heading, children); return group;
}

/** Merge the live state into the selected page and retain its cursor when entries spill over. */
function visibleHistoryRows() {
    const merged = new Map([...rows, ...running.filter(row => pageIndex === 0 || rows.has(row.id)).map(row => [row.id, row])]);
    const ordered = [...merged.values()].filter(matches).sort(newestFirst), visible = ordered.slice(0, pageSize);
    if (!loading && ordered.length > pageSize) {
        const oldest = visible.at(-1);
        next = `${oldest.started_at}|${oldest.id}`;
        pageCursors[pageIndex + 1] = next;
        rows = new Map(visible.map(row => [row.id, row]));
    }
    return visible;
}

/** Replace the selected history page while retaining its keyboard focus and disclosures. */
function renderHistory() {
    const list = find("activity-list");
    const opened = new Set([...list.querySelectorAll("details[open]")].map(item => item.dataset.activityId));
    const focused = document.activeElement?.tagName === "SUMMARY" ? document.activeElement.parentElement.dataset.activityId : undefined;
    const visible = visibleHistoryRows();
    find("activity-previous").disabled = loading || pageIndex === 0;
    find("activity-next").disabled = loading || !next;
    find("activity-page").textContent = `Page ${pageIndex + 1}`;
    list.setAttribute("aria-busy", String(loading));
    let restoreFocus;
    list.replaceChildren(...visible.map(row => {
        const entry = document.createElement("li"), details = activityRow(row);
        details.open = opened.has(row.id);
        if (focused === row.id) restoreFocus = details.querySelector("summary");
        entry.append(details); return entry;
    }));
    restoreFocus?.focus({preventScroll:true});
    if (!visible.length) {
        const empty = document.createElement("li"); empty.className = "activity-empty";
        empty.textContent = loading ? "Loading activities." : "No activities recorded for this selection.";
        list.append(empty);
    }
}

/** Show known work in the next six hours for the selected activity type. */
function renderUpcoming(kind) {
    const upcoming = scheduled.filter(row => Date.parse(row.due_at) >= Date.now() && Date.parse(row.due_at) <= Date.now() + 6*60*60*1000 && (!kind || row.kind === kind)).sort((left,right) => left.due_at.localeCompare(right.due_at));
    find("activity-upcoming").replaceChildren(...upcoming.map(row => {
        const item = document.createElement("p"); item.className = "activity-row"; item.textContent = `${clock(row.due_at)}: ${activityTitle(row)}`; return item;
    }));
    if (!upcoming.length) find("activity-upcoming").textContent = "No known scheduled work in this window.";
}

/** Group producers by responsibility and prefer configured producers to their placeholders. */
function moduleTree() {
    const modules = new Map();
    for (const row of health) {
        let children = modules, node;
        for (const part of modulePath(row.owner)) {
            if (!children.has(part)) children.set(part, {children: new Map()});
            node = children.get(part); children = node.children;
        }
        // An area-qualified price producer supersedes its unconfigured adapter placeholder.
        if (!node.row || row.owner.split(":").length >= node.row.owner.split(":").length) node.row = row;
    }
    return modules;
}

/** Replace module status while retaining open groups, producer details and their focus. */
function renderModuleHealth() {
    const panel = find("activity-health");
    const focusedGroup = document.activeElement?.parentElement?.dataset.moduleGroup;
    const focusedOwner = document.activeElement?.parentElement?.dataset.owner;
    const openModules = new Set([...panel.querySelectorAll("details[data-owner][open]")].map(item => item.dataset.owner));
    const expanded = new Map([...panel.querySelectorAll("details[data-module-group]")].map(item => [item.dataset.moduleGroup, item.open]));
    panel.replaceChildren(...[...moduleTree()].sort(([left], [right]) => left.localeCompare(right)).map(([key, node]) => moduleBranch(node, [key], expanded)));
    for (const details of panel.querySelectorAll("details[data-owner]")) {
        details.open = openModules.has(details.dataset.owner);
        if (details.dataset.owner === focusedOwner) details.querySelector("summary").focus({preventScroll:true});
    }
    if (focusedGroup) [...panel.querySelectorAll("details[data-module-group]")].find(item => item.dataset.moduleGroup === focusedGroup)?.querySelector("summary").focus({preventScroll:true});
}

/** Render the selected local day, the independent schedule and the current producer status. */
function render() {
    find("activity-status").textContent = loadProblem || liveProblem || historyProblem || (loading ? "Loading activity history." : "");
    const kind = find("activity-type").value;
    find("activity-refresh").disabled = loading;
    renderHistory();
    renderUpcoming(kind);
    renderModuleHealth();
}

/** Read the requested page with the selected day and filter using its existing history cursor. */
function historyPagePath(page) {
    const day = activityDay.value.start;
    const before = pageCursors[page] ? `&before=${encodeURIComponent(pageCursors[page])}` : "";
    const kind = find("activity-type").value;
    return `v1/activities?day=${encodeURIComponent(day)}&limit=${pageSize}${kind ? `&kind=${encodeURIComponent(kind)}` : ""}${before}`;
}

/** Validate the rows and timezone before applying the page with its newer live outcomes. */
function applyHistoryPage(result, page) {
    const loadedRows = new Map(result.activities.map(row => [row.id, pendingUpdates.get(row.id) || row]));
    const loadedTimezone = result.timezone || "UTC";
    new Intl.DateTimeFormat("en", {timeZone:loadedTimezone});
    useHomeAssistantTimezone(loadedTimezone);
    rows = loadedRows;
    if (page === 0) {
        for (const [identity, row] of pendingUpdates) if (matches(row)) rows.set(identity, row);
    }
    pageIndex = page;
    pageCursors = pageCursors.slice(0, page + 1);
    next = result.next;
    if (next) pageCursors[page + 1] = next;
    loadProblem = "";
}

/** Restore focus only when disabling a pagination button caused it to move to the document body. */
function restorePaginationFocus(focusedPageControl) {
    if (!focusedPageControl || document.activeElement !== document.body) return;
    const otherControl = focusedPageControl === "activity-next" ? "activity-previous" : "activity-next";
    [focusedPageControl, otherControl, "activity-refresh"].map(find).find(button => !button.disabled)?.focus({preventScroll:true});
}

/** A late page response must not replace newer live outcomes for the same execution. */
async function load(page = 0) {
    activityDay.set({max: localDate()});
    const version = ++generation;
    const focusedPageControl = ["activity-previous", "activity-next"].includes(document.activeElement?.id) ? document.activeElement.id : undefined;
    requestedPage = page;
    loading = true;
    loadProblem = "";
    render();
    find("activity-status").textContent = "Loading activity history.";
    try {
        // A replay reset requires a fresh page read after the in-flight response settles.
        do {
            resetPending = false;
            pendingUpdates = new Map();
            try {
                const result = await api(historyPagePath(page));
                if (version !== generation) return;
                applyHistoryPage(result, page);
            } catch {if (version === generation) loadProblem = historyFailure;}
        } while (version === generation && resetPending);
    } finally {
        if (version === generation) {
            loading = false; requestedPage = pageIndex; render();
            restorePaginationFocus(focusedPageControl);
        }
    }
}

/** Start at the newest matching activity without changing the selected day or type. */
function resetHistory() {
    rows.clear(); next = undefined; pageIndex = 0; pageCursors = [undefined]; load();
}

/** Keep running work current and retain matching live outcomes across an in-flight page read. */
function updateActivity(row) {
    running = running.filter(item => item.id !== row.id);
    if (row.state === "running") running.push(row);
    if (!matches(row)) return;
    if (loading) pendingUpdates.set(row.id, row);
    if (pageIndex === 0 || rows.has(row.id)) rows.set(row.id, row);
}

/** Apply only the live topics owned by the Activity screen. */
function applyEvent({topic, value: row}) {
    switch (topic) {
        case "activity":
            updateActivity(row);
            break;
        case "schedule":
            scheduled = [...scheduled.filter(item => item.id !== row.id), ...(row.activity ? [row.activity] : [])];
            break;
        case "health":
            health = [...health.filter(item => item.owner !== row.owner), ...(row.removed ? [] : [row])];
            break;
        case "history":
            historyProblem = historyWarning(row.available, row.lost_updates);
            break;
    }
}

/** Re-read the requested history page after the stream replaces its live projection. */
function applySnapshot(snapshot) {
    running = snapshot.running; scheduled = snapshot.scheduled; health = snapshot.health;
    historyProblem = historyWarning(snapshot.history_available, snapshot.lost_updates);
    if (loading) resetPending = true;
    else load(requestedPage);
}

/** Render after applying an ordered live batch, or retain visible data if updates are unavailable. */
function renderEventBatch(message) {
    if (message.unavailable) {
        liveProblem = "Live updates unavailable. Displayed information may be out of date.";
        if (message.unauthorized) find("connect").hidden = false;
        render();
        return;
    }
    liveProblem = "";
    if (message.reset) applySnapshot(message.snapshot);
    for (const event of message.events || []) applyEvent(event);
    render();
}
observeEvents(renderEventBatch);
find("activity-type").addEventListener("change", resetHistory);
find("activity-refresh").addEventListener("click", () => {if (!loading) resetHistory();});
find("activity-previous").addEventListener("click", () => {if (!loading && pageIndex > 0) load(pageIndex - 1);});
find("activity-next").addEventListener("click", () => {if (!loading && next) load(pageIndex + 1);});
find("connect").addEventListener("submit", event => {event.preventDefault(); token = find("token").value; find("token").value = ""; find("connect").hidden = true; connectEvents(base, token); load();});
api("setup/session").then(session => {useHomeAssistantTimezone(session.timezone); activityDay.set({start: localDate(), max: localDate(), selection: "today"}); find("connect").hidden = Boolean(session.ingress); if (session.ingress) {connectEvents(base); load();} else {loadProblem = "Connect to read activity history."; render();}}).catch(() => {loadProblem = historyFailure; render();});
setInterval(render, 60000);
