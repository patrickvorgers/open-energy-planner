/** Keep each device editor's unsaved settings when changing appliance categories. */
export function bindApplianceSettingsNavigation(saveState) {
    document.querySelector('nav[aria-label="Appliance settings"]')?.addEventListener("click", event => {
        if (!event.target.closest("a[href]") || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
        if (saveState.busy || saveState.dirty) {
            event.preventDefault();
            saveState.update("Save or discard your changes before opening another appliance category.");
        }
    });
}
