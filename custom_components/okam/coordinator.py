"""Low-frequency status coordinator for the battery camera."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import OkamApi, OkamApiError
from .const import (
    CONF_CAMERA_ID,
    CONF_CAMERA_UID,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
)


class OkamCoordinator(DataUpdateCoordinator[dict]):
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, api: OkamApi) -> None:
        interval = int(
            entry.options.get(
                CONF_SNAPSHOT_INTERVAL,
                entry.data.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
            )
        )
        super().__init__(
            hass,
            logger=__import__("logging").getLogger(__name__),
            name=DOMAIN,
            update_interval=timedelta(seconds=max(60, interval)),
            config_entry=entry,
        )
        self.api = api
        self.camera_uid = str(
            entry.options.get(
                CONF_CAMERA_UID,
                entry.data.get(
                    CONF_CAMERA_UID,
                    entry.options.get(CONF_CAMERA_ID, entry.data[CONF_CAMERA_ID]),
                ),
            )
        )
        # Keep camera_id as the stable UID used by the bridge API. Existing
        # callers and entity object IDs continue to use this attribute.
        self.camera_id = self.camera_uid
        self.camera_name = str(
            entry.data.get(
                "camera_name", entry.data.get("camera_alias", self.camera_uid)
            )
        )

    async def _async_update_data(self) -> dict:
        try:
            result = await self.api.status(self.camera_uid)
            if isinstance(result.get("name"), str) and result["name"]:
                self.camera_name = result["name"]
            return result
        except OkamApiError as exc:
            raise UpdateFailed(str(exc)) from exc
