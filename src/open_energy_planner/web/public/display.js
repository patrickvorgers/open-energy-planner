/** Shared presentation only: preserve calculation precision and use the HA installation timezone. */
export const MONEY_DECIMALS = 2;
const SOURCE_NAMES_REFRESH_MS = 30000;
/** Refresh read-only views after a metadata rename, preserving their selected plan/day. */
export function watchSourceNames(readSession, refresh, revision) {
    let busy = false;
    const check = async () => {
        if (busy || document.hidden) return;
        busy = true;
        try {
            const session = await readSession();
            if (revision !== undefined && session.source_names_revision !== revision && await refresh() !== true) return;
            revision = session.source_names_revision;
        } catch { /* Keep the last confirmed labels and retry after transient read failures. */ }
        finally { busy = false; }
    };
    check();
    setInterval(check, SOURCE_NAMES_REFRESH_MS);
}
/** Name an Energy source without exposing its technical ID as a default label. */
export function sourceName(metadata, fallback) {
    for (const key of ["energy_name", "device_name", "name"]) {
        const value = metadata?.[key];
        if (typeof value === "string" && value.trim() && value.trim() !== metadata.entity_id) return value.trim();
    }
    return fallback;
}
const PERCENTAGE_DECIMALS = 1;
const DATE_FIELD_ORDERS = {DMY: ["day", "month", "year"], MDY: ["month", "day", "year"], YMD: ["year", "month", "day"]};
let timeZone = "UTC";
/** Read only the current HA user's locale through same-origin ingress and workspace frames. */
function homeAssistantLocale() {
    let frame = window;
    try {
        while (true) {
            const locale = frame.document.querySelector("home-assistant")?.hass?.locale;
            if (locale && typeof locale === "object") return locale;
            if (frame.parent === frame) break;
            frame = frame.parent;
        }
    } catch { /* Standalone or cross-origin embedding cannot read HA preferences. */ }
    return {};
}

/** System numeric dates use the browser. Text dates retain HA language, with invalid language falling back safely. */
function dateLanguage(locale, format) {
    const textual = format.weekday !== undefined || ["short", "long", "narrow"].includes(format.month) || ["medium", "long", "full"].includes(format.dateStyle);
    if ((locale.date_format === "system" && !textual) || typeof locale.language !== "string") return undefined;
    try { return Intl.getCanonicalLocales(locale.language)[0]; }
    catch { return undefined; }
}
export function useHomeAssistantTimezone(value) { timeZone = value || "UTC"; }
export function money(value) { return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {minimumFractionDigits: MONEY_DECIMALS, maximumFractionDigits: MONEY_DECIMALS}) : "-"; }
/** Format percent units, not fractions, with one locale decimal. Separate-unit tables omit the suffix. */
export function percentage(value, withUnit = true) {
    return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString(undefined, {minimumFractionDigits: PERCENTAGE_DECIMALS, maximumFractionDigits: PERCENTAGE_DECIMALS}) + (withUnit ? "%" : "") : "Unavailable";
}
/** Round idle percentage editors visually while retaining exact draft values and normal native validation. */
export function percentageInput(input, value) {
    input.dataset.precisePercentage = value == null ? "" : String(value);
    const show = () => {
        const precise = input.dataset.precisePercentage; input.value = precise;
        if (precise !== "" && input.validity.valid && Number.isFinite(Number(precise))) input.value = Number(precise).toLocaleString("en-US", {useGrouping: false, minimumFractionDigits: PERCENTAGE_DECIMALS, maximumFractionDigits: PERCENTAGE_DECIMALS});
    };
    input.addEventListener("input", () => { input.dataset.precisePercentage = input.value; });
    input.addEventListener("focus", () => { input.value = input.dataset.precisePercentage; input.select(); });
    input.addEventListener("blur", show);
    show();
}
/** Read exact numeric drafts. Empty or invalid percentages stay null for validation, never zero. */
export function inputNumber(input) {
    const precise = input.dataset.precisePercentage;
    if (precise !== undefined && (precise === "" || !input.validity.valid)) return null;
    return Number(precise ?? input.value);
}
/** Show the supported plan strategy without exposing unknown archived values. */
export function planStrategy(value) {
    return value === "minimize cost" ? "Minimize cost" : value === "minimize consumption" ? "Minimize consumption" : "Unknown";
}
/** Name a captured plan consistently with the Planning selector, using the installation timezone. */
export function planName(plan) {
    return `${when(plan.generated_at)}: ${planStrategy(plan.strategy)}`;
}
/** Show minute precision in HA time or an explicit source zone, overriding options.timeZone without changing stored instants. */
export function when(value, options = {}, zone = timeZone) {
    const hasStyle = options.dateStyle !== undefined || options.timeStyle !== undefined;
    const explicitParts = ["year", "month", "day", "weekday", "hour", "minute"].some(part => options[part] !== undefined);
    const defaults = explicitParts || hasStyle ? {} : {year: "numeric", month: "numeric", day: "numeric", hour: "numeric", minute: "2-digit"};
    const format = {...defaults, ...options, second: undefined, timeZone: zone};
    if (["full", "long", "medium"].includes(format.timeStyle)) format.timeStyle = "short";
    const locale = homeAssistantLocale(), language = dateLanguage(locale, format), instant = new Date(value);
    const rendered = instant.toLocaleString(language, format);
    const order = Object.hasOwn(DATE_FIELD_ORDERS, locale.date_format) ? DATE_FIELD_ORDERS[locale.date_format] : undefined;
    if (!order || (format.month && !["numeric", "2-digit"].includes(format.month)) || (format.dateStyle && format.dateStyle !== "short")) return rendered;
    const parts = new Intl.DateTimeFormat(language, format).formatToParts(instant);
    const dateParts = parts.filter(part => order.includes(part.type));
    if (dateParts.length !== 3) return rendered;
    const first = parts.indexOf(dateParts[0]), last = parts.indexOf(dateParts.at(-1));
    const original = parts.slice(first, last + 1).map(part => part.value).join("");
    const separator = parts.slice(first, last).find(part => part.type === "literal")?.value || "/";
    return rendered.replace(original, order.map(name => dateParts.find(part => part.type === name).value).join(separator));
}
/** Present a calendar date without shifting its day. Timestamp dates still use installation time. */
export function dateLabel(value) {
    if (value == null || value === "") return "Unavailable";
    const calendar = typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value);
    return when(calendar ? value + "T00:00:00Z" : value, {year: "numeric", month: "numeric", day: "numeric"}, calendar ? "UTC" : timeZone);
}
export function localDate(value = new Date()) { const parts = new Intl.DateTimeFormat("en-CA", {timeZone, year: "numeric", month: "2-digit", day: "2-digit"}).formatToParts(new Date(value)); const part = (name) => parts.find((item) => item.type === name).value; return `${part("year")}-${part("month")}-${part("day")}`; }
/** Restore an optional saved instant in a minute-precision datetime-local editor using HA time. */
export function localDateTime(value) {
    if (value == null) return "";
    return new Intl.DateTimeFormat("sv-SE", {timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23"}).format(new Date(value)).replace(" ", "T");
}
export function optionLabel(value) { const text = value.replaceAll("_", " "); return text.charAt(0).toUpperCase() + text.slice(1); }

const MILLISECONDS_PER_DAY = 86400000;
export function nextDate(value) { return new Date(Date.parse(value + "T00:00:00Z") + MILLISECONDS_PER_DAY).toISOString().slice(0, 10); }
/** Match installation-local wall time, rejecting DST gaps and choosing the first occurrence of a fold. */
export function localInstant(value) {
    const wall = value.length === 10 ? value + "T00:00:00" : value.length === 16 ? value + ":00" : value;
    if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$/.test(wall)) throw new Error("Choose a valid date and time.");
    const target = Date.parse(wall + "Z");
    const format = new Intl.DateTimeFormat("sv-SE", {timeZone, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23"});
    const local = (instant) => format.format(new Date(instant)).replace(" ", "T");
    const offsets = new Set([-MILLISECONDS_PER_DAY, 0, MILLISECONDS_PER_DAY].map((delta) => Date.parse(local(target + delta) + "Z") - target - delta));
    const candidates = [...offsets].map((offset) => target - offset).filter((instant) => local(instant) === wall).sort((left, right) => left - right);
    if (!candidates.length) throw new Error("This time does not exist in the Home Assistant installation timezone.");
    return new Date(candidates[0]).toISOString();
}
