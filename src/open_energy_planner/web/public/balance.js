// Presentation of immutable, independently validated physical flows. Never optimize or infer energy routes here.
const FLOW_COLORS = Object.freeze({solar: "#23965e", import: "#2586c4", export: "#56bac7", discharge: "#d45464", charge: "#8560b5", load: "#d29323", losses: "#88949f"});
const MILLISECONDS_PER_HOUR = 3600000;
export const QUARTER_MILLISECONDS = MILLISECONDS_PER_HOUR / 4;
const TIMESTAMP_ROUNDOFF_MILLISECONDS = 1;
const PERCENT = 100;
const BALANCE_TOLERANCE_KWH = 0.00001;
// Match the independent physical validator's per-flow roundoff allowance. Never relax conservation to make bars fit.
const DISPLAY_ZERO_TOLERANCE_KWH = 0.0000001;
const SOC_COLORS = ["#324f9d", "#78541d", "#5e3979", "#22665b"];
const APPLIANCE_COLOR = "#b55a9d";
const EV_COLOR = "#5868b5";
const THERMAL_COLORS = ["#b9673c", "#8f7441", "#a14e66", "#8b648c"];
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const energy = (value) => finite(value) && value >= -DISPLAY_ZERO_TOLERANCE_KWH;
const efficiency = (value) => finite(value) && value > 0 && value <= 1;
const total = (values) => values.reduce((sum, value) => sum + value, 0);

/** Project balance bars onto UTC quarters without changing archived flows or SoC points.
 * Weight mean powers by overlap duration. Coarse intervals retain their recorded mean.
 * Partial horizon edges average only their covered duration and expose that coverage.
 * Gaps, overlaps and unavailable values invalidate the affected quarter, never become zero.
 */
export function quarterBalanceSeries(series) {
    return series.map((entry) => {
        if (entry.type !== "bar" || !entry.intervals?.length) return entry;
        const source = entry.intervals;
        const first = source[0].start, last = Math.round(source.at(-1).end);
        const intervals = [];
        for (let start = Math.floor(first / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS; start < last; start += QUARTER_MILLISECONDS) {
            const end = start + QUARTER_MILLISECONDS;
            const coveredStart = Math.max(start, first), coveredEnd = Math.min(end, last);
            let cursor = coveredStart, weighted = 0, valid = true;
            for (const row of source) {
                const left = Math.max(coveredStart, row.start), right = Math.min(coveredEnd, Math.round(row.end));
                if (right <= left) continue;
                if (Math.abs(left - cursor) > TIMESTAMP_ROUNDOFF_MILLISECONDS || !finite(row.value)) valid = false;
                weighted += (right - left) * row.value;
                cursor = right;
            }
            if (Math.abs(cursor - coveredEnd) > TIMESTAMP_ROUNDOFF_MILLISECONDS) valid = false;
            intervals.push({start, end, coveredStart, coveredEnd, value: valid ? weighted / (coveredEnd - coveredStart) : null});
        }
        return {...entry, intervals};
    });
}

/** Require exactly one model flow per frozen thermal store, without filling absent evidence. */
function thermalEnergy(flow, work) {
    if (!work.length) return 0;
    if (!Array.isArray(flow.thermal) || flow.thermal.length !== work.length
        || new Set(work.map((store) => store.device_id)).size !== work.length
        || !work.every((store) => flow.thermal.filter((row) => row && row.device_id === store.device_id && energy(row.ac_kwh)).length === 1)) return null;
    return total(flow.thermal.map((row) => row.ac_kwh));
}

/** Convert signed kWh to mean kW using duration_h. Inflows are positive, outflows negative and null values remain gaps. */
function flowSeries(name, color, intervals, values, tooltipGroup) {
    return {name, color, tooltipGroup, type: "bar", stack: "energy", unit: "kW", intervals: intervals.map((interval, index) => ({
        start: Date.parse(interval.start), end: Date.parse(interval.start) + interval.duration_h * MILLISECONDS_PER_HOUR,
        value: values[index] == null ? null : Math.abs(values[index]) <= DISPLAY_ZERO_TOLERANCE_KWH ? 0 : values[index] / interval.duration_h
    }))};
}

/**
 * Express internal stored kWh as percentages of frozen capacity at interval endpoints.
 * Start with initial_kwh, then use the previous end_kwh. Include the final end state.
 * Invalid or absent states remain null. Roundoff within the balance tolerance is clamped to 0–100%.
 */
function socSeries(plan, intervals, units, labels) {
    return units.map((unit) => {
        const points = [];
        intervals.forEach((interval, index) => {
            const start = Date.parse(interval.start), end = start + interval.duration_h * MILLISECONDS_PER_HOUR;
            const previous = index ? plan.expected_flows?.[index - 1]?.batteries?.find((battery) => battery.device_id === unit.device_id)?.end_kwh : unit.battery.initial_kwh;
            const stored = plan.expected_flows?.[index]?.batteries?.find((battery) => battery.device_id === unit.device_id)?.end_kwh;
            const percentage = (value) => finite(value) && value >= -BALANCE_TOLERANCE_KWH && value <= unit.battery.capacity_kwh + BALANCE_TOLERANCE_KWH && unit.battery.capacity_kwh > 0 ? Math.max(0, Math.min(PERCENT, value / unit.battery.capacity_kwh * PERCENT)) : null;
            points.push([start, percentage(previous)], [end, percentage(stored)]);
        });
        const identityIndex = plan.input_snapshot.problem.batteries.findIndex((battery) => battery.device_id === unit.device_id);
        return {name: `${labels.get(unit.device_id) || "Battery"}: SoC`, points, color: SOC_COLORS[identityIndex % SOC_COLORS.length], axis: "soc", unit: "%", tooltipGroup: "Battery state"};
    });
}

/**
 * Build signed kWh balances, then convert to mean kW: solar/import/discharge supply
 * positive energy. Load/export/charge/losses consume negative energy. Installation
 * grid/load boundaries are AC. Solar and battery terminals are DC. Converter groups
 * use their own AC input/output and local DC terminals. Losses use declared inverter
 * and battery DC efficiencies, never a residual. Internal storage losses affect SoC.
 * Invalid timing, efficiencies, missing evidence or failed conservation within
 * BALANCE_TOLERANCE_KWH produce null interval gaps counted in missing.
 */
export function energyBalances(plan, labels) {
    const problem = plan.input_snapshot?.problem || {}, intervals = problem.intervals || [];
    const units = problem.batteries || [], sources = problem.pv_sources || [], inverters = problem.inverters || [];
    const groups = [{id: "plant", name: "Installation energy balance", units},
        ...inverters.filter((inverter) => units.some((unit) => unit.inverter_id === inverter.inverter_id))
            .map((inverter, index) => ({id: inverter.inverter_id, inverter, name: `Battery converter ${index + 1}`,
                units: units.filter((unit) => unit.inverter_id === inverter.inverter_id)}))];
    return groups.map((group) => {
        const rows = intervals.map((interval, index) => {
            const flow = plan.expected_flows?.[index];
            if (!flow || !finite(interval.duration_h) || interval.duration_h <= 0 || !Number.isFinite(Date.parse(interval.start))) return null;
            const converters = !group.inverter ? inverters : inverters.filter((inverter) => inverter.inverter_id === group.id);
            const localSources = sources.filter((source) => converters.some((inverter) => inverter.inverter_id === source.inverter_id));
            const solar = localSources.map((source) => flow.solar?.find((entry) => entry.source_id === source.source_id)?.used_kwh);
            const batteries = group.units.map((unit) => flow.batteries?.find((entry) => entry.device_id === unit.device_id));
            const conversionFlows = converters.map((inverter) => flow.inverters?.find((entry) => entry.inverter_id === inverter.inverter_id));
            if (!solar.every(energy)
                || !batteries.every((battery) => battery && energy(battery.charge_kwh) && energy(battery.discharge_kwh))
                || !conversionFlows.every((converter) => converter && energy(converter.import_kwh) && energy(converter.export_kwh))
                || !converters.every((inverter) => efficiency(inverter.efficiency))
                || !group.units.every((unit) => efficiency(unit.dc_efficiency))) return null;
            const imported = !group.inverter ? flow.grid_import : conversionFlows[0]?.import_kwh;
            const exported = !group.inverter ? flow.grid_export : conversionFlows[0]?.export_kwh;
            const load = !group.inverter ? interval.load_kwh : 0;
            const appliance = !group.inverter ? total((plan.appliance_schedule || []).map((task) => task.interval_kwh[index])) : 0;
            const ev = !group.inverter && problem.ev_work?.length ? (Array.isArray(flow.ev) && flow.ev.length === problem.ev_work.length && flow.ev.every((row) => energy(row.ac_kwh)) ? total(flow.ev.map((row) => row.ac_kwh)) : null) : 0;
            const thermal = !group.inverter ? thermalEnergy(flow, problem.thermal_work || []) : 0;
            if (!energy(ev) || !energy(thermal)) return null;
            if (![imported, exported, load].every(energy)) return null;
            const losses = total(converters.map((inverter, converterIndex) =>
                conversionFlows[converterIndex].import_kwh * (1 - inverter.efficiency)
                + conversionFlows[converterIndex].export_kwh * (1 / inverter.efficiency - 1)))
                + total(group.units.map((unit, batteryIndex) =>
                    batteries[batteryIndex].charge_kwh * (1 / unit.dc_efficiency - 1)
                    + batteries[batteryIndex].discharge_kwh * (1 - unit.dc_efficiency)));
            const row = {
                solar: total(solar), import: imported,
                discharge: total(batteries.map((battery) => battery.discharge_kwh)),
                load: -load, appliance: -appliance, ev: -ev, thermal: -thermal, export: -exported,
                charge: -total(batteries.map((battery) => battery.charge_kwh)), losses: -losses
            };
            return Math.abs(total(Object.values(row))) <= BALANCE_TOLERANCE_KWH ? row : null;
        });
        const converter = Boolean(group.inverter);
        const definitions = [
            ["solar", "Used solar: DC", "Production"],
            ["import", converter ? "AC supply to converter" : "Grid import: AC", "Production"],
            ["discharge", "Battery discharge: DC", "Production"], ["load", "Household demand: AC", "Consumption"],
            ["appliance", "Scheduled appliances: AC", "Consumption"],
            ["ev", "EV charging: AC", "Consumption"],
            ["export", converter ? "Converter output to AC" : "Grid export: AC", "Consumption"],
            ["charge", "Battery charge: DC", "Consumption"], ["losses", "Conversion losses", "Consumption"]
        ];
        const series = definitions
            .filter(([key]) => !(converter && ["load", "appliance", "ev"].includes(key)) && (key !== "appliance" || plan.appliance_schedule?.length) && (key !== "ev" || problem.ev_work?.length) && (group.units.length || !["charge", "discharge"].includes(key)))
            .map(([key, name, tooltipGroup]) => flowSeries(name, key === "appliance" ? APPLIANCE_COLOR : key === "ev" ? EV_COLOR : FLOW_COLORS[key], intervals, rows.map((row) => row?.[key]), tooltipGroup));
        series.push(...socSeries(plan, intervals, group.units, labels));
        // Thermal stores consume AC at the installation boundary, never inside a battery converter.
        if (!converter) for (const [storeIndex, work] of (problem.thermal_work || []).entries()) {
            const values = rows.map((row, index) => row == null ? null : -plan.expected_flows[index].thermal.find((item) => item.device_id === work.device_id).ac_kwh);
            series.push(flowSeries(`${labels.get(work.device_id) || `Thermal store ${storeIndex + 1}`}: Heating AC`, THERMAL_COLORS[storeIndex % THERMAL_COLORS.length], intervals, values, "Consumption"));
        }
        if (!converter) for (const [vehicleIndex, work] of (problem.ev_work || []).entries()) {
            let stored = work.initial_soc * work.capacity_kwh / PERCENT;
            const points = [];
            intervals.forEach((step, index) => {
                const start = Date.parse(step.start), row = plan.expected_flows?.[index]?.ev?.find((item) => item.session_id === work.session_id);
                points.push([start, stored == null ? null : stored / work.capacity_kwh * PERCENT]);
                stored = stored == null || !energy(row?.stored_kwh) ? null : stored + row.stored_kwh;
                points.push([start + step.duration_h * MILLISECONDS_PER_HOUR, stored == null ? null : stored / work.capacity_kwh * PERCENT]);
            });
            series.push({name: `${labels.get(work.vehicle_id) || `Vehicle ${vehicleIndex + 1}`}: SoC`, points, color: SOC_COLORS[(group.units.length + vehicleIndex) % SOC_COLORS.length], axis: "soc", unit: "%", tooltipGroup: "Battery state"});
        }
        return {...group, series, missing: rows.filter((row) => row == null).length};
    });
}
