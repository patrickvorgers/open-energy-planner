/** Keep source identity, rather than display names, authoritative across all appliance categories. */
export function assignedEnergySources(consumers) {
    if (consumers.issue === "consumer_configuration_unreadable") return new Set(consumers.options.map(source => source.entity_id));
    return new Set([...consumers.selected.map(source => source.entity_id), ...consumers.charging_meters, ...consumers.thermal_meters]);
}

/** Reuse one Energy-device chooser across consumption, charging and electric-boiler settings. */
export function pickEnergySource(dialog, consumers, title) {
    const added = assignedEnergySources(consumers);
    const unavailable = consumers.issue === "consumer_configuration_unreadable";
    dialog.classList.add("source-picker");
    dialog.replaceChildren(document.createElement("div"));
    const body = dialog.firstElementChild;
    body.id = "source-dialog-body";
    body.innerHTML = '<header><h2 id="source-title"></h2><p>Choose a source from Home Assistant Energy.</p><div class="source-filters"><label>Search devices<input id="source-search" type="search" placeholder="Device name or sensor ID"></label><label>Show<select id="source-filter" aria-label="Show"><option value="available">Available</option><option value="added">Already added</option><option value="all">All sources</option></select></label></div><p id="source-count" role="status"></p></header><div id="source-results" class="source-results" role="region" aria-label="Energy sources" tabindex="0"></div><footer><p>Missing a device? Add its energy meter and device power consumption in Home Assistant Energy first.</p><div class="dialog-actions"><button type="button" id="close-sources">Cancel</button></div></footer>';
    const find = id => body.querySelector(`#${id}`);
    find("source-title").textContent = title;
    let selected = null;
    /** Filter only display names and source identities, leaving assigned sources disabled in every category. */
    function render() {
        const search = find("source-search").value.trim().toLowerCase(), filter = find("source-filter").value;
        const results = find("source-results"); results.replaceChildren(); let count = 0;
        for (const source of consumers.options) {
            const used = added.has(source.entity_id);
            if ((filter === "available" && used) || (filter === "added" && !used) || !`${source.name} ${source.entity_id}`.toLowerCase().includes(search)) continue;
            count += 1;
            const button = document.createElement("button"), name = document.createElement("strong"), state = document.createElement("small");
            button.type = "button"; button.className = "source-choice"; button.title = source.entity_id; button.disabled = used;
            name.textContent = source.name; state.textContent = unavailable ? "Ownership unavailable" : used ? "Already added" : "Available"; button.append(name, state);
            button.onclick = () => { selected = source; dialog.close(); };
            results.append(button);
        }
        if (!count) { const empty = document.createElement("p"); empty.textContent = "No matching devices. Try another search or change the filter."; results.append(empty); }
        find("source-count").textContent = unavailable ? "Device assignments could not be read. Restore the saved database and reload before adding devices." : `${count} of ${consumers.options.length} sources shown. Devices already added in any appliance category cannot be selected again.`;
        results.scrollTop = 0;
    }
    find("source-search").oninput = render; find("source-filter").onchange = render; find("close-sources").onclick = () => dialog.close();
    render();
    return new Promise(resolve => {
        dialog.addEventListener("close", () => resolve(selected), {once: true});
        dialog.showModal(); find("source-search").focus();
    });
}
