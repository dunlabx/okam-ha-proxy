import io
import json
import struct
import subprocess
import sys
from urllib.parse import urlsplit

import pytest

from okam_native.p2p import (
    _jpeg_dimensions,
    _stream_result,
    ConnectResult,
    P2PError,
    diagnostic_line,
    get_service_parameter,
    open_stream_process,
    open_authenticated_stream_process,
    resolve_client_id,
    run_authentication_probe,
    run_connect_probe,
    run_stream_probe,
    select_camera_password,
)
from okam_native import amd64_helper


def test_service_parameter_uses_only_device_family() -> None:
    requests = []

    def opener(request, timeout: float) -> bytes:
        requests.append((request, timeout))
        return json.dumps(["A" * 32]).encode()

    value = get_service_parameter("ABCD-sensitive-device-id", opener=opener)

    assert value == "A" * 32
    assert urlsplit(requests[0][0].full_url).path == "/getInitstring"
    assert json.loads(requests[0][0].data) == {"uid": ["ABCD"]}
    assert b"sensitive-device-id" not in requests[0][0].data


def test_virtual_id_is_resolved_without_exposing_it() -> None:
    requests = []

    def opener(request, timeout: float) -> bytes:
        requests.append((request, timeout))
        return json.dumps({"uid": "ABCD123456789"}).encode()

    result = resolve_client_id("VUID1234567A", opener=opener)

    assert result == "ABCD123456789"
    assert urlsplit(requests[0][0].full_url).hostname == "vuid.eye4.cn"


def test_physical_id_does_not_call_virtual_resolver() -> None:
    def opener(_request, _timeout: float) -> bytes:
        raise AssertionError("resolver should not be called")

    assert resolve_client_id("ABCD123456789", opener=opener) == "ABCD123456789"


def test_connect_probe_passes_sensitive_fields_only_on_stdin(monkeypatch) -> None:
    recorded = {}

    def run(command, **kwargs):
        recorded["command"] = command
        recorded["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=b'{"connected":true,"connect_state":3,"disconnected":true}\n',
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    result = run_connect_probe(
        "/helper",
        "/library",
        "sensitive-device-id",
        "sensitive-service-parameter",
        environment={"SAFE": "1"},
    )

    assert result.connected is True
    assert recorded["command"] == ["/helper", "/library"]
    assert b"sensitive-device-id" in recorded["input"]
    first_size = struct.unpack(">I", recorded["input"][:4])[0]
    assert first_size == len(b"sensitive-device-id")


def test_directory_errors_do_not_expose_identifier() -> None:
    def opener(_request, _timeout: float) -> bytes:
        raise OSError("failure containing ABCD-sensitive-device-id")

    with pytest.raises(P2PError) as caught:
        get_service_parameter("ABCD-sensitive-device-id", opener=opener)
    assert "sensitive-device-id" not in str(caught.value)


def test_camera_password_prefers_explicit_override() -> None:
    assert select_camera_password("account-value", "configured-value") == "configured-value"


def test_camera_password_uses_enumerated_value_when_available() -> None:
    assert select_camera_password("account-value") == "account-value"


def test_camera_password_uses_safe_default_when_account_omits_it() -> None:
    assert select_camera_password("") == "888888"
    assert select_camera_password(None, "") == "888888"


def test_camera_password_rejects_invalid_override_without_echoing_it() -> None:
    secret = "invalid\nsecret"
    with pytest.raises(P2PError) as caught:
        select_camera_password(None, secret)
    assert secret not in str(caught.value)


def test_authentication_probe_passes_all_sensitive_fields_only_on_stdin(monkeypatch) -> None:
    recorded = {}

    def run(command, **kwargs):
        recorded["command"] = command
        recorded["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                b'{"connected":true,"connect_state":3,"login_sent":true,'
                b'"login_response_received":true,"authenticated":true,'
                b'"login_command":24736,"login_result":0,"disconnected":true}\n'
            ),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    result = run_authentication_probe(
        "/helper",
        "/library",
        "sensitive-device-id",
        "sensitive-service-parameter",
        "sensitive-device-password",
        environment={"SAFE": "1"},
    )

    assert result.authenticated is True
    assert result.login_result == 0
    assert recorded["command"] == ["/helper", "/library", "--authenticate"]
    assert b"sensitive-device-password" in recorded["input"]
    assert b"sensitive-device-password" not in b" ".join(
        value.encode() for value in recorded["command"]
    )


def test_empty_password_survives_exact_container_stdin_serialization_and_parser(monkeypatch, capsys) -> None:
    serialized = {}

    def subprocess_run(command, **kwargs):
        serialized["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                b'{"connected":true,"connect_state":3,"login_sent":true,'
                b'"login_response_received":true,"authenticated":true,'
                b'"login_command":24577,"login_result":0,"disconnected":true}\n'
            ),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", subprocess_run)
    run_authentication_probe(
        "/helper", "/library", "UID", "service", "", environment={}
    )

    received = {}

    def helper_run(mode, uid, service, password, **_kwargs):
        received.update(mode=mode, uid=uid, service=service, password=password)
        return 0, {"connected": True}

    monkeypatch.setattr(amd64_helper, "run", helper_run)
    monkeypatch.setattr(sys, "argv", ["helper", "library", "--authenticate"])
    monkeypatch.setattr(
        sys,
        "stdin",
        io.TextIOWrapper(io.BytesIO(serialized["input"]), encoding="utf-8"),
    )
    assert amd64_helper.main() == 0
    assert received == {
        "mode": "authenticate",
        "uid": "UID",
        "service": "service",
        "password": "",
    }
    output = capsys.readouterr().out
    assert "native_login_input username_present=true password_present=true password_length=0" in output
    assert "connected" in output  # helper emitted a valid response


def test_debug_empty_password_logs_identical_zero_length_bytes(monkeypatch, capsys) -> None:
    recorded = {}

    def run(command, **kwargs):
        recorded["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command, 0,
            stdout=(b'{"connected":true,"connect_state":3,"login_sent":true,'
                    b'"login_response_received":true,"authenticated":true,'
                    b'"login_command":24577,"login_result":0,"disconnected":true}\n'),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    run_authentication_probe(
        "/helper", "/library", "UID", "service", "",
        environment={"OKAM_DEBUG_CREDENTIALS": "1"},
    )
    output = capsys.readouterr().out
    assert "ipc_write username_present=true password_present=true username_repr='admin' username_length=5 password_repr='' password_length=0 password_hex=" in output
    assert "native_login_input username_present=true password_present=true username_repr='admin' username_length=5 password_repr='' password_length=0 password_hex=" in output
    assert struct.unpack(">I", recorded["input"][-4:])[0] == 0


def test_api_empty_and_manual_empty_use_identical_native_bytes() -> None:
    from okam_native.p2p import _field

    api_bytes = _field("UID") + _field("service") + _field("", allow_empty=True)
    manual_bytes = _field("UID") + _field("service") + _field("", allow_empty=True)
    assert api_bytes == manual_bytes
    assert api_bytes[-4:] == b"\x00\x00\x00\x00"


def test_stream_probe_returns_only_sanitized_metrics(monkeypatch) -> None:
    recorded = {}

    def run(command, **kwargs):
        recorded["command"] = command
        recorded["input"] = kwargs["input"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=(
                b'{"connected":true,"connect_state":3,"login_sent":true,'
                b'"login_response_received":true,"authenticated":true,'
                b'"login_command":24577,"login_result":0,'
                b'"stream_start_sent":true,"stream_stop_sent":true,'
                b'"h264_received":true,"h264_frames":4,"h264_bytes":8192,'
                b'"keyframe_seen":true,"h265_frames":0,"disconnected":true}\n'
            ),
            stderr=b"",
        )

    monkeypatch.setattr(subprocess, "run", run)
    result = run_stream_probe(
        "/helper",
        "/library",
        "sensitive-device-id",
        "sensitive-service-parameter",
        "sensitive-device-password",
        environment={"SAFE": "1"},
    )

    assert result.h264_received is True
    assert result.h264_frames == 4
    assert result.h264_bytes == 8192
    assert recorded["command"] == ["/helper", "/library", "--stream-test"]
    assert b"sensitive-device-password" in recorded["input"]
    assert b"sensitive-device-password" not in b" ".join(
        value.encode() for value in recorded["command"]
    )


def test_jpeg_dimensions_accepts_bounded_sof_header() -> None:
    jpeg = (
        b"\xff\xd8"
        b"\xff\xe0\x00\x04\x00\x00"
        b"\xff\xc0\x00\x11\x08\x01\xe0\x02\x80"
        b"\x03\x01\x11\x00\x02\x11\x00\x03\x11\x00"
        b"\xff\xd9"
    )

    assert _jpeg_dimensions(jpeg) == (640, 480)


def test_open_stream_process_keeps_sensitive_fields_out_of_argv(monkeypatch) -> None:
    recorded = {}

    class Input:
        def write(self, value):
            recorded["input"] = value

        def close(self):
            pass

    class Process:
        stdin = Input()
        stdout = object()
        stderr = object()

    def popen(command, **kwargs):
        recorded["command"] = command
        return Process()

    monkeypatch.setattr(subprocess, "Popen", popen)
    open_stream_process(
        "/helper",
        "/library",
        "sensitive-device-id",
        "sensitive-service-parameter",
        "sensitive-device-password",
        environment={"SAFE": "1"},
    )

    assert recorded["command"] == ["/helper", "/library", "--stream-stdout"]
    assert b"sensitive-device-password" in recorded["input"]
    assert "sensitive-device-password" not in " ".join(recorded["command"])


def test_authenticated_stream_waits_for_pre_media_auth_event(monkeypatch) -> None:
    class Input:
        def write(self, _value):
            pass

        def close(self):
            pass

    class ErrorPipe:
        def __iter__(self):
            yield b'{"okam_auth":true,"connected":true,"connect_state":3,"login_sent":true,"login_response_received":true,"authenticated":true,"login_command":24577,"login_result":0}\n'

    class Process:
        stdin = Input()
        stdout = object()
        stderr = ErrorPipe()

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(subprocess, "Popen", lambda *_args, **_kwargs: Process())
    process, result = open_authenticated_stream_process(
        "/helper", "/library", "UID", "service", "secret", environment={}
    )
    assert process is not None
    assert result.authenticated is True
    assert result.login_result == 0


def _stream_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "connected": True,
        "connect_state": 3,
        "login_sent": True,
        "login_response_received": True,
        "authenticated": True,
        "login_command": 0x6001,
        "login_result": 0,
        "disconnected": True,
        "stream_start_sent": True,
        "stream_stop_sent": True,
        "h264_received": False,
        "h264_frames": 0,
        "h264_bytes": 0,
        "keyframe_seen": False,
        "h265_frames": 0,
    }
    payload.update(overrides)
    return payload


def test_stream_result_carries_transport_diagnostics() -> None:
    result = _stream_result(
        _stream_payload(
            connect_path="direct-punch",
            login_candidate=2,
            login_attempts=[-1, None, 0],
            stream_start_command=0x60D1,
            stream_start_result=0,
            counters={"channel0_bytes": 96, "channel1_bytes": 0},
        ),
        0,
    )

    assert result.connect_path == "direct-punch"
    assert result.login_candidate == 2
    assert result.login_attempts == (-1, None, 0)
    assert result.stream_start_result == 0
    assert result.counters == {"channel0_bytes": 96, "channel1_bytes": 0}


@pytest.mark.parametrize(
    "counters",
    [
        {"channel1_bytes": -1},
        {"channel1_bytes": "many"},
        {"channel1_bytes": True},
        [("channel1_bytes", 1)],
        {str(index): index for index in range(65)},
    ],
)
def test_malformed_counters_degrade_instead_of_failing_the_probe(counters: object) -> None:
    # Diagnostics explain a failure, so they must never cause one.
    result = _stream_result(_stream_payload(counters=counters), 0)

    assert result.counters == {}
    assert result.connected is True


def test_diagnostic_line_is_counts_only_and_survives_partial_results() -> None:
    connect_only = ConnectResult(
        connected=True,
        connect_state=3,
        disconnected=True,
        connect_path="relay",
        counters={"packets_from_other_source": 7, "channel1_bytes": 0},
    )

    line = diagnostic_line(connect_only)

    assert line.startswith("transport_diagnostics ")
    assert "connect_path=relay" in line
    assert "channel1_bytes=0 packets_from_other_source=7" in line
    assert "login_attempts=-" in line


def test_diagnostic_line_renders_silent_login_candidates() -> None:
    result = _stream_result(
        _stream_payload(login_attempts=[-1, None], counters={}), 0
    )

    assert "login_attempts=-1,none" in diagnostic_line(result)
