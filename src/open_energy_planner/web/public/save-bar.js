/** One save state and floating action pattern for editable configuration pages. */
export class SaveBar {
    /** Bind form/actions and JSON-serializable draft readers. Request native unload confirmation while busy or dirty. */
    constructor({form, bar, save, discard, status, canSave = () => true, read}) {
        this.form = document.getElementById(form);
        this.bar = document.getElementById(bar);
        this.save = document.getElementById(save);
        this.discard = document.getElementById(discard);
        this.status = document.getElementById(status);
        this.canSave = canSave;
        this.read = read || (() => [...this.form.elements]
            .filter((control) => ["INPUT", "SELECT", "TEXTAREA"].includes(control.tagName))
            .map((control) => [control.name || control.id || control.dataset.field || "", control.type, control.type === "checkbox" ? control.checked : control.dataset.precisePercentage ?? control.value]));
        this.ready = false;
        this.busy = false;
        this.activity = "";
        this.locked = new Map();
        this.baseline = null;
        this.detail = "";
        this.bar.classList.add("save-bar");
        document.body.classList.add("has-save-bar");
        new ResizeObserver(() => {
            document.body.style.setProperty("--save-bar-height", `${this.bar.getBoundingClientRect().height}px`);
        }).observe(this.bar);
        this.bar.setAttribute("aria-label", "Save changes");
        this.status.setAttribute("role", "status");
        this.status.setAttribute("aria-live", "polite");
        this.discard.textContent = "Discard changes";
        window.addEventListener("beforeunload", event => {
            if (this.busy || this.dirty) { event.preventDefault(); event.returnValue = ""; }
        });
        document.addEventListener("input", (event) => {
            if (event.target.form === this.form) { this.detail = ""; this.update(); }
        });
        document.addEventListener("change", (event) => {
            if (event.target.form === this.form) { this.detail = ""; this.update(); }
        });
        this.form.addEventListener("invalid", () => {
            this.detail = "Complete the highlighted fields."; this.update();
        }, true);
        this.update();
    }
    /** Compare the current draft with confirmed persistence, only after loading. */
    get dirty() { return this.ready && JSON.stringify(this.read()) !== this.baseline; }
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
        const state = this.busy ? this.activity : !this.ready ? "Load settings to edit." : this.dirty ? "Unsaved changes." : "All changes saved.";
        this.bar.dataset.saveState = this.busy ? "saving" : !this.ready ? "loading" : this.dirty ? "dirty" : "saved";
        this.status.textContent = [state, this.detail].filter(Boolean).join(" ");
        this.save.disabled = this.busy || !this.ready || !this.dirty || !this.canSave();
        this.discard.disabled = this.busy || !this.ready || !this.dirty;
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
