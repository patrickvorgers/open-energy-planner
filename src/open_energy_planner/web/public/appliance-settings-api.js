/** Share authenticated EV/boiler requests, bounded reads and safe token-rejection advice. */
const READ_TIMEOUT_MILLISECONDS = 30000;

/** Read with the current editor token; supplied deadlines and write acknowledgements stay authoritative. */
export function applianceSettingsApi(base, token) {
    return async (path, body, signal) => {
        signal ??= body ? undefined : AbortSignal.timeout(READ_TIMEOUT_MILLISECONDS);
        const rejectedToken = new Error("The installation token was rejected.");
        const rejectedSettings = new Error("Settings were rejected. Your edits are retained. Refresh before saving and review the selected sources and physical settings.");
        try {
            const response = await fetch(base + path, {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${token()}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store", signal});
            if (response.status === 401) throw rejectedToken;
            if (!response.ok) {
                if (body && [400, 409].includes(response.status)) throw rejectedSettings;
                throw new Error();
            }
            const data = await response.json(); signal?.throwIfAborted(); return data;
        } catch (error) {
            if (error === rejectedToken || error === rejectedSettings) throw error;
            throw new Error(body && !signal
                ? "Save could not be confirmed. Your edits are retained. Settings may already be saved. Reload before retrying."
                : "Home Assistant data could not be read. Check the connection and retry.");
        }
    };
}
