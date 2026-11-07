/** Explicit source reads poll only request progress, never schedule background metadata audits. */
const SOURCE_WAIT_MS = 65000;
const SOURCE_STATUS_MS = 1000;

/** Rebuild source options while retaining draft input text, invalid values and keyboard focus. */
export function retainInputs(render) {
    const controls = () => [...document.querySelectorAll("input[data-field], input[data-hour]")];
    const previous = controls(), values = previous.map(input => input.value);
    const focused = document.activeElement, focusIndex = previous.indexOf(focused);
    const field = focused?.dataset.field, fieldIndex = field ? [...document.querySelectorAll("[data-field]")].filter(control => control.dataset.field === field).indexOf(focused) : -1;
    render();
    const replacement = controls();
    replacement.forEach((input, index) => { if (index < values.length) input.value = values[index]; });
    if (focused?.isConnected) focused.focus({preventScroll: true});
    else if (focusIndex >= 0) replacement[focusIndex]?.focus({preventScroll: true});
    else if (fieldIndex >= 0) [...document.querySelectorAll("[data-field]")].filter(control => control.dataset.field === field)[fieldIndex]?.focus({preventScroll: true});
}

/** Wait for one deliberate HA read, retaining the last accepted choices on failure. */
export function sourceRefresh(api, scope, button, status, apply) {
    let busy = false;
    return async () => {
        if (busy) return;
        busy = true; button.disabled = true;
        status.textContent = "Refreshing Home Assistant sources. Your edits are retained.";
        try {
            const path = scope ? `v1/ha/source-choices/${scope}` : "v1/ha/catalogue/refresh";
            let result = await api(path, {});
            const deadline = Date.now() + SOURCE_WAIT_MS;
            while (result.pending && Date.now() < deadline) {
                await new Promise(resolve => setTimeout(resolve, SOURCE_STATUS_MS));
                result = await api(path);
            }
            if (result.pending || result.failed) throw new Error("Home Assistant sources could not be refreshed. Your edits are retained. Check the integration connection and choose Refresh Home Assistant sources to retry.");
            await apply(result.catalogue);
            status.textContent = "Home Assistant sources refreshed. Your edits are retained.";
        } catch (error) { status.textContent = error.message; }
        finally { busy = false; button.disabled = false; }
    };
}
