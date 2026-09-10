"""Bridge URL defaults derived from Home Assistant's configured local URL."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit

BRIDGE_PORT = 8099


def select_bridge_url(defaults: object, detected_bridge_url: str) -> str:
    """Keep a saved URL intact, otherwise use detection/fallback for a form."""

    if isinstance(defaults, dict):
        saved = defaults.get("bridge_url")
        if isinstance(saved, str):
            return saved
    return detected_bridge_url


def _usable_host(host: str) -> str | None:
    """Return a safe LAN host, rejecting loopback and add-on-only names."""

    host = host.strip().rstrip(".")
    if not host:
        return None
    lowered = host.casefold()
    if lowered in {"localhost", "supervisor"} or "okam-ha-proxy" in lowered:
        return None
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # A configured Home Assistant hostname is a valid LAN fallback. Avoid
        # names that are only meaningful inside Supervisor's app network.
        if any(character.isspace() for character in host) or "/" in host:
            return None
        return host
    if address.version == 4:
        if address.is_loopback or address.is_link_local or address.is_unspecified:
            return None
        return str(address)
    if address.is_loopback or address.is_link_local or address.is_unspecified:
        return None
    return f"[{address}]"


def bridge_url_from_homeassistant_url(value: object, port: int = BRIDGE_PORT) -> str:
    """Build the HTTP bridge URL from HA's configured internal URL.

    The frontend scheme and port are intentionally ignored: the bridge is an
    HTTP service on its own port. An empty result means no safe default exists.
    """

    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = urlsplit(value.strip())
        host = parsed.hostname
    except ValueError:
        return ""
    if not parsed.scheme or not host:
        return ""
    safe_host = _usable_host(host)
    return f"http://{safe_host}:{port}" if safe_host else ""


def default_bridge_url(hass: object, port: int = BRIDGE_PORT) -> str:
    """Return a safe default for a new config entry, or an empty fallback."""

    config = getattr(hass, "config", None)
    return bridge_url_from_homeassistant_url(
        getattr(config, "internal_url", None), port
    )
