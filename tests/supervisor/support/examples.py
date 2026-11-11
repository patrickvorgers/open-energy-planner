"""Supply a simulated HA boundary and replaced registry/Supervisor transport for example provisioning scenarios."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest
from aiohttp import ClientResponseError

from script.home_assistant.bootstrap import provision_ha_examples as ha_examples
from script.provisioning import device_examples as examples
from tests.provisioning.support.registry import example_registry


class HomeAssistantBoundary:
    """Retain administrator changes and simulate successful writes with lost responses."""

    def __init__(self, journal):
        """Expose renamed virtual entities so fixtures cannot rely on generated display names."""
        self.calls, self.definitions = [], {}
        self.sidebar = False
        self.fail_after = None
        self.ev = examples.ev_configuration()
        journal.state["ev_configuration"] = deepcopy(self.ev)
        self.options = [
            {"entity_id": point["energy_entity"], "power_entity": point["power_entity"]}
            for point in self.ev["points"]
        ]
        self.thermal = examples.boiler_configuration()
        self.consumers, self.appliances = [], []
        for name in ("dishwasher", "washing_machine", "dryer"):
            source = f"sensor.oep_test_{name}_energy"
            self.consumers.append({"entity_id": source, "weather_sensitive": False})
            self.appliances.append({"entity_id": source, "shiftable": True})
        self.registry, self.states = example_registry(journal)

    async def app_request(self, method, path):
        """Read current product bindings independently of older bootstrap ownership receipts."""
        self.calls.append((method, path, {}))
        assert method == "GET"
        return deepcopy(
            {
                "/v1/ev": self.ev,
                "/v1/thermal": {"configuration": self.thermal},
                "/v1/consumers": {"selected": self.consumers, "options": self.options},
                "/v1/appliances": {"devices": self.appliances},
            }[path]
        )

    async def request(self, method, path, **kwargs):
        """Use native object paths, preserving any pre-existing automation configuration verbatim."""
        self.calls.append((method, path, deepcopy(kwargs)))
        if path.endswith("/info"):
            return {"ingress_panel": self.sidebar}
        if path.endswith("/options"):
            assert method == "POST" and kwargs["json"] == {"ingress_panel": True}
            self.sidebar = True
            result = {"result": "ok"}
        elif path == "/api/states":
            return deepcopy(self.states)
        elif path.startswith("/api/config/automation/config/"):
            key = path.rsplit("/", 1)[-1]
            if method == "GET":
                if key not in self.definitions:
                    raise ClientResponseError(
                        SimpleNamespace(real_url="http://homeassistant" + path), (), status=404
                    )
                return deepcopy(self.definitions[key])
            assert method == "POST"
            self.definitions[key] = deepcopy(kwargs["json"])
            result = {"result": "ok"}
        else:
            pytest.fail("Unexpected HA operation " + path)
        if self.fail_after == path:
            self.fail_after = None
            raise ConnectionError("Successful HA response lost")
        return result


def run(boundary, journal, monkeypatch):
    """Replace only registry transport. The provisioner's native HTTP operation flow is unchanged."""

    async def registry(_session, url):
        """Return detached registered identities after asserting the isolated HA destination."""
        assert url == "http://homeassistant"
        return deepcopy(boundary.registry)

    monkeypatch.setattr(ha_examples, "entity_registry", registry)

    async def supervisor(_session, url, command):
        """Use HA's documented Supervisor WebSocket bridge, never its restricted HTTP proxy."""
        assert url == "http://homeassistant" and command["type"] == "supervisor/api"
        return await boundary.request(
            command["method"].upper(),
            command["endpoint"],
            **({"json": command["data"]} if "data" in command else {}),
        )

    monkeypatch.setattr(ha_examples, "native_command", supervisor)
    asyncio.run(
        ha_examples.provision(
            None,
            "http://homeassistant",
            boundary.request,
            journal.path,
            app_request=boundary.app_request,
        )
    )
