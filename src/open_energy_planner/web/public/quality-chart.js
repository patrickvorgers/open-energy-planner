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
    let selectedInterval, pinned = false, renderedInterval;
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
        for (const [label, field, color, dash] of [["Forecast irradiance", "forecast_w_m2", "#b75c00", [8, 4]], ["Actual irradiance (satellite)", "satellite_w_m2", "#bd3449", []]]) {
            datasets.push({label, type: "line", order: 0, borderColor: color, backgroundColor: color, borderWidth: 2.8, borderDash: dash, pointRadius: 0, pointHitRadius: 8, fill: false, spanGaps: false, tension: 0, yAxisID: "radiation", data: points(radiation.rows, field)});
        }
    }
    /** Keep each model and reference on its own original evidence intervals. */
    const datasetRows = index => datasets[index].yAxisID === "radiation" ? radiation.rows : index === 0 ? source.rows : forecasts[index - 1].rows;
    for (const [index, dataset] of datasets.entries()) {
        const rows = datasetRows(index);
        dataset.intervalBounds = rows.map(row => ({start: Date.parse(row.start), end: Date.parse(row.end)}));
        if (dataset.type !== "line") continue;
        dataset.segment = {borderColor: context => Date.parse(rows[context.p0DataIndex].end) === Date.parse(rows[context.p1DataIndex].start) ? undefined : "transparent"};
    }
    const intervals = {id: "quality-interval-bounds",
        /** Fit partial edge intervals in pixels without changing their original evidence timestamps or values. */
        afterDatasetsUpdate(chart) {
            datasets.forEach((dataset, datasetIndex) => {
                const rows = datasetRows(datasetIndex);
                chart.getDatasetMeta(datasetIndex).data.forEach((element, index) => {
                    const start = Date.parse(rows[index].start), end = Date.parse(rows[index].end);
                    const left = Math.max(start, Date.parse(day.start)), right = Math.min(end, Date.parse(day.end));
                    const fraction = Math.max(0, right - left) / (end - start);
                    if (fraction >= 1) return;
                    element.x = chart.scales.x.getPixelForValue((left + right) / 2);
                    if (dataset.type === "bar") element.width *= fraction;
                });
            });
        },
        /** Draw each covered run to its recorded interval edges without adding or moving evidence points. */
        afterDatasetDraw(chart, {index, meta}) {
            const dataset = datasets[index];
            if (dataset.type !== "line") return;
            const rows = datasetRows(index), points = meta.data, context = chart.ctx, area = chart.chartArea;
            context.save();
            context.beginPath(); context.rect(area.left, area.top, area.right - area.left, area.bottom - area.top); context.clip();
            context.strokeStyle = dataset.borderColor; context.lineWidth = dataset.borderWidth;
            context.setLineDash(dataset.borderDash || []);
            rows.forEach((row, rowIndex) => {
                const point = points[rowIndex];
                if (point.skip) return;
                const start = Date.parse(row.start), end = Date.parse(row.end);
                if (end <= chart.scales.x.min || start >= chart.scales.x.max) return;
                const previous = rows[rowIndex - 1], next = rows[rowIndex + 1];
                if (!previous || points[rowIndex - 1].skip || Date.parse(previous.end) !== start) {
                    context.beginPath(); context.moveTo(chart.scales.x.getPixelForValue(start), point.y);
                    context.lineTo(point.x, point.y); context.stroke();
                }
                if (!next || points[rowIndex + 1].skip || end !== Date.parse(next.start)) {
                    context.beginPath(); context.moveTo(point.x, point.y);
                    context.lineTo(chart.scales.x.getPixelForValue(end), point.y); context.stroke();
                }
            });
            context.restore();
        }
    };
    /** Keep long provenance readable on touch screens without clipping a canvas tooltip. */
    function showEvidence({chart, tooltip: state}) {
        if (window.matchMedia("(max-width:640px)").matches && !pinned) {tooltip.hidden = true; return;}
        if (!state.opacity) { if (!pinned && !tooltip.matches(":hover") || !datasets.some((_, index) => chart.isDatasetVisible(index))) tooltip.hidden = true; return; }
        const point = state.dataPoints[0];
        if (!point) return;
        const hour = datasetRows(point.datasetIndex)[point.dataIndex];
        selectedInterval = datasets[point.datasetIndex].intervalBounds[point.dataIndex];
        const matchingIndex = datasetIndex => datasets[datasetIndex].intervalBounds.findIndex(interval => interval.start === selectedInterval.start && interval.end === selectedInterval.end);
        const key = [selectedInterval.start, selectedInterval.end, ...datasets.map((_, index) => chart.isDatasetVisible(index))].join(":");
        if (key === renderedInterval) {tooltip.hidden = false; return;}
        renderedInterval = key;
        const lines = [`${evidenceTime(hour.start)} to ${evidenceTime(hour.end, {hour: "2-digit", minute: "2-digit"})}`];
        if (chart.isDatasetVisible(0)) lines.push(`Measured: ${number(source.rows[matchingIndex(0)]?.measured_kwh)} kWh`);
        forecasts.forEach((forecast, index) => {
            if (!chart.isDatasetVisible(index + 1)) return;
            const row = forecast.rows[matchingIndex(index + 1)];
            lines.push(`${forecast.model_label}${index ? "" : ": Original choice"}: ${number(row?.predicted_kwh)} kWh`, `Difference: ${number(row?.error_kwh)} kWh`, `Evaluation: ${row ? row.evaluation_issue || "Eligible" : "No matching forecast interval"}`, `Issued: ${when(forecast.issued_at)}`, `Forecast version: ${forecast.forecast_version ?? "Unavailable"}`);
        });
        if (radiation) for (const [index, field] of ["forecast_w_m2", "satellite_w_m2"].entries()) if (chart.isDatasetVisible(radiationOffset + index)) lines.push(`${datasets[radiationOffset + index].label}: ${number(radiation.rows[matchingIndex(radiationOffset + index)]?.[field])} W/m²`);
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
        ...(radiation ? {radiation: {display: true, position: "right", min: 0, grid: {drawOnChartArea: false}, title: {display: true, text: "Global horizontal irradiance (W/m²)"}}} : {}),
    }, plugins: {legend: {display: false}, tooltip: {enabled: false, animation: false, external: showEvidence, bodyFont: {family: "monospace"}, callbacks: {
        title: items => {const row = datasetRows(items[0].datasetIndex)[items[0].dataIndex]; return `${evidenceTime(row.start)} to ${evidenceTime(row.end, {hour: "2-digit", minute: "2-digit"})}`;},
        label: item => `${item.dataset.label}: ${item.parsed.y == null ? "Unavailable" : item.parsed.y.toFixed(3)} ${item.dataset.yAxisID === "radiation" ? "W/m²" : "kWh"}`,
    }}}}});
    /** Refresh the selected interval from visible original rows, including recorded null values. */
    function showIntervalEvidence(interval) {
        const points = datasets.flatMap((dataset, datasetIndex) => {
            const dataIndex = dataset.intervalBounds.findIndex(candidate => candidate.start === interval.start && candidate.end === interval.end);
            return chart.isDatasetVisible(datasetIndex) && dataIndex >= 0 ? [{datasetIndex, dataIndex}] : [];
        });
        if (!points.length) {tooltip.hidden = true; return;}
        showEvidence({chart, tooltip: {opacity: 1, dataPoints: points}});
    }
    canvas.addEventListener("click", () => {pinned = !pinned; if (pinned) showEvidence({chart, tooltip: chart.tooltip}); else tooltip.hidden = true;});
    canvas.addEventListener("keydown", event => {
        if (event.key === "Escape") {pinned = false; tooltip.hidden = true; return;}
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        const available = [...new Map(datasets.flatMap((dataset, index) => chart.isDatasetVisible(index) ? dataset.intervalBounds.filter(interval => interval.end > Date.parse(day.start) && interval.start < Date.parse(day.end)).map(interval => [`${interval.start}:${interval.end}`, interval]) : [])).values()].sort((left, right) => left.start - right.start || left.end - right.end);
        if (!available.length) {tooltip.hidden = true; return;}
        event.preventDefault(); pinned = true;
        const previous = Math.max(0, available.findIndex(interval => interval.start === selectedInterval?.start && interval.end === selectedInterval?.end));
        const selected = event.key === "Home" ? available[0] : event.key === "End" ? available.at(-1) : available[Math.max(0, Math.min(available.length - 1, previous + (event.key === "ArrowLeft" ? -1 : 1)))];
        showIntervalEvidence(selected);
    });
    tooltip.addEventListener("mouseleave", () => {if (!pinned) tooltip.hidden = true;});
    tooltip.addEventListener("keydown", event => {if (event.key === "Escape") {pinned = false; tooltip.hidden = true; canvas.focus({preventScroll: true});}});
    const controls = document.createElement("div"); controls.className = "chart-legend quality-evidence-controls";
    controls.setAttribute("role", "group"); controls.setAttribute("aria-label", `${source.title}: Show in chart`);
    datasets.forEach((dataset, index) => {
        const label = document.createElement("label"), input = document.createElement("input"), swatch = document.createElement("span");
        input.type = "checkbox"; input.checked = !dataset.hidden;
        input.onchange = () => {
            const interval = selectedInterval;
            chart.setDatasetVisibility(index, input.checked);
            if (radiation) chart.options.scales.radiation.display = datasets.some((_, datasetIndex) => datasetIndex >= radiationOffset && chart.isDatasetVisible(datasetIndex));
            chart.update();
            if (pinned && interval) showIntervalEvidence(interval);
        };
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
        details.textContent = `Actual irradiance is estimated from ${radiation.source} satellite observations. Last fetched: ${radiation.received_at ? when(radiation.received_at) : "Unavailable"}. Compare both irradiance lines on the right axis (W/m²) to see if the weather forecast may explain a production difference.${radiation.issue ? ` ${radiation.issue}` : ""}`;
        plot.after(status, details);
    }
    return chart;
}
