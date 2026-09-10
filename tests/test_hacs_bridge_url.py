"""Tests for safe Home Assistant LAN bridge URL defaults."""

from __future__ import annotations

import importlib.util
from types import SimpleNamespace
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "okam_url", Path(__file__).parents[1] / "custom_components/okam/url.py"
)
assert _spec and _spec.loader
_url = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_url)
bridge_url_from_homeassistant_url = _url.bridge_url_from_homeassistant_url
default_bridge_url = _url.default_bridge_url
select_bridge_url = _url.select_bridge_url


def test_new_setup_uses_configured_home_assistant_lan_ipv4() -> None:
    hass = SimpleNamespace(
        config=SimpleNamespace(internal_url="http://192.168.1.110:8123")
    )
    assert default_bridge_url(hass) == "http://192.168.1.110:8099"


def test_frontend_port_is_not_reused_for_bridge() -> None:
    assert (
        bridge_url_from_homeassistant_url("http://192.168.1.110:8123")
        == "http://192.168.1.110:8099"
    )


def test_https_frontend_still_defaults_to_http_bridge() -> None:
    assert (
        bridge_url_from_homeassistant_url("https://ha.example.local:8123")
        == "http://ha.example.local:8099"
    )


def test_loopback_is_not_offered_as_lan_default() -> None:
    assert bridge_url_from_homeassistant_url("http://127.0.0.1:8123") == ""
    assert bridge_url_from_homeassistant_url("http://[::1]:8123") == ""


def test_supervisor_addon_hostname_is_not_offered_as_lan_default() -> None:
    assert (
        bridge_url_from_homeassistant_url("http://dc28dd67-okam-ha-proxy:8099")
        == ""
    )
    assert bridge_url_from_homeassistant_url("http://supervisor:80") == ""


def test_unknown_address_has_safe_empty_fallback() -> None:
    hass = SimpleNamespace(config=SimpleNamespace(internal_url=None))
    assert default_bridge_url(hass) == ""
    assert bridge_url_from_homeassistant_url("not a url") == ""


def test_saved_url_is_preserved_for_reconfigure() -> None:
    assert select_bridge_url(
        {"bridge_url": "http://old-bridge.example:8099"},
        "http://192.168.1.110:8099",
    ) == "http://old-bridge.example:8099"


def test_new_form_uses_detected_url_when_no_saved_entry_exists() -> None:
    assert (
        select_bridge_url({}, "http://192.168.1.110:8099")
        == "http://192.168.1.110:8099"
    )
