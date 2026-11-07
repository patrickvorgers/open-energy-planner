/** Explicit source reads poll only request progress, never schedule background metadata audits. */
const SOURCE_WAIT_MS = 65000;
const SOURCE_STATUS_MS = 1000;

/** Rebuild source options while retaining draft input text, invalid values and keyboard focus. */
export function retainInputs(render) {
    const controls = () => [...document.querySelectorAll("input[data-field], input[data-hour]")];
    const previous = controls(), values = previous.map(input => input.value);
    const focused = document.activeElement, focusIndex = previous.indexOf(focused);
    const field = focused?.dataset.field, fieldIndex = field ? [...document.querySelectorAll("[data-field]")].filter(control => control.dataset.field === field).indexOf(focused) : -1;
    const handlers = focusIndex >= 0 ? [focused.onfocus, focused.onblur] : null;
    if (handlers) { focused.onfocus = null; focused.onblur = null; }
    try {
        render();
        const replacement = controls();
        // Keep the native input itself: incomplete numbers and their caret cannot be read through .value.
        if (focusIndex >= 0 && replacement[focusIndex] && replacement[focusIndex] !== focused) replacement[focusIndex].replaceWith(focused);
        replacement.forEach((input, index) => { if (index < values.length && index !== focusIndex) input.value = values[index]; });
        if (focused?.isConnected) focused.focus({preventScroll: true});
        else if (fieldIndex >= 0) [...document.querySelectorAll("[data-field]")].filter(control => control.dataset.field === field)[fieldIndex]?.focus({preventScroll: true});
    } finally {
        if (handlers) [focused.onfocus, focused.onblur] = handlers;
    }
}

/** Wait for one deliberate HA read, retaining the last accepted choices on failure. */
export function sourceRefresh(api, scope, button, status, apply) {
    let busy = false;
    return async () => {
        if (busy) return;
        busy = true; button.disabled = true;
        status.textContent = "Refreshing Home Assistant sources. Your edits are retained.";
        try {
            const signal = AbortSignal.timeout(SOURCE_WAIT_MS);
            const path = scope ? `v1/ha/source-choices/${scope}` : "v1/ha/catalogue/refresh";
            let result = await api(path, {}, signal);
            while (result.pending) {
                await new Promise(resolve => setTimeout(resolve, SOURCE_STATUS_MS));
                signal.throwIfAborted();
                result = await api(path, undefined, signal);
            }
            signal.throwIfAborted();
            if (result.failed || (scope && !result.catalogue)) throw new Error();
            const catalogue = await apply(result.catalogue, signal) ?? result.catalogue;
            signal.throwIfAborted();
            status.textContent = catalogue?.entities?.length
                ? "Home Assistant sources refreshed. Your edits are retained."
                : "Home Assistant returned no sources. Your edits are retained. Check the integration and configured sources.";
        } catch { status.textContent = "Home Assistant sources could not be refreshed. Your edits are retained. Check the integration connection and choose Refresh Home Assistant sources to retry."; }
        finally { busy = false; button.disabled = false; }
    };
}
