"""End-to-end HACS client checks against the production bridge HTTP server."""

from __future__ import annotations

import asyncio
import importlib.util
import threading
from pathlib import Path

import aiohttp
import pytest

from okam_native.bridge import BridgeRegistry, CameraBridge, QuietThreadingHTTPServer, make_handler
from okam_native.session import SessionStatus

ROOT = Path(__file__).parents[1]


def _load_api_module():
    path = ROOT / "custom_components/okam/api.py"
    spec = importlib.util.spec_from_file_location("okam_hacs_api", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _PassiveSession:
    idle_timeout = 120.0

    def __init__(self) -> None:
        self.acquire_calls = 0

    def status(self) -> SessionStatus:
        return SessionStatus(False, 0, None, None, False)

    def acquire(self, **_kwargs):
        self.acquire_calls += 1
        raise AssertionError("passive config flow must not acquire a stream")


def test_real_bridge_server_and_hacs_client_resolve_alias_without_camera_activation():
    api_module = _load_api_module()
    token = "safe-api-token-123"
    first_session = _PassiveSession()
    second_session = _PassiveSession()
    registry = BridgeRegistry()
    registry.add(
        CameraBridge(
            camera_id="camera1",
            camera_uid="UID_CAMERA_1",
            camera_name="Front",
            api_token=token,
            session=first_session,  # type: ignore[arg-type]
            ffmpeg="ffmpeg",
        )
    )
    registry.add(
        CameraBridge(
            camera_id="camera2",
            camera_uid="UID_CAMERA_2",
            camera_name="Back",
            api_token=token,
            session=second_session,  # type: ignore[arg-type]
            ffmpeg="ffmpeg",
        )
    )
    status = {
        "loader_ready": True,
        "account_ready": True,
        "camera_ready": True,
        "configuration_required": False,
    }
    server = QuietThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(lambda: status, lambda: registry)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    async def exercise() -> None:
        base = f"http://127.0.0.1:{server.server_port}/"
        async with aiohttp.ClientSession() as session:
            client = api_module.OkamApi(session, f"  {base}  ", f" {token} ")
            health = await client.health()
            devices = await client.devices()
            assert health["status"] == "ok"
            assert {item["uid"] for item in devices} == {"UID_CAMERA_1", "UID_CAMERA_2"}
            identity_spec = importlib.util.spec_from_file_location(
                "okam_hacs_identity", ROOT / "custom_components/okam/identity.py"
            )
            assert identity_spec and identity_spec.loader
            identity = importlib.util.module_from_spec(identity_spec)
            identity_spec.loader.exec_module(identity)
            result = identity.validated_from_devices(
                {"bridge_url": client.base_url, "camera_id": "camera1"}, devices
            )
            assert result["camera_uid"] == "UID_CAMERA_1"
            assert result["camera_id"] == "UID_CAMERA_1"

            with pytest.raises(api_module.OkamAuthError):
                await api_module.OkamApi(session, client.base_url, "wrong-token-123").devices()
            with pytest.raises(api_module.OkamAuthError):
                await api_module.OkamApi(session, client.base_url, "").devices()

    try:
        asyncio.run(exercise())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert first_session.acquire_calls == 0
    assert second_session.acquire_calls == 0


def test_bridge_url_normalization_rejects_invalid_scheme():
    api_module = _load_api_module()
    with pytest.raises(api_module.OkamInvalidResponseError):
        api_module.normalize_bridge_url("homeassistant.local:8099")
