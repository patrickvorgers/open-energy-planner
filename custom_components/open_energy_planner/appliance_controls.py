"""Native dashboard inputs. Draft intent is durable in the app, never a physical command."""

from datetime import date, time
from uuid import uuid4

from aiohttp import ClientError
from homeassistant.components.button import ButtonEntity
from homeassistant.components.date import DateEntity
from homeassistant.components.time import TimeEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from .appliances import consumer_identity
from .entity import PlannerEntity
from .protocol import timestamp

CLOSED_TASK_STATUSES = ("cancelled", "completed")


class ApplianceControl(PlannerEntity):
    """Share the Energy-owned virtual device and authoritative task readiness checks."""

    def __init__(self, coordinator, entity, name, key):
        """Keep editable deadline and readiness on the same device as task evidence."""
        super().__init__(
            coordinator,
            consumer_identity(entity),
            key,
            {
                "finish_date": "Finish date",
                "finish_time": "Finish time",
                "ready_for_planning": "Ready for planning",
                "cancel_planning": "Cancel planning",
            }[key],
        )
        self.entity_id_source = entity
        self._attr_device_info["name"] = name
        self._attr_icon = (
            "mdi:calendar"
            if key == "finish_date"
            else "mdi:clock-outline"
            if key == "finish_time"
            else "mdi:calendar-check"
        )

    def row(self):
        """Read current app-owned permission, draft and accepted task."""
        return next(
            (
                row
                for row in self.coordinator.appliances.get("devices", [])
                if row["entity_id"] == self.entity_id_source
            ),
            {},
        )

    @property
    def available(self):
        """Never permit another readiness request or deadline edit while execution is unresolved."""
        row = self.row()
        task = row.get("task")
        return bool(row.get("shiftable")) and (not task or task["status"] in CLOSED_TASK_STATUSES)

    async def write(self, operation, body):
        """Forward intent to the app and read its committed state. Never change HA optimistically."""
        try:
            await self.coordinator.client.request(
                "POST", "/v1/appliances/" + operation, {"entity_id": self.entity_id_source, **body}
            )
            await self.coordinator.sync()
        except (ClientError, ValueError, KeyError) as error:
            raise HomeAssistantError("The app could not accept this appliance request") from error


class ApplianceDate(ApplianceControl, DateEntity):
    """A compact native date row. No datetime widget wider than the HA device card."""

    @property
    def native_value(self):
        """Read the saved local date, retaining existing complete deadlines without extra helpers."""
        row = self.row()
        if row.get("finish_date"):
            return date.fromisoformat(row["finish_date"])
        return (
            dt_util.as_local(timestamp(row["finish_before"])).date()
            if row.get("finish_before")
            else None
        )

    async def async_set_value(self, value):
        """Save one local field. Incomplete or invalid drafts never authorize readiness."""
        await self.write(
            "deadline_field",
            {"field": "date", "value": value.isoformat(), "time_zone": self.hass.config.time_zone},
        )


class ApplianceTime(ApplianceControl, TimeEntity):
    """A compact native time row combined with the date by the app."""

    @property
    def native_value(self):
        """Read the saved wall-clock time in the Home Assistant timezone."""
        row = self.row()
        if row.get("finish_time"):
            return time.fromisoformat(row["finish_time"])
        return (
            dt_util.as_local(timestamp(row["finish_before"])).time()
            if row.get("finish_before")
            else None
        )

    async def async_set_value(self, value):
        """Save one local field. Daylight-saving resolution remains in the application."""
        await self.write(
            "deadline_field",
            {"field": "time", "value": value.isoformat(), "time_zone": self.hass.config.time_zone},
        )


class ApplianceReady(ApplianceControl, ButtonEntity):
    """Submit explicit readiness with the previously selected finish-before time."""

    @property
    def available(self):
        """Require a learned cycle, future deadline and no unresolved task before offering readiness."""
        row = self.row()
        value = row.get("finish_before")
        return (
            super().available
            and bool(row.get("profile"))
            and bool(value)
            and timestamp(value) > dt_util.utcnow()
        )

    async def async_press(self):
        """Use a fresh correlation ID. App-side unresolved-task validation also guards concurrent presses."""
        if not self.available:
            raise HomeAssistantError(
                "Choose a future finish-before time and resolve any open task first"
            )
        await self.write(
            "tasks",
            {
                "request_id": str(uuid4()),
                "ready_at": dt_util.utcnow().isoformat(),
                "deadline": self.row()["finish_before"],
            },
        )


class ApplianceCancel(ApplianceControl, ButtonEntity):
    """Withdraw planning at any stage without claiming to stop physical execution."""

    @property
    def available(self):
        """Offer cancellation for every open task, including running and unknown execution."""
        row = self.row()
        task = row.get("task")
        return (
            bool(row.get("shiftable")) and bool(task) and task["status"] not in CLOSED_TASK_STATUSES
        )

    async def async_press(self):
        """Withdraw this exact task. Physical cancellation remains the user's HA automation."""
        if not self.available:
            raise HomeAssistantError("There is no open planning task to cancel")
        await self.write("cancel", {"task_id": self.row()["task"]["id"]})
