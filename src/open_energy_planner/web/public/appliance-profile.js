/** Show five-minute intervals while preserving the declared source detail and total energy. */
const LEGACY_PHASE_SECONDS = 15 * 60;
const DISPLAY_INTERVAL_MINUTES = 5;
const WATTS_PER_KILOWATT = 1000;
const SECONDS_PER_MINUTE = 60;
const MINUTES_PER_HOUR = 60;

/** Distribute coarser mean power uniformly without pretending it is measured fine detail. */
export function profileMetrics(profile) {
    if (profile.phase_durations_seconds) {
        let elapsed = 0;
        const edgesMinutes = [0], labels = [], phaseEnergy = [];
        profile.phase_durations_seconds.forEach((seconds, index) => {
            labels.push(`${(elapsed / 60).toFixed(3)}–${((elapsed + seconds) / 60).toFixed(3)} min`);
            elapsed += seconds;
            edgesMinutes.push(elapsed / 60);
            phaseEnergy.push(profile.power_w[index] * seconds / 3600000);
        });
        return {minutes: elapsed / 60, variable: true, phaseMinutes: null, sourcePhaseMinutes: null, edgesMinutes, labels, powerW: profile.power_w, phaseEnergy, energyKwh: phaseEnergy.reduce((total, value) => total + value, 0)};
    }
    const sourcePhaseMinutes = (profile.phase_seconds ?? LEGACY_PHASE_SECONDS) / SECONDS_PER_MINUTE;
    const minutes = profile.measured_duration_seconds == null ? profile.power_w.length * sourcePhaseMinutes : profile.measured_duration_seconds / SECONDS_PER_MINUTE;
    const powerW = Array.from({length: Math.ceil(minutes / DISPLAY_INTERVAL_MINUTES)}, (_, index) => profile.power_w[Math.floor(index * DISPLAY_INTERVAL_MINUTES / sourcePhaseMinutes)]);
    const phaseEnergy = powerW.map((power, index) => power * Math.min(DISPLAY_INTERVAL_MINUTES, minutes - index * DISPLAY_INTERVAL_MINUTES) / MINUTES_PER_HOUR / WATTS_PER_KILOWATT);
    return {minutes, phaseMinutes: DISPLAY_INTERVAL_MINUTES, sourcePhaseMinutes, powerW, energyKwh: phaseEnergy.reduce((total, energy) => total + energy, 0), phaseEnergy};
}
