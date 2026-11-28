import {QUARTER_MILLISECONDS} from "./balance.js";

function coveredSum(parts, valueOf) {
    let total = 0;
    for (const {step, fraction} of parts) {
        const value = valueOf(step);
        if (!Number.isFinite(value)) return null;
        total += value * fraction;
    }
    return total;
}

/** Use frozen rates for each fragment before combining cash and energy. */
function quarterSettlement(parts, energyKey, rateKey) {
    const energy = coveredSum(parts, step => step[energyKey]);
    const rates = new Set(parts.map(({step}) => step[rateKey]));
    const cost = coveredSum(parts, step => {
        const energy = step[energyKey], rate = step[rateKey];
        return Number.isFinite(energy) && Number.isFinite(rate) ? energy * rate : null;
    });
    const rate = rates.size === 1 ? parts[0].step[rateKey] : energy > 0 && cost != null ? cost / energy : null;
    return {energy, rate, cost, varies: rates.size > 1};
}

/** Collect commands by identity and only show a stored state at an actual covered endpoint. */
function quarterCommands(parts, coveredEnd) {
    const devices = new Map();
    for (const {step, fraction, end} of parts) {
        for (const row of step.commands) {
            const key = JSON.stringify([row.kind, row.id]);
            if (!devices.has(key)) devices.set(key, {...row, energy: 0, states: new Set(), recorded: 0, stored: null});
            const device = devices.get(key);
            if (row.kind === "appliance") {
                device.energy += row.energy * fraction;
            } else {
                device.states.add(row.text);
                device.recorded++;
                if (step.end === coveredEnd && end === coveredEnd) device.stored = row.stored;
            }
        }
    }
    return [...devices.values()].map(({states, recorded, ...device}) => ({
        ...device,
        text: recorded !== parts.length ? "Control coverage incomplete"
            : states.size > 1 ? "Control varies within the quarter" : states.values().next().value,
    }));
}

/** Prepare one display readout per UTC quarter while retaining actual coverage at plan edges. */
export function planningQuarterReadouts(steps) {
    const partsByStart = new Map();
    for (const step of steps) {
        if (!(step.end > step.start)) continue;
        const first = Math.floor(step.start / QUARTER_MILLISECONDS) * QUARTER_MILLISECONDS;
        for (let start = first; start < step.end; start += QUARTER_MILLISECONDS) {
            const left = Math.max(start, step.start), end = Math.min(start + QUARTER_MILLISECONDS, step.end);
            if (!partsByStart.has(start)) partsByStart.set(start, []);
            partsByStart.get(start).push({step, start: left, end, fraction: (end - left) / (step.end - step.start)});
        }
    }
    return new Map([...partsByStart].map(([start, parts]) => {
        const coveredStart = parts[0].start, coveredEnd = parts.at(-1).end;
        const contiguous = parts.every((part, index) => !index || part.start === parts[index - 1].end);
        const provenance = new Set(parts.map(({step}) => step.priceStatus));
        return [start, {
            start, end: start + QUARTER_MILLISECONDS, coveredStart, coveredEnd,
            currency: parts[0].step.currency, firstStep: parts[0].step,
            priceStatus: contiguous && provenance.size === 1 ? parts[0].step.priceStatus
                : "Market rate provenance varies within the covered quarter",
            imports: contiguous ? quarterSettlement(parts, "gridImport", "buyRate") : {},
            exports: contiguous ? quarterSettlement(parts, "gridExport", "sellRate") : {},
            commands: contiguous ? quarterCommands(parts, coveredEnd) : [],
        }];
    }));
}
