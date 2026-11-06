/** One authenticated, reconnecting subscription per content document. Tokens stay in memory. */
let current, controller, snapshot, listeners = new Set();

/** Share the authoritative snapshot with late listeners in the current document. */
export function observeEvents(listener) {
    listeners.add(listener);
    if (snapshot) listener({reset: true, snapshot});
    return () => listeners.delete(listener);
}

/** Notify independent consumers without allowing one failed renderer to stop the subscription. */
function deliver(message) {
    for (const listener of listeners) {
        try { listener(message); } catch { /* A consumer can recover on the next snapshot. */ }
    }
}

/** Apply deltas to the in-memory state so new consumers do not miss earlier events. */
function update(message) {
    if (message.reset) snapshot = message.snapshot;
    if (snapshot) for (const event of message.events || []) {
        const row = event.value;
        if (event.topic === "notifications") snapshot.notifications = row;
        if (event.topic === "history") {snapshot.history_available = row.available; snapshot.lost_updates = row.lost_updates;}
        if (event.topic === "health") snapshot.health = [...snapshot.health.filter(item => item.owner !== row.owner), ...(row.removed ? [] : [row])];
        if (event.topic === "schedule") snapshot.scheduled = [...snapshot.scheduled.filter(item => item.id !== row.id), ...(row.activity ? [row.activity] : [])];
        if (event.topic === "activity") snapshot.running = [...snapshot.running.filter(item => item.id !== row.id), ...(row.state === "running" ? [row] : [])];
    }
    deliver(message);
}

/** Long polling supports bearer headers and resets explicitly when replay is no longer available. */
export function connectEvents(base, token = "") {
    if (current?.base === base && current?.token === token && !controller?.signal.aborted) return;
    controller?.abort();
    current = {base, token};
    snapshot = undefined;
    controller = new AbortController();
    const signal = controller.signal;
    const session = current;
    (async () => {
        let cursor;
        while (!signal.aborted) {
            try {
                const url = new URL("v1/events", new URL(base, location.href));
                if (cursor) url.searchParams.set("cursor", cursor);
                const response = await fetch(url, {headers: session.token ? {Authorization: `Bearer ${session.token}`} : {},
                    cache: "no-store", redirect: "error", signal: AbortSignal.any([signal, AbortSignal.timeout(35000)])});
                if (!response.ok) {
                    if ([401, 403].includes(response.status)) {
                        deliver({unavailable: true, unauthorized: true});
                        if (current === session) current = undefined;
                        return;
                    }
                    throw new Error("Event subscription unavailable");
                }
                const message = await response.json();
                if (signal.aborted) return;
                if (typeof message.cursor !== "string" || !Array.isArray(message.events) || (message.reset && !message.snapshot)) throw new Error("Invalid event response");
                cursor = message.cursor;
                update(message);
            } catch {
                if (signal.aborted) return;
                deliver({unavailable: true});
                await new Promise(resolve => {
                    const done = () => {clearTimeout(timer); signal.removeEventListener("abort", done); resolve();};
                    const timer = setTimeout(done, 3000);
                    signal.addEventListener("abort", done, {once: true});
                });
            }
        }
    })();
}

window.addEventListener("pagehide", () => controller?.abort());
window.addEventListener("pageshow", event => {
    if (event.persisted && current) connectEvents(current.base, current.token);
});
