/** Keep navigation mounted while the browser owns each content document and its lifecycle. */
import {bindSettingsActions, closeSettingsNavigation} from "./settings-actions.js";
bindSettingsActions();

const frame = document.getElementById("workspace-content");
const base = new URL(document.querySelector('meta[name="oep-api-base"]').content, location.href);
let focusHeading = false;

/** Forward outside clicks and Escape without propagating other content events into the shell. */
function dismissMenu(event) {
    if (event.type === "click" || event.key === "Escape") closeSettingsNavigation(event.type === "keydown");
}

/** Reflect only the loaded document, so cancelled navigation never selects another page. */
function loaded() {
    const content = frame.contentDocument;
    if (!content?.querySelector(".workspace-main")) return;
    const current = new URL(frame.contentWindow.location.href);
    document.title = content.title;
    const active = content.querySelector('.sidebar a[aria-current="page"]');
    for (const link of document.querySelectorAll(".sidebar a")) {
        if (active && new URL(link.href).pathname === new URL(active.href).pathname && link.hash === active.hash) link.setAttribute("aria-current", "page");
        else link.removeAttribute("aria-current");
    }
    const address = new URL(location.href);
    address.search = "";
    address.hash = "";
    address.searchParams.set("page", current.pathname.slice(base.pathname.length - 1) + current.hash);
    history.replaceState(null, "", address);
    if (focusHeading) {
        const heading = content.querySelector("h1");
        heading?.setAttribute("tabindex", "-1");
        heading?.focus({preventScroll:true});
        focusHeading = false;
    }
}

/** Attach hash tracking to each new document without reading an inaccessible error document. */
function contentLoaded() {
    loaded();
    if (frame.contentDocument) {
        frame.contentWindow.addEventListener("hashchange", loaded);
        frame.contentDocument.addEventListener("click", dismissMenu);
        frame.contentDocument.addEventListener("keydown", dismissMenu);
    }
}
frame.addEventListener("load", contentLoaded);

document.querySelector(".sidebar").addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const target = new URL(link.href);
    if (target.origin !== base.origin || !target.pathname.startsWith(base.pathname)) return;
    event.preventDefault();
    target.searchParams.set("embedded", "1");
    focusHeading = event.detail === 0;
    const current = frame.contentDocument?.URL;
    if (current && target.href.split("#")[0] === current.split("#")[0] && frame.contentDocument.readyState === "complete") {
        frame.contentWindow.stop();
        if (target.href === current) loaded();
        else frame.contentWindow.location.hash = target.hash;
    }
    else frame.src = target.href;
});

// Resolve legacy root fragments before the first content navigation to avoid flashing Planning.
const initialPage = new URL(frame.dataset.startPage, location.href);
if (initialPage.pathname === base.pathname + "overview" && !initialPage.hash && ["#planning", "#savings"].includes(location.hash)) initialPage.hash = location.hash;
frame.src = initialPage.href;
