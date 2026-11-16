import {calendarDays, calendarTime} from "./chart_days.js";
import {when, percentage} from "./display.js";

const HOUR_MILLISECONDS = 3600000;
const MAX_TIME_LABELS = 8;
/** Keep model colours stable when a different model was selected at midnight. */
function modelColor(model) {
    if (model?.startsWith("ha-production")) return "#1a3060";
    if (model?.startsWith("pvwatts") && !model.includes("calibrated")) return "#b58800";
    return "#006da6";
}

/** Draw one frozen comparison with independent units, gaps and accessible series controls. */
export function qualityChart(canvas, plot, source, day, evidenceTime) {
    const tooltip = document.createElement("div");
    tooltip.className = "quality-tooltip"; tooltip.id = `${canvas.id}-evidence`;
    tooltip.hidden = true; tooltip.tabIndex = 0; tooltip.setAttribute("role", "region");
    tooltip.setAttribute("aria-label", "Hourly forecast evidence"); tooltip.setAttribute("aria-live", "polite"); plot.append(tooltip);
    canvas.tabIndex = 0; canvas.setAttribute("aria-describedby", tooltip.id);
    canvas.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight Home End Escape");
    let selectedHour = 0, pinned = false, renderedHour;
    const close = document.createElement("button"); close.type = "button"; close.textContent = "Close";
    close.setAttribute("aria-label", "Close hourly evidence");
    close.addEventListener("click", () => {pinned = false; tooltip.hidden = true; canvas.focus({preventScroll: true});});
    const number = value => typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "Unavailable";
    const points = (rows, field) => rows.map(row => ({x: (Date.parse(row.start) + Date.parse(row.end)) / 2, y: row[field]}));
    const forecasts = [source, ...(source.alternatives || [])];
    const datasets = [{label: "Measured", type: "bar", order: 1, backgroundColor: "#078b8080", borderWidth: 0, grouped: false, data: points(source.rows, "measured_kwh"), yAxisID: "y"},
        ...forecasts.map((forecast, index) => ({label: index ? forecast.model_label : `${forecast.model_label}: Original choice`, type: "line", order: 0, borderColor: modelColor(forecast.model), backgroundColor: modelColor(forecast.model), borderWidth: 2.5, borderDash: index ? index % 2 ? [8, 4] : [2, 4] : [], pointRadius: 0, pointHitRadius: 8, fill: false, spanGaps: false, tension: 0, yAxisID: "y", data: points(forecast.rows, "predicted_kwh")}))];
    const radiation = source.radiation;
    const radiationOffset = datasets.length;
    if (radiation) {
        for (const [label, field, color, dash] of [["Forecast irradiance", "forecast_w_m2", "#b75c00", [8, 4]], ["Satellite estimate", "satellite_w_m2", "#bd3449", []]]) {
            datasets.push({label, type: "line", order: 0, borderColor: color, backgroundColor: color, borderWidth: 2.8, borderDash: dash, pointRadius: 0, pointHitRadius: 8, fill: false, spanGaps: false, tension: 0, yAxisID: "radiation", hidden: true, data: points(radiation.rows, field)});
        }
    }
    const intervals = {id: "quality-interval-bounds",
        /** Fit partial edge intervals in pixels without changing their original evidence timestamps or values. */
        afterDatasetsUpdate(chart) {
            datasets.forEach((dataset, datasetIndex) => {
                const rows = dataset.yAxisID === "radiation" ? radiation.rows : datasetIndex === 0 ? source.rows : forecasts[datasetIndex - 1].rows;
                chart.getDatasetMeta(datasetIndex).data.forEach((element, index) => {
                    const start = Date.parse(rows[index].start), end = Date.parse(rows[index].end);
                    const left = Math.max(start, Date.parse(day.start)), right = Math.min(end, Date.parse(day.end));
                    const fraction = Math.max(0, right - left) / (end - start);
                    if (fraction >= 1) return;
                    element.x = chart.scales.x.getPixelForValue((left + right) / 2);
                    if (dataset.type === "bar") element.width *= fraction;
                });
            });
        }
    };
    /** Keep long provenance readable on touch screens without clipping a canvas tooltip. */
    function showEvidence({chart, tooltip: state}) {
        if (window.matchMedia("(max-width:640px)").matches && !pinned) {tooltip.hidden = true; return;}
        if (!state.opacity) { if (!pinned && !tooltip.matches(":hover") || !datasets.some((_, index) => chart.isDatasetVisible(index))) tooltip.hidden = true; return; }
        selectedHour = state.dataPoints[0]?.dataIndex ?? selectedHour;
        const hour = source.rows[selectedHour];
        if (!hour) return;
        const key = [selectedHour, ...datasets.map((_, index) => chart.isDatasetVisible(index))].join(":");
        if (key === renderedHour) {tooltip.hidden = false; return;}
        renderedHour = key;
        const lines = [`${evidenceTime(hour.start)} to ${evidenceTime(hour.end, {hour: "2-digit", minute: "2-digit"})}`];
        if (chart.isDatasetVisible(0)) lines.push(`Measured: ${number(hour.measured_kwh)} kWh`);
        forecasts.forEach((forecast, index) => {
            if (!chart.isDatasetVisible(index + 1)) return;
            const row = forecast.rows[selectedHour];
            lines.push(`${forecast.model_label}${index ? "" : ": Original choice"}: ${number(row?.predicted_kwh)} kWh`, `Difference: ${number(row?.error_kwh)} kWh`, `Evaluation: ${row?.evaluation_issue || "Eligible"}`, `Issued: ${when(forecast.issued_at)}`, `Forecast version: ${forecast.forecast_version}`);
        });
        if (radiation) for (const [index, field] of ["forecast_w_m2", "satellite_w_m2"].entries()) if (chart.isDatasetVisible(radiationOffset + index)) lines.push(`${datasets[radiationOffset + index].label}: ${number(radiation.rows[selectedHour]?.[field])} W/m²`);
        lines.push(`Original model: ${source.model_label}`, `Measured hours: ${source.coverage.measured_hours} of ${source.coverage.hours}: Comparable pairs: ${source.coverage.paired_hours}`, `MAE: ${number(source.metrics.mae_kwh)} kWh/hour: Bias: ${number(source.metrics.bias_kwh)} kWh/hour`, `RMSE: ${number(source.metrics.rmse_kwh)} kWh/hour: WAPE: ${percentage(source.metrics.wape_percent)}`, `Latest measurement receipt: ${source.last_received_at ? when(source.last_received_at) : "Unavailable"}`);
        if (source.measurement_boundary) lines.push(`Measurement boundary: ${source.measurement_boundary}`);
        if (source.comparison_issue) lines.push(source.comparison_issue);
        if (source.weather_sensitive != null) lines.push(`Weather influence: ${source.weather_sensitive ? "Requested" : "Not requested"}: Temperature features: ${source.weather_features_used ? "Used" : "Not used"}: Individual forecast: ${source.composition_selected ? "Included in the active household composition" : "Retained as a shadow component"}`);
        if (source.daily.relative_error_percent != null) lines.push(`Relative daily error: ${percentage(source.daily.relative_error_percent)}`);
        tooltip.replaceChildren(close, ...lines.map(line => {const paragraph = document.createElement("p"); paragraph.textContent = line; return paragraph;}));
        tooltip.hidden = false;
    }
    const chart = new Chart(canvas, {type: "bar", plugins: [intervals, calendarDays], data: {datasets}, options: {responsive: true, maintainAspectRatio: false, parsing: false, animation: false, interaction: {mode: "nearest", axis: "x", intersect: false}, scales: {
        x: {type: "linear", offset: false, min: Date.parse(day.start), max: Date.parse(day.end), afterBuildTicks: scale => {const first = Math.ceil(scale.min / HOUR_MILLISECONDS), last = Math.floor(scale.max / HOUR_MILLISECONDS), stride = Math.max(1, Math.ceil((last - first) / (MAX_TIME_LABELS - 1))); scale.ticks = []; for (let hour = first; hour <= last; hour += stride) scale.ticks.push({value: hour * HOUR_MILLISECONDS});}, ticks: {stepSize: HOUR_MILLISECONDS, maxTicksLimit: MAX_TIME_LABELS, callback: calendarTime}},
        y: {min: 0, title: {display: true, text: "Energy per hour (kWh)"}},
        ...(radiation ? {radiation: {display: false, position: "right", min: 0, grid: {drawOnChartArea: false}, title: {display: true, text: "Global horizontal irradiance (W/m²)"}}} : {}),
    }, plugins: {legend: {display: false}, tooltip: {enabled: false, animation: false, external: showEvidence, bodyFont: {family: "monospace"}, callbacks: {
        title: items => {const dataset = items[0].dataset, row = dataset.yAxisID === "radiation" ? radiation.rows[items[0].dataIndex] : source.rows[items[0].dataIndex]; return `${evidenceTime(row.start)} to ${evidenceTime(row.end, {hour: "2-digit", minute: "2-digit"})}`;},
        label: item => `${item.dataset.label}: ${item.parsed.y == null ? "Unavailable" : item.parsed.y.toFixed(3)} ${item.dataset.yAxisID === "radiation" ? "W/m²" : "kWh"}`,
    }}}}});
    canvas.addEventListener("click", () => {pinned = !pinned; if (pinned) showEvidence({chart, tooltip: chart.tooltip}); else tooltip.hidden = true;});
    canvas.addEventListener("keydown", event => {
        if (event.key === "Escape") {pinned = false; tooltip.hidden = true; return;}
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        if (!source.rows.length) return;
        event.preventDefault(); pinned = true;
        selectedHour = event.key === "Home" ? 0 : event.key === "End" ? source.rows.length - 1 : Math.max(0, Math.min(source.rows.length - 1, selectedHour + (event.key === "ArrowLeft" ? -1 : 1)));
        chart.tooltip.setActiveElements(datasets.flatMap((_, datasetIndex) => chart.isDatasetVisible(datasetIndex) ? [{datasetIndex, index: selectedHour}] : []), {x: 0, y: 0});
        chart.update("none");
    });
    tooltip.addEventListener("mouseleave", () => {if (!pinned) tooltip.hidden = true;});
    tooltip.addEventListener("keydown", event => {if (event.key === "Escape") {pinned = false; tooltip.hidden = true; canvas.focus({preventScroll: true});}});
    const controls = document.createElement("div"); controls.className = "chart-legend quality-evidence-controls";
    controls.setAttribute("role", "group"); controls.setAttribute("aria-label", `${source.title}: Show in chart`);
    datasets.forEach((dataset, index) => {
        const label = document.createElement("label"), input = document.createElement("input"), swatch = document.createElement("span");
        input.type = "checkbox"; input.checked = !dataset.hidden;
        input.onchange = () => {chart.setDatasetVisibility(index, input.checked); if (radiation) chart.options.scales.radiation.display = datasets.some((_, datasetIndex) => datasetIndex >= radiationOffset && chart.isDatasetVisible(datasetIndex)); chart.update();};
        swatch.className = dataset.type === "bar" ? "series-swatch" : "quality-line-swatch";
        swatch.style.setProperty("--series-color", dataset.borderColor || "#078b80");
        if (dataset.type === "bar") {swatch.style.backgroundColor = "#078b8080"; swatch.style.border = "0";}
        else swatch.style.borderTopStyle = dataset.borderDash?.length ? dataset.borderDash[0] > 2 ? "dashed" : "dotted" : "solid";
        swatch.setAttribute("aria-hidden", "true"); label.append(input, swatch, document.createTextNode(dataset.label)); controls.append(label);
    });
    plot.append(controls);
    if (radiation) {
        const status = document.createElement("p"); status.className = "quality-radiation-status";
        status.textContent = `Satellite data complete through: ${radiation.complete_until ? when(radiation.complete_until) : "Unavailable"}: Coverage: ${radiation.observed_hours} of ${radiation.hours} hours`;
        const details = document.createElement("p"); details.className = "quality-radiation-source";
        details.textContent = `${radiation.source}: Satellite estimate, not a local sensor measurement. Last fetched: ${radiation.received_at ? when(radiation.received_at) : "Unavailable"}. Both irradiance series use the right axis in W/m².${radiation.issue ? ` ${radiation.issue}` : ""}`;
        plot.after(status, details);
    }
    return chart;
}
