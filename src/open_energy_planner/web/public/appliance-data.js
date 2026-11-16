const READ_TIMEOUT_MILLISECONDS = 30000;
/** Bound authenticated GETs through response decoding. Preserve write acknowledgement and HTTP status handling. */
export async function applianceApi(token, path, body, method = body ? "POST" : "GET") {
    const response = await fetch(document.querySelector('meta[name="oep-api-base"]').content + path, {method, headers: {Authorization: `Bearer ${token}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store", signal: method === "GET" ? AbortSignal.timeout(READ_TIMEOUT_MILLISECONDS) : undefined});
    if (!response.ok) { const error = new Error(await response.text()); error.status = response.status; throw error; }
    return response.json();
}
/** Settle every related read before callers release their refresh guard or report a failure. */
export async function readApplianceEvidence(token) {
    const results = await Promise.allSettled([applianceApi(token, "v1/appliances"), applianceApi(token, "v1/consumers?view=settings"), applianceApi(token, "setup/session")]);
    if (results.some(result => result.status === "rejected")) throw new Error();
    return results.map(result => result.value);
}
/** Limit profile inspection to followed cycle devices, including those still scanning without a profile or shift permission. */
export function followedDevices(data, consumers) {
    if (!Array.isArray(consumers.charging_meters) || !Array.isArray(consumers.thermal_meters)) throw new Error("Specialized-meter membership unavailable.");
    const specialized = new Set([...consumers.charging_meters, ...consumers.thermal_meters]);
    const selected = new Set(consumers.selected.map(item => item.entity_id));
    return data.devices.filter(device => (device.shiftable || device.profile || device.recent_cycles?.length || device.history_import || device.learning_history || device.raw_history) && selected.has(device.entity_id) && !specialized.has(device.entity_id));
}
