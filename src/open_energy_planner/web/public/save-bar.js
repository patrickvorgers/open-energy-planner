/** One save state and floating action pattern for editable configuration pages. */
import {confirmAction} from "./confirmation.js";

const MODIFIED_CLICK_KEYS = ["ctrlKey", "metaKey", "shiftKey", "altKey"];

/** Compare each editable control's persisted value, including precise percentage drafts. */
function readForm(form) {
    return [...form.elements]
        .filter(control => ["INPUT", "SELECT", "TEXTAREA"].includes(control.tagName))
        .map(control => [
            control.name || control.id || control.dataset.field || "",
            control.type,
            control.type === "checkbox" ? control.checked : control.dataset.precisePercentage ?? control.value,
        ]);
}

/** Leave modified clicks, downloads and other browsing contexts to the browser. */
function navigationLink(event) {
    const link = event.target.closest("a[href]");
    if (!link || event.defaultPrevented || event.button !== 0) return null;
    if (MODIFIED_CLICK_KEYS.some(key => event[key])) return null;
    if (link.hasAttribute("download")) return null;
    return link.target && link.target !== "_self" ? null : link;
}

/** Guard document navigation inside the app base. In-page fragments retain their draft. */
function appDestination(link) {
    const destination = new URL(link.href);
    const base = new URL(document.querySelector('meta[name="oep-api-base"]').content, location.href);
    if (destination.origin !== base.origin || !destination.pathname.startsWith(base.pathname)) return null;
    const localFragment = destination.href.includes("#") && destination.href.split("#")[0] === location.href.split("#")[0];
    return localFragment ? null : destination;
}

/** Let the loaded document guard app navigation without sharing its draft with the workspace shell. */
export function guardNavigation(navigate, content = document) {
    return !content.dispatchEvent(new CustomEvent("oep-before-navigation", {cancelable: true, detail: {navigate}}));
}

export class SaveBar {
    /** Bind persistence state, custom app navigation consent and native browser unload protection. */
    constructor({form, bar, save, discard, status, canSave = () => true, read}) {
        this.form = document.getElementById(form);
        this.bar = document.getElementById(bar);
        this.save = document.getElementById(save);
        this.discard = document.getElementById(discard);
        this.status = document.getElementById(status);
        this.canSave = canSave;
        this.read = read || (() => readForm(this.form));
        this.ready = false;
        this.busy = false;
        this.activity = "";
        this.locked = new Map();
        this.baseline = null;
        this.detail = "";
        this.navigationPending = false;
        this.allowUnload = false;
        this.configureBar();
        this.bindNavigation();
        this.watchForm();
        this.update();
    }

    /** Preserve space for the bar and expose its live persistence status. */
    configureBar() {
        this.bar.classList.add("save-bar");
        document.body.classList.add("has-save-bar");
        new ResizeObserver(() => {
            document.body.style.setProperty("--save-bar-height", `${this.bar.getBoundingClientRect().height}px`);
        }).observe(this.bar);
        this.bar.setAttribute("aria-label", "Save changes");
        this.status.setAttribute("role", "status");
        this.status.setAttribute("aria-live", "polite");
        this.discard.textContent = "Discard changes";
    }

    /** Keep native unload protection separate from app navigation consent. */
    bindNavigation() {
        window.addEventListener("beforeunload", event => {
            if (this.allowUnload) { this.allowUnload = false; return; }
            if (this.busy || this.dirty) { event.preventDefault(); event.returnValue = ""; }
        });
        document.addEventListener("oep-before-navigation", event => {
            if (!this.busy && !this.dirty) return;
            event.preventDefault();
            this.leave(event.detail.navigate);
        });
        document.addEventListener("click", event => {
            const link = navigationLink(event);
            if (!link) return;
            const destination = appDestination(link);
            if (!destination) return;
            if (guardNavigation(() => {
                if (!link.isConnected || link.href !== destination.href) return false;
                location.assign(destination.href);
            })) event.preventDefault();
        });
    }

    /** Clear obsolete status detail when this form changes and report native validation failures. */
    watchForm() {
        const edit = event => {
            if (event.target.form !== this.form) return;
            this.detail = "";
            this.update();
        };
        document.addEventListener("input", edit);
        document.addEventListener("change", edit);
        this.form.addEventListener("invalid", () => {
            this.detail = "Complete the highlighted fields."; this.update();
        }, true);
    }
    /** Compare the current draft with confirmed persistence, only after loading. */
    get dirty() { return this.ready && JSON.stringify(this.read()) !== this.baseline; }
    /** Ask once for a stable draft. Consent permits only the immediate app navigation, never a later browser unload. */
    async leave(navigate) {
        if (this.busy) { this.update("Wait for the current save or refresh to finish before leaving this page."); return; }
        if (this.navigationPending) return;
        this.navigationPending = true;
        const draft = JSON.stringify(this.read());
        try {
            const accepted = await confirmAction({title: "Leave without saving?", message: "Your unsaved changes on this page will be discarded. Saved settings will stay unchanged.", confirmLabel: "Discard and leave"});
            if (!accepted) return;
            if (this.busy || JSON.stringify(this.read()) !== draft) { this.update("Settings changed while confirmation was open. Review your changes before leaving."); return; }
            this.allowUnload = true;
            try { if (navigate() === false) this.allowUnload = false; }
            catch (error) { this.allowUnload = false; throw error; }
            finally { setTimeout(() => { this.allowUnload = false; }, 0); }
        } finally { this.navigationPending = false; }
    }
    /** Capture confirmed state. False leaves a null baseline. Lock newly rendered controls during reload. */
    loaded(saved = true) {
        this.ready = true;
        this.baseline = saved ? JSON.stringify(this.read()) : null;
        this.detail = "";
        if (this.busy) this.lock();
        this.update();
    }
    /** Render state and failure detail without changing the persistence baseline. */
    update(detail) {
        if (detail !== undefined) this.detail = detail;
        const [state, message] = this.presentation();
        this.bar.dataset.saveState = state;
        this.status.textContent = [message, this.detail].filter(Boolean).join(" ");
        this.save.disabled = state !== "dirty" || !this.canSave();
        this.discard.disabled = state !== "dirty";
    }
    /** Decide the bar state once, with busy work taking precedence over draft changes. */
    presentation() {
        if (this.busy) return ["saving", this.activity];
        if (!this.ready) return ["loading", "Load settings to edit."];
        return this.dirty ? ["dirty", "Unsaved changes."] : ["saved", "All changes saved."];
    }
    /** Remember each control's disabled state once, including controls rebuilt by a reload. */
    lock() {
        for (const control of this.form.elements) {
            if (!this.locked.has(control)) this.locked.set(control, control.disabled);
            control.disabled = true;
        }
    }
    /** Lock editing for a write/reload. Every begin must be paired with end in finally. */
    begin(activity = "Saving changes…") {
        this.busy = true;
        this.activity = activity;
        this.detail = "";
        this.locked.clear();
        this.lock();
        this.update();
    }
    /** Restore captured disabled states, overriding interim disabled edits, then recompute action availability. */
    end() {
        for (const [control, disabled] of this.locked) if (control.isConnected) control.disabled = disabled;
        this.busy = false;
        this.update();
    }
    /** Ignore busy refreshes. Refuse dirty drafts unless discard is explicit. Keep failures by the actions. */
    async refresh(load, {discard = false} = {}) {
        if (this.busy) return;
        if (this.dirty && !discard) { this.update("Save or discard your changes before reloading settings."); return; }
        this.begin(discard ? "Discarding changes…" : "Refreshing settings…");
        try { await load(); } catch (error) { this.update(error.message); }
        finally { this.end(); }
    }
}
