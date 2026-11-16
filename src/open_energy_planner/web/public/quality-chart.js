import {calendarDays, calendarTime} from "./chart_days.js";
import {when} from "./display.js";

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
    const chart = new Chart(canvas, {type: "bar", plugins: [intervals, calendarDays], data: {datasets}, options: {responsive: true, maintainAspectRatio: false, parsing: false, animation: false, interaction: {mode: "nearest", axis: "x", intersect: false}, scales: {
        x: {type: "linear", offset: false, min: Date.parse(day.start), max: Date.parse(day.end), afterBuildTicks: scale => {const first = Math.ceil(scale.min / HOUR_MILLISECONDS), last = Math.floor(scale.max / HOUR_MILLISECONDS), stride = Math.max(1, Math.ceil((last - first) / (MAX_TIME_LABELS - 1))); scale.ticks = []; for (let hour = first; hour <= last; hour += stride) scale.ticks.push({value: hour * HOUR_MILLISECONDS});}, ticks: {stepSize: HOUR_MILLISECONDS, maxTicksLimit: MAX_TIME_LABELS, callback: calendarTime}},
        y: {min: 0, title: {display: true, text: "Energy per hour (kWh)"}},
        ...(radiation ? {radiation: {display: false, position: "right", min: 0, grid: {drawOnChartArea: false}, title: {display: true, text: "Global horizontal irradiance (W/m²)"}}} : {}),
    }, plugins: {legend: {display: false}, tooltip: {bodyFont: {family: "monospace"}, callbacks: {
        title: items => {const dataset = items[0].dataset, row = dataset.yAxisID === "radiation" ? radiation.rows[items[0].dataIndex] : source.rows[items[0].dataIndex]; return `${evidenceTime(row.start)} to ${evidenceTime(row.end, {hour: "2-digit", minute: "2-digit"})}`;},
        label: item => `${item.dataset.label}: ${item.parsed.y == null ? "Unavailable" : item.parsed.y.toFixed(3)} ${item.dataset.yAxisID === "radiation" ? "W/m²" : "kWh"}`,
    }}}}});
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
