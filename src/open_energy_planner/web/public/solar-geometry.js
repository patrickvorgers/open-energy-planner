/** Present north-based solar azimuth without changing exact geometry or legacy configurations. */
const COMPASS_DIRECTIONS = ["North", "North-northeast", "Northeast", "East-northeast", "East", "East-southeast", "Southeast", "South-southeast", "South", "South-southwest", "Southwest", "West-southwest", "West", "West-northwest", "Northwest", "North-northwest"];
const LEGACY_AZIMUTHS = {north: [0], east: [90], south: [180], west: [270], "east-west": [90, 270]};
export const DEFAULT_PANEL_TILT_DEGREES = 30;
export const DIRECTION_CHOICES = [["", "Custom angle"], ...COMPASS_DIRECTIONS.flatMap((label, index) => index % 2 ? [] : [[String(index * 360 / COMPASS_DIRECTIONS.length), label]])];

/** Pair exact degrees with the nearest compass direction, retaining numeric precision. */
export function azimuthLabel(degrees) {
    if (typeof degrees !== "number" || !Number.isFinite(degrees) || degrees < 0 || degrees > 360) return "Not configured";
    const direction = COMPASS_DIRECTIONS[Math.round(degrees / (360 / COMPASS_DIRECTIONS.length)) % COMPASS_DIRECTIONS.length];
    return `${degrees.toLocaleString(undefined, {maximumFractionDigits: 20})}°: ${direction}`;
}

/** Expand the existing manual convention into editable planes without saving or migrating it. */
export function editableSolarPanels(source) {
    if (source.panels?.length) return source.panels;
    const azimuths = LEGACY_AZIMUTHS[source.orientation || "south"];
    return azimuths.map((azimuth, index) => ({panel_id: String(index), name: "", capacity_w: source.capacity_w ? source.capacity_w / azimuths.length : null, azimuth, tilt: source.tilt ?? DEFAULT_PANEL_TILT_DEGREES}));
}
