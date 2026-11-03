import {money, when} from "./display.js";

const names = {energy_price_forecast_eu: "EnergyPriceForecast EU", epex_predictor: "EpexPredictor", jeroen_nl: "Jeroen.nl", nord_pool: "Nord Pool"};
const name = source => names[source] || source;
const reasons = {
    insufficient_evidence: "Collecting comparable forecast history",
    retained_within_threshold: "Preference retained: Improvement threshold not met",
    challenger_pending: "A better challenger must qualify on consecutive daily assessments",
    switched_after_three_days: "Changed after qualifying daily assessments",
    available_source_fallback: "Preferred coverage unavailable: Using another available forecast",
    published_prices: "Published market prices used",
    available_forecast: "Available forecast used",
    no_forecast_coverage: "Forecast coverage unavailable",
    manual_preference: "Manual forecast preference applied",
};
const CENTS_PER_EURO = 100;
const RETAIN_SAVED_SOURCE = "__retain_saved_source__";

/** Keep provider text out of HTML parsing, including diagnostics and source names. */
function text(tag, value, parent) {
    const node = document.createElement(tag);
    node.textContent = value;
    parent.append(node);
    return node;
}

/** Own only the price-source draft. The page's shared SaveBar owns persistence actions. */
export class PriceSourceSettings {
    constructor(changed) {
        this.changed = changed;
        this.controls = [];
        this.baseline = null;
        this.version = null;
        this.area = null;
        this.latest = null;
    }
    get values() { return this.controls.map(control => control.value === RETAIN_SAVED_SOURCE ? control.dataset.retainedSource : control.value || null); }
    get dirty() { return this.baseline !== null && JSON.stringify(this.values) !== this.baseline; }
    get ready() { return this.controls.length > 0; }
    /** Preserve focus and unsaved choices while read-only polling refreshes quality evidence.
     * Disable refreshStatus only for draft restoration, which must not revive cached status
     * after a failed poll. Confirmed saves and fresh reads always render their own status.
     */
    update(prices, discard = false, refreshStatus = true) {
        this.latest = prices;
        if (refreshStatus) renderQuality(prices);
        const settings = prices.source_preferences;
        const container = document.getElementById("price-source-controls");
        if (!container) return;
        if (!settings?.available_sources?.length) {
            if (!this.dirty || discard) {
                container.replaceChildren(); this.controls = []; this.baseline = null;
                this.area = null; this.version = null;
            }
            return;
        }
        if (!this.dirty || discard) {
            const changedArea = this.area !== prices.market_area;
            this.area = prices.market_area;
            this.version = settings.configuration_version;
            const stored = settings.stored_preferences || settings.preferences || [];
            const inactive = stored.map(source => source && !settings.available_sources.includes(source) ? source : null);
            const choices = JSON.stringify([settings.available_sources, settings.source_horizons_hours, inactive]);
            if (!this.ready || changedArea || this.choices !== choices) {
                container.replaceChildren(); this.controls = []; this.choices = choices;
                for (const row of prices.forecast_selection || []) {
                    const label = text("label", `Forecast preference: ${row.lead_start_hours}–${row.lead_end_hours} hours ahead`, container);
                    const select = document.createElement("select");
                    select.id = `price-source-${this.controls.length}`;
                    select.setAttribute("aria-label", label.textContent);
                    select.setAttribute("form", "wizard");
                    select.dataset.pricePreference = "true";
                    select.setAttribute("data-view-control", "");
                    const retained = inactive[this.controls.length];
                    if (retained) {
                        const option = text("option", "Automatic: Keep saved preference", select);
                        option.value = RETAIN_SAVED_SOURCE; option.disabled = true;
                        select.dataset.retainedSource = retained;
                    }
                    for (const [value, title] of [["", "Automatic"], ...settings.available_sources.map(source => [source, name(source)])]) {
                        const horizon = settings.source_horizons_hours?.[value];
                        const unsupported = value && horizon != null && row.lead_end_hours > horizon;
                        const option = text("option", unsupported ? `${title}: Up to ${horizon} hours` : title, select);
                        option.value = value; option.disabled = Boolean(unsupported);
                    }
                    select.onchange = () => this.changed();
                    label.append(select); this.controls.push(select);
                    if (retained) text("small", `The saved source ${name(retained)} is not supported in this market. Automatic applies until replaced.`, label);
                }
            }
            this.controls.forEach((control, index) => { control.value = inactive[index] ? RETAIN_SAVED_SOURCE : settings.preferences[index] || ""; });
            this.baseline = JSON.stringify(this.values);
        }
    }
    /** Write against the captured area and version, then confirm only the server response. */
    async save(api) {
        const result = await api("/v1/prices/preferences", "PUT", {preferences: this.values, configuration_version: this.version, market_area: this.area});
        this.update(result, true);
    }
    /** Restore only source preferences. Cached status must not replace a later polling failure. */
    discard() { if (this.latest) this.update(this.latest, true, false); }
}

/** Present recommendations separately from actual/manual use and pair-specific quality evidence.
 * Read chronological entries from now and stop published Nord Pool coverage at the first
 * forecast, other source or gap. Update coverage, replace source rows while preserving open
 * details, then replace provenance. Without #price-source-list, skip rows and provenance.
 */
function renderQuality(prices) {
    const source = document.getElementById("price-published-source");
    if (source) {
        const route = prices.published_source;
        source.hidden = !route;
        const routeReasons = {
            discovering: "Checking Home Assistant configuration",
            ready: "Compatible Home Assistant integration available",
            not_configured: "No Home Assistant Nord Pool integration configured",
            not_loaded: "Home Assistant Nord Pool integration unavailable",
            incompatible: "Home Assistant prices do not match the contract area and currency",
            ambiguous: "More than one compatible Home Assistant Nord Pool integration",
            connection_stale: "Home Assistant connection unavailable",
            waiting_for_publication: "Waiting for Home Assistant to receive published prices",
            request_failed: "Price request failed: Retrying automatically with cached prices retained",
        };
        const selected = route?.selected === "home_assistant_nord_pool" ? "Home Assistant Nord Pool"
            : route?.selected === "nord_pool_direct" ? "Nord Pool directly" : "Checking Home Assistant";
        if (route) {
            const message = `Published source: Automatic: ${selected}: ${routeReasons[route.reason] || "Checking source availability"}`;
            if (source.textContent !== message) source.textContent = message;
        }
    }
    const coverage = document.getElementById("price-published-coverage");
    if (coverage) {
        let publishedUntil = null;
        for (const entry of prices.entries || []) {
            if (entry.kind !== "published" || entry.source_id !== "nord_pool"
                || (publishedUntil !== null && Date.parse(entry.start) !== Date.parse(publishedUntil))) break;
            publishedUntil = entry.end;
        }
        coverage.textContent = publishedUntil
            ? `Known prices from now until ${when(publishedUntil)}. Forecasts fill only unpublished hours.`
            : "No published Nord Pool prices available from now. Forecasts fill unpublished hours.";
    }
    const container = document.getElementById("price-source-list");
    if (!container) return;
    const expanded = [...container.children].map(item => item.querySelector("details")?.open);
    container.replaceChildren();
    for (const row of prices.forecast_selection || []) {
        const item = document.createElement("li");
        text("h3", `${row.lead_start_hours}–${row.lead_end_hours} hours ahead`, item);
        const active = [...(row.published_prices_used ? ["Nord Pool (published)"] : []), ...(row.active_sources || []).map(name)];
        text("p", `Active: ${active.join(", ") || "Unavailable"}`, item);
        text("p", `Mode: ${row.selection_mode === "manual" ? "Manual" : "Automatic"}: Preference: ${name(row.preferred_source) || "Unavailable"}`, item);
        text("p", `Automatic recommendation: ${name(row.recommended_source || row.preferred_source) || "Collecting evidence"}`, item);
        text("p", reasons[row.usage_reason] || "Waiting for prices", item);
        if (row.reason && row.reason !== row.usage_reason) text("p", `Automatic assessment: ${reasons[row.reason] || row.reason}`, item);
        if (row.challenger) text("p", `Challenger: ${name(row.challenger)}: ${row.streak}/${prices.selection_policy?.consecutive_days ?? "?"} daily assessments`, item);
        const comparisons = row.comparisons || [{primary_source: row.primary_source, alternative_source: row.alternative_source, ...row}];
        const detail = document.createElement("details"); text("summary", "Forecast quality: Matched original predictions", detail);
        detail.open = expanded[container.children.length] || false;
        for (const pair of comparisons) {
            const group = document.createElement("dl"); group.className = "settings-summary";
            for (const [title, value] of [
                [name(pair.primary_source) || "Primary", pair.primary_mae == null ? "Not yet measured" : `${money(pair.primary_mae * CENTS_PER_EURO)} ct/kWh MAE`],
                [name(pair.alternative_source) || "Alternative", pair.alternative_mae == null ? "Not yet measured" : `${money(pair.alternative_mae * CENTS_PER_EURO)} ct/kWh MAE`],
                ["Matched hours", pair.matched_hours ?? 0], ["Settled days", pair.settled_days ?? 0],
                ["Assessed", pair.assessed_on ? `${pair.assessed_on} UTC` : "Not yet assessed"],
            ]) { const entry = document.createElement("div"); text("dt", title, entry); text("dd", String(value), entry); group.append(entry); }
            detail.append(group);
        }
        item.append(detail);
        if (row.selection_issue) text("p", "Forecast comparison storage unavailable: The preference may be defaulted after restart", item);
        container.append(item);
    }
    const provenance = document.getElementById("price-source-provenance");
    if (provenance) {
        const expandedSources = new Set([...provenance.querySelectorAll("details[open]")].map(detail => detail.dataset.source));
        provenance.replaceChildren();
        text("p", "Available forecasts can fill unpublished hours. Actual use is shown in the horizon blocks below.", provenance);
        const sources = text("ul", "", provenance);
        sources.className = "price-source-status";
        const sourceStatuses = new Map((prices.source_status || []).map(source => [source.source_id, source]));
        const sourceIds = new Set([...(prices.source_preferences?.available_sources || []), ...sourceStatuses.keys()]);
        if (!sourceIds.size) text("p", "No forecast source status available.", provenance);
        for (const sourceId of sourceIds) {
            const source = sourceStatuses.get(sourceId) || {source_id: sourceId, available: false};
            const item = text("li", "", sources);
            const identity = text("div", "", item);
            text("strong", name(source.source_id), identity);
            text("small", source.available ? "Available" : "Unavailable", identity);
            const dates = text("dl", "", item);
            for (const [label, value] of [
                ["Forecast issued", source.issued_at ? when(source.issued_at) : "Not known"],
                ["Last forecast interval ends", source.covered_until ? when(source.covered_until) : "Not known"],
            ]) {
                const field = text("div", "", dates);
                text("dt", label, field);
                text("dd", value, field);
            }
            const details = text("details", "", item);
            details.dataset.source = source.source_id;
            details.open = expandedSources.has(source.source_id);
            text("summary", "Details", details).setAttribute("aria-label", `Details: ${name(source.source_id)}`);
            text("p", `Received by OEP: ${source.received_at ? when(source.received_at) : "Not known"}`, details);
            text("p", "Received is when OEP ingested the forecast. The last interval does not guarantee uninterrupted coverage.", details);
            if (!source.issued_at) text("p", "No forecast issue time is available from this source.", details);
        }
        for (const [source, issue] of Object.entries(prices.issues || {})) text("p", `${name(source)}: ${issue}`, provenance);
    }
}
