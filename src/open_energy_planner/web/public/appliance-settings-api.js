/** Share authenticated EV/boiler requests, bounded reads and safe token-rejection advice. */
const READ_TIMEOUT_MILLISECONDS = 30000;

/** Read with the current editor token; supplied deadlines and write acknowledgements stay authoritative. */
export function applianceSettingsApi(base, token) {
    return async (path, body, signal) => {
        signal ??= body ? undefined : AbortSignal.timeout(READ_TIMEOUT_MILLISECONDS);
        const rejectedToken = new Error("The installation token was rejected.");
        try {
            const response = await fetch(base + path, {method: body ? "POST" : "GET", headers: {Authorization: `Bearer ${token()}`, "Content-Type": "application/json"}, body: body ? JSON.stringify(body) : undefined, redirect: "error", cache: "no-store", signal});
            if (response.status === 401) throw rejectedToken;
            if (!response.ok) { if (signal) throw new Error(); throw new Error(await response.text()); }
            const data = await response.json(); signal?.throwIfAborted(); return data;
        } catch (error) {
            if (error === rejectedToken) throw error;
            if (signal) throw new Error("Home Assistant data could not be read. Check the connection and retry.");
            throw error;
        }
    };
}
