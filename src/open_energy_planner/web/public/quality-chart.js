import {calendarDays, calendarTime} from "./chart_days.js";
import {when, percentage} from "./display.js";

const HOUR_MILLISECONDS = 3600000;
const MAX_TIME_LABELS = 8;
const RADIATION_SERIES = [
    ["Forecast irradiance", "forecast_w_m2", "#b75c00", [8, 4]],
    ["Actual irradiance (satellite)", "satellite_w_m2", "#bd3449", []],
];

/** Keep model colours stable when a different model was selected at midnight. */
function modelColor(model) {
    if (model?.startsWith("ha-production")) return "#1a3060";
    if (model?.startsWith("pvwatts") && !model.includes("calibrated")) return "#b58800";
    return "#006da6";
}

/** Place recorded values at their interval midpoints without filling nulls or gaps. */
function intervalPoints(rows, field) {
    return rows.map(row => ({x: (Date.parse(row.start) + Date.parse(row.end)) / 2, y: row[field]}));
}

/** Bind a model line to its original forecast and comparison styling. */
function forecastDataset(forecast, index) {
    const color = modelColor(forecast.model);
    const borderDash = index === 0 ? [] : index % 2 ? [8, 4] : [2, 4];
    return {
        label: index ? forecast.model_label : `${forecast.model_label}: Original choice`,
        type: "line", order: 0, borderColor: color, backgroundColor: color, borderWidth: 2.5,
        borderDash, pointRadius: 0, pointHitRadius: 8, fill: false, spanGaps: false, tension: 0,
        yAxisID: "y", data: intervalPoints(forecast.rows, "predicted_kwh"),
    };
}

/** Keep each measured or predicted series on its own original evidence intervals. */
function bindIntervals(dataset, rows) {
    const bounds = rows.map(row => ({start: Date.parse(row.start), end: Date.parse(row.end)}));
    dataset.intervalBounds = bounds;
    if (dataset.type === "line") {
        dataset.segment = {
            borderColor: ({p0DataIndex, p1DataIndex}) =>
                bounds[p0DataIndex].end === bounds[p1DataIndex].start ? undefined : "transparent",
        };
    }
}

/** Assemble native energy and optional irradiance series with their independent row indices. */
function qualitySeries(source) {
    const forecasts = [source, ...(source.alternatives || [])];
    const datasets = [{
        label: "Measured", type: "bar", order: 1, backgroundColor: "#078b8080", borderWidth: 0,
        grouped: false, data: intervalPoints(source.rows, "measured_kwh"), yAxisID: "y",
    }, ...forecasts.map(forecastDataset)];
    const rows = [source.rows, ...forecasts.map(forecast => forecast.rows)];
    const radiationOffset = datasets.length;
    const radiation = source.radiation;
    if (radiation) {
        for (const [label, field, color, dash] of RADIATION_SERIES) {
            datasets.push({
                label, type: "line", order: 0, borderColor: color, backgroundColor: color,
                borderWidth: 2.8, borderDash: dash, pointRadius: 0, pointHitRadius: 8, fill: false,
                spanGaps: false, tension: 0, yAxisID: "radiation",
                data: intervalPoints(radiation.rows, field),
            });
            rows.push(radiation.rows);
        }
    }
    datasets.forEach((dataset, index) => bindIntervals(dataset, rows[index]));
    return {datasets, rows, forecasts, radiation, radiationOffset};
}

/** Fit partial visible intervals in pixels without altering recorded bounds or values. */
function fitPartialInterval(element, type, {start, end}, scale, dayStart, dayEnd) {
    const left = Math.max(start, dayStart);
    const right = Math.min(end, dayEnd);
    const fraction = Math.max(0, right - left) / (end - start);
    if (fraction >= 1) return;
    element.x = scale.getPixelForValue((left + right) / 2);
    if (type === "bar") element.width *= fraction;
}

/** Stroke one horizontal continuation using the dataset's current canvas style. */
function drawIntervalEdge(context, from, to, y) {
    context.beginPath();
    context.moveTo(from, y);
    context.lineTo(to, y);
    context.stroke();
}

/** Extend a line to the uncovered start or end of its current run, including gaps. */
function drawRunEdges(context, scale, bounds, points, index) {
    const point = points[index];
    if (point.skip) return;
    const {start, end} = bounds[index];
    if (end <= scale.min || start >= scale.max) return;
    if (bounds[index - 1]?.end !== start || points[index - 1].skip) {
        drawIntervalEdge(context, scale.getPixelForValue(start), point.x, point.y);
    }
    if (bounds[index + 1]?.start !== end || points[index + 1].skip) {
        drawIntervalEdge(context, point.x, scale.getPixelForValue(end), point.y);
    }
}

/** Draw recorded line edges and clip partial edge intervals to the selected day. */
function intervalBoundsPlugin(datasets, dayStart, dayEnd) {
    return {
        id: "quality-interval-bounds",
        /** Fit partial bar and line elements after Chart.js computes their positions. */
        afterDatasetsUpdate(chart) {
            datasets.forEach((dataset, datasetIndex) => {
                chart.getDatasetMeta(datasetIndex).data.forEach((element, index) => {
                    fitPartialInterval(
                        element, dataset.type, dataset.intervalBounds[index], chart.scales.x,
                        dayStart, dayEnd,
                    );
                });
            });
        },
        /** Draw both edges of each native data run using its own stroke and visible scale. */
        afterDatasetDraw(chart, {index, meta}) {
            const dataset = datasets[index];
            if (dataset.type !== "line") return;
            const context = chart.ctx;
            const area = chart.chartArea;
            context.save();
            context.beginPath();
            context.rect(area.left, area.top, area.right - area.left, area.bottom - area.top);
            context.clip();
            context.strokeStyle = dataset.borderColor;
            context.lineWidth = dataset.borderWidth;
            context.setLineDash(dataset.borderDash || []);
            dataset.intervalBounds.forEach((_, intervalIndex) => {
                drawRunEdges(context, chart.scales.x, dataset.intervalBounds, meta.data, intervalIndex);
            });
            context.restore();
        },
    };
}

/** Format only finite evidence values as measured quantities. */
function evidenceNumber(value) {
    return typeof value === "number" && Number.isFinite(value) ? value.toFixed(3) : "Unavailable";
}

/** Make the selected interval readable in both native and pinned detail. */
function intervalTitle(row, evidenceTime) {
    return `${evidenceTime(row.start)} to ${evidenceTime(row.end, {hour: "2-digit", minute: "2-digit"})}`;
}

/** Match both recorded boundaries instead of relying on the index of a different series. */
function intervalIndex(dataset, interval) {
    return dataset.intervalBounds.findIndex(candidate =>
        candidate.start === interval.start && candidate.end === interval.end);
}

/** Present one selected model's interval evidence and original provenance. */
function forecastEvidence(forecast, row, index) {
    const name = index ? forecast.model_label : `${forecast.model_label}: Original choice`;
    return [
        `${name}: ${evidenceNumber(row?.predicted_kwh)} kWh`,
        `Difference: ${evidenceNumber(row?.error_kwh)} kWh`,
        `Evaluation: ${row ? row.evaluation_issue || "Eligible" : "No matching forecast interval"}`,
        `Issued: ${when(forecast.issued_at)}`,
        `Forecast version: ${forecast.forecast_version ?? "Unavailable"}`,
    ];
}

/** Summarize weather evidence and the component's observed role in the active model. */
function weatherEvidence(source) {
    const requested = source.weather_sensitive ? "Requested" : "Not requested";
    const used = source.weather_features_used ? "Used" : "Not used";
    const composition = source.composition_selected
        ? "Included in the active household composition" : "Retained as a shadow component";
    return `Weather influence: ${requested}: Temperature features: ${used}: Individual forecast: ${composition}`;
}

/** Keep coverage, scoring and source caveats attached to the original comparison. */
function comparisonEvidence(source) {
    const lines = [
        `Original model: ${source.model_label}`,
        `Measured hours: ${source.coverage.measured_hours} of ${source.coverage.hours}: Comparable pairs: ${source.coverage.paired_hours}`,
        `MAE: ${evidenceNumber(source.metrics.mae_kwh)} kWh/hour: Bias: ${evidenceNumber(source.metrics.bias_kwh)} kWh/hour`,
        `RMSE: ${evidenceNumber(source.metrics.rmse_kwh)} kWh/hour: WAPE: ${percentage(source.metrics.wape_percent)}`,
        `Latest measurement receipt: ${source.last_received_at ? when(source.last_received_at) : "Unavailable"}`,
    ];
    if (source.measurement_boundary) lines.push(`Measurement boundary: ${source.measurement_boundary}`);
    if (source.comparison_issue) lines.push(source.comparison_issue);
    if (source.weather_sensitive != null) lines.push(weatherEvidence(source));
    if (source.daily.relative_error_percent != null) {
        lines.push(`Relative daily error: ${percentage(source.daily.relative_error_percent)}`);
    }
    return lines;
}

/** Assemble visible values from their exact independent rows followed by comparison evidence. */
function selectedIntervalEvidence(source, series, interval, visible, title) {
    const {datasets, rows, forecasts, radiationOffset} = series;
    const matchedRow = index => rows[index][intervalIndex(datasets[index], interval)];
    const lines = [title];
    if (visible[0]) lines.push(`Measured: ${evidenceNumber(matchedRow(0)?.measured_kwh)} kWh`);
    for (const [index, forecast] of forecasts.entries()) {
        if (visible[index + 1]) lines.push(...forecastEvidence(forecast, matchedRow(index + 1), index));
    }
    for (const [offset, [, field]] of RADIATION_SERIES.entries()) {
        const index = radiationOffset + offset;
        if (visible[index]) {
            lines.push(`${datasets[index].label}: ${evidenceNumber(matchedRow(index)?.[field])} W/m²`);
        }
    }
    return lines.concat(comparisonEvidence(source));
}

/** Enumerate distinct visible intervals intersecting the selected day in chronological order. */
function visibleIntervals(chart, datasets, dayStart, dayEnd) {
    const intervals = new Map();
    datasets.forEach((dataset, index) => {
        if (!chart.isDatasetVisible(index)) return;
        for (const interval of dataset.intervalBounds) {
            if (interval.end > dayStart && interval.start < dayEnd) {
                intervals.set(`${interval.start}:${interval.end}`, interval);
            }
        }
    });
    return [...intervals.values()].sort((left, right) => left.start - right.start || left.end - right.end);
}

/** Resolve a keyboard selection while retaining the chart's bounded navigation. */
function keyboardInterval(key, available, previous) {
    if (key === "Home") return available[0];
    if (key === "End") return available.at(-1);
    const previousIndex = Math.max(0, available.findIndex(interval =>
        interval.start === previous?.start && interval.end === previous?.end));
    const next = previousIndex + (key === "ArrowLeft" ? -1 : 1);
    return available[Math.max(0, Math.min(available.length - 1, next))];
}

/** Create the accessible detail panel and associate it with its canvas. */
function hourlyEvidencePanel(canvas, plot) {
    const tooltip = document.createElement("div");
    tooltip.className = "quality-tooltip";
    tooltip.id = `${canvas.id}-evidence`;
    tooltip.hidden = true;
    tooltip.tabIndex = 0;
    tooltip.setAttribute("role", "region");
    tooltip.setAttribute("aria-label", "Hourly forecast evidence");
    tooltip.setAttribute("aria-live", "polite");
    plot.append(tooltip);
    canvas.tabIndex = 0;
    canvas.setAttribute("aria-describedby", tooltip.id);
    canvas.setAttribute("aria-keyshortcuts", "ArrowLeft ArrowRight Home End Escape");
    return tooltip;
}

/** Own one chart's hover, touch and keyboard detail selection. */
function qualityEvidence(canvas, plot, source, series, dayStart, dayEnd, evidenceTime) {
    const tooltip = hourlyEvidencePanel(canvas, plot);
    const {datasets, rows} = series;
    let selectedInterval, renderedInterval, pinned = false;

    /** Dismiss both pointer and pinned evidence. */
    function dismiss() {
        pinned = false;
        tooltip.hidden = true;
    }

    const close = document.createElement("button");
    close.type = "button";
    close.textContent = "Close";
    close.setAttribute("aria-label", "Close hourly evidence");
    close.addEventListener("click", () => {
        dismiss();
        canvas.focus({preventScroll: true});
    });

    /** Update selected evidence from Chart.js without showing unpinned hover on narrow screens. */
    function showEvidence({chart, tooltip: state}) {
        if (window.matchMedia("(max-width:640px)").matches && !pinned) {
            tooltip.hidden = true;
            return;
        }
        if (!state.opacity) {
            const outside = !pinned && !tooltip.matches(":hover");
            if (outside || !datasets.some((_, index) => chart.isDatasetVisible(index))) tooltip.hidden = true;
            return;
        }
        const point = state.dataPoints[0];
        if (!point) return;
        selectedInterval = datasets[point.datasetIndex].intervalBounds[point.dataIndex];
        const visible = datasets.map((_, index) => chart.isDatasetVisible(index));
        const key = [selectedInterval.start, selectedInterval.end, ...visible].join(":");
        if (key !== renderedInterval) {
            renderedInterval = key;
            const hour = rows[point.datasetIndex][point.dataIndex];
            const title = intervalTitle(hour, evidenceTime);
            const lines = selectedIntervalEvidence(source, series, selectedInterval, visible, title);
            tooltip.replaceChildren(close, ...lines.map(line => {
                const paragraph = document.createElement("p");
                paragraph.textContent = line;
                return paragraph;
            }));
        }
        tooltip.hidden = false;
    }

    /** Refresh detail from a visible interval, including its recorded null values. */
    function showIntervalEvidence(chart, interval) {
        const points = datasets.flatMap((dataset, datasetIndex) => {
            const dataIndex = intervalIndex(dataset, interval);
            return chart.isDatasetVisible(datasetIndex) && dataIndex >= 0 ? [{datasetIndex, dataIndex}] : [];
        });
        if (!points.length) {
            tooltip.hidden = true;
            return;
        }
        showEvidence({chart, tooltip: {opacity: 1, dataPoints: points}});
    }

    /** Keep pinned evidence on its selected interval across a synchronous chart update. */
    function refreshSelection(chart, update) {
        const interval = selectedInterval;
        update();
        if (pinned && interval) showIntervalEvidence(chart, interval);
    }

    /** Bind input after Chart.js has installed and initialized its own interactions. */
    function bindChart(chart) {
        canvas.addEventListener("click", () => {
            pinned = !pinned;
            if (pinned) showEvidence({chart, tooltip: chart.tooltip});
            else tooltip.hidden = true;
        });
        canvas.addEventListener("keydown", event => {
            if (event.key === "Escape") return dismiss();
            if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
            const available = visibleIntervals(chart, datasets, dayStart, dayEnd);
            if (!available.length) {
                tooltip.hidden = true;
                return;
            }
            event.preventDefault();
            pinned = true;
            showIntervalEvidence(chart, keyboardInterval(event.key, available, selectedInterval));
        });
        tooltip.addEventListener("mouseleave", () => {
            if (!pinned) tooltip.hidden = true;
        });
        tooltip.addEventListener("keydown", event => {
            if (event.key !== "Escape") return;
            dismiss();
            canvas.focus({preventScroll: true});
        });
    }

    return {showEvidence, bindChart, refreshSelection};
}

/** Build stable hourly ticks without changing the chart's recorded interval boundaries. */
function buildHourTicks(scale) {
    const first = Math.ceil(scale.min / HOUR_MILLISECONDS);
    const last = Math.floor(scale.max / HOUR_MILLISECONDS);
    const stride = Math.max(1, Math.ceil((last - first) / (MAX_TIME_LABELS - 1)));
    scale.ticks = [];
    for (let hour = first; hour <= last; hour += stride) {
        scale.ticks.push({value: hour * HOUR_MILLISECONDS});
    }
}

/** Create the chart with independent energy and optional radiation units. */
function createQualityChart(canvas, series, dayStart, dayEnd, evidenceTime, showEvidence) {
    const {datasets, rows, radiation} = series;
    const scales = {
        x: {
            type: "linear", offset: false, min: dayStart, max: dayEnd, afterBuildTicks: buildHourTicks,
            ticks: {stepSize: HOUR_MILLISECONDS, maxTicksLimit: MAX_TIME_LABELS, callback: calendarTime},
        },
        y: {min: 0, title: {display: true, text: "Energy per hour (kWh)"}},
    };
    if (radiation) {
        scales.radiation = {
            display: true, position: "right", min: 0, grid: {drawOnChartArea: false},
            title: {display: true, text: "Global horizontal irradiance (W/m²)"},
        };
    }
    return new Chart(canvas, {
        type: "bar", plugins: [intervalBoundsPlugin(datasets, dayStart, dayEnd), calendarDays],
        data: {datasets},
        options: {
            responsive: true, maintainAspectRatio: false, parsing: false, animation: false,
            interaction: {mode: "nearest", axis: "x", intersect: false}, scales,
            plugins: {
                legend: {display: false},
                tooltip: {
                    enabled: false, animation: false, external: showEvidence, bodyFont: {family: "monospace"},
                    callbacks: {
                        title: ([item]) => intervalTitle(rows[item.datasetIndex][item.dataIndex], evidenceTime),
                        label: item => `${item.dataset.label}: ${item.parsed.y == null ? "Unavailable" : item.parsed.y.toFixed(3)} ${item.dataset.yAxisID === "radiation" ? "W/m²" : "kWh"}`,
                    },
                },
            },
        },
    });
}

/** Build a decorative swatch matching the measured bar or forecast line. */
function seriesSwatch(dataset) {
    const swatch = document.createElement("span");
    const bar = dataset.type === "bar";
    swatch.className = bar ? "series-swatch" : "quality-line-swatch";
    swatch.style.setProperty("--series-color", dataset.borderColor || "#078b80");
    if (bar) {
        swatch.style.backgroundColor = "#078b8080";
        swatch.style.border = "0";
    } else {
        swatch.style.borderTopStyle = dataset.borderDash?.length
            ? dataset.borderDash[0] > 2 ? "dashed" : "dotted"
            : "solid";
    }
    swatch.setAttribute("aria-hidden", "true");
    return swatch;
}

/** Create visible keyboard-operable toggles and retain the independent radiation-axis state. */
function evidenceControls(chart, title, series, refreshSelection) {
    const {datasets, radiation, radiationOffset} = series;
    const controls = document.createElement("div");
    controls.className = "chart-legend quality-evidence-controls";
    controls.setAttribute("role", "group");
    controls.setAttribute("aria-label", `${title}: Show in chart`);
    datasets.forEach((dataset, index) => {
        const label = document.createElement("label");
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = !dataset.hidden;
        input.onchange = () => refreshSelection(chart, () => {
            chart.setDatasetVisibility(index, input.checked);
            if (radiation) {
                chart.options.scales.radiation.display = datasets.some((_, datasetIndex) =>
                    datasetIndex >= radiationOffset && chart.isDatasetVisible(datasetIndex));
            }
            chart.update();
        });
        label.append(input, seriesSwatch(dataset), document.createTextNode(dataset.label));
        controls.append(label);
    });
    return controls;
}

/** Display independent satellite coverage and fetch provenance under its chart. */
function showRadiationStatus(plot, radiation) {
    const status = document.createElement("p");
    status.className = "quality-radiation-status";
    status.textContent = `Satellite data complete through: ${radiation.complete_until ? when(radiation.complete_until) : "Unavailable"}: Coverage: ${radiation.observed_hours} of ${radiation.hours} hours`;
    const details = document.createElement("p");
    details.className = "quality-radiation-source";
    details.textContent = `Actual irradiance is estimated from ${radiation.source} satellite observations. Last fetched: ${radiation.received_at ? when(radiation.received_at) : "Unavailable"}. Compare both irradiance lines on the right axis (W/m²) to see if the weather forecast may explain a production difference.${radiation.issue ? ` ${radiation.issue}` : ""}`;
    plot.after(status, details);
}

/** Draw one frozen comparison with independent units, gaps and accessible series controls. */
export function qualityChart(canvas, plot, source, day, evidenceTime) {
    const dayStart = Date.parse(day.start);
    const dayEnd = Date.parse(day.end);
    const series = qualitySeries(source);
    const evidence = qualityEvidence(canvas, plot, source, series, dayStart, dayEnd, evidenceTime);
    const chart = createQualityChart(canvas, series, dayStart, dayEnd, evidenceTime, evidence.showEvidence);
    evidence.bindChart(chart);
    plot.append(evidenceControls(chart, source.title, series, evidence.refreshSelection));
    if (series.radiation) showRadiationStatus(plot, series.radiation);
    return chart;
}
