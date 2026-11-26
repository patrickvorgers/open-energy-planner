import {calendarTime} from "./chart_days.js";
import {localDate, when} from "./display.js";

// Chart.js adds the tick length above the labels as well as this padding.
export const GRID_MODE_TICK_PADDING = 21;
export const ZERO_GRID_LABEL = "Zero on the meter";

/** Join adjacent active commands using the archived mode and exact elapsed timestamps. */
function activeBlocks(steps) {
    const blocks = [];
    for (const step of steps) {
        if (step.grid_control_mode !== "zero_grid") continue;
        const start = Date.parse(step.start), end = Date.parse(step.end);
        if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) continue;
        const last = blocks.at(-1);
        if (last?.end === start) last.end = end;
        else blocks.push({start, end});
    }
    return blocks;
}

/** Overlay accessible controls in the space reserved above the existing x-axis labels. */
export function gridModeStrip(steps, positionTooltip) {
    const blocks = activeBlocks(steps);
    const events = new AbortController();
    let track, tooltip, period, owner = null, pinned = false;
    const listen = (target, type, listener, capture = false) =>
        target.addEventListener(type, listener, {signal: events.signal, capture});

    function close() {
        owner?.removeAttribute("aria-describedby");
        owner = null;
        pinned = false;
        tooltip.hidden = true;
    }

    return {
        id: "gridModeStrip",
        beforeInit(chart) {
            track = document.createElement("div");
            track.className = "grid-mode-track";
            track.setAttribute("role", "group");
            track.setAttribute("aria-label", ZERO_GRID_LABEL);
            chart.canvas.after(track);
            tooltip = document.createElement("div");
            tooltip.id = `${chart.canvas.id}-grid-mode-tooltip`;
            tooltip.className = "chart-tooltip grid-mode-tooltip";
            tooltip.setAttribute("role", "tooltip");
            tooltip.hidden = true;
            const title = document.createElement("strong");
            title.textContent = ZERO_GRID_LABEL;
            period = document.createElement("span");
            tooltip.append(title, period);
            document.body.append(tooltip);
            for (const block of blocks) {
                const {start, end} = block;
                const format = localDate(start) === localDate(end) ? calendarTime : when;
                const time = `${format(start)}–${format(end)}`;
                const button = document.createElement("button");
                button.type = "button";
                button.className = "grid-mode-block";
                button.setAttribute("aria-label", `${ZERO_GRID_LABEL}, ${when(start)} to ${when(end)}`);
                button.hidden = true;
                block.button = button;
                track.append(button);
                const show = () => {
                    if (owner !== button) close();
                    owner = button;
                    period.textContent = time;
                    tooltip.hidden = false;
                    button.setAttribute("aria-describedby", tooltip.id);
                    positionTooltip(tooltip, button);
                };
                listen(button, "pointerenter", event => { if (event.pointerType === "mouse") show(); });
                listen(button, "pointerleave", () => {
                    if (owner === button && !pinned && !button.matches(":focus-visible")) close();
                });
                listen(button, "focus", show);
                listen(button, "blur", () => { if (owner === button) close(); });
                listen(button, "click", () => {
                    if (owner === button && pinned) close();
                    else { show(); pinned = true; }
                });
            }
            listen(document, "pointerdown", event => { if (owner && !owner.contains(event.target)) close(); }, true);
            listen(document, "keydown", event => { if (owner && event.key === "Escape") close(); });
            listen(window, "scroll", () => {
                if (!owner) return;
                const bounds = owner.getBoundingClientRect();
                if ((pinned || document.activeElement === owner)
                    && bounds.bottom > 0 && bounds.top < innerHeight
                    && bounds.right > 0 && bounds.left < innerWidth) {
                    positionTooltip(tooltip, owner);
                } else close();
            }, true);
        },
        afterUpdate(chart) {
            const x = chart.scales.x, area = chart.chartArea;
            if (!x || !area || !(x.max > x.min)) { track.hidden = true; close(); return; }
            track.hidden = false;
            track.style.left = `${area.left}px`;
            track.style.width = `${area.width}px`;
            track.style.top = `${area.bottom}px`;
            for (const {start, end, button} of blocks) {
                const left = x.getPixelForValue(Math.max(start, x.min));
                const right = x.getPixelForValue(Math.min(end, x.max));
                button.hidden = right <= left;
                if (button.hidden) {
                    if (owner === button) close();
                } else {
                    button.style.left = `${left - area.left}px`;
                    button.style.width = `${right - left}px`;
                    if (owner === button) positionTooltip(tooltip, button);
                }
            }
        },
        afterDestroy() {
            events.abort();
            track.remove();
            tooltip.remove();
        }
    };
}
