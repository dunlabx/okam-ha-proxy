"""O-KAM Native Bridge integration setup."""

from __future__ import annotations

from dataclasses import dataclass

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import ConfigEntryNotReady

from .api import OkamApi
from .const import (
    CONF_API_TOKEN,
    CONF_BRIDGE_URL,
    CONF_CAMERA_ID,
    CONF_CAMERA_UID,
    CONF_IDLE_TIMEOUT,
    DEFAULT_IDLE_TIMEOUT,
    PLATFORMS,
)
from .coordinator import OkamCoordinator


@dataclass(slots=True)
class OkamRuntime:
    api: OkamApi
    coordinator: OkamCoordinator


# Keep the integration importable on the Python 3.11 baseline declared by the
# repository; the ``type Alias = ...`` syntax is Python 3.12-only.
OkamConfigEntry = ConfigEntry[OkamRuntime]


async def async_setup_entry(hass: HomeAssistant, entry: OkamConfigEntry) -> bool:
    api = OkamApi(
        async_get_clientsession(hass),
        entry.data[CONF_BRIDGE_URL],
        entry.data[CONF_API_TOKEN],
    )
    camera_uid = await _resolve_camera_uid(hass, api, entry)
    idle_timeout = int(
        entry.options.get(
            CONF_IDLE_TIMEOUT,
            entry.data.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT),
        )
    )
    await api.configure(camera_uid, idle_timeout)
    coordinator = OkamCoordinator(hass, entry, api)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = OkamRuntime(api, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def _resolve_camera_uid(
    hass: HomeAssistant, api: OkamApi, entry: OkamConfigEntry
) -> str:
    """Resolve old alias entries without guessing in a multi-camera bridge."""

    configured_uid = entry.options.get(CONF_CAMERA_UID, entry.data.get(CONF_CAMERA_UID))
    legacy_id = entry.options.get(CONF_CAMERA_ID, entry.data.get(CONF_CAMERA_ID))
    devices = await api.devices()

    def uid(item: dict) -> str:
        value = item.get("camera_uid") or item.get("camera_id")
        return str(value) if isinstance(value, str) else ""

    selected = None
    if isinstance(configured_uid, str) and configured_uid:
        selected = next((item for item in devices if uid(item) == configured_uid), None)
    if selected is None and isinstance(legacy_id, str) and legacy_id:
        selected = next((item for item in devices if item.get("camera_id") == legacy_id), None)
    if selected is None and not configured_uid and len(devices) == 1:
        selected = devices[0]
    if selected is None:
        raise ConfigEntryNotReady(
            "The bridge exposes multiple cameras; reconfigure this entry and select a camera UID"
        )
    selected_uid = uid(selected)
    if not selected_uid:
        raise ConfigEntryNotReady("The bridge returned an invalid camera identity")
    selected_name = selected.get("name")
    if selected_uid != configured_uid or selected_name:
        data = dict(entry.data)
        data[CONF_CAMERA_UID] = selected_uid
        if isinstance(selected_name, str) and selected_name:
            data["camera_name"] = selected_name
        hass.config_entries.async_update_entry(entry, data=data)
    return selected_uid


async def async_unload_entry(hass: HomeAssistant, entry: OkamConfigEntry) -> bool:
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def _async_reload_entry(hass: HomeAssistant, entry: OkamConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)
