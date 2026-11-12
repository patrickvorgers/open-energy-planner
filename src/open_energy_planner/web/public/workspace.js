/** Keep navigation mounted while the browser owns each content document and its lifecycle. */
import {bindSettingsActions, closeSettingsNavigation} from "./settings-actions.js";
import {guardNavigation} from "./save-bar.js";
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
    // Attach after the child view handler so hash navigation reads its updated selection.
    if (frame.contentDocument?.readyState !== "complete") return;
    loaded();
    if (frame.contentDocument) {
        frame.contentWindow.addEventListener("hashchange", loaded);
        frame.contentDocument.addEventListener("click", dismissMenu);
        frame.contentDocument.addEventListener("keydown", dismissMenu);
    }
}
frame.addEventListener("load", contentLoaded);

document.addEventListener("click", event => {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (!link.closest(".sidebar") && link.target !== frame.name) return;
    if (link.hasAttribute("download") || (link.target && link.target !== "_self" && link.target !== frame.name)) return;
    const href = link.href, target = new URL(href);
    if (target.origin !== base.origin || !target.pathname.startsWith(base.pathname)) return;
    event.preventDefault();
    target.searchParams.set("embedded", "1");
    const current = frame.contentDocument?.URL;
    if (current && target.href.split("#")[0] === current.split("#")[0] && frame.contentDocument.readyState === "complete") {
        focusHeading = event.detail === 0;
        frame.contentWindow.stop();
        if (target.href === current) loaded();
        else frame.contentWindow.location.hash = target.hash;
    }
    else {
        const content = frame.contentDocument;
        const navigate = () => {
            if (content !== frame.contentDocument || !link.isConnected || link.href !== href) return false;
            focusHeading = event.detail === 0;
            frame.src = target.href;
        };
        if (!content || !guardNavigation(navigate, content)) navigate();
    }
});

// The browser starts content immediately. Reconcile legacy root fragments without reloading normal navigation.
const initialPage = new URL(frame.src, location.href);
if (initialPage.pathname === base.pathname + "overview" && !initialPage.hash && ["#planning", "#savings"].includes(location.hash)) initialPage.hash = location.hash;
if (frame.src !== initialPage.href) frame.src = initialPage.href;
else contentLoaded();
document.querySelector(".sidebar").removeAttribute("inert");
document.querySelector(".sidebar").removeAttribute("aria-busy");
