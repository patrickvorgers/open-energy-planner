/** Keep database maintenance visible independently of the runtime that is being restarted. */
import {percentage} from "./display.js";
const PHASES = {idle: "Ready to change database", preparing: "Preparing: Waiting for OEP tasks to stop", copying: "Copying OEP data", verifying: "Verifying copied data", checking: "Checking the new database and application", restarting: "Restarting OEP", completed: "Migration complete: OEP is using the new database", restored: "Previous database restored", interrupted: "Migration interrupted: Review recovery options", failed: "Migration needs attention"};
const POLL_MILLISECONDS = 2000, STATUS_TIMEOUT_MILLISECONDS = 5000, ACTION_TIMEOUT_MILLISECONDS = 35000;

/** Bind one explicit editor and an independently reconnecting, authenticated maintenance status. */
export function bindStorageMigration(getToken, onReady) {
    const find = id => document.getElementById(id);
    const base = document.querySelector('meta[name="oep-api-base"]').content;
    let checkId = null, checkGeneration = 0, latest = null, timer = null, requestPending = false, enabled = false, polling = false, recoveryError = "";
    const recovery = "Open Energy Planner: Database migration recovery\n\nKeep the original database and the complete OEP data directory.\nBefore release: Use Restore previous database in Data storage.\nAfter release: Never select the old source directly: New data may exist. Use a new migration.\nIf the app cannot start: Stop OEP and add --recover-storage to the normal Python module startup command, keeping the normal --live/--demo and --data arguments. This only restores an unreleased migration.\nFor Home Assistant app recovery without a reachable maintenance screen: Enable recover_storage in the app configuration and start OEP. Disable it after the original database is restored. This only applies before release. A released migration requires repair of the selected database or a backup consistent with the released target. Never restore pre-migration bootstrap settings after release.\nThe private database-migration.json journal retains the original connection. database-selection.json overrides Supervisor/container connection settings. Do not delete these files or publish their credentials.\nA network timeout does not prove failure. Reconnect and check durable status before taking action.\n";
    const download = find("download-recovery");
    download.href = URL.createObjectURL(new Blob([recovery], {type: "text/plain"}));
    window.addEventListener("pagehide", () => {clearTimeout(timer); URL.revokeObjectURL(download.href);}, {once: true});

    /** Never surface database-driver errors or follow redirects carrying the administration token. */
    async function request(action = "", body = null) {
        const response = await fetch(base + "v1/storage/migration" + (action ? "/" + action : ""), {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${getToken()}`, ...(body ? {"Content-Type": "application/json"} : {})}, ...(body ? {body: JSON.stringify(body)} : {}), redirect: "error", cache: "no-store", signal: AbortSignal.timeout(body ? ACTION_TIMEOUT_MILLISECONDS : STATUS_TIMEOUT_MILLISECONDS)});
        if ([401, 404].includes(response.status)) enabled = false;
        if (response.status === 401) find("connect").hidden = false;
        if (!response.ok) throw new Error(response.status === 404 ? "Maintenance controls are unavailable in this runtime." : [400, 503].includes(response.status) ? await response.text() : "The operation could not be completed. Check the connection, settings and confirmation.");
        return response.json();
    }

    /** Invalidate preflight whenever destination input changes. Stale consent cannot start a copy. */
    function invalidate() {
        checkGeneration += 1;
        checkId = null;
        find("migration-confirmation").hidden = true;
        find("overwrite-destination").checked = false;
        find("start-migration").disabled = true;
        find("destination-result").textContent = "";
    }

    /** Show only progress from real counts. Retain recovery controls after a failed reconnect. */
    function render(status) {
        const wasRunning = latest?.running;
        latest = status;
        find("connect").hidden = true;
        find("database-connection").dataset.running = String(status.running);
        if (!status.running) find("status").textContent = "OEP is paused for database maintenance. Storage measurements return after restart.";
        find("database-connection").hidden = false;
        find("connection-summary").textContent = status.connection || status.backend;
        find("change-database").disabled = !status.running || requestPending;
        const visible = status.phase !== "idle";
        find("migration-progress").hidden = !visible;
        find("migration-phase").textContent = PHASES[status.phase] || "Checking migration status";
        find("migration-summary").textContent = status.source && status.destination ? `${status.source} → ${status.destination}` : "";
        if (status.running || ["preparing", "copying", "verifying", "checking", "restarting"].includes(status.phase)) recoveryError = "";
        const error = recoveryError || status.error || "";
        find("migration-error").hidden = !error;
        find("migration-error").textContent = error;
        const meter = find("migration-meter"), counted = Number.isFinite(status.percent);
        meter.hidden = !["preparing", "copying", "verifying", "checking", "restarting"].includes(status.phase);
        if (counted) meter.value = status.percent; else meter.removeAttribute("value");
        find("migration-count").textContent = counted ? `${status.done.toLocaleString()} of ${status.total.toLocaleString()} rows: ${percentage(status.percent)}` : "";
        find("retry-migration").hidden = !status.can_retry;
        find("retry-migration").textContent = status.released ? "Retry OEP startup" : "Retry migration";
        find("restore-database").hidden = !status.can_restore;
        find("retry-connection").hidden = true;
        if (!status.running) {find("migration-editor").hidden = true; find("destination-url").value = "";}
        if (status.running && wasRunning === false) queueMicrotask(onReady);
    }

    /** Keep polling during runtime shutdown. Loss of HTTP is never reported as migration failure. */
    async function refresh() {
        if (!enabled || polling) return;
        polling = true;
        clearTimeout(timer);
        try { render(await request()); }
        catch (error) {
            if (latest && !latest.running) {
                find("migration-phase").textContent = "Reconnecting: Migration status temporarily unavailable";
                find("retry-connection").hidden = false;
            } else if (latest) {
                find("migration-error").hidden = false;
                find("migration-error").textContent = error.message;
            }
        } finally {polling = false; if (enabled) timer = setTimeout(refresh, POLL_MILLISECONDS);}
    }

    /** Serialize actions, keep instructions visible, and recover status after ambiguous HTTP outcomes. */
    async function action(name, body) {
        if (requestPending) return;
        requestPending = true;
        recoveryError = "";
        for (const id of ["start-migration", "retry-migration", "restore-database", "check-destination"]) find(id).disabled = true;
        try { render(await request(name, body)); }
        catch (error) {
            if (latest?.running) find("destination-result").textContent = error.message;
            else {recoveryError = error.message; find("migration-error").hidden = false; find("migration-error").textContent = recoveryError; find("migration-progress").hidden = false;}
        }
        finally {
            requestPending = false;
            for (const id of ["retry-migration", "restore-database", "check-destination"]) find(id).disabled = false;
            await refresh();
            find("start-migration").disabled = !latest?.running || !checkId || !find("overwrite-destination").checked;
        }
    }
    find("change-database").onclick = () => {invalidate(); find("migration-editor").hidden = false; find("destination-kind").focus();};
    find("cancel-migration-edit").onclick = () => {invalidate(); find("migration-editor").hidden = true; find("destination-url").value = ""; find("change-database").focus();};
    find("destination-kind").onchange = () => {
        invalidate();
        const kind = find("destination-kind").value, local = kind === "sqlite";
        find("destination-url-label").hidden = local; find("destination-url").required = !local; find("destination-url").value = "";
        find("destination-format").textContent = {postgresql: "Format: postgresql+psycopg://USER:PASSWORD@HOST:5432/DATABASE", mariadb: "Format: mariadb+pymysql://USER:PASSWORD@HOST:3306/DATABASE", mysql: "Format: mysql+pymysql://USER:PASSWORD@HOST:3306/DATABASE", sqlite: "Uses the local database in OEP's existing data directory."}[kind];
    };
    find("destination-url").oninput = invalidate;
    find("destination-url").required = true;
    find("overwrite-destination").onchange = () => {find("start-migration").disabled = !checkId || !find("overwrite-destination").checked;};
    find("migration-editor").onsubmit = async event => {
        event.preventDefault();
        if (requestPending) return;
        invalidate(); requestPending = true; find("check-destination").disabled = true;
        const generation = checkGeneration;
        find("destination-kind").disabled = true; find("destination-url").disabled = true;
        const kind = find("destination-kind").value;
        const url = kind === "sqlite" ? null : find("destination-url").value.trim();
        try {
            const result = await request("check", {url});
            if (generation !== checkGeneration) return;
            checkId = result.check_id;
            find("destination-result").textContent = result.contains_oep_data ? "Connection verified: Existing OEP data will be replaced." : "Connection verified: No existing OEP tables found.";
            find("migration-route").textContent = `${latest.connection || latest.backend} → ${result.destination || result.backend}`;
            find("migration-confirmation").hidden = false;
        } catch (error) {if (generation === checkGeneration) find("destination-result").textContent = error.message;}
        finally {requestPending = false; find("check-destination").disabled = false; find("destination-kind").disabled = false; find("destination-url").disabled = false;}
    };
    find("start-migration").onclick = () => {
        if (!checkId || !find("overwrite-destination").checked) return;
        find("migration-recovery").open = true;
        action("start", {check_id: checkId, overwrite: true});
    };
    find("retry-migration").onclick = () => {
        if (!latest.released && !window.confirm("Retry migration? Existing OEP tables in the destination will be replaced again. The original database is retained.")) return;
        action("retry", {overwrite: !latest.released});
    };
    find("restore-database").onclick = () => action("restore", {});
    find("retry-connection").onclick = () => {enabled = true; refresh();};
    return () => {enabled = true; refresh();};
}
