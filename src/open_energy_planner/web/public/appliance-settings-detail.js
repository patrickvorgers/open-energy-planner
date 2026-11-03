/** Render saved, type-specific configuration with the shared Settings presentation. */
export function settingsSummary(parent, sections) {
    parent.replaceChildren();
    for (const [heading, facts] of sections) {
        const group = document.createElement("section"), title = document.createElement("h3"), list = document.createElement("dl");
        group.className = "settings-facts-group"; title.textContent = heading; list.className = "settings-summary";
        for (const [label, value, code = false] of facts) {
            const row = document.createElement("div"), term = document.createElement("dt"), description = document.createElement("dd"), text = document.createElement(code ? "code" : "span");
            term.textContent = label; text.textContent = value ?? "Unavailable";
            description.append(text); row.append(term, description); list.append(row);
        }
        group.append(title, list); parent.append(group);
    }
}
