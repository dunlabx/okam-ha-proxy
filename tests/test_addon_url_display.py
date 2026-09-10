"""Tests for the add-on's documented Supervisor LAN URL discovery."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "okam_test_url_entrypoint", ROOT / "okam_native_app" / "app_entrypoint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_supervisor_network_info_selects_primary_host_ipv4(monkeypatch):
    app = _load_entrypoint()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")
    payload = {
        "result": "ok",
        "data": {
            "interfaces": [
                {
                    "interface": "eth0",
                    "primary": True,
                    "ipv4": {"ip_address": "192.168.1.110/24"},
                },
            ],
            "docker": {"address": "172.30.32.0/23"},
        },
    }
    monkeypatch.setattr(
        app.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload)
    )
    assert app.resolve_supervisor_lan_ipv4() == "192.168.1.110"


def test_supervisor_network_info_ignores_docker_only_addresses(monkeypatch):
    app = _load_entrypoint()
    monkeypatch.setenv("SUPERVISOR_TOKEN", "token")
    payload = {
        "result": "ok",
        "data": {
            "interfaces": [
                {
                    "interface": "docker0",
                    "primary": True,
                    "ipv4": {"ip_address": "172.30.32.2/23"},
                },
            ],
            "docker": {"address": "172.30.32.0/23"},
        },
    }
    monkeypatch.setattr(
        app.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload)
    )
    assert app.resolve_supervisor_lan_ipv4() is None


def test_addon_url_output_uses_bridge_port_and_lan_host(monkeypatch):
    app = _load_entrypoint()
    monkeypatch.setattr(app, "resolve_supervisor_lan_ipv4", lambda: "192.168.1.110")
    lines = []
    monkeypatch.setattr(app, "print", lambda value, **_kwargs: lines.append(value))
    app.log_bridge_urls(8099)
    assert lines == [
        "Bridge internal URL: http://[HOST]:[PORT:8099]",
        "Bridge LAN URL: http://192.168.1.110:8099",
    ]


def test_addon_url_output_falls_back_without_fake_container_ip(monkeypatch):
    app = _load_entrypoint()
    monkeypatch.setattr(app, "resolve_supervisor_lan_ipv4", lambda: None)
    lines = []
    monkeypatch.setattr(app, "print", lambda value, **_kwargs: lines.append(value))
    app.log_bridge_urls(8099)
    assert lines[1] == (
        "Bridge LAN URL: unavailable; configure HACS with "
        "http://<HOME_ASSISTANT_LAN_IP>:8099"
    )
    assert "172." not in lines[1]
