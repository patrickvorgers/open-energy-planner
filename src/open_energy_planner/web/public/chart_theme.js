/** Scale visible data, synchronize comparison groups and share responsive legends without changing series. */
const mobile = window.matchMedia("(max-width: 640px)");
const AXIS_MARGIN_FRACTION = 0.10;
const VALUE_AXIS_INTERVALS = 6;
const AXIS_SIGNIFICANT_DIGITS = 14;
const NICE_INTERVALS = [1, 2, 2.5, 5, 10];
let synchronizingGroup = false;
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
/** Keep ten percent of axis height at each unrestricted end, independently of nice ticks.
 * Explicit physical limits stay fixed. Only unrestricted ends receive padding.
 * Grouped comparisons share their visible extrema and refresh peers after updates.
 * Constant/empty ranges receive finite fallback spans. Time and SoC axes are untouched.
 */
Chart.register({id: "valueAxisHeadroom", afterDataLimits(chart, {scale}, options) {
    if (scale.axis !== "y" || scale.id === "soc") return;
    let {minimum, maximum} = visibleRange(chart, scale.id);
    if (options.group) for (const peer of Object.values(Chart.instances)) {
        if (peer === chart || peer.options.plugins.valueAxisHeadroom?.group !== options.group) continue;
        const range = visibleRange(peer, scale.id);
        minimum = Math.min(minimum, range.minimum); maximum = Math.max(maximum, range.maximum);
    }
    const fixedMinimum = Number.isFinite(scale.options.min), fixedMaximum = Number.isFinite(scale.options.max);
    if (!Number.isFinite(minimum) || !Number.isFinite(maximum)) { minimum = 0; maximum = 1; }
    if (fixedMinimum) minimum = scale.options.min;
    if (fixedMaximum) maximum = scale.options.max;
    if (minimum === maximum) { const span = Math.abs(minimum) || 1; if (!fixedMinimum) minimum -= span / 2; if (!fixedMaximum) maximum += span / 2; }
    const freeEnds = Number(!fixedMinimum) + Number(!fixedMaximum);
    const padding = (maximum - minimum) * AXIS_MARGIN_FRACTION / (1 - freeEnds * AXIS_MARGIN_FRACTION);
    scale.min = fixedMinimum ? minimum : minimum - padding; scale.max = fixedMaximum ? maximum : maximum + padding;
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
}, afterUpdate(chart, _arguments, options) {
    if (!options.group || synchronizingGroup) return;
    synchronizingGroup = true;
    try {
        for (const peer of Object.values(Chart.instances)) {
            if (peer !== chart && peer.options.plugins.valueAxisHeadroom?.group === options.group) peer.update("none");
        }
    } finally { synchronizingGroup = false; }
}});
/** Apply the same legend placement to new and existing Chart.js instances. */
function positionLegends() {
    const position = mobile.matches ? "bottom" : "right";
    Chart.defaults.plugins.legend.position = position;
    for (const chart of Object.values(Chart.instances)) {
        chart.options.plugins.legend.position = position;
        chart.update("none");
    }
}
positionLegends();
mobile.addEventListener("change", positionLegends);
