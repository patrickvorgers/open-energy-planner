"""Actual HA navigation can detach ingress frames while platform acceptance polls readiness."""

from types import SimpleNamespace

import pytest
from playwright.sync_api import Error

from script.supervisor.supervisor_smoke import workspace_frame


@pytest.mark.parametrize("inventory_detaches", [False, True])
def test_ingress_frame_poll_recovers_from_transient_navigation(inventory_detaches):
    """A detached frame or changing frame inventory is not ready once and resolves on the next poll."""
    attempts = 0

    def count():
        """Simulate one failed document lookup followed by a ready product document."""
        nonlocal attempts
        attempts += 1
        if not inventory_detaches and attempts == 1:
            raise Error("Execution context was destroyed")
        return 1

    frame = SimpleNamespace(locator=lambda _selector: SimpleNamespace(count=count))

    class NavigatingPage:
        """Expose an inventory that may navigate before the product iframe is inspectable."""

        @property
        def frames(self):
            """Fail one inventory access when the login redirect replaces its frame tree."""
            nonlocal attempts
            if inventory_detaches and attempts == 0:
                attempts += 1
                raise Error("Frame was detached")
            return [frame]

    page = NavigatingPage()
    assert workspace_frame(page) is None
    assert workspace_frame(page) is frame
