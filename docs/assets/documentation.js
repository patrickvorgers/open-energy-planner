/* Keep document reading independent of the optional menu and local search. */
const script = document.currentScript;
const root = new URL(script.dataset.root.replace(/\/?$/, "/"), location.href);
const menu = document.getElementById("menu-toggle");
const navigation = document.getElementById("site-navigation");
menu.hidden = false;
navigation.classList.add("menu-enabled");
function closeMenu() {
    navigation.classList.remove("menu-open");
    menu.setAttribute("aria-expanded", "false");
}
menu.addEventListener("click", () => {
    const open = navigation.classList.toggle("menu-open");
    menu.setAttribute("aria-expanded", String(open));
});
document.addEventListener("keydown", event => {
    if (event.key === "Escape" && navigation.classList.contains("menu-open")) {
        closeMenu();
        menu.focus();
    }
});
const input = document.getElementById("search-query");
const status = document.getElementById("search-status");
const panel = document.getElementById("search-panel");
const results = document.getElementById("search-results");
document.querySelector(".search-form").addEventListener("submit", event => event.preventDefault());
document.getElementById("search-close").addEventListener("click", () => {
    panel.hidden = true;
    input.value = "";
    document.getElementById("content").focus();
});
if (window.Worker) {
    const worker = new Worker(new URL("search/worker.js", root));
    worker.postMessage({init: true});
    worker.addEventListener("error", () => {
        status.textContent = "Search unavailable. Browse the guide below.";
        input.disabled = true;
    });
    worker.addEventListener("message", event => {
        if (event.data.allowSearch) {
            input.disabled = false;
            status.textContent = "Search stays in your browser.";
        }
        if (event.data.results) {
            results.replaceChildren();
            const matches = event.data.results.slice(0, 12);
            status.textContent = matches.length ? `${matches.length} matching sections` : "No matches. Try fewer words.";
            panel.hidden = !input.value.trim();
            for (const match of matches) {
                const article = document.createElement("article");
                const heading = document.createElement("h3");
                const link = document.createElement("a");
                link.href = new URL(match.location, root);
                link.textContent = match.title;
                link.addEventListener("click", closeMenu);
                heading.append(link);
                const summary = document.createElement("p");
                summary.textContent = match.summary;
                article.append(heading, summary);
                results.append(article);
            }
            if (!matches.length) results.textContent = "No matching sections. Try installation, contract or first plan.";
        }
    });
    input.addEventListener("input", () => {
        if (!input.value.trim()) {
            panel.hidden = true;
            return;
        }
        const words = input.value.match(/[\p{L}\p{N}]+/gu);
        if (words) worker.postMessage({query: words.join(" ")});
    });
    document.querySelector(".search-form").addEventListener("submit", () => {
        closeMenu();
        if (panel.hidden) return;
        document.getElementById("search-heading").scrollIntoView();
        document.getElementById("search-close").focus();
    });
} else {
    status.textContent = "Search unavailable. Browse the guide below.";
}
