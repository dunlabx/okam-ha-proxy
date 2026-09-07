"""Production wiring tests for the add-on startup orchestration."""

import importlib.util
import sys
from pathlib import Path

import pytest

from okam_native.account import AccountDevice
from okam_native.auth import CameraAuthenticator, CredentialSourceCache, FALLBACK_SOURCE
from okam_native.p2p import AuthenticationResult
from okam_native.bridge import BridgeRegistry


ROOT = Path(__file__).parents[1]


def _load_entrypoint():
    spec = importlib.util.spec_from_file_location(
        "okam_test_entrypoint", ROOT / "okam_native_app" / "app_entrypoint.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.stdout = FakePipe()
        self.stderr = FakePipe()

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = 1

    def wait(self, timeout=None):
        self.returncode = 0
        return 0


class FakePipe:
    def read(self, _size=-1):
        return b""


def _auth_result(success: bool, login_result: int) -> AuthenticationResult:
    return AuthenticationResult(
        connected=True,
        connect_state=3,
        login_sent=True,
        login_response_received=True,
        authenticated=success,
        login_command=0x6001,
        login_result=login_result,
        disconnected=False,
    )


@pytest.fixture
def entrypoint(monkeypatch, tmp_path):
    app = _load_entrypoint()
    app.BRIDGES = BridgeRegistry()
    app.DATA = tmp_path
    app.AUTHENTICATOR = CameraAuthenticator(
        CredentialSourceCache(tmp_path / "camera_auth_cache.json"),
        logger=lambda line: app_logs.append(line),
    )
    app_logs = []
    options = {
        "account_username": "user@example.com",
        "account_password": "account-secret",
        "api_token": "safe-api-token-123",
        "cameras": [],
        "run_connect_test": False,
        "run_auth_test": False,
        "run_stream_test": False,
        "run_snapshot_test": False,
        "api_port": 8099,
        "rtsp_port": 8100,
        "idle_timeout_seconds": 10,
    }
    monkeypatch.setattr(app, "load_options", lambda: options)
    monkeypatch.setattr(app, "load_wake_credentials", lambda _path: ("key", "secret"))
    monkeypatch.setattr(app, "resolve_client_id", lambda uid: uid)
    monkeypatch.setattr(app, "get_service_parameter", lambda uid: "service")
    monkeypatch.setattr(app, "run_p2p_acceptance", lambda _device: None)
    return app, options, app_logs


def test_production_startup_enumerates_and_registers_two_cameras(entrypoint, monkeypatch, capsys):
    app, _options, logs = entrypoint
    devices = [AccountDevice("CAMERA_FRONT", "Front", "wrong"), AccountDevice("CAMERA_BACK", "Back", "back")]

    class FakeAccount:
        last_raw_device_count = 2

        def enumerate(self, _username, _password):
            return devices

    monkeypatch.setattr(app, "Eye4AccountClient", FakeAccount)
    wake_calls = []
    monkeypatch.setattr(app, "wake_camera", lambda *args, **kwargs: wake_calls.append(1))
    selections = app.enumerate_account()
    assert selections is not None
    assert [item.device.uid for item in selections] == ["CAMERA_FRONT", "CAMERA_BACK"]
    assert app.initialize_camera_runtimes(selections) == 2
    assert [bridge.camera_uid for bridge in app.BRIDGES.values()] == ["CAMERA_FRONT", "CAMERA_BACK"]
    output = capsys.readouterr().out
    assert "account_enumerated=true raw_count=2 parsed_count=2 selected_count=2" in output
    assert "camera_registered uid=CAMERA_FRONT" in output
    assert wake_calls == []


def test_production_lazy_stream_uses_bounded_fallback_and_reuses_cache(entrypoint, monkeypatch):
    app, _options, logs = entrypoint
    devices = [AccountDevice("CAMERA_FRONT", "Front", "wrong")]
    monkeypatch.setattr(app, "Eye4AccountClient", type("FakeAccount", (), {"last_raw_device_count": 1, "enumerate": lambda self, _u, _p: devices}))
    selections = app.enumerate_account()
    assert selections is not None
    assert app.initialize_camera_runtimes(selections) == 1
    calls = []

    def fake_open(_helper, _library, _uid, _service, password, **_kwargs):
        calls.append(password)
        success = password == "888888"
        return _auth_result(success, 0 if success else -1), FakeProcess()

    monkeypatch.setattr(app, "open_authenticated_stream_process", fake_open)
    bridge = app.BRIDGES.values()[0]
    subscription = bridge.session.acquire()
    subscription.close()
    assert calls == ["wrong", "888888"]
    assert any("camera_auth_selected uid=CAMERA_FRONT candidate=fallback_888888" in line for line in logs)
    bridge.session.close()
    app.BRIDGES = BridgeRegistry()
    assert app.configure_bridge(selections[0], selected_count=1) is not None
    second = app.BRIDGES.values()[0].session.acquire()
    second.close()
    assert calls == ["wrong", "888888", "888888"]
    assert any("candidate=cached_fallback_888888" in line for line in logs)


def test_production_startup_isolates_one_camera_failure(entrypoint, monkeypatch):
    app, _options, _logs = entrypoint
    selections = [
        type("Selection", (), {"device": AccountDevice("A", "A", "pw"), "alias": None})(),
        type("Selection", (), {"device": AccountDevice("B", "B", "pw"), "alias": None})(),
    ]
    monkeypatch.setattr(app, "run_p2p_acceptance", lambda device: (_ for _ in ()).throw(RuntimeError()) if device.uid == "A" else None)
    assert app.initialize_camera_runtimes(selections) == 1
    assert [bridge.camera_uid for bridge in app.BRIDGES.values()] == ["B"]
