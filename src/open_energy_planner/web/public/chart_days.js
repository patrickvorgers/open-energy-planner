/** Calendar bands share the installation timezone without changing timestamps or series. */
import {localDate, localInstant, nextDate, when} from "./display.js";

// Heading height and width thresholds use CSS pixels.
const HEADER_HEIGHT = 36;
const FULL_HEADING_WIDTH = 90;
const COMPACT_HEADING_WIDTH = 40;
const MIN_HEADING_WIDTH = 22;
const MILLISECONDS_PER_DAY = 86400000;
const bands = new WeakMap();

/** Day headings supply the date. Compact clock ticks use installation time without timezone names. */
export function calendarTime(value) {
    return when(value, {hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
}

/** Empty charts keep their original layout until at least one real timestamp is present. */
function hasTimestamps(chart) {
    return chart.data.datasets.some(dataset => dataset.data.some(point => Number.isFinite(point?.x)));
}

/** Find the first existing instant of a calendar date without accepting nonexistent input wall times. */
export function calendarDayStart(date) {
    try {
        return Date.parse(localInstant(date));
    } catch {
        // A clock jump can remove midnight. Locate the actual date boundary instead of assuming 24 hours.
        const nominal = Date.parse(date + "T00:00:00Z");
        let lower = nominal - MILLISECONDS_PER_DAY, upper = nominal + MILLISECONDS_PER_DAY;
        while (upper - lower > 1) {
            const middle = Math.floor((lower + upper) / 2);
            if (localDate(middle) < date) lower = middle;
            else upper = middle;
        }
        return upper;
    }
}

/** Find the next local date even where a timezone transition skips midnight or a whole date. */
function dayEnd(maximum, date, utc) {
    const next = nextDate(date);
    return Math.min(maximum, utc ? Date.parse(next + "T00:00:00Z") : calendarDayStart(next));
}

/** Decorate real calendar charts only. Relative appliance-cycle axes do not install this plugin. */
export const calendarDays = {
    id: "calendarDays",
    /** Own the heading container before the canvas and snapshot the original layout padding. */
    beforeInit(chart) {
        const headings = document.createElement("div");
        headings.className = "chart-day-headings";
        headings.setAttribute("role", "list");
        headings.setAttribute("aria-label", "Chart calendar days");
        chart.canvas.before(headings);
        const padding = chart.options.layout.padding;
        bands.set(chart, {headings, padding: typeof padding === "number" ? {top: padding, right: padding, bottom: padding, left: padding} : {...padding}});
    },
    /** Replace padding from the snapshot on each layout pass, reserving headings only for real timestamps. */
    beforeLayout(chart) {
        const {padding} = bands.get(chart);
        chart.options.layout.padding = {...padding, top: (padding.top || 0) + (hasTimestamps(chart) ? HEADER_HEIGHT : 0)};
    },
    /** Rebuild headings and paint day bands and boundaries using UTC dates when options.utc is true, otherwise installation dates. */
    beforeDraw(chart, _arguments, options) {
        const {headings} = bands.get(chart), {ctx, chartArea, scales: {x}} = chart;
        headings.replaceChildren();
        if (!x || !hasTimestamps(chart) || !Number.isFinite(x.min) || !Number.isFinite(x.max) || x.max <= x.min) return;
        const dateAt = options.utc ? value => new Date(value).toISOString().slice(0, 10) : localDate;
        const today = dateAt(Date.now()), tomorrow = nextDate(today);
        headings.style.top = `${chartArea.top - HEADER_HEIGHT}px`;
        ctx.save();
        for (let start = x.min; start < x.max;) {
            const date = dateAt(start), end = dayEnd(x.max, date, options.utc);
            const left = Math.max(chartArea.left, x.getPixelForValue(start)), right = Math.min(chartArea.right, x.getPixelForValue(end));
            const alternate = Math.floor(Date.parse(date + "T00:00:00Z") / MILLISECONDS_PER_DAY) % 2;
            const color = alternate ? "#f3f8fc" : "#f3faf6";
            ctx.fillStyle = color;
            ctx.fillRect(left, chartArea.top, right - left, chartArea.bottom - chartArea.top);
            if (start > x.min) {
                ctx.strokeStyle = "#9bafbc"; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
                ctx.beginPath(); ctx.moveTo(left, chartArea.top); ctx.lineTo(left, chartArea.bottom); ctx.stroke();
            }
            const heading = document.createElement("span"), title = document.createElement("strong"), caption = document.createElement("span");
            const instant = Date.parse(date + "T12:00:00Z");
            const format = parts => when(options.utc ? instant : start, parts, options.utc ? "UTC" : undefined);
            const relative = date === today ? "Today" : date === tomorrow ? "Tomorrow" : null;
            const full = `${relative ? relative + ": " : ""}${format({weekday: "long", year: "numeric", month: "long", day: "numeric"})}`;
            const width = right - left;
            heading.style.cssText = `left:${left}px;width:${width}px;background:${alternate ? "#edf5fa" : "#edf6f2"}`;
            heading.setAttribute("role", "listitem");
            heading.setAttribute("aria-label", full); heading.title = full;
            if (width >= MIN_HEADING_WIDTH) {
                title.textContent = width >= FULL_HEADING_WIDTH && relative ? relative : format(width >= COMPACT_HEADING_WIDTH ? {weekday: "short"} : {day: "numeric"});
                caption.textContent = format(width >= COMPACT_HEADING_WIDTH ? {day: "numeric", month: "short"} : {month: "short"});
                heading.append(title, caption);
            }
            headings.append(heading);
            start = end;
        }
        ctx.restore();
    },
    /** Remove the owned heading container and release the chart's stored layout state. */
    afterDestroy(chart) { bands.get(chart)?.headings.remove(); bands.delete(chart); }
};
