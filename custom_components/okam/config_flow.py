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
from .identity import (
    CameraSelectionRequired,
    camera_uid,
    resolve_reference,
    validated_from_devices,
)


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
    return camera_uid(item)


def _camera_selector(devices: list[dict[str, Any]]) -> vol.Schema:
    options = [
        {
            "value": uid,
            "label": _camera_label(item, uid),
        }
        for item in devices
        if (uid := _camera_uid(item))
    ]
    if not options:
        raise ValueError("camera_not_found")
    return vol.Schema(
        {
            vol.Required(CONF_CAMERA_UID): selector.SelectSelector(
                selector.SelectSelectorConfig(options=options)
            )
        }
    )


def _camera_label(item: dict[str, Any], uid: str) -> str:
    """Build a readable selector label while keeping UID as the value."""
    names: list[str] = []
    for key in ("alias", "name", "camera_id"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            value = value.strip()
            if value.casefold() != uid.casefold() and all(
                value.casefold() != name.casefold() for name in names
            ):
                names.append(value)
    return f"{' / '.join(names) if names else 'O-KAM camera'} — {uid}"


def _matching_devices(
    devices: list[dict[str, Any]], key: str, value: str
) -> list[dict[str, Any]]:
    """Return all matches so legacy aliases cannot select silently."""
    return [
        item
        for item in devices
        if isinstance(item.get(key), str)
        and item[key].strip().casefold() == value.strip().casefold()
    ]


_resolve_reference = resolve_reference
_validated_from_devices = validated_from_devices


async def _validate(hass, data: dict[str, Any]) -> dict[str, Any]:
    api = OkamApi(
        async_get_clientsession(hass), data[CONF_BRIDGE_URL], data[CONF_API_TOKEN]
    )
    await api.health()
    devices = await api.devices()
    return _validated_from_devices(data, devices)


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
                try:
                    return await self._create_camera_entry(
                        _validated_from_devices(user_input, devices)
                    )
                except CameraSelectionRequired:
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
                try:
                    validated = _validated_from_devices(user_input, devices)
                except CameraSelectionRequired:
                    return self.async_show_form(
                        step_id="reconfigure_camera",
                        data_schema=_camera_selector(devices),
                        errors={},
                    )
                return self.async_update_reload_and_abort(entry, data=validated)
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
