/** Shared Energy-style calendar selection. Page owners retain their data and date limits. */
import {dateLabel, localDate, when} from "./display.js";

const DAY_MILLISECONDS = 86400000;
const CALENDAR_COLUMNS = 7;
/** Calendar arithmetic uses nominal UTC dates. Page owners convert selections into HA-local instants. */
export function shiftDay(day, count) {
    return new Date(Date.parse(day + "T00:00:00Z") + count * DAY_MILLISECONDS).toISOString().slice(0, 10);
}
/** Reject rollover dates as well as non-calendar values before comparing ISO keys. */
function validDate(value) {
    return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
        && Number.isFinite(Date.parse(value + "T00:00:00Z"))
        && new Date(value + "T00:00:00Z").toISOString().slice(0, 10) === value;
}
/** Own the compact trigger, presets and native modal. Single-day pages never emit a range. */
export function createPeriodSelector(host, {dayOnly = false, onChange, label = "Period", describedBy} = {}) {
    let start = null, end = null, minimum = null, maximum = null, available = null, preset = "custom";
    let month = null, anchor = null, disabled = true, openingPreset = "custom", committed = false;
    const make = (tag, className, text) => {const element = document.createElement(tag); element.className = className; if (text) element.textContent = text; return element;};
    const button = (text, name, action) => {const element = make("button", "secondary", text); element.type = "button"; element.setAttribute("aria-label", name); element.onclick = action; return element;};
    const control = make("div", "period-control"); control.setAttribute("role", "group"); control.setAttribute("aria-label", label);
    const previous = button("‹", "Previous " + (dayOnly ? "day" : "period"), () => navigate(-1));
    const next = button("›", "Next " + (dayOnly ? "day" : "period"), () => navigate(1));
    const trigger = button("Choose " + (dayOnly ? "day" : "period"), "Choose " + (dayOnly ? "day" : "period"), open);
    trigger.classList.add("period-trigger"); trigger.setAttribute("aria-haspopup", "dialog");
    if (describedBy) trigger.setAttribute("aria-describedby", describedBy);
    for (const element of [previous, trigger, next]) element.disabled = true;
    control.append(previous, trigger, next); host.replaceChildren(control);
    const dialog = make("dialog", "period-dialog"); dialog.setAttribute("aria-label", "Choose " + (dayOnly ? "day" : "period"));
    const heading = make("header", "period-dialog-heading"); heading.append(make("h2", "", "Select " + (dayOnly ? "day" : "period")), button("×", "Close date selector", () => dialog.close()));
    const layout = make("div", "period-dialog-layout"), shortcuts = make("div", "period-shortcuts"); shortcuts.setAttribute("role", "group"); shortcuts.setAttribute("aria-label", "Standard " + (dayOnly ? "days" : "periods"));
    const calendar = make("div", "period-calendar"), monthHeading = make("div", "period-month"), monthTitle = make("strong");
    const previousMonth = button("‹", "Previous month", () => moveMonth(-1)), nextMonth = button("›", "Next month", () => moveMonth(1));
    monthHeading.append(previousMonth, monthTitle, nextMonth);
    const weekdays = make("div", "period-weekdays"); weekdays.setAttribute("aria-hidden", "true");
    for (let index = 0; index < CALENDAR_COLUMNS; index++) weekdays.append(make("span", "", when(shiftDay("2026-11-16", index) + "T00:00:00Z", {weekday: "narrow"}, "UTC")));
    const days = make("div", "period-days"); days.setAttribute("role", "group"); days.setAttribute("aria-label", "Calendar dates");
    const instruction = make("p", "period-instruction", dayOnly ? "Choose one whole day." : "Choose a start date and an end date.");
    const form = make("form", "period-custom"), from = make("input"), to = make("input"), error = make("p", "period-error"); error.setAttribute("role", "alert");
    for (const [input, title] of [[from, dayOnly ? "Day" : "From"], [to, "To"]]) {
        input.type = "date"; input.required = true;
        const field = make("label", "", title); field.append(input); form.append(field);
        input.oninput = () => {preset = "custom"; anchor = null; error.textContent = ""; draw();};
    }
    if (describedBy) from.setAttribute("aria-describedby", describedBy);
    to.closest("label").hidden = dayOnly; to.required = !dayOnly;
    const apply = make("button", "", "Apply " + (dayOnly ? "day" : "period")); apply.type = "submit"; form.append(error, apply);
    calendar.append(monthHeading, weekdays, days, instruction, form); layout.append(shortcuts, calendar); dialog.append(heading, layout); document.body.append(dialog);
    const definitions = dayOnly ? [["today", "Today"], ["yesterday", "Yesterday"], ["custom", "Choose day"]] : [["today", "Today"], ["yesterday", "Yesterday"], ["week", "This week"], ["seven", "Last 7 days"], ["month", "This month"], ["year", "This year"], ["all", "All history"], ["custom", "Custom period"]];
    const presets = new Map(definitions.map(([key, title]) => {
        const element = button(title, title, () => {
            if (key === "custom") {preset = key; from.focus(); draw(); return;}
            const range = presetRange(key); if (!allowed(range.start, range.end)) return;
            preset = key; commit(range.start, range.end); dialog.close();
        }); shortcuts.append(element); return [key, element];
    }));
    /** Keep availability checks at every entry, including stale dialog drafts and page refreshes. */
    function allowed(first, last) {
        return !disabled && validDate(first) && validDate(last) && first <= last && (!dayOnly || first === last)
            && (!minimum || first >= minimum) && (!maximum || last <= maximum)
            && (!available || available.has(first) && first === last);
    }
    /** Bound current-calendar shortcuts at the latest permitted date, never at the browser timezone. */
    function presetRange(key) {
        const today = localDate(), latest = maximum && maximum < today ? maximum : today;
        if (key === "today") return {start: today, end: today};
        if (key === "yesterday") return {start: shiftDay(today, -1), end: shiftDay(today, -1)};
        if (key === "all") return {start: minimum, end: maximum};
        if (key === "seven") return {start: shiftDay(latest, -6), end: latest};
        if (key === "week") return {start: shiftDay(today, -((new Date(today + "T00:00:00Z").getUTCDay() + 6) % 7)), end: latest};
        return {start: key === "year" ? today.slice(0, 4) + "-01-01" : today.slice(0, 7) + "-01", end: latest};
    }
    /** Calendar month/year steps retain complete period units where data allow them. */
    function shifted(direction) {
        if (!start) return {};
        if (!dayOnly && ["month", "year"].includes(preset)) {
            const date = new Date(start + "T00:00:00Z");
            const year = date.getUTCFullYear() + (preset === "year" ? direction : 0);
            const monthIndex = preset === "year" ? 0 : date.getUTCMonth() + direction;
            const first = new Date(Date.UTC(year, monthIndex, 1));
            const last = new Date(Date.UTC(year, preset === "year" ? 12 : monthIndex + 1, 0));
            return boundPeriod(first.toISOString().slice(0, 10), last.toISOString().slice(0, 10));
        }
        if (dayOnly && available) {
            const values = [...available].sort(), index = values.indexOf(start) + direction;
            return {start: values[index], end: values[index]};
        }
        const length = dayOnly ? 1 : ["week", "seven"].includes(preset) ? 7 : Math.round((Date.parse(end) - Date.parse(start)) / DAY_MILLISECONDS) + 1;
        const origin = preset === "week" ? shiftDay(start, -((new Date(start + "T00:00:00Z").getUTCDay() + 6) % 7)) : start;
        const first = shiftDay(origin, direction * length);
        return preset === "week" ? boundPeriod(first, shiftDay(first, 6)) : {start: first, end: shiftDay(first, length - 1)};
    }
    /** Return to a partially elapsed current calendar period after visiting a complete earlier one. */
    function boundPeriod(first, last) {return {start: minimum && first < minimum ? minimum : first, end: maximum && last > maximum ? maximum : last};}
    function navigate(direction) {const range = shifted(direction); if (allowed(range.start, range.end)) commit(range.start, range.end);}
    function commit(first, last) {start = first; end = last; committed = true; refresh(); onChange?.({start, end});}
    /** Publish only a confirmed selection. A cancelled dialog never changes the page's filter. */
    function refresh() {
        trigger.textContent = start ? `${dateLabel(start)}${start === end ? "" : " – " + dateLabel(end)} ⌄` : "Choose " + (dayOnly ? "day" : "period");
        trigger.disabled = disabled; previous.disabled = !allowed(shifted(-1).start, shifted(-1).end); next.disabled = !allowed(shifted(1).start, shifted(1).end);
    }
    function open() {
        if (disabled) return;
        openingPreset = preset; committed = false;
        instruction.textContent = dayOnly ? "Choose one whole day." : "Choose a start date and an end date.";
        from.value = start || maximum || localDate(); to.value = end || from.value; month = (end || from.value).slice(0, 7); anchor = null; error.textContent = "";
        draw(); dialog.showModal();
    }
    function moveMonth(direction) {month = new Date(Date.UTC(Number(month.slice(0, 4)), Number(month.slice(5)) - 1 + direction, 1)).toISOString().slice(0, 7); draw();}
    /** Build real buttons so date selection and disabled boundaries work with native keyboard focus. */
    function draw() {
        const first = month + "-01", monthDate = new Date(first + "T00:00:00Z");
        monthTitle.textContent = when(first + "T00:00:00Z", {year: "numeric", month: "long"}, "UTC");
        const last = new Date(Date.UTC(monthDate.getUTCFullYear(), monthDate.getUTCMonth() + 1, 0)).toISOString().slice(0, 10);
        previousMonth.disabled = Boolean(minimum && first <= minimum.slice(0, 7) + "-01");
        nextMonth.disabled = Boolean(maximum && last >= maximum);
        days.replaceChildren();
        for (let index = 0; index < (monthDate.getUTCDay() + 6) % CALENDAR_COLUMNS; index++) days.append(make("span"));
        for (let day = first; day <= last; day = shiftDay(day, 1)) {
            const value = day;
            const element = button(String(Number(day.slice(-2))), dateLabel(day), () => {
                preset = "custom";
                if (dayOnly) {if (allowed(value, value)) {commit(value, value); dialog.close();} return;}
                if (!anchor) {anchor = value; from.value = value; to.value = value; instruction.textContent = "Now choose the end date.";}
                else {from.value = value < anchor ? value : anchor; to.value = value > anchor ? value : anchor; anchor = null; instruction.textContent = "Select Apply period to show these dates.";}
                draw();
            });
            element.disabled = !allowed(day, day);
            element.classList.toggle("period-edge", day === from.value || day === to.value);
            element.classList.toggle("period-inside", day > from.value && day < to.value);
            element.setAttribute("aria-pressed", String(day >= from.value && day <= (dayOnly ? from.value : to.value)));
            days.append(element);
        }
        for (const [key, element] of presets) {const range = key === "custom" ? null : presetRange(key); element.disabled = Boolean(range && !allowed(range.start, range.end)); element.setAttribute("aria-pressed", String(key === preset));}
        for (const input of [from, to]) {input.min = minimum || ""; input.max = maximum || "";}
    }
    form.onsubmit = event => {
        event.preventDefault();
        const first = from.value, last = dayOnly ? first : to.value;
        if (!allowed(first, last)) {error.textContent = "Choose available dates in increasing order."; return;}
        preset = "custom"; commit(first, last); dialog.close();
    };
    dialog.addEventListener("click", event => {if (event.target !== dialog) return; const bounds = dialog.getBoundingClientRect(); if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) dialog.close();});
    dialog.addEventListener("close", () => {if (!committed) preset = openingPreset;});
    return {
        get value() {return {start, end};},
        /** Update server-owned bounds and dates without causing a second network request. */
        set({start: first = start, end: last = end, min = minimum, max = maximum, availableDays, enabled = true, selection = preset} = {}) {
            minimum = min; maximum = max; available = availableDays ? new Set(availableDays) : available; disabled = !enabled; preset = selection;
            start = validDate(first) ? first : null; end = dayOnly ? start : validDate(last) ? last : start;
            if (start && minimum && start < minimum) start = minimum;
            if (start && maximum && start > maximum) start = maximum;
            if (end && minimum && end < minimum) end = minimum;
            if (end && maximum && end > maximum) end = maximum;
            if (dayOnly) end = start;
            if (available && !available.has(start)) start = end = [...available].sort().at(-1) || null;
            if (start && end && start > end) start = end;
            refresh(); if (dialog.open) {from.value = start || ""; to.value = end || ""; draw();}
        },
    };
}
