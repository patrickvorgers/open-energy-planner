/** Share active notices with navigation and show their full explanation on the recovery page. */
import {when} from "./display.js";
import {connectEvents, observeEvents} from "./events.js";

const NOTIFICATION_READ_TIMEOUT_MILLISECONDS = 5000;
let banner, metadata = {}, controller, generation = 0, focusedReadGeneration = 0, lastDocument, lastSignature, token = "";
let center, popup, notificationButton, notificationCount, centerItems, centerWarning, centerSignature, hostFrame;
let detailFocusRequested = location.hash === "#planning-notifications";

/** Bind the shell's center or the authenticated content document without sharing credentials. */
export function bindNotifications() {
    const main = document.querySelector(".workspace-main");
    if (!main || banner || center) return;
    if (document.body.classList.contains("workspace-shell")) {
        bindCenter();
        const frame = document.getElementById("workspace-content");
        let source;
        frame.addEventListener("oep-notifications-loading", event => {
            if (event.detail.source !== frame.contentDocument) return;
            source = event.detail.source;
        });
        frame.addEventListener("oep-notifications", event => {
            if (event.detail.source !== frame.contentDocument) return;
            source = event.detail.source;
            renderCenter(event.detail.payload, event.detail.warning);
        });
        frame.addEventListener("load", () => {
            if (source !== frame.contentDocument) renderCenter({notifications: []}, "Notification status is unavailable. Check the app connection.");
        });
        return;
    }
    try {
        const frame = parent !== window && parent.document.getElementById("workspace-content");
        if (frame?.contentWindow === window && parent.document.body.classList.contains("workspace-shell")) hostFrame = frame;
    } catch { /* A standalone page keeps all notification state in its own document. */ }
    if (!hostFrame) bindCenter();
    banner = document.createElement("section");
    banner.id = "planning-notifications";
    banner.className = "planning-notifications";
    banner.setAttribute("aria-label", "Notifications");
    banner.setAttribute("aria-live", "polite");
    banner.hidden = true;
    main.prepend(banner);
    observeEvents(message => {
        if (message.reset || message.events?.some(event => event.topic === "notifications")) refreshNotifications();
    });
    if (hostFrame) hostFrame.dispatchEvent(new CustomEvent("oep-notifications-loading", {detail: {source: document}}));
    else presentCenter({notifications: []});
    window.addEventListener("hashchange", () => {
        detailFocusRequested = location.hash === "#planning-notifications";
        focusDetail();
    });
    document.addEventListener("submit", event => {
        if (["connect", "overview-connect", "contract-connect"].includes(event.target.id)) {
            token = event.target.querySelector('input[type="password"]')?.value || "";
            focusedReadGeneration = 0;
            refreshNotifications();
        }
    }, true);
}

/** Keep the bell in desktop navigation and the mobile header, with a native light-dismiss popup. */
function bindCenter() {
    center = document.createElement("div");
    center.className = "notification-center";
    center.setAttribute("aria-live", "polite");
    center.hidden = true;
    notificationButton = document.createElement("button");
    notificationButton.type = "button";
    notificationButton.className = "secondary notification-toggle";
    notificationButton.setAttribute("popovertarget", "notification-center-panel");
    notificationButton.setAttribute("aria-expanded", "false");
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg"), label = document.createElement("span");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.classList.add("notification-bell");
    for (const shape of ["M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9", "M10 21h4"]) {
        const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
        path.setAttribute("d", shape);
        icon.append(path);
    }
    label.textContent = "Notifications";
    label.className = "notification-label";
    notificationCount = document.createElement("span");
    notificationCount.className = "notification-count";
    notificationCount.setAttribute("aria-hidden", "true");
    notificationButton.append(icon, label, notificationCount);
    center.append(notificationButton);
    popup = document.createElement("section");
    popup.id = "notification-center-panel";
    popup.className = "notification-popup";
    popup.setAttribute("popover", "auto");
    popup.setAttribute("role", "dialog");
    popup.setAttribute("aria-label", "Notifications");
    const header = document.createElement("header"), heading = document.createElement("h2"), close = document.createElement("button");
    heading.textContent = "Notifications";
    close.type = "button";
    close.autofocus = true;
    close.className = "secondary";
    close.textContent = "Close";
    close.setAttribute("aria-label", "Close notifications");
    close.addEventListener("click", () => popup.hidePopover());
    header.append(heading, close);
    centerWarning = document.createElement("p");
    centerWarning.className = "notification-stale";
    centerWarning.hidden = true;
    centerItems = document.createElement("ul");
    popup.append(header, centerWarning, centerItems);
    document.body.append(popup);
    const mobileViewport = window.matchMedia("(max-width: 640px)");
    const placeCenter = () => {
        popup.hidePopover();
        if (mobileViewport.matches) document.querySelector(".settings-mobilebar").insertBefore(center, document.querySelector('[aria-controls="settings-navigation"]'));
        else document.querySelector(".sidebar").insertBefore(center, document.querySelector(".sidebar nav"));
    };
    mobileViewport.addEventListener("change", placeCenter);
    placeCenter();
    popup.addEventListener("beforetoggle", event => {
        if (event.newState !== "open") return;
        const position = notificationButton.getBoundingClientRect();
        const top = position.bottom + 8;
        popup.style.left = `${mobileViewport.matches ? 16 : position.left}px`;
        popup.style.top = `${top}px`;
        popup.style.maxHeight = `${Math.max(0, innerHeight - top - 16)}px`;
    });
    popup.addEventListener("toggle", event => notificationButton.setAttribute("aria-expanded", String(event.newState === "open")));
    popup.addEventListener("click", event => {
        if (event.target.closest("a")) popup.hidePopover();
    });
}

/** Restrict links to this installation and retain embedded routing and native draft guards. */
function recoveryTarget(notice) {
    if (!notice.action_path) return null;
    const base = new URL(document.querySelector('meta[name="oep-api-base"]').content, location.href);
    let target;
    try { target = new URL(notice.action_path.replace(/^\//, ""), base); } catch { return null; }
    if (target.origin !== base.origin || !target.pathname.startsWith(base.pathname)) return null;
    if (new URL(location.href).searchParams.get("embedded") === "1" || document.body.classList.contains("workspace-shell")) target.searchParams.set("embedded", "1");
    target.hash = "planning-notifications";
    return target;
}

/** Project only display data to the owning shell. The authenticated token stays in the content page. */
function presentCenter(payload, warning = "") {
    if (hostFrame) hostFrame.dispatchEvent(new CustomEvent("oep-notifications", {detail: {source: document, payload, warning}}));
    else renderCenter(payload, warning);
}

/** Retain popup state across unchanged reads and hide both the bell and list after confirmed recovery. */
function renderCenter(payload, warning = "") {
    const signature = JSON.stringify([payload, warning]);
    if (signature === centerSignature) return;
    centerSignature = signature;
    const count = payload.notifications.length;
    center.hidden = !count && !warning;
    notificationButton.setAttribute("aria-label", count ? `Notifications: ${count} active` : "Notifications: Status unavailable");
    notificationCount.textContent = count || "!";
    centerWarning.hidden = !warning;
    centerWarning.textContent = warning;
    const focusedItem = centerItems.contains(document.activeElement);
    centerItems.replaceChildren();
    for (const notice of payload.notifications) {
        const item = document.createElement("li"), target = recoveryTarget(notice);
        const title = document.createElement(target ? "a" : "strong");
        title.textContent = notice.title;
        if (target) {
            title.href = target.href;
            if (document.body.classList.contains("workspace-shell")) title.target = "oep-workspace-content";
        }
        const heading = document.createElement("div");
        heading.className = "notification-heading";
        heading.append(title);
        appendTemporaryIndicator(heading, notice);
        item.append(heading);
        appendExplanation(item, notice.message);
        centerItems.append(item);
    }
    if (center.hidden) popup.hidePopover();
    else if (focusedItem) popup.querySelector("button").focus();
}

/** Explain a source-owned temporary lifetime without hiding unresolved or unreadable notices. */
function appendTemporaryIndicator(container, notice) {
    if (!notice.auto_clear_minutes || notice.stale) return;
    const badge = document.createElement("span"), tooltip = document.createElement("span");
    badge.className = "notification-expiry";
    badge.tabIndex = 0;
    badge.setAttribute("role", "img");
    const explanation = `Temporary notification. Disappears automatically after ${notice.auto_clear_minutes} minutes.`;
    badge.setAttribute("aria-label", explanation);
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("aria-hidden", "true");
    icon.setAttribute("viewBox", "0 0 24 24");
    const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M12 8v4l3 2 M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0");
    icon.append(path);
    tooltip.className = "notification-expiry-tooltip";
    tooltip.setAttribute("aria-hidden", "true");
    tooltip.hidden = true;
    tooltip.textContent = explanation;
    let hovered = false, focused = false, dismissed = false;
    const syncTooltip = () => { tooltip.hidden = dismissed || !(hovered || focused); };
    badge.addEventListener("pointerenter", event => {
        if (event.pointerType === "touch") return;
        hovered = true;
        dismissed = false;
        syncTooltip();
    });
    badge.addEventListener("pointerleave", event => {
        if (event.pointerType === "touch") return;
        hovered = false;
        syncTooltip();
    });
    badge.addEventListener("focus", () => { focused = true; dismissed = false; syncTooltip(); });
    badge.addEventListener("blur", () => { focused = false; syncTooltip(); });
    badge.addEventListener("pointerdown", event => {
        if (event.pointerType !== "touch") return;
        event.preventDefault();
        const reveal = tooltip.hidden;
        badge.focus({preventScroll: true});
        dismissed = !reveal;
        syncTooltip();
    });
    badge.addEventListener("keydown", event => {
        if (event.key !== "Escape" || tooltip.hidden) return;
        dismissed = true;
        syncTooltip();
        event.preventDefault();
        event.stopPropagation();
    });
    badge.append(icon, document.createTextNode(`${notice.auto_clear_minutes} min`), tooltip);
    container.append(badge);
}

/** Keep actionable resource identities visible as inert paragraphs, without raw diagnostic dumps. */
function appendExplanation(container, message) {
    for (const paragraph of message.split("\n\n")) {
        const element = document.createElement("p");
        element.textContent = paragraph;
        element.style.overflowWrap = "anywhere";
        container.append(element);
    }
}

/** Focus the requested explanation once it is available without interrupting later polling or edits. */
function focusDetail() {
    if (!detailFocusRequested || !banner || banner.hidden) return;
    banner.setAttribute("tabindex", "-1");
    banner.focus();
    detailFocusRequested = false;
}

/** Keep user/provider text inert and restrict recovery links to the current ingress installation. */
function render(payload, warning = "") {
    const signature = JSON.stringify([payload, warning, metadata.timezone]);
    if (signature === lastSignature) return;
    lastSignature = signature;
    presentCenter(payload, warning);
    banner.replaceChildren();
    const currentPath = location.pathname.replace(/\/$/, "");
    const notices = payload.notifications.filter(notice => recoveryTarget(notice)?.pathname.replace(/\/$/, "") === currentPath);
    banner.hidden = !notices.length;
    if (warning && notices.length) {
        const stale = window.document.createElement("p");
        stale.className = "notification-stale";
        stale.textContent = warning;
        banner.append(stale);
    }
    for (const notice of notices) {
        const article = window.document.createElement("article"), title = window.document.createElement("h2");
        article.className = `planning-notice${notice.level === "info" ? " informational" : notice.level === "error" ? " notification-error" : ""}`;
        title.textContent = notice.title;
        const heading = document.createElement("div");
        heading.className = "notification-heading";
        heading.append(title);
        appendTemporaryIndicator(heading, notice);
        article.append(heading);
        appendExplanation(article, notice.message);
        if (notice.since) {
            const since = window.document.createElement("p");
            since.className = "notification-since";
            const started = metadata.timezone ? when(notice.since, {}, metadata.timezone) : new Date(notice.since).toISOString().slice(0, 16).replace("T", " ") + " UTC";
            const label = notice.blocks?.length === 0 ? "Active since" : "Blocked since";
            since.textContent = `${label}: ${started}`;
            article.append(since);
        }
        banner.append(article);
    }
    focusDetail();
}

/** Preserve stale evidence on failed reads. Focus fresh notices only after an explicit user action. */
export async function refreshNotifications(session, focus = false) {
    if (!banner) return;
    if (session) metadata = session;
    if (!focus && focusedReadGeneration && (metadata.ingress || token)) return;
    const requestGeneration = ++generation;
    controller?.abort();
    if (!metadata.ingress && !token) {
        lastDocument = lastSignature = undefined;
        banner.replaceChildren();
        banner.hidden = true;
        presentCenter({notifications: []});
        return;
    }
    controller = new AbortController();
    if (focus) focusedReadGeneration = requestGeneration;
    try {
        const base = new URL(document.querySelector('meta[name="oep-api-base"]').content, location.href);
        const response = await fetch(new URL("v1/notifications", base), {
            headers: token ? {Authorization: `Bearer ${token}`} : {}, cache: "no-store", redirect: "error",
            signal: AbortSignal.any([controller.signal, AbortSignal.timeout(NOTIFICATION_READ_TIMEOUT_MILLISECONDS)]),
        });
        if (requestGeneration !== generation) return;
        if ([401, 403].includes(response.status)) {
            lastDocument = undefined;
            render({notifications: []}, "Sign in again to read notifications.");
            return;
        }
        if (!response.ok) throw new Error("Notification status unavailable");
        const result = await response.json();
        if (requestGeneration !== generation) return;
        connectEvents(document.querySelector('meta[name="oep-api-base"]').content, token);
        if (!Array.isArray(result.notifications) || result.notifications.some(item => !item ||
            [item.code, item.title, item.message].some(value => typeof value !== "string") ||
            [item.action_path, item.action_label, item.detail].some(value => value != null && typeof value !== "string") ||
            [item.key, item.owner].some(value => value != null && (typeof value !== "string" || !value.trim())) ||
            (item.level != null && !["info", "warning", "error"].includes(item.level)) ||
            (item.state != null && !["active", "recovering"].includes(item.state)) ||
            (item.blocks != null && (!Array.isArray(item.blocks) || item.blocks.some(value => typeof value !== "string" || !value.trim()))) ||
            (item.stale != null && typeof item.stale !== "boolean") ||
            (item.auto_clear_minutes != null && (!Number.isSafeInteger(item.auto_clear_minutes) || item.auto_clear_minutes <= 0)) ||
            (item.fallback != null && typeof item.fallback !== "boolean") ||
            (item.since != null && (typeof item.since !== "string" || !Number.isFinite(Date.parse(item.since)))))) throw new Error("Invalid planning status");
        const unavailable = result.unavailable_sources ?? [];
        if (!Array.isArray(unavailable) || unavailable.some(value => typeof value !== "string" || !value.trim())) throw new Error("Invalid notification sources");
        const keys = result.notifications.flatMap(notice => notice.key ? [notice.key] : []);
        if (new Set(keys).size !== keys.length) throw new Error("Duplicate notification causes");
        const retained = new Map(result.notifications.map(notice => [notice.key || notice.code, notice]));
        for (const previous of lastDocument?.notifications || []) {
            const key = previous.key || previous.code;
            const current = retained.get(key);
            if (unavailable.includes(previous.owner) &&
                (!current || previous.fallback !== true && current.fallback === true)) {
                retained.set(key, {...previous, stale: true});
            }
        }
        lastDocument = {...result, notifications: [...retained.values()]};
        const stale = lastDocument.notifications.some(notice => notice.stale);
        const warning = stale
            ? "Some notification sources are unavailable. Their last known notifications are retained."
            : unavailable.length ? "Some notification sources are unavailable." : "";
        render(lastDocument, warning);
        if (focus && !banner.hidden) {
            detailFocusRequested = true;
            focusDetail();
        }
    } catch {
        if (requestGeneration !== generation) return;
        const retained = lastDocument ? {...lastDocument, notifications: lastDocument.notifications.map(notice => ({...notice, stale: true}))} : {notifications: []};
        render(retained, lastDocument?.notifications.length ? "Notification status could not be refreshed. These are the last known notifications." : "Notification status is unavailable. Check the app connection before relying on an earlier plan.");
    } finally {
        if (focusedReadGeneration === requestGeneration) focusedReadGeneration = 0;
    }
}
