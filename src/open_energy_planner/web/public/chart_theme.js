/** Scale visible data, synchronize comparison groups and share responsive legends without changing series. */
const AXIS_MARGIN_FRACTION = 0.10;
const VALUE_AXIS_INTERVALS = 6;
const AXIS_SIGNIFICANT_DIGITS = 14;
const NICE_INTERVALS = [1, 2, 2.5, 5, 10];
let synchronizingGroup = false;
let batchRanges = null;
const renderedStates = new WeakMap();
/** Remember rendered inputs rather than treating an unchanged range request as new data. */
function chartState(chart) {
    return JSON.stringify([chart.data, chart.data.datasets.map((_, index) => chart.isDatasetVisible(index)), chart.config.options.scales, chart.width, chart.height]);
}
/** Collect visible extrema, with signed stack totals and interpolated line edges.
 * Interval bars contribute whenever any part overlaps the selected window.
 * Missing values break lines. Hidden datasets and the separate SoC axis contribute nothing.
 */
function visibleRange(chart, axis) {
    const x = chart.options.scales.x || {}, lower = x.min ?? -Infinity, upper = x.max ?? Infinity;
    let minimum = Infinity, maximum = -Infinity;
    const stacks = new Map();
    const include = (value) => { minimum = Math.min(minimum, value); maximum = Math.max(maximum, value); };
    chart.data.datasets.forEach((dataset, datasetIndex) => {
        if (!chart.isDatasetVisible(datasetIndex) || (dataset.yAxisID || "y") !== axis) return;
        const bars = (dataset.type || chart.config.type) === "bar";
        let previous = null;
        dataset.data.forEach((raw, index) => {
            const point = typeof raw === "number" ? {x: index, y: raw} : raw;
            if (!point || !Number.isFinite(point.y)) { previous = null; return; }
            const inside = bars && point.start != null ? point.end > lower && point.start < upper : point.x >= lower && point.x <= upper;
            if (inside) {
                if (chart.options.scales[axis].stacked) {
                    const key = JSON.stringify([dataset.stack || "default", point.x, point.y < 0]);
                    stacks.set(key, (stacks.get(key) || 0) + point.y);
                } else include(point.y);
            }
            if (!bars && previous && point.x > previous.x) {
                for (const boundary of [lower, upper]) {
                    if (previous.x < boundary && boundary < point.x) include(previous.y + (point.y - previous.y) * (boundary - previous.x) / (point.x - previous.x));
                }
            }
            previous = point;
        });
        if (bars) include(0);
    });
    for (const value of stacks.values()) include(value);
    const suggested = chart.options.scales[axis];
    if (Number.isFinite(minimum) && Number.isFinite(suggested.suggestedMin)) include(suggested.suggestedMin);
    if (Number.isFinite(maximum) && Number.isFinite(suggested.suggestedMax)) include(suggested.suggestedMax);
    return {minimum, maximum};
}
/** Calculate padded physical bounds for both redraw decisions and scale rendering. */
function axisBounds({minimum, maximum}, options) {
    if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) { minimum = 0; maximum = 1; }
    if (options.beginAtZero) { minimum = Math.min(minimum, 0); maximum = Math.max(maximum, 0); }
    const fixedMinimum = Number.isFinite(options.min) || options.beginAtZero && minimum === 0;
    const fixedMaximum = Number.isFinite(options.max) || options.beginAtZero && maximum === 0 && minimum < 0;
    if (fixedMinimum) minimum = Number.isFinite(options.min) ? options.min : 0;
    if (fixedMaximum) maximum = Number.isFinite(options.max) ? options.max : 0;
    if (minimum === maximum) { const span = Math.abs(minimum) || 1; if (!fixedMinimum) minimum -= span / 2; if (!fixedMaximum) maximum += span / 2; }
    const freeEnds = Number(!fixedMinimum) + Number(!fixedMaximum);
    const padding = (maximum - minimum) * AXIS_MARGIN_FRACTION / (1 - freeEnds * AXIS_MARGIN_FRACTION);
    return {min: fixedMinimum ? minimum : minimum - padding, max: fixedMaximum ? maximum : maximum + padding};
}
/** Update changed charts after complete groups' windows and visibility have been applied.
 * Visible group extrema exist only for this synchronous batch. Native resize or
 * standalone updates retain the ordinary plugin path. Failures cannot retain bounds.
 */
export function updateCharts(instances) {
    const charts = [...instances], previous = batchRanges, ranges = new Map();
    for (const chart of charts) {
        const group = chart.options.plugins.valueAxisHeadroom?.group;
        if (!group) continue;
        for (const scale of Object.values(chart.scales)) {
            if (scale.axis !== "y" || scale.id === "soc") continue;
            const key = JSON.stringify([group, scale.id]), range = visibleRange(chart, scale.id);
            const shared = ranges.get(key) || {minimum: Infinity, maximum: -Infinity};
            shared.minimum = Math.min(shared.minimum, range.minimum);
            shared.maximum = Math.max(shared.maximum, range.maximum);
            ranges.set(key, shared);
        }
    }
    batchRanges = ranges;
    try {
        for (const chart of charts) {
            const group = chart.options.plugins.valueAxisHeadroom?.group;
            const changedBounds = Object.values(chart.scales).some(scale => {
                if (scale.axis !== "y" || scale.id === "soc") return false;
                const range = ranges.get(JSON.stringify([group, scale.id])) || visibleRange(chart, scale.id);
                const bounds = axisBounds(range, scale.options);
                return bounds.min !== scale.min || bounds.max !== scale.max;
            });
            if (changedBounds || renderedStates.get(chart) !== chartState(chart)) chart.update("none");
        }
    }
    finally { batchRanges = previous; }
}
/** Keep ten percent of axis height at each unrestricted end, independently of nice ticks.
 * Explicit physical limits stay fixed. Only unrestricted ends receive padding.
 * Grouped comparisons share their visible extrema and refresh peers only when bounds change.
 * Constant/empty ranges receive finite fallback spans. Time and SoC axes are untouched.
 */
Chart.register({id: "valueAxisHeadroom", afterDataLimits(chart, {scale}, options) {
    if (scale.axis !== "y" || scale.id === "soc") return;
    const prepared = batchRanges?.get(JSON.stringify([options.group, scale.id]));
    let {minimum, maximum} = prepared || visibleRange(chart, scale.id);
    if (!prepared && options.group) for (const peer of Object.values(Chart.instances)) {
        if (peer === chart || peer.options.plugins.valueAxisHeadroom?.group !== options.group) continue;
        const range = visibleRange(peer, scale.id);
        minimum = Math.min(minimum, range.minimum); maximum = Math.max(maximum, range.maximum);
    }
    const bounds = axisBounds({minimum, maximum}, scale.options);
    scale.min = bounds.min; scale.max = bounds.max;
    const ideal = (scale.max - scale.min) / VALUE_AXIS_INTERVALS, magnitude = 10 ** Math.floor(Math.log10(ideal));
    const step = NICE_INTERVALS.find(value => value * magnitude >= ideal) * magnitude;
    scale.options.bounds = "data";
    scale.options.grace = 0;
    scale.options.ticks.includeBounds = false;
    scale.options.ticks.stepSize = step;
    scale.options.ticks.maxTicksLimit = VALUE_AXIS_INTERVALS + 1;
    if (options.group) scale.options.ticks.autoSkip = false;
}, afterBuildTicks(_chart, {scale}) {
    if (scale.axis !== "y" || scale.id === "soc") return;
    const step = scale.options.ticks.stepSize;
    scale.ticks = [];
    for (let index = Math.ceil(scale.min / step); index <= Math.floor(scale.max / step); index++) scale.ticks.push({value: Number((index * step).toPrecision(AXIS_SIGNIFICANT_DIGITS))});
}, afterRender(chart) {
    renderedStates.set(chart, chartState(chart));
}, afterUpdate(chart, _arguments, options) {
    if (batchRanges || !options.group || synchronizingGroup) return;
    synchronizingGroup = true;
    try {
        for (const peer of Object.values(Chart.instances)) {
            if (peer === chart || peer.options.plugins.valueAxisHeadroom?.group !== options.group) continue;
            const changed = Object.values(chart.scales).some(scale => scale.axis === "y" && scale.id !== "soc"
                && (peer.scales[scale.id]?.min !== scale.min || peer.scales[scale.id]?.max !== scale.max));
            if (changed) peer.update("none");
        }
    } finally { synchronizingGroup = false; }
}});
// Keep native legends below the plot, including charts created before the theme loads.
Chart.defaults.plugins.legend.position = "bottom";
for (const chart of Object.values(Chart.instances)) {
    chart.options.plugins.legend.position = "bottom";
    chart.update("none");
}
