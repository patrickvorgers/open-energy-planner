import {bindNotifications, refreshNotifications} from "./notifications.js";

/** Dismiss the shared mobile menu, including interactions originating in the content document. */
export function closeSettingsNavigation(focus = false) {
    const sidebar = document.querySelector(".sidebar");
    if (!sidebar?.classList.contains("settings-menu-open")) return;
    const toggle = document.querySelector('[aria-controls="settings-navigation"]');
    sidebar.classList.remove("settings-menu-open");
    toggle?.setAttribute("aria-expanded", "false");
    if (focus) toggle?.focus();
}

/** Bind shared settings navigation once after the sidebar markup exists.
 * Insert a mobile Menu bar and register document click and keydown listeners.
 * Keep aria-controls and aria-expanded synchronized with sidebar visibility.
 * Outside clicks dismiss navigation and open overflow disclosures. Escape closes
 * disclosures first, then closes an open sidebar and restores focus to Menu.
 * Leaving the mobile viewport clears its open state without moving focus.
 * Leave page-specific editing and persistence to their existing owners.
 */
export function bindSettingsActions() {
    const versionStatus = document.getElementById("integration-version-status");
    const integrationDetail = document.getElementById("integration-version-detail");
    const versionRefreshMilliseconds = 15000;
    const versionReadTimeoutMilliseconds = 5000;
    /** Show only a current version/build mismatch, clearing stale warnings after a failed read. */
    async function refreshVersion() {
        if (!versionStatus) return;
        try {
            const response = await fetch(new URL("session", import.meta.url), {cache: "no-store", redirect: "error", signal: AbortSignal.timeout(versionReadTimeoutMilliseconds)});
            if (!response.ok) throw new Error("Version status unavailable");
            const session = await response.json(), info = session.build_info;
            await refreshNotifications(session);
            versionStatus.hidden = info?.status !== "mismatch";
            const integration = info?.integration;
            if (integrationDetail) {
                integrationDetail.hidden = info?.status !== "mismatch" || !integration;
                integrationDetail.textContent = integration ? `Integration version: ${integration.version || "Unknown"}: Build: ${integration.build || "Development"}` : "";
            }
            versionStatus.title = integration ? `Last observed integration: ${integration.version || "Unknown"}: Build: ${integration.build || "Development"}` : "No recent integration contact";
        } catch {
            await refreshNotifications();
            versionStatus.hidden = true;
            versionStatus.removeAttribute("title");
            if (integrationDetail) integrationDetail.hidden = true;
        } finally { setTimeout(refreshVersion, versionRefreshMilliseconds); }
    }
    refreshVersion();
    const sidebar = document.querySelector(".sidebar");
    const mobileBar = document.createElement("div"), name = document.createElement("strong"), toggle = document.createElement("button");
    mobileBar.className = "settings-mobilebar"; name.textContent = "Open Energy Planner";
    toggle.type = "button"; toggle.className = "secondary"; toggle.textContent = "Menu";
    sidebar.id = "settings-navigation"; toggle.setAttribute("aria-controls", sidebar.id); toggle.setAttribute("aria-expanded", "false");
    mobileBar.append(name, toggle); document.body.prepend(mobileBar);
    bindNotifications();
    const closeNavigation = () => closeSettingsNavigation();
    toggle.onclick = () => { const open = sidebar.classList.toggle("settings-menu-open"); toggle.setAttribute("aria-expanded", String(open)); };
    const mobileViewport = window.matchMedia("(max-width: 640px)");
    mobileViewport.addEventListener("change", event => { if (!event.matches) closeNavigation(); });
    document.addEventListener("click", event => {
        if (sidebar.contains(event.target) && event.target.closest("a")) {
            closeNavigation();
            if (mobileViewport.matches) toggle.focus();
        } else if (!sidebar.contains(event.target) && !mobileBar.contains(event.target)) closeNavigation();
        for (const menu of document.querySelectorAll(".settings-more[open]")) if (!menu.contains(event.target)) menu.open = false;
    });
    document.addEventListener("keydown", event => {
        if (event.key === "Escape") for (const menu of document.querySelectorAll(".settings-more[open]")) { menu.open = false; menu.querySelector("summary").focus(); }
        if (event.key === "Escape" && sidebar.classList.contains("settings-menu-open")) { closeNavigation(); toggle.focus(); }
    });
}
