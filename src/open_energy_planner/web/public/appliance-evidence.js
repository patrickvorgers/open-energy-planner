const EVIDENCE_REFRESH_MS = 10000;

/** Poll read-only appliance evidence, stopping on page exit and resuming once after browser history restores it. */
export function pollApplianceEvidence(refresh, intervalMs = EVIDENCE_REFRESH_MS) {
    let timer;
    const start = () => { clearInterval(timer); timer = setInterval(refresh, intervalMs); };
    start();
    window.addEventListener("pagehide", () => clearInterval(timer));
    window.addEventListener("pageshow", event => { if (event.persisted) start(); });
}
