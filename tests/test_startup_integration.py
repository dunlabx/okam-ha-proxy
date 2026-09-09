"""Production wiring tests for the add-on startup orchestration."""

import importlib.util
import sys
from pathlib import Path

import pytest

from okam_native.account import AccountDevice, CameraSelection
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


def test_real_entrypoint_options_emit_plaintext_api_diagnostics_when_enabled(entrypoint, monkeypatch, capsys):
    app, options, _logs = entrypoint
    options["debug_credentials"] = True

    def opener(request, _timeout):
        path = request.full_url.split("?", 1)[0]
        if path.endswith("/user/summary"):
            return b'{"userid":123}'
        if path.endswith("/login/token"):
            return b'{"token":"opaque"}'
        if path.endswith("/PC/device/show"):
            return b'[{"uid":"TEST_UID","nickname":"Test Camera","password":""}]'
        raise AssertionError(path)

    from okam_native.account import Eye4AccountClient

    monkeypatch.setattr(
        app,
        "Eye4AccountClient",
        lambda **kwargs: Eye4AccountClient(opener=opener, **kwargs),
    )
    selected = app.enumerate_account()
    assert selected is not None
    output = capsys.readouterr().out
    assert "api_device_raw uid=TEST_UID nickname='Test Camera' password=''" in output
    assert "api_device_parsed uid=TEST_UID password=''" in output
    assert "camera_registered uid=TEST_UID alias=Test Camera auth_method=automatic" in output


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


def test_production_path_propagates_other_account_password(entrypoint, monkeypatch):
    app, _options, logs = entrypoint
    devices = [
        AccountDevice("CAMERA_A", "A", "password-a"),
        AccountDevice("CAMERA_B", "B", "password-b"),
    ]
    monkeypatch.setattr(
        app,
        "Eye4AccountClient",
        type("FakeAccount", (), {"last_raw_device_count": 2, "enumerate": lambda self, _u, _p: devices}),
    )
    selections = app.enumerate_account()
    assert selections is not None
    monkeypatch.setattr(app, "open_authenticated_stream_process", lambda *args, **kwargs: (_auth_result(kwargs.get("credential_index") == 0 and args[4] == "password-b", 0 if args[4] == "password-b" else -1), FakeProcess()))
    assert app.initialize_camera_runtimes(selections, tuple(devices)) == 2
    first = app.BRIDGES.values()[0].session.acquire()
    first.close()
    assert any("source_uid=CAMERA_B" in line for line in logs)


def test_production_path_isolates_manual_and_automatic_camera_auth(entrypoint, monkeypatch, capsys):
    app, options, logs = entrypoint
    options["cameras"] = [
        {"uid": "CAMERA_MANUAL", "password": "manual-secret"},
        {"uid": "CAMERA_AUTO"},
    ]
    devices = [
        AccountDevice("CAMERA_MANUAL", "Manual", "account-secret"),
        AccountDevice("CAMERA_AUTO", "Automatic", "account-auto"),
    ]
    selections = [
        CameraSelection(devices[0], password="manual-secret"),
        CameraSelection(devices[1]),
    ]
    calls = []

    def fake_open(_helper, _library, _uid, _service, password, **_kwargs):
        calls.append(password)
        ok = password in {"manual-secret", "account-auto"}
        return _auth_result(ok, 0 if ok else -1), FakeProcess()

    monkeypatch.setattr(app, "open_authenticated_stream_process", fake_open)
    assert app.initialize_camera_runtimes(selections, tuple(devices)) == 2
    for bridge in app.BRIDGES.values():
        subscription = bridge.session.acquire()
        subscription.close()
    assert calls == ["manual-secret", "account-auto"]
    rendered = "\n".join(logs) + capsys.readouterr().out
    assert "auth_method=password" in rendered
    assert "manual-secret" not in rendered


def test_stale_global_camera_password_is_ignored(entrypoint):
    app, _options, _logs = entrypoint
    selection = CameraSelection(AccountDevice("CAMERA", "Camera", "account-password"))
    password, strict = app._camera_password_override(
        selection, {"camera_password": "stale-global-password", "cameras": []}
    )
    assert password is None
    assert strict is False


def test_production_startup_never_runs_camera_diagnostics(entrypoint, monkeypatch):
    app, options, _logs = entrypoint
    options["run_auth_test"] = True
    selections = [
        type("Selection", (), {"device": AccountDevice("A", "A", "pw"), "alias": None})(),
        type("Selection", (), {"device": AccountDevice("B", "B", "pw"), "alias": None})(),
    ]
    diagnostic_calls = []
    monkeypatch.setattr(app, "run_p2p_acceptance", lambda selection: diagnostic_calls.append(selection.device.uid))
    assert app.initialize_camera_runtimes(selections) == 2
    assert diagnostic_calls == []
    assert [bridge.camera_uid for bridge in app.BRIDGES.values()] == ["A", "B"]


def test_build_fingerprint_reports_runtime_identity(entrypoint, monkeypatch, capsys):
    app, _options, _logs = entrypoint
    monkeypatch.setenv("OKAM_BUILD_VERSION", "1.2.14")
    monkeypatch.setenv("OKAM_BUILD_COMMIT", "commit-under-test")
    app.log_build_fingerprint()
    output = capsys.readouterr().out
    assert "build_fingerprint build_version=1.2.14 build_commit=commit-under-test" in output
    assert "architecture=" in output
    assert "runtime_module bridge=" in output
    assert "session=" in output and "auth=" in output
    assert "rtsp=" in output and "p2p=" in output


def test_ready_status_does_not_start_or_wake_a_blocked_session(entrypoint):
    app, _options, _logs = entrypoint
    starts = []

    def start():
        starts.append(True)
        return FakeProcess()

    session = app.NativeStreamSession(start)  # type: ignore[arg-type]
    app.BRIDGES.add(app.CameraBridge(
        camera_id="front", camera_uid="UID_FRONT", camera_name="Front",
        api_token="x" * 16, session=session, ffmpeg="ffmpeg",
    ))
    payload = app.get_status()
    assert payload["camera_count"] == 1
    assert starts == []
    session.close()
