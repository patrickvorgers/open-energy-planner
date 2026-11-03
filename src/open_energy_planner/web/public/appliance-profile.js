/** Quarter-hour mean AC watts describe a complete cycle, including zero-power pauses. */
export const PROFILE_MINUTES = 15;
const WATTS_PER_KILOWATT = 1000;
const MINUTES_PER_HOUR = 60;
export const WATT_PHASES_PER_KWH = WATTS_PER_KILOWATT * MINUTES_PER_HOUR / PROFILE_MINUTES;

/** Derive duration and energy from the same learned phases used by appliance planning charts. */
export function profileMetrics(profile) {
    return {
        minutes: profile.power_w.length * PROFILE_MINUTES,
        energyKwh: profile.power_w.reduce((total, power) => total + power, 0) / WATT_PHASES_PER_KWH,
    };
}
