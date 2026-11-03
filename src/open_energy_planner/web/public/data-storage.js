/** Present read-only storage evidence using the shared authenticated app shell. */
import {bindSettingsActions} from "./settings-actions.js";
import {bindStorageMigration} from "./storage-migration.js";
bindSettingsActions();
const base = document.querySelector('meta[name="oep-api-base"]').content;
const find = id => document.getElementById(id);
const READ_TIMEOUT_MILLISECONDS = 35000;
let token = "", busy = false;
const refreshMigration = bindStorageMigration(() => token, load);

/** Format byte measurements without converting unknown space into zero. */
function bytes(value) {
    if (!Number.isFinite(value)) return "Unknown";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
    return `${value.toLocaleString(undefined, {maximumFractionDigits: 2})} ${units[unit]}`;
}

/** Clear stale evidence on failures and serialize refreshes without storing credentials. */
async function load() {
    if (busy) return;
    refreshMigration();
    busy = true;
    find("refresh").disabled = true;
    find("connect").querySelector("button").disabled = true;
    find("storage").hidden = true;
    find("storage-warning").hidden = true;
    find("status").textContent = "Loading storage evidence…";
    try {
        const response = await fetch(base + "v1/storage", {headers: {Authorization: `Bearer ${token}`}, redirect: "error", cache: "no-store", signal: AbortSignal.timeout(READ_TIMEOUT_MILLISECONDS)});
        if (!response.ok) { if (response.status === 401) find("connect").hidden = false; throw new Error("Storage evidence unavailable. Check the connection and try again."); }
        const usage = await response.json();
        find("backend").textContent = {sqlite: "SQLite", postgresql: "PostgreSQL", mariadb: "MariaDB", mysql: "MySQL"}[usage.backend] || "Unknown";
        find("database-size").textContent = bytes(usage.database_bytes) + (usage.database_estimated ? " (estimated)" : "");
        find("free-space").textContent = bytes(usage.disk_free_bytes);
        find("volume-size").textContent = bytes(usage.disk_total_bytes);
        find("capacity-status").textContent = {ok: "Sufficient space", low: "Low space", unknown: "Unknown", unavailable: "Measurement unavailable"}[usage.status] || "Unknown";
        find("size-scope").textContent = usage.backend === "sqlite" ? "For file-backed SQLite, includes WAL and shared-memory sidecars. Sizes can change during writes." : usage.database_estimated ? "Estimated size of visible tables and indexes in the connected database, including tables outside OEP. Server storage overhead is excluded." : "Whole connected database, including tables outside OEP. Server storage overhead is excluded.";
        find("capacity-note").textContent = usage.disk_free_bytes === null ? "Free space cannot be determined for this database. Check the database host or provider for capacity and quotas." : "Free space on the volume containing the active database. This is not a database quota.";
        const warning = find("storage-warning");
        warning.hidden = usage.status !== "low";
        warning.textContent = "Low storage space. Free space on the database volume or expand its capacity to protect future writes. Back up retained evidence before removing any files.";
        find("status").textContent = usage.measurement_failed ? "Some storage measurements are unavailable. Refresh to try again." : "Storage evidence refreshed.";
        find("connect").hidden = true;
        find("storage").hidden = false;
    } catch {
        find("status").textContent = find("database-connection").dataset.running === "false" ? "OEP is paused for database maintenance. Storage measurements return after restart." : "Storage evidence unavailable. Check the connection and try again.";
    } finally {
        busy = false;
        find("refresh").disabled = false;
        find("connect").querySelector("button").disabled = false;
    }
}

find("refresh").onclick = load;
find("connect").onsubmit = event => { event.preventDefault(); token = find("token").value; load(); };
fetch(base + "setup/session", {redirect: "error", cache: "no-store", signal: AbortSignal.timeout(READ_TIMEOUT_MILLISECONDS)})
    .then(response => response.json()).then(session => { if (session.ingress) return load(); })
    .catch(() => { find("status").textContent = "Connection status unavailable. Connect or refresh to try again."; });
