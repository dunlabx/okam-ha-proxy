"""UI configuration for the O-KAM Native Bridge."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import OkamApi, OkamApiError, OkamAuthError
from .const import (
    CONF_API_TOKEN,
    CONF_BRIDGE_URL,
    CONF_CAMERA_ID,
    CONF_CAMERA_UID,
    CONF_IDLE_TIMEOUT,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_CAMERA_ID,
    DEFAULT_IDLE_TIMEOUT,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
)


class CameraSelectionRequired(ValueError):
    def __init__(self, devices: list[dict[str, Any]]) -> None:
        super().__init__("camera_selection_required")
        self.devices = devices


def _schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    defaults = defaults or {}
    return vol.Schema(
        {
            vol.Required(
                CONF_BRIDGE_URL,
                default=defaults.get(CONF_BRIDGE_URL, "http://homeassistant.local:8099"),
            ): str,
            vol.Required(
                CONF_API_TOKEN, default=defaults.get(CONF_API_TOKEN, "")
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(
                CONF_CAMERA_ID, default=defaults.get(CONF_CAMERA_ID, DEFAULT_CAMERA_ID)
            ): str,
            vol.Optional(
                CONF_IDLE_TIMEOUT,
                default=defaults.get(CONF_IDLE_TIMEOUT, DEFAULT_IDLE_TIMEOUT),
            ): vol.All(vol.Coerce(int), vol.Range(min=10, max=600)),
            vol.Optional(
                CONF_SNAPSHOT_INTERVAL,
                default=defaults.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL),
            ): vol.All(vol.Coerce(int), vol.Range(min=60, max=86400)),
        }
    )


def _camera_uid(item: dict[str, Any]) -> str:
    value = item.get("camera_uid") or item.get("camera_id")
    return value.strip() if isinstance(value, str) else ""


def _camera_selector(devices: list[dict[str, Any]]) -> vol.Schema:
    options = [
        {
            "value": uid,
            "label": str(item.get("name") or item.get("camera_id") or uid),
        }
        for item in devices
        if (uid := _camera_uid(item))
    ]
    return vol.Schema(
        {
            vol.Required(CONF_CAMERA_UID): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            )
        }
    )


async def _validate(hass, data: dict[str, Any]) -> dict[str, Any]:
    api = OkamApi(
        async_get_clientsession(hass), data[CONF_BRIDGE_URL], data[CONF_API_TOKEN]
    )
    await api.health()
    devices = await api.devices()
    if not devices:
        raise ValueError("camera_not_found")
    requested_uid = data.get(CONF_CAMERA_UID)
    requested_id = data.get(CONF_CAMERA_ID)
    selected = None
    if isinstance(requested_uid, str) and requested_uid:
        selected = next((item for item in devices if _camera_uid(item) == requested_uid), None)
    if selected is None and isinstance(requested_id, str) and requested_id:
        selected = next((item for item in devices if item.get("camera_id") == requested_id), None)
    if selected is None and len(devices) == 1:
        selected = devices[0]
    if selected is None:
        raise CameraSelectionRequired(devices)
    uid = _camera_uid(selected)
    if not uid:
        raise ValueError("camera_not_found")
    result = dict(data)
    result[CONF_CAMERA_UID] = uid
    result[CONF_CAMERA_ID] = str(selected.get("camera_id") or uid)
    name = selected.get("name")
    if isinstance(name, str) and name:
        result["camera_name"] = name
    return result


async def _discover(hass, data: dict[str, Any]) -> list[dict[str, Any]]:
    """Authenticate and return the cameras available for a selector step."""

    api = OkamApi(
        async_get_clientsession(hass), data[CONF_BRIDGE_URL], data[CONF_API_TOKEN]
    )
    await api.health()
    devices = await api.devices()
    if not devices:
        raise ValueError("camera_not_found")
    return devices


class OkamConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def _create_camera_entry(self, data: dict[str, Any]):
        bridge_url = data[CONF_BRIDGE_URL].lower()
        camera_uid = data[CONF_CAMERA_UID].lower()
        if any(
            entry.data.get(CONF_BRIDGE_URL, "").lower() == bridge_url
            and str(
                entry.data.get(CONF_CAMERA_UID, entry.data.get(CONF_CAMERA_ID, ""))
            ).lower()
            == camera_uid
            for entry in self.hass.config_entries.async_entries(DOMAIN)
        ):
            return self.async_abort(reason="already_configured")
        await self.async_set_unique_id(
            f"{bridge_url}:{camera_uid}"
        )
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title=f"O-KAM {data.get('camera_name') or data[CONF_CAMERA_UID]}", data=data
        )

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                devices = await _discover(self.hass, user_input)
            except OkamAuthError:
                errors["base"] = "invalid_auth"
            except OkamApiError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "camera_not_found"
            else:
                self._pending_data = dict(user_input)
                self._pending_devices = devices
                return self.async_show_form(
                    step_id="camera",
                    data_schema=_camera_selector(devices),
                    errors={},
                )
        return self.async_show_form(
            step_id="user", data_schema=_schema(user_input), errors=errors
        )

    async def async_step_camera(self, user_input=None):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._pending_data, **user_input}
            try:
                validated = await _validate(self.hass, data)
            except CameraSelectionRequired:
                errors["base"] = "camera_not_found"
            except OkamAuthError:
                errors["base"] = "invalid_auth"
            except OkamApiError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "camera_not_found"
            else:
                return await self._create_camera_entry(validated)
        return self.async_show_form(
            step_id="camera",
            data_schema=_camera_selector(self._pending_devices),
            errors=errors,
        )

    async def async_step_reconfigure(self, user_input=None):
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                devices = await _discover(self.hass, user_input)
            except OkamAuthError:
                errors["base"] = "invalid_auth"
            except OkamApiError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "camera_not_found"
            else:
                self._pending_entry = entry
                self._pending_data = dict(user_input)
                self._pending_devices = devices
                return self.async_show_form(
                    step_id="reconfigure_camera",
                    data_schema=_camera_selector(devices),
                    errors={},
                )
        return self.async_show_form(
            step_id="reconfigure", data_schema=_schema(entry.data), errors=errors
        )

    async def async_step_reconfigure_camera(self, user_input=None):
        entry = self._pending_entry
        errors: dict[str, str] = {}
        if user_input is not None:
            data = {**self._pending_data, **user_input}
            try:
                validated = await _validate(self.hass, data)
            except CameraSelectionRequired:
                errors["base"] = "camera_not_found"
            except OkamAuthError:
                errors["base"] = "invalid_auth"
            except OkamApiError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "camera_not_found"
            else:
                return self.async_update_reload_and_abort(entry, data=validated)
        return self.async_show_form(
            step_id="reconfigure_camera",
            data_schema=_camera_selector(self._pending_devices),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data):
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        errors: dict[str, str] = {}
        assert self._reauth_entry is not None
        if user_input is not None:
            data = {**self._reauth_entry.data, **user_input}
            try:
                validated = await _validate(self.hass, data)
            except CameraSelectionRequired:
                errors["base"] = "camera_not_found"
            except OkamAuthError:
                errors["base"] = "invalid_auth"
            except OkamApiError:
                errors["base"] = "cannot_connect"
            except ValueError:
                errors["base"] = "camera_not_found"
            else:
                return self.async_update_reload_and_abort(
                    self._reauth_entry, data=validated
                )
        schema = vol.Schema(
            {
                vol.Required(CONF_API_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                )
            }
        )
        return self.async_show_form(
            step_id="reauth_confirm", data_schema=schema, errors=errors
        )
