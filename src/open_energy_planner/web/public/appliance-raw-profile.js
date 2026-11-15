import {when} from "./display.js";

let powerChart, energyChart, rendered;

/** Plot original Recorder event times with explicit state hold, separate from a learned average. */
export function renderRawHistory(device) {
    const card = document.getElementById("recorded-cycle"), raw = device?.raw_history;
    card.hidden = !raw;
    const key = JSON.stringify([device?.entity_id, raw]);
    if (key === rendered) return;
    powerChart?.destroy(); energyChart?.destroy(); powerChart = energyChart = null;
    rendered = key;
    if (!raw) return;
    if (!raw.power_curve || !raw.energy_curve) {
        document.getElementById("recorded-cycle-status").textContent = "Loading recorded state details…";
        return;
    }
    const statuses = {
        running: "Cycle in progress. Measurements do not confirm execution.",
        complete: "Power and energy agree for a completed cycle.",
        unverified: "These measurements cannot establish a conserved cycle. The existing usable profile is retained.",
        limited: "Only a limited recent tail is available. Missing earlier coverage remains unknown.",
        complexity_limited: "The recorded curve is retained. The existing profile remains in use because this trace is too detailed for planning.",
        no_complete_cycle: "No complete cycle established. Previously learned profiles remain available."
    };
    document.getElementById("recorded-cycle-status").textContent = `${statuses[raw.status] || "Recorded states available."} Captured ${when(raw.end)}. Actual event times are shown. Lines hold each recorded state until the next event. A carried state is not a new measurement.${raw.limited ? " The source response is limited." : ""}${raw.synthetic ? " Simulated data." : ""}`;
    const min = Date.parse(raw.cycle_started_at || raw.start) - (raw.cycle_started_at ? 60000 : 0);
    const max = Math.min(Date.parse(raw.end), Date.parse(raw.cycle_completed_at || raw.end) + 60000);
    const options = title => ({responsive: true, maintainAspectRatio: false, animation: false, spanGaps: false,
        scales: {x: {type: "linear", min, max, title: {display: true, text: "Recorded time"}, ticks: {maxTicksLimit: 4, callback: value => when(new Date(value).toISOString())}}, y: {title: {display: true, text: title}}},
        plugins: {tooltip: {callbacks: {title: items => items.length ? when(new Date(items[0].parsed.x).toISOString()) : ""}}}});
    powerChart = new Chart(document.getElementById("recorded-power-chart"), {type: "line",
        data: {datasets: [{label: "Recorded power (W)", data: raw.power_curve.map(row => ({x: Date.parse(row.at), y: row.power_w})), stepped: "before", borderColor: "#009e73", backgroundColor: "#009e73", pointRadius: 2}]},
        options: {...options("Recorded AC power (W)"), scales: {...options("").scales, y: {min: 0, title: {display: true, text: "Recorded AC power (W)"}}}}});
    const baseline = raw.energy_curve.find(row => row.kwh != null)?.kwh ?? 0;
    energyChart = new Chart(document.getElementById("recorded-energy-chart"), {type: "line",
        data: {datasets: [{label: "Recorded counter change (kWh)", data: raw.energy_curve.map(row => ({x: Date.parse(row.at), y: row.kwh == null ? null : row.kwh - baseline})), stepped: "before", borderColor: "#0072b2", backgroundColor: "#0072b2", pointRadius: 2}]},
        options: options("Change in recorded counter (kWh)")});
}
