/** Scale visible data, synchronize comparison groups and share responsive legends without changing series. */
const AXIS_MARGIN_FRACTION = 0.10;
const VALUE_AXIS_INTERVALS = 6;
const AXIS_SIGNIFICANT_DIGITS = 14;
const NICE_INTERVALS = [1, 2, 2.5, 5, 10];
let synchronizingGroup = false;
let batchRanges = null;
const renderedStates = new WeakMap();
/** Exact numeric time bounds apply to every chart. Category bars keep their interval-sized end space. */
Chart.register({id: "timeAxisBounds", beforeDataLimits(_chart, {scale}) {
    if (scale.axis !== "x" || scale.type !== "linear") return;
    scale.options.offset = false;
    scale.options.bounds = "data";
}});
/** Remember rendered inputs rather than treating an unchanged range request as new data. */
function chartState(chart) {
    return JSON.stringify([chart.data, chart.data.datasets.map((_, index) => chart.isDatasetVisible(index)), chart.config.options.scales, chart.width, chart.height]);
}
/** An interval can overlap the window even when its plotted point is outside it. */
function isInWindow(point, interval, {lower, upper}) {
    if (interval?.start != null) return interval.end > lower && interval.start < upper;
    return point.x >= lower && point.x <= upper;
}
/** Include the visible part of a line segment when its points straddle an edge. */
function includeLineEdges(previous, point, {lower, upper}, include) {
    if (!previous || !(point.x > previous.x)) return;
    for (const boundary of [lower, upper]) {
        if (previous.x < boundary && boundary < point.x) {
            const rise = (point.y - previous.y) * (boundary - previous.x);
            include(previous.y + rise / (point.x - previous.x));
        }
    }
}
/** Scan one series, keeping missing points as line breaks and bars anchored at zero. */
function includeDataset(chartType, dataset, window, includePoint, include) {
    const bars = (dataset.type || chartType) === "bar";
    let previous = null;
    dataset.data.forEach((raw, index) => {
        const point = typeof raw === "number" ? {x: index, y: raw} : raw;
        if (!point || !Number.isFinite(point.y)) { previous = null; return; }
        const interval = dataset.intervalBounds?.[index] || (bars ? point : null);
        if (isInWindow(point, interval, window)) includePoint(point);
        if (!bars) includeLineEdges(previous, point, window, include);
        previous = point;
    });
    if (bars) include(0);
}
/** Collect visible extrema, with signed stack totals and interpolated line edges.
 * Interval values contribute whenever any part overlaps the selected window, even if their midpoint is outside.
 * Missing values break lines. Hidden datasets and the separate SoC axis contribute nothing.
 */
function visibleRange(chart, axis) {
    const x = chart.options.scales.x || {};
    const window = {lower: x.min ?? -Infinity, upper: x.max ?? Infinity};
    let minimum = Infinity, maximum = -Infinity;
    const stacks = new Map();
    const include = (value) => { minimum = Math.min(minimum, value); maximum = Math.max(maximum, value); };
    chart.data.datasets.forEach((dataset, datasetIndex) => {
        if (!chart.isDatasetVisible(datasetIndex) || (dataset.yAxisID || "y") !== axis) return;
        includeDataset(chart.config.type, dataset, window, (point) => {
            if (chart.options.scales[axis].stacked) {
                const key = JSON.stringify([dataset.stack || "default", point.x, point.y < 0]);
                stacks.set(key, (stacks.get(key) || 0) + point.y);
            } else include(point.y);
        }, include);
    });
    for (const value of stacks.values()) include(value);
    const suggested = chart.options.scales[axis];
    if (Number.isFinite(minimum) && Number.isFinite(suggested.suggestedMin)) include(suggested.suggestedMin);
    if (Number.isFinite(maximum) && Number.isFinite(suggested.suggestedMax)) include(suggested.suggestedMax);
    return {minimum, maximum};
}
/** Give empty ranges a finite span and honor axes whose displayed domain includes zero. */
function normalizedRange({minimum, maximum}, beginAtZero) {
    if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) { minimum = 0; maximum = 1; }
    if (beginAtZero) { minimum = Math.min(minimum, 0); maximum = Math.max(maximum, 0); }
    return {minimum, maximum};
}
/** Return an explicit physical end or the pinned zero end of a one-sided axis. */
function fixedBound(explicit, pinZero) {
    if (Number.isFinite(explicit)) return explicit;
    return pinZero ? 0 : null;
}
/** Calculate padded physical bounds for both redraw decisions and scale rendering. */
function axisBounds(range, options) {
    let {minimum, maximum} = normalizedRange(range, options.beginAtZero);
    const lowerLimit = fixedBound(options.min, options.beginAtZero && minimum === 0);
    const upperLimit = fixedBound(options.max, options.beginAtZero && maximum === 0 && minimum < 0);
    const fixedMinimum = lowerLimit !== null, fixedMaximum = upperLimit !== null;
    minimum = lowerLimit ?? minimum;
    maximum = upperLimit ?? maximum;
    if (minimum === maximum) {
        const span = Math.abs(minimum) || 1;
        if (!fixedMinimum) minimum -= span / 2;
        if (!fixedMaximum) maximum += span / 2;
    }
    const freeEnds = Number(!fixedMinimum) + Number(!fixedMaximum);
    const padding = (maximum - minimum) * AXIS_MARGIN_FRACTION / (1 - freeEnds * AXIS_MARGIN_FRACTION);
    return {min: fixedMinimum ? minimum : minimum - padding, max: fixedMaximum ? maximum : maximum + padding};
}
/** SoC keeps its own percentage domain and never affects visible value ranges. */
function isValueScale(scale) {
    return scale.axis === "y" && scale.id !== "soc";
}
/** Read the comparison group configured for a chart instance. */
function chartGroup(chart) {
    return chart.options.plugins.valueAxisHeadroom?.group;
}
/** Iterate other charts that share this chart's value-axis group. */
function* comparisonPeers(chart, group) {
    for (const peer of Object.values(Chart.instances)) {
        if (peer !== chart && chartGroup(peer) === group) yield peer;
    }
}
/** Enlarge a comparison range without changing the peer range it is based on. */
function includeRange(range, added) {
    range.minimum = Math.min(range.minimum, added.minimum);
    range.maximum = Math.max(range.maximum, added.maximum);
}
/** Collect one range for each comparison group and axis before drawing any charts. */
function collectBatchRanges(charts) {
    const ranges = new Map();
    for (const chart of charts) {
        const group = chartGroup(chart);
        if (!group) continue;
        for (const scale of Object.values(chart.scales).filter(isValueScale)) {
            const key = JSON.stringify([group, scale.id]);
            const shared = ranges.get(key) || {minimum: Infinity, maximum: -Infinity};
            includeRange(shared, visibleRange(chart, scale.id));
            ranges.set(key, shared);
        }
    }
    return ranges;
}
/** Update changed charts after complete groups' windows and visibility have been applied.
 * Visible group extrema exist only for this synchronous batch. Native resize or
 * standalone updates retain the ordinary plugin path. Failures cannot retain bounds.
 */
export function updateCharts(instances) {
    const charts = [...instances], previous = batchRanges;
    const ranges = collectBatchRanges(charts);
    batchRanges = ranges;
    try {
        for (const chart of charts) {
            const group = chartGroup(chart);
            const changedBounds = Object.values(chart.scales).filter(isValueScale).some(scale => {
                const range = ranges.get(JSON.stringify([group, scale.id])) || visibleRange(chart, scale.id);
                const bounds = axisBounds(range, scale.options);
                return bounds.min !== scale.min || bounds.max !== scale.max;
            });
            if (changedBounds || renderedStates.get(chart) !== chartState(chart)) {
                renderedStates.delete(chart);
                chart.update("none");
            }
        }
    }
    finally { batchRanges = previous; }
}
/** Use prepared batch extrema or merge the live peers for a standalone chart update. */
function comparisonRange(chart, axis, group) {
    const prepared = batchRanges?.get(JSON.stringify([group, axis]));
    if (prepared) return prepared;
    const range = visibleRange(chart, axis);
    if (group) {
        for (const peer of comparisonPeers(chart, group)) includeRange(range, visibleRange(peer, axis));
    }
    return range;
}
/** Keep ten percent of axis height at each unrestricted end, independently of nice ticks.
 * Explicit physical limits stay fixed. Only unrestricted ends receive padding.
 * Grouped comparisons share their visible extrema and refresh peers only when bounds change.
 * Constant/empty ranges receive finite fallback spans. Time and SoC axes are untouched.
 */
Chart.register({id: "valueAxisHeadroom", afterDataLimits(chart, {scale}, options) {
    if (!isValueScale(scale)) return;
    const bounds = axisBounds(comparisonRange(chart, scale.id, options.group), scale.options);
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
    if (!isValueScale(scale)) return;
    const step = scale.options.ticks.stepSize;
    scale.ticks = [];
    for (let index = Math.ceil(scale.min / step); index <= Math.floor(scale.max / step); index++) scale.ticks.push({value: Number((index * step).toPrecision(AXIS_SIGNIFICANT_DIGITS))});
}, afterRender(chart) {
    renderedStates.set(chart, chartState(chart));
}, afterUpdate(chart, _arguments, options) {
    if (batchRanges || !options.group || synchronizingGroup) return;
    synchronizingGroup = true;
    try {
        const scales = Object.values(chart.scales).filter(isValueScale);
        for (const peer of comparisonPeers(chart, options.group)) {
            const changed = scales.some(scale =>
                peer.scales[scale.id]?.min !== scale.min || peer.scales[scale.id]?.max !== scale.max);
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
