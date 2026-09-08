import http.client
import json
import threading
from http.server import ThreadingHTTPServer
from urllib.parse import urlsplit

from okam_native.bridge import CameraBridge, QuietThreadingHTTPServer, make_handler
from okam_native.session import SessionStatus


class FakeSubscription:
    def __init__(self) -> None:
        self.closed = False

    def __iter__(self):
        yield b"\x00\x00\x00\x01h264"

    def close(self) -> None:
        self.closed = True


class FakeSession:
    idle_timeout = 30.0

    def __init__(self) -> None:
        self.subscription = FakeSubscription()
        self.running = False
        self.media_ready = False

    def status(self) -> SessionStatus:
        return SessionStatus(self.running, 0, True, None, self.media_ready)

    def snapshot(self, _ffmpeg: str):
        return b"\xff\xd8jpeg\xff\xd9", 2304, 1296

    def acquire(self, *, reason: str = "active") -> FakeSubscription:
        return self.subscription


def request(server, method: str, path: str, *, token: str | None = None, body=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=3)
    headers = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    encoded = None
    if body is not None:
        encoded = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    payload = response.read()
    connection.close()
    return response.status, response.getheader("Content-Type"), payload


def test_bridge_api_is_authenticated_and_exposes_native_camera() -> None:
    session = FakeSession()
    bridge = CameraBridge(
        camera_id="cabin",
        camera_name="Cabin",
        api_token="safe-api-token-123",
        session=session,  # type: ignore[arg-type]
        ffmpeg="/usr/bin/ffmpeg",
    )
    status = {
        "loader_ready": True,
        "account_ready": True,
        "camera_ready": True,
        "configuration_required": False,
    }
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(lambda: status, lambda: bridge)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "GET", "/health")[0] == 200
        assert request(server, "GET", "/ready")[0] == 200
        assert request(server, "GET", "/api/devices")[0] == 401
        code, _, payload = request(
            server, "GET", "/api/devices", token="safe-api-token-123"
        )
        assert code == 200
        assert json.loads(payload) == [{"uid": "cabin", "camera_id": "cabin", "camera_uid": "cabin", "alias": None, "name": "Cabin"}]

        code, _, payload = request(
            server,
            "GET",
            "/api/cameras/cabin/stream/source",
            token="safe-api-token-123",
        )
        assert code == 200
        stream_url = json.loads(payload)["stream_url"]
        assert "safe-api-token-123" not in stream_url
        parsed = urlsplit(stream_url)
        # The advertised source is MPEG-TS: a raw elementary stream carries no
        # timestamps and Home Assistant's stream worker rejects it.
        assert parsed.path.endswith("/stream.ts")
        raw_path = parsed.path.replace("/stream.ts", "/stream.h264")
        code, content_type, payload = request(server, "GET", raw_path + "?" + parsed.query)
        assert code == 200
        assert content_type == "video/h264"
        assert payload.endswith(b"h264")
        assert session.subscription.closed is True

        # The muxed endpoint degrades rather than hanging when the muxer is
        # unavailable, and it never leaks the subscription.
        code, _content_type, _payload = request(
            server, "GET", parsed.path + "?" + parsed.query
        )
        assert code == 503
        assert session.subscription.closed is True

        code, content_type, payload = request(
            server,
            "GET",
            "/api/cameras/cabin/snapshot.jpg",
            token="safe-api-token-123",
        )
        assert code == 200
        assert content_type == "image/jpeg"
        assert payload.startswith(b"\xff\xd8")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_devices_endpoint_lists_each_enabled_camera_without_credentials() -> None:
    first = CameraBridge(
        camera_id="front-alias",
        camera_uid="UID_FRONT",
        camera_name="Front Door",
        api_token="shared-token-123",
        session=FakeSession(),  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )
    second = CameraBridge(
        camera_id="back-alias",
        camera_uid="UID_BACK",
        camera_name="Back Door",
        api_token="shared-token-123",
        session=FakeSession(),  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )
    from okam_native.bridge import BridgeRegistry

    registry = BridgeRegistry()
    registry.add(first)
    registry.add(second)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(lambda: {}, registry)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert request(server, "GET", "/api/devices")[0] == 401
        code, _content_type, payload = request(
            server, "GET", "/api/devices", token="shared-token-123"
        )
        assert code == 200
        assert json.loads(payload) == [
            {"uid": "UID_FRONT", "camera_id": "front-alias", "camera_uid": "UID_FRONT", "alias": "front-alias", "name": "Front Door"},
            {"uid": "UID_BACK", "camera_id": "back-alias", "camera_uid": "UID_BACK", "alias": "back-alias", "name": "Back Door"},
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_bridge_idle_timeout_is_bounded() -> None:
    session = FakeSession()
    bridge = CameraBridge(
        camera_id="cabin",
        camera_name="Cabin",
        api_token="safe-api-token-123",
        session=session,  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0),
        make_handler(lambda: {"loader_ready": True, "camera_ready": True}, lambda: bridge),
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        code, _, _ = request(
            server,
            "PATCH",
            "/api/cameras/cabin/config",
            token="safe-api-token-123",
            body={"idle_timeout_seconds": 45},
        )
        assert code == 200
        assert session.idle_timeout == 45.0
        code, _, _ = request(
            server,
            "PATCH",
            "/api/cameras/cabin/config",
            token="safe-api-token-123",
            body={"idle_timeout_seconds": 1},
        )
        assert code == 400
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def test_bridge_reports_idle_waking_and_streaming_states() -> None:
    session = FakeSession()
    bridge = CameraBridge(
        camera_id="cabin",
        camera_name="Cabin",
        api_token="safe-api-token-123",
        session=session,  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )

    assert bridge.status()["state"] == "idle"
    assert bridge.status()["media_ready"] is False
    assert bridge.status()["idle_timeout_seconds"] == 30
    session.running = True
    assert bridge.status()["state"] == "waking"
    session.media_ready = True
    assert bridge.status()["state"] == "streaming"


def test_dropped_clients_do_not_report_a_crash(capsys) -> None:
    # The supervisor polls the bridge and closes connections abruptly. The
    # default handler prints a traceback and the peer address for each one.
    server = object.__new__(QuietThreadingHTTPServer)
    peer = ("172.30.32.2", 47536)

    for benign in (ConnectionResetError, BrokenPipeError, TimeoutError):
        try:
            raise benign("client went away")
        except benign:
            server.handle_error(object(), peer)
    assert capsys.readouterr().err == ""

    try:
        raise ValueError("a real fault")
    except ValueError:
        server.handle_error(object(), peer)
    captured = capsys.readouterr().err
    assert "bridge_request_failed error=ValueError" in captured
    assert "message=a real fault" in captured
    assert "method=UNKNOWN path=- camera_uid=- operation=server.handle_error" in captured
    assert "172.30.32.2" not in captured
    assert "traceback=" in captured


def test_unexpected_request_exception_logs_safe_production_context(capsys) -> None:
    bridge = CameraBridge(
        camera_id="front",
        camera_uid="UID_FRONT",
        camera_name="Front",
        api_token="safe-token-123",
        session=FakeSession(),  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )

    def failing_status() -> dict[str, object]:
        raise AttributeError("Popen object has no attribute connected password=secret")

    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), make_handler(failing_status, lambda: bridge)
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            request(server, "GET", "/ready?token=secret", token="safe-token-123")
        except (ConnectionError, OSError, http.client.HTTPException):
            pass
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    captured = capsys.readouterr().err
    assert "bridge_request_failed error=AttributeError" in captured
    assert "message=Popen object has no attribute connected" in captured
    assert "method=GET path=/ready" in captured
    assert "operation=do_GET" in captured
    assert "traceback=" in captured
    assert "secret" not in captured
