"""Report loaded-component mismatch without exposing build contact metadata publicly."""

from open_energy_planner.notifications.model import NoticeText, Notification


def version_notifications(blocking_mismatch: bool, *, available: bool = True):
    """Keep a diagnosed mismatch until an identified matching peer confirms recovery.

    A retained mismatch remains authoritative after the recent-contact window.
    Missing contact without a diagnosed mismatch is unknown, not recovery proof.
    The build-status owner supplies the facts and retains its existing clearing rule.
    """
    if blocking_mismatch:
        return (
            Notification(
                "versions:release-mismatch",
                "release_mismatch",
                NoticeText(
                    "App and integration versions do not match",
                    "Update the app and Home Assistant integration from the same installation bundle. "
                    "A mismatched integration cannot exchange current planning data.",
                    "Check installation",
                    "/setup",
                ),
                blocks=("home_assistant_exchange",),
            ),
        )
    return () if available else None
