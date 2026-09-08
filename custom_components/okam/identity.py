"""Pure camera identity resolution shared by config flow and tests."""

from __future__ import annotations

from typing import Any


class CameraSelectionRequired(ValueError):
    def __init__(self, devices: list[dict[str, Any]]) -> None:
        super().__init__("camera_selection_required")
        self.devices = devices


def camera_uid(item: dict[str, Any]) -> str:
    value = item.get("uid") or item.get("camera_uid") or item.get("camera_id")
    return value.strip() if isinstance(value, str) else ""


def resolve_reference(devices: list[dict[str, Any]], reference: str) -> dict[str, Any] | None:
    """Resolve exact UID first, then an unambiguous configured alias."""
    normalized = reference.strip().casefold()
    if not normalized:
        return None
    uid_matches = [item for item in devices if camera_uid(item).casefold() == normalized]
    if len(uid_matches) > 1:
        raise CameraSelectionRequired(devices)
    if uid_matches:
        return uid_matches[0]
    alias_matches = [
        item for item in devices
        if isinstance(item.get("alias"), str)
        and item["alias"].strip().casefold() == normalized
    ]
    if len(alias_matches) > 1:
        raise CameraSelectionRequired(devices)
    return alias_matches[0] if alias_matches else None


def validated_from_devices(data: dict[str, Any], devices: list[dict[str, Any]]) -> dict[str, Any]:
    if not devices:
        raise ValueError("camera_not_found")
    reference = data.get("camera_uid") or data.get("camera_id")
    selected = None
    if isinstance(reference, str) and reference.strip():
        selected = resolve_reference(devices, reference)
    elif len(devices) == 1:
        selected = devices[0]
    if selected is None:
        raise CameraSelectionRequired(devices)
    uid = camera_uid(selected)
    if not uid:
        raise ValueError("camera_not_found")
    result = dict(data)
    result["camera_uid"] = uid
    result["camera_id"] = uid
    name = selected.get("name")
    if isinstance(name, str) and name:
        result["camera_name"] = name
    alias = selected.get("alias")
    if isinstance(alias, str) and alias:
        result["camera_alias"] = alias
    return result
