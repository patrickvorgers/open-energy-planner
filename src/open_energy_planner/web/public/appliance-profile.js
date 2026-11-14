/** Quarter-hour mean AC watts describe a complete cycle, including zero-power pauses. */
export const PROFILE_MINUTES = 15;
const WATTS_PER_KILOWATT = 1000;
const MINUTES_PER_HOUR = 60;
export const WATT_PHASES_PER_KWH = WATTS_PER_KILOWATT * MINUTES_PER_HOUR / PROFILE_MINUTES;

/** Derive duration and energy from the same learned phases used by appliance planning charts. */
export function profileMetrics(profile) {
    const minutes = profile.measured_duration_seconds == null ? profile.power_w.length * PROFILE_MINUTES : profile.measured_duration_seconds / MINUTES_PER_HOUR;
    const phaseEnergy = profile.power_w.map((power, index) => power * Math.min(PROFILE_MINUTES, minutes - index * PROFILE_MINUTES) / MINUTES_PER_HOUR / WATTS_PER_KILOWATT);
    return {minutes, energyKwh: phaseEnergy.reduce((total, energy) => total + energy, 0), phaseEnergy};
}
