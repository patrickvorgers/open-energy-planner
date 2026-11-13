// Presentation of immutable, independently validated physical flows. Never optimize or infer energy routes here.
const FLOW_COLORS = Object.freeze({solar: "#23965e", solarAC: "#165c3c", import: "#2586c4", export: "#56bac7", discharge: "#d45464", charge: "#8560b5", load: "#d29323", losses: "#88949f"});
const MILLISECONDS_PER_HOUR = 3600000;
export const QUARTER_MILLISECONDS = MILLISECONDS_PER_HOUR / 4;
const TIMESTAMP_ROUNDOFF_MILLISECONDS = 1;
const PERCENT = 100;
const BALANCE_TOLERANCE_KWH = 0.00001;
// Match the independent physical validator's per-flow roundoff allowance. Never relax conservation to make bars fit.
const DISPLAY_ZERO_TOLERANCE_KWH = 0.0000001;
const SOC_COLORS = ["#324f9d", "#78541d", "#5e3979", "#22665b"];
const DEVICE_COLORS = ["#b55a9d", "#5868b5", "#b9673c", "#267e78", "#8b648c", "#8f7441"];
const THERMAL_COLORS = ["#b9673c", "#8f7441", "#a14e66", "#8b648c"];
const finite = (value) => typeof value === "number" && Number.isFinite(value);
const energy = (value) => finite(value) && value >= -DISPLAY_ZERO_TOLERANCE_KWH;
const efficiency = (value) => finite(value) && value > 0 && value <= 1;
const total = (values) => values.reduce((sum, value) => sum + value, 0);

/**
 * Integrate a covered period without mutating the intervals.
 * @param {Array} intervals Ordered intervals with epoch-millisecond start/end and mean kW value.
 * Optional coveredStart/coveredEnd clip their real coverage. Supply is positive and use negative.
 * @param {number} start Inclusive period start in epoch milliseconds.
 * @param {number} end Exclusive period end in epoch milliseconds.
 * @returns {number|null} Signed kWh, or null for an empty period, non-finite power or incomplete
 * coverage. Intersecting intervals must be ordered and contiguous within one millisecond.
 * Larger gaps or overlaps make energy unavailable. Rows outside the period are ignored.
 */
export function balancePeriodEnergy(intervals, start, end) {
    let cursor = start, result = 0;
    for (const interval of intervals) {
        const left = Math.max(start, interval.coveredStart ?? interval.start);
        const right = Math.min(end, interval.coveredEnd ?? interval.end);
        if (right <= left) continue;
        if (Math.abs(left - cursor) > TIMESTAMP_ROUNDOFF_MILLISECONDS || !finite(interval.value)) return null;
        result += interval.value * (right - left) / MILLISECONDS_PER_HOUR;
        cursor = right;
    }
    return end > start && Math.abs(cursor - end) <= TIMESTAMP_ROUNDOFF_MILLISECONDS ? result : null;
}

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

/** Require exactly one model flow per frozen controlled device, without filling absent evidence. */
function workEnergy(flows, work, identity) {
    if (!work.length) return 0;
    if (!Array.isArray(flows) || flows.length !== work.length
        || new Set(work.map(device => device[identity])).size !== work.length
        || !work.every(device => flows.filter(row => row && row[identity] === device[identity] && energy(row.ac_kwh)).length === 1)) return null;
    return total(flows.map(row => row.ac_kwh));
}

/** Convert signed kWh to mean kW using duration_h. Inflows are positive, outflows negative and null values remain gaps. */
function flowSeries(name, color, intervals, values, tooltipGroup) {
    return {name, color, tooltipGroup, type: "bar", stack: "energy", unit: "kW", intervals: intervals.map((interval, index) => ({
        start: Date.parse(interval.start), end: Date.parse(interval.start) + interval.duration_h * MILLISECONDS_PER_HOUR,
        value: values[index] == null ? null : Math.abs(values[index]) <= DISPLAY_ZERO_TOLERANCE_KWH ? 0 : values[index] / interval.duration_h
    }))};
}

/** Retain the ISO fraction below Date's millisecond precision without rounding archived timestamps. */
function timestampRemainder(start) {
    const fraction = String(start).match(/\.(\d+)(?=Z|[+-]\d{2}:\d{2}$)/)?.[1] || "";
    return Number(`0.${fraction.slice(3)}`);
}

/** Integrate archived kWh using relative times to preserve microsecond weights and avoid epoch roundoff. */
function componentEnergy(steps, interval) {
    const start = Date.parse(interval.start), end = interval.duration_h * MILLISECONDS_PER_HOUR;
    if (!finite(start) || !finite(end) || end <= 0) return null;
    const remainder = timestampRemainder(interval.start);
    let cursor = 0, result = 0;
    for (const step of steps || []) {
        const sourceStart = Date.parse(step.start) - start + timestampRemainder(step.start) - remainder;
        const sourceEnd = sourceStart + step.duration_h * MILLISECONDS_PER_HOUR;
        if (!finite(sourceStart) || !finite(sourceEnd) || !finite(step.duration_h) || step.duration_h <= 0 || !finite(step.kwh) || step.kwh < 0) return null;
        const left = Math.max(0, sourceStart), right = Math.min(end, sourceEnd);
        if (right <= left) continue;
        if (Math.abs(left - cursor) > TIMESTAMP_ROUNDOFF_MILLISECONDS) return null;
        result += step.kwh * (right - left) / (sourceEnd - sourceStart);
        cursor = right;
    }
    return Math.abs(cursor - end) <= TIMESTAMP_ROUNDOFF_MILLISECONDS ? result : null;
}

/** Show original household expectation before concrete tasks replaced part of the device forecasts. */
export function householdForecastEnergy(plan, interval) {
    const components = plan.input_snapshot?.load_components || [];
    if (!components.length) return finite(interval.load_kwh) ? interval.load_kwh : null;
    const values = components.map(component => componentEnergy(component.forecast_steps, interval));
    return values.every(energy) ? total(values) : null;
}

/** Project each frozen device once. Expected contributions are already included in interval.load_kwh. */
function deviceContributions(plan, intervals, labels) {
    const problem = plan.input_snapshot?.problem || {}, components = plan.input_snapshot?.load_components || [];
    const parts = [];
    const chargerMeters = new Map((plan.input_snapshot?.ev_evidence || []).map(row => [row.work.point_id, row.energy_entity]));
    const colorIds = [...new Set([...components.map(component => component.entity_id), ...(plan.appliance_schedule || []).map(task => task.entity_id || task.task_id), ...(problem.ev_work || []).map(work => chargerMeters.get(work.point_id) || work.point_id || work.session_id)].filter(Boolean))].sort();
    const deviceColor = identity => DEVICE_COLORS[Math.max(0, colorIds.indexOf(identity)) % DEVICE_COLORS.length];
    const identities = components.map(component => component.entity_id);
    const valid = !components.length || (new Set(identities).size === identities.length && identities.filter(identity => identity == null).length === 1);
    const base = intervals.map(interval => {
        if (!valid) return null;
        if (!components.length) return finite(interval.load_kwh) ? interval.load_kwh : null;
        const values = components.map(component => componentEnergy(component.steps, interval));
        if (!values.every(energy) || Math.abs(total(values) - interval.load_kwh) > BALANCE_TOLERANCE_KWH) return null;
        return values[identities.indexOf(null)];
    });
    for (const component of components.filter(component => component.entity_id != null)) {
        const name = labels.get(component.entity_id) || "Appliance";
        parts.push({identity: component.entity_id, legendName: name, state: "expected", name: `${name}: Expected`, color: deviceColor(component.entity_id), included: true,
            values: intervals.map((interval, index) => base[index] == null ? null : componentEnergy(component.steps, interval))});
    }
    for (const [index, task] of (plan.appliance_schedule || []).entries()) {
        const name = labels.get(task.entity_id) || `Appliance ${index + 1}`;
        parts.push({identity: task.entity_id || task.task_id, legendName: name, state: "scheduled", name: `${name}: Scheduled`, color: deviceColor(task.entity_id || task.task_id),
            values: intervals.map((_interval, intervalIndex) => energy(task.interval_kwh?.[intervalIndex]) ? task.interval_kwh[intervalIndex] : null)});
    }
    for (const [index, work] of (problem.ev_work || []).entries()) {
        const identity = chargerMeters.get(work.point_id) || work.point_id || work.session_id;
        const name = labels.get(identity) || labels.get(work.point_id) || `Charge point ${index + 1}`;
        parts.push({identity, legendName: name, state: "scheduled", name: `${name}: Scheduled`, color: deviceColor(identity),
            values: intervals.map((_interval, intervalIndex) => {
                const matches = plan.expected_flows?.[intervalIndex]?.ev?.filter(row => row?.session_id === work.session_id) || [];
                return matches.length === 1 && energy(matches[0].ac_kwh) ? matches[0].ac_kwh : null;
            })});
    }
    return {base, parts};
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
 * grid/load boundaries are AC. Separate PV inverters supply AC, hybrid PV and battery
 * terminals use DC. Converter groups
 * use their own AC input/output and local DC terminals. Losses use declared inverter
 * and battery DC efficiencies, never a residual. Internal storage losses affect SoC.
 * Invalid timing, efficiencies, missing evidence or failed conservation within
 * BALANCE_TOLERANCE_KWH produce null interval gaps counted in missing.
 */
export function energyBalances(plan, labels) {
    const problem = plan.input_snapshot?.problem || {}, intervals = problem.intervals || [];
    const units = problem.batteries || [], sources = problem.pv_sources || [], inverters = problem.inverters || [];
    const consumption = deviceContributions(plan, intervals, labels);
    const acSolar = new Set(inverters.filter((inverter) => !inverter.hybrid
        && !units.some((unit) => unit.inverter_id === inverter.inverter_id)
        && sources.some((source) => source.inverter_id === inverter.inverter_id))
        .map((inverter) => inverter.inverter_id));
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
            const load = !group.inverter ? consumption.base[index] : 0;
            const devices = !group.inverter ? consumption.parts.map(part => part.values[index]) : [];
            const thermal = !group.inverter ? workEnergy(flow.thermal, problem.thermal_work || [], "device_id") : 0;
            if (!group.inverter && !energy(workEnergy(flow.ev, problem.ev_work || [], "session_id"))) return null;
            if (!devices.every(energy) || !energy(thermal)) return null;
            if (![imported, exported, load].every(energy)) return null;
            const converterLosses = converters.map((inverter, converterIndex) =>
                conversionFlows[converterIndex].import_kwh * (1 - inverter.efficiency)
                + conversionFlows[converterIndex].export_kwh * (1 / inverter.efficiency - 1));
            const losses = total(converterLosses)
                + total(group.units.map((unit, batteryIndex) =>
                    batteries[batteryIndex].charge_kwh * (1 / unit.dc_efficiency - 1)
                    + batteries[batteryIndex].discharge_kwh * (1 - unit.dc_efficiency)));
            const row = {
                solar: total(solar), import: imported,
                discharge: total(batteries.map((battery) => battery.discharge_kwh)),
                load: -load, devices: -total(devices), thermal: -thermal, export: -exported,
                charge: -total(batteries.map((battery) => battery.charge_kwh)), losses: -losses
            };
            // Validate original DC evidence before moving separate PV losses outside the display boundary.
            if (!(Math.abs(total(Object.values(row))) <= BALANCE_TOLERANCE_KWH)) return null;
            if (!group.inverter) {
                row.solar = total(solar.filter((_, sourceIndex) => !acSolar.has(localSources[sourceIndex].inverter_id)));
                row.solarAC = total(conversionFlows.filter((_, converterIndex) => acSolar.has(converters[converterIndex].inverter_id))
                    .map((converter) => converter.export_kwh));
                row.losses += total(converterLosses.filter((_, converterIndex) => acSolar.has(converters[converterIndex].inverter_id)));
            }
            return Math.abs(total(Object.values(row))) <= BALANCE_TOLERANCE_KWH ? row : null;
        });
        const converter = Boolean(group.inverter);
        const definitions = [
            ["solar", "Used solar: DC", "Production"],
            ...(!converter ? [["solarAC", "Used solar: AC", "Production"]] : []),
            ["import", converter ? "AC supply to converter" : "Grid import: AC", "Production"],
            ["discharge", "Battery discharge: DC", "Production"], ["load", plan.input_snapshot?.load_components?.length ? plan.input_snapshot.load_components.find(part => part.entity_id == null)?.unseparated_entities?.length ? "Household remainder: AC" : "Household base: AC" : "Household demand: AC", "Consumption"],
            ["export", converter ? "Converter output to AC" : "Grid export: AC", "Consumption"],
            ["charge", "Battery charge: DC", "Consumption"], ["losses", "Conversion losses", "Consumption"]
        ];
        const series = definitions
            .filter(([key]) => !(converter && key === "load") && (group.units.length || !["charge", "discharge"].includes(key)))
            .map(([key, name, tooltipGroup]) => flowSeries(name, FLOW_COLORS[key], intervals, rows.map((row) => row?.[key]), tooltipGroup));
        if (!converter) for (const part of consumption.parts) {
            series.push({...flowSeries(part.name, part.color, intervals, rows.map((row, index) => row == null ? null : -part.values[index]), "Consumption"), legendId: part.identity, legendName: part.legendName, deviceState: part.state});
        }
        series.push(...socSeries(plan, intervals, group.units, labels));
        // Thermal stores consume AC at the installation boundary, never inside a battery converter.
        if (!converter) for (const [storeIndex, work] of (problem.thermal_work || []).entries()) {
            const values = rows.map((row, index) => row == null ? null : -plan.expected_flows[index].thermal.find((item) => item.device_id === work.device_id).ac_kwh);
            const meter = plan.input_snapshot?.thermal_evidence?.find(row => row.configuration.id === work.device_id)?.configuration.energy_entity;
            const name = labels.get(meter) || labels.get(work.device_id) || `Thermal store ${storeIndex + 1}`;
            const expected = meter ? consumption.parts.find(part => part.identity === meter) : null;
            series.push({...flowSeries(`${name}: Scheduled`, expected?.color || THERMAL_COLORS[storeIndex % THERMAL_COLORS.length], intervals, values, "Consumption"), legendId: meter || work.device_id, legendName: name, deviceState: "scheduled"});
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
