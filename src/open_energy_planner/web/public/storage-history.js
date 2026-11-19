import {createPeriodSelector} from "./period-selector.js";
/** Share the latest daily observation or an explicitly refreshed live snapshot with the card. */
import "./chart_theme.js";
import {dateLabel, localDate, nextDate, useHomeAssistantTimezone, when} from "./display.js";

const READ_TIMEOUT_MILLISECONDS = 35000;
const MILLISECONDS_PER_DAY = 86400000;
const BYTES_PER_MIB = 1024 ** 2;
const BACKENDS = {sqlite: "SQLite", postgresql: "PostgreSQL", mysql: "MySQL", mariadb: "MariaDB"};

/** Own request ordering, keyboard inspection and chart teardown for this read-only page. */
export function bindStorageHistory(readToken, bytes, showDatabase) {
    const find = id => document.getElementById(id);
    const base = document.querySelector('meta[name="oep-api-base"]').content;
    let explicitPeriod = false;
    const period = createPeriodSelector(find("storage-period"), {label: "Database history period", onChange: () => {explicitPeriod = true; load();}});
    let chart = null, requestVersion = 0, samples = [], selected = 0, keyboardSelection = false, currentUsage = null, measurement = null, live = false;
    /** Expose the same measurement to pointer, keyboard and assistive readers. */
    function inspect(index) {
        selected = index;
        const row = samples[index];
        find("storage-history-selected").textContent = row ? `${row.current ? "Current: " : ""}${when(row.observed_at)}: ${bytes(row.database_bytes)}${row.database_estimated ? " (estimated)" : ""}: ${BACKENDS[row.backend] || "Unknown backend"}` : "";
    }
    /** Replace old evidence immediately so failed or reordered reads cannot show a stale period. */
    async function load(usage, current = false) {
        if (usage) {
            currentUsage = usage;
            live = current;
            measurement = current ? {...usage, current: true} : null;
            showDatabase(measurement || {...usage, database_bytes: null});
        }
        const version = ++requestVersion;
        chart?.destroy(); chart = null; samples = [];
        find("storage-history-plot").hidden = true;
        find("storage-observation-field").hidden = true;
        find("storage-observation").replaceChildren();
        find("storage-history-selected").textContent = "";
        find("storage-history-summary").textContent = "Loading database history…";
        try {
            const selected = period.value;
            const query = explicitPeriod ? new URLSearchParams({start: selected.start, end: selected.end}) : new URLSearchParams({period: "month"});
            const response = await fetch(base + `v1/storage/history?${query}`, {headers: {Authorization: `Bearer ${readToken()}`}, redirect: "error", cache: "no-store", signal: AbortSignal.timeout(READ_TIMEOUT_MILLISECONDS)});
            if (!response.ok) throw new Error();
            const history = await response.json();
            if (version !== requestVersion) return;
            useHomeAssistantTimezone(history.timezone);
            if (history.timezone) {
                period.set({start: history.start, end: history.end, min: history.calendar?.min_day, max: history.calendar?.max_day || localDate()});
                if (period.value.start !== history.start || period.value.end !== history.end) return load();
            }
            samples = history.samples;
            if (!live) {
                measurement = history.latest;
                showDatabase(measurement || {...currentUsage, database_bytes: null});
            }
            if (!history.timezone) {
                find("storage-history-summary").textContent = "Waiting for the Home Assistant installation timezone. Refresh after the connection is restored.";
                return;
            }
            const measurementDay = measurement && (measurement.day || localDate(measurement.observed_at));
            const start = live && history.period === "all" && measurementDay < history.start ? measurementDay : history.start;
            if (measurement) samples = samples.filter(row => row.day < measurementDay);
            if (measurementDay >= start && measurementDay <= history.end) {
                // Keep the view aligned with the displayed snapshot, including across local midnight.
                samples.push({...measurement, day: measurementDay});
            }
            const known = samples.filter(row => Number.isFinite(row.database_bytes));
            const first = known[0], last = known.at(-1);
            let summary = samples.length ? "Database size is unavailable for the observations in this period. Missing sizes remain gaps." : "No database size measurements in this period.";
            if (known.length === 1) summary = "";
            if (known.length > 1) {
                const change = last.database_bytes - first.database_bytes;
                const sameBackend = samples.every(row => row.backend === first.backend);
                const elapsedDays = (Date.parse(last.observed_at) - Date.parse(first.observed_at)) / MILLISECONDS_PER_DAY;
                summary = `${dateLabel(start)} – ${dateLabel(history.end)}: Measurements: ${known.length}: `;
                summary += sameBackend && elapsedDays > 0 ? `${change < 0 ? "Decrease" : "Increase"}: ${bytes(Math.abs(change))}: Average: ${change < 0 ? "−" : "+"}${bytes(Math.abs(change) / elapsedDays)} per day${known.some(row => row.database_estimated) ? " (estimated)" : ""}` : "Growth is not compared across different database backends.";
            }
            find("storage-history-summary").textContent = summary;
            if (!known.length) {
                if (samples.length) {
                    find("storage-observation").replaceChildren(...samples.map((row, index) => new Option(when(row.observed_at), String(index))));
                    find("storage-observation-field").hidden = false;
                    find("storage-observation").value = String(samples.length - 1);
                    inspect(samples.length - 1);
                }
                return;
            }
            const byDay = new Map(samples.map(row => [row.day, row]));
            const points = [];
            for (let day = start; day <= history.end; day = nextDate(day)) {
                const row = byDay.get(day);
                points.push({x: Date.parse(day + "T00:00:00Z"), y: row?.database_bytes == null ? null : row.database_bytes / BYTES_PER_MIB, row});
            }
            find("storage-history-plot").hidden = false;
            chart = new Chart(find("storage-history-chart"), {
                type: "line",
                data: {datasets: [{
                    label: "Database size", data: points, borderColor: "#087f8c", backgroundColor: "#087f8c",
                    borderWidth: 2, pointRadius: 2, pointHitRadius: 12, spanGaps: false,
                    segment: {borderColor: context => points[context.p0DataIndex].row?.backend !== points[context.p1DataIndex].row?.backend ? "transparent" : undefined},
                }]},
                options: {
                    responsive: true, maintainAspectRatio: false, animation: false,
                    onHover: (_event, elements) => {if (!keyboardSelection && elements[0]) inspect(samples.indexOf(points[elements[0].index].row));},
                    scales: {
                        x: {
                            type: "linear", min: points[0].x,
                            max: points.at(-1).x + (points.length === 1 ? MILLISECONDS_PER_DAY : 0),
                            afterBuildTicks: scale => {
                                const limit = scale.chart.width < 500 ? 3 : 5;
                                const stride = Math.max(1, Math.ceil((points.length - 1) / (limit - 1)));
                                scale.ticks = points.filter((_point, index) => index % stride === 0 && index < points.length - 1).map(point => ({value: point.x}));
                                scale.ticks.push({value: points.at(-1).x});
                            },
                            ticks: {maxRotation: 0, callback: value => dateLabel(new Date(value).toISOString().slice(0, 10))},
                        },
                        y: {title: {display: true, text: "MiB"}, ticks: {callback: value => value.toLocaleString(undefined, {maximumFractionDigits: 2})}},
                    },
                    plugins: {legend: {display: false}, tooltip: {callbacks: {
                        title: items => `${items[0].raw.row.current ? "Current: " : ""}${when(items[0].raw.row.observed_at)}`,
                        label: item => `${bytes(item.raw.row.database_bytes)}${item.raw.row.database_estimated ? " (estimated)" : ""}: ${BACKENDS[item.raw.row.backend] || "Unknown backend"}`,
                    }}},
                },
            });
            inspect(samples.length - 1);
        } catch {
            if (version === requestVersion) {
                chart?.destroy(); chart = null; samples = [];
                find("storage-history-plot").hidden = true;
                find("storage-observation-field").hidden = true;
                find("storage-history-selected").textContent = "";
                find("storage-history-summary").textContent = "Database history unavailable. Check the connection and choose Refresh to try again.";
            }
        }
    }
    find("storage-observation").addEventListener("change", event => inspect(Number(event.target.value)));
    find("storage-history-chart").addEventListener("keydown", event => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key) || !samples.length || !chart) return;
        event.preventDefault();
        keyboardSelection = true;
        inspect(event.key === "Home" ? 0 : event.key === "End" ? samples.length - 1 : Math.max(0, Math.min(samples.length - 1, selected + (event.key === "ArrowRight" ? 1 : -1))));
        const index = chart.data.datasets[0].data.findIndex(point => point.row === samples[selected]);
        const point = chart.getDatasetMeta(0).data[index];
        const active = [{datasetIndex: 0, index}];
        chart.setActiveElements(active);
        chart.tooltip.setActiveElements(active, {x: point.x, y: point.y});
        chart.update();
    });
    find("storage-history-chart").addEventListener("pointerdown", () => { keyboardSelection = false; });
    find("storage-history-chart").addEventListener("pointermove", () => { keyboardSelection = false; });
    return load;
}
