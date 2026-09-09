"""Authenticated HTTP compatibility API for the native O-KAM bridge."""

from __future__ import annotations

import hmac
import json
import re
import secrets
import subprocess
import sys
import threading
import time
import traceback
import unicodedata
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlsplit

from .p2p import P2PError
from .session import NativeStreamSession
from .logging import timestamped_print


print = timestamped_print


MAX_REQUEST_BYTES = 4096
STREAM_CHUNK_BYTES = 32 * 1024
MEDIA_WRITE_TIMEOUT_SECONDS = 5.0
# A client going away mid-request is normal here: the supervisor polls the
# bridge, and media consumers disconnect whenever a view closes.
_EXPECTED_DISCONNECTS = (
    BrokenPipeError,
    ConnectionAbortedError,
    ConnectionResetError,
    TimeoutError,
)
HOST_PATTERN = re.compile(r"^(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:]+\])(?::[0-9]{1,5})?$")
_SECRET_TEXT = re.compile(r"(?i)(password|token|secret|authorization)(\s*[=:]\s*)([^\s,;]+)")


def _redact_diagnostic_text(value: str) -> str:
    """Keep request diagnostics useful without echoing credential material."""

    return _SECRET_TEXT.sub(r"\1\2<redacted>", value).replace("\n", "\\n")


def _request_camera_uid(path: str) -> str | None:
    parts = [unquote(part) for part in urlsplit(path).path.split("/") if part]
    if len(parts) >= 3 and parts[:2] == ["api", "cameras"]:
        return parts[2]
    return None


def _session_diagnostic(bridge: CameraBridge, event: str, **fields: object) -> None:
    emit = getattr(bridge.session, "diagnostic", None)
    if callable(emit):
        emit(event, **fields)


class QuietThreadingHTTPServer(ThreadingHTTPServer):
    """An HTTP server that does not report a dropped client as a crash.

    The default handler prints a full traceback and the peer address whenever a
    client disconnects mid-request. The supervisor polls this bridge and closes
    connections abruptly, which filled the app log with alarming
    ConnectionResetError tracebacks for entirely normal behaviour.
    """

    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        error = sys.exc_info()[1]
        if isinstance(error, _EXPECTED_DISCONNECTS):
            return
        message = _redact_diagnostic_text(str(error)) or "<empty>"
        stack = _redact_diagnostic_text(traceback.format_exc())
        print(
            "bridge_request_failed "
            f"error={type(error).__name__} message={message} "
            "method=UNKNOWN path=- camera_uid=- operation=server.handle_error "
            f"traceback={stack}",
            file=sys.stderr,
            flush=True,
        )


class CameraBridge:
    """One camera exposed through a reference-counted native stream session."""

    def __init__(
        self,
        *,
        camera_id: str,
        camera_uid: str | None = None,
        camera_name: str,
        api_token: str,
        session: NativeStreamSession,
        ffmpeg: str,
        battery_camera: bool = True,
        camera_ip: str | None = None,
    ) -> None:
        self.camera_id = camera_id
        self.camera_uid = camera_uid or camera_id
        self.camera_name = camera_name
        self._api_token = api_token
        self._stream_token = secrets.token_urlsafe(32)
        self.session = session
        self.ffmpeg = ffmpeg
        self.battery_camera = battery_camera
        self.camera_ip = camera_ip

    def authenticated(self, authorization: str | None) -> bool:
        expected = f"Bearer {self._api_token}"
        return isinstance(authorization, str) and hmac.compare_digest(
            authorization, expected
        )

    def stream_authenticated(self, token: str | None) -> bool:
        return isinstance(token, str) and hmac.compare_digest(token, self._stream_token)

    def stream_url(self, host: str | None) -> str:
        safe_host = host if isinstance(host, str) and HOST_PATTERN.fullmatch(host) else "127.0.0.1:8099"
        camera = quote(self.camera_id, safe="")
        token = quote(self._stream_token, safe="")
        return f"http://{safe_host}/api/cameras/{camera}/stream.ts?token={token}"

    def status(self) -> dict[str, object]:
        session = self.session.status()
        if session.running:
            state = "streaming" if session.media_ready else "waking"
        elif session.standby:
            state = "standby"
        else:
            state = "idle"
        return {
            "camera_id": self.camera_id,
            "camera_uid": self.camera_uid,
            "name": self.camera_name,
            "online": True,
            "state": state,
            "viewers": session.viewers,
            "media_ready": session.media_ready,
            "idle_timeout_seconds": int(self.session.idle_timeout),
            "battery_percent": None,
            "signal_dbm": None,
            "last_event": None,
            "pir_motion": None,
            "charging": None,
            "clean_disconnect": session.clean_disconnect,
            "last_error": session.last_error,
        }


class BridgeRegistry:
    """Thread-safe registry containing independent camera runtimes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bridges: dict[str, CameraBridge] = {}
        self._aliases: dict[str, CameraBridge] = {}

    @staticmethod
    def _route_key(value: str) -> str:
        return unicodedata.normalize("NFC", value.strip()).casefold()

    def add(self, bridge: CameraBridge) -> None:
        with self._lock:
            identifiers = {self._route_key(bridge.camera_id), self._route_key(bridge.camera_uid)}
            existing = {
                self._route_key(item.camera_id)
                for item in self._bridges.values()
            } | {
                self._route_key(item.camera_uid)
                for item in self._bridges.values()
            } | set(self._aliases)
            if identifiers & existing:
                raise ValueError("duplicate camera identifier")
            self._bridges[bridge.camera_id] = bridge
            if bridge.camera_id != bridge.camera_uid and bridge.camera_id.strip():
                self._aliases[self._route_key(bridge.camera_id)] = bridge

    def get(self, identifier: str) -> CameraBridge | None:
        if not identifier.strip():
            return None
        with self._lock:
            bridge = self._bridges.get(identifier)
            if bridge is not None:
                return bridge
            for candidate in self._bridges.values():
                if candidate.camera_uid == identifier:
                    return candidate
            return self._aliases.get(self._route_key(identifier))

    def values(self) -> tuple[CameraBridge, ...]:
        with self._lock:
            return tuple(self._bridges.values())

    def close(self) -> None:
        for bridge in self.values():
            try:
                bridge.session.close()
            except Exception as error:  # pragma: no cover - defensive shutdown path
                print(f"camera_close_failed error={type(error).__name__}", file=sys.stderr)

    def status(self) -> dict[str, object]:
        bridges = self.values()
        statuses = [bridge.status() for bridge in bridges]
        return {
            "camera_count": len(statuses),
            "ready_camera_count": sum(bool(item.get("online")) for item in statuses),
            "streaming_camera_count": sum(item.get("state") == "streaming" for item in statuses),
            "cameras": statuses,
        }


StatusProvider = Callable[[], dict[str, object]]
BridgeProvider = Callable[[], CameraBridge | BridgeRegistry | None] | BridgeRegistry


def _all_bridges(provider: BridgeProvider) -> tuple[CameraBridge, ...]:
    value = provider if isinstance(provider, BridgeRegistry) else provider()
    if isinstance(value, BridgeRegistry):
        return value.values()
    return (value,) if isinstance(value, CameraBridge) else ()


def _bridge_for(provider: BridgeProvider, identifier: str | None = None) -> CameraBridge | None:
    value = provider if isinstance(provider, BridgeRegistry) else provider()
    if isinstance(value, BridgeRegistry):
        if identifier is None:
            bridges = value.values()
            return bridges[0] if bridges else None
        return value.get(identifier)
    if value is None or identifier is None:
        return value if isinstance(value, CameraBridge) else None
    return value if identifier in (value.camera_id, value.camera_uid) else None


def make_handler(
    status_provider: StatusProvider, bridge_provider: BridgeProvider
) -> type[BaseHTTPRequestHandler]:
    """Build an isolated request handler bound to the supplied runtime providers."""

    class BridgeHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def setup(self) -> None:
            super().setup()
            self._request_started = time.monotonic()

        def handle_one_request(self) -> None:  # noqa: D105 - BaseHTTPRequestHandler API
            self._request_started = time.monotonic()
            super().handle_one_request()

        def handle(self) -> None:  # noqa: D105 - BaseHTTPRequestHandler API
            try:
                super().handle()
            except _EXPECTED_DISCONNECTS:
                return
            except Exception as error:  # pragma: no cover - exercised by boundary test
                raw_path = getattr(self, "path", "")
                request_path = urlsplit(raw_path).path or "/"
                method = getattr(self, "command", None) or "UNKNOWN"
                operation = getattr(self, "_request_operation", None) or "handle"
                camera_uid = _request_camera_uid(request_path)
                stack = _redact_diagnostic_text(traceback.format_exc())
                message = _redact_diagnostic_text(str(error)) or "<empty>"
                print(
                    "bridge_request_failed "
                    f"error={type(error).__name__} message={message} "
                    f"method={method} path={request_path} "
                    f"camera_uid={camera_uid or '-'} operation={operation} "
                    f"traceback={stack}",
                    file=sys.stderr,
                    flush=True,
                )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._request_operation = "do_GET"
            parsed = urlsplit(self.path)
            if parsed.path == "/health":
                self._json(200, {"service": "okam-native", "status": "ok"})
                return
            if parsed.path == "/ready":
                payload = status_provider()
                ready = bool(payload.get("loader_ready")) and (
                    bool(payload.get("configuration_required"))
                    or bool(payload.get("camera_ready"))
                    or (
                        bool(payload.get("account_ready"))
                        and (
                            not payload.get("connect_test_enabled")
                            or bool(payload.get("p2p_ready"))
                        )
                        and (
                            not payload.get("auth_test_enabled")
                            or bool(payload.get("camera_authenticated"))
                        )
                        and (
                            not payload.get("stream_test_enabled")
                            or bool(payload.get("h264_ready"))
                        )
                        and (
                            not payload.get("snapshot_test_enabled")
                            or bool(payload.get("snapshot_ready"))
                        )
                    )
                )
                self._json(200 if ready else 503, payload)
                return

            path_parts = [unquote(part) for part in parsed.path.split("/") if part]
            camera_identifier = (
                path_parts[2]
                if len(path_parts) >= 3 and path_parts[:2] == ["api", "cameras"]
                else None
            )
            if path_parts == ["api", "devices"]:
                bridge = _bridge_for(bridge_provider)
                if bridge is None:
                    self._json(503, {"error": "bridge_not_ready"})
                    return
                if not bridge.authenticated(self.headers.get("Authorization")):
                    self._json(401, {"error": "unauthorized"})
                    return
                self._json(
                    200,
                    [
                        {
                            "uid": item.camera_uid,
                            "camera_id": item.camera_id,
                            "camera_uid": item.camera_uid,
                            "alias": item.camera_id if item.camera_id != item.camera_uid else None,
                            "name": item.camera_name,
                        }
                        for item in _all_bridges(bridge_provider)
                    ],
                )
                return

            bridge = _bridge_for(bridge_provider, camera_identifier)
            if bridge is None:
                self._json(404 if camera_identifier else 503, {"error": "camera_not_found" if camera_identifier else "bridge_not_ready"})
                return
            camera_prefixes = {
                f"/api/cameras/{quote(bridge.camera_id, safe='')}",
                f"/api/cameras/{quote(bridge.camera_uid, safe='')}",
            }
            stream_paths = tuple(
                f"{prefix}/stream.{suffix}"
                for prefix in camera_prefixes
                for suffix in ("h264", "ts")
            )
            if parsed.path in stream_paths:
                token = parse_qs(parsed.query).get("token", [None])[0]
                if not bridge.stream_authenticated(token):
                    self._json(401, {"error": "unauthorized"})
                    return
                if parsed.path.endswith("/stream.ts"):
                    _session_diagnostic(bridge, "stream_http_request_received", path=parsed.path)
                    self._muxed_stream(bridge)
                else:
                    self._raw_stream(bridge)
                return
            if not bridge.authenticated(self.headers.get("Authorization")):
                self._json(401, {"error": "unauthorized"})
                return
            if any(parsed.path == f"{prefix}/status" for prefix in camera_prefixes):
                self._json(200, bridge.status())
            elif any(parsed.path == f"{prefix}/snapshot.jpg" for prefix in camera_prefixes):
                self._snapshot(bridge)
            elif any(parsed.path == f"{prefix}/stream/source" for prefix in camera_prefixes):
                self._json(200, {"stream_url": bridge.stream_url(self.headers.get("Host"))})
            else:
                self._json(404, {"error": "not_found"})

        def do_PATCH(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._request_operation = "do_PATCH"
            bridge = self._authorized_bridge()
            if bridge is None:
                return
            path = urlsplit(self.path).path
            camera_prefixes = {
                f"/api/cameras/{quote(bridge.camera_id, safe='')}",
                f"/api/cameras/{quote(bridge.camera_uid, safe='')}",
            }
            if not any(path == f"{prefix}/config" for prefix in camera_prefixes):
                self._json(404, {"error": "not_found"})
                return
            body = self._request_json()
            if body is None:
                return
            idle_timeout = body.get("idle_timeout_seconds")
            if not isinstance(idle_timeout, int) or not 10 <= idle_timeout <= 600:
                self._json(400, {"error": "invalid_idle_timeout"})
                return
            bridge.session.idle_timeout = float(idle_timeout)
            self._json(200, {"idle_timeout_seconds": idle_timeout})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            self._request_operation = "do_POST"
            bridge = self._authorized_bridge()
            if bridge is None:
                return
            path = urlsplit(self.path).path
            camera_prefixes = {
                f"/api/cameras/{quote(bridge.camera_id, safe='')}",
                f"/api/cameras/{quote(bridge.camera_uid, safe='')}",
            }
            if any(path == f"{prefix}/stream/start" for prefix in camera_prefixes):
                if self._request_json() is None:
                    return
                self._json(200, {"stream_url": bridge.stream_url(self.headers.get("Host"))})
            elif any(path == f"{prefix}/stream/stop" for prefix in camera_prefixes):
                if self._request_json() is None:
                    return
                self._json(200, {"stopped": True})
            else:
                self._json(404, {"error": "not_found"})

        def _authorized_bridge(self) -> CameraBridge | None:
            path_parts = [unquote(part) for part in urlsplit(self.path).path.split("/") if part]
            identifier = (
                path_parts[2]
                if len(path_parts) >= 3 and path_parts[:2] == ["api", "cameras"]
                else None
            )
            bridge = _bridge_for(bridge_provider, identifier)
            if bridge is None:
                self._json(404 if identifier else 503, {"error": "camera_not_found" if identifier else "bridge_not_ready"})
                return None
            if not bridge.authenticated(self.headers.get("Authorization")):
                self._json(401, {"error": "unauthorized"})
                return None
            return bridge

        def _snapshot(self, bridge: CameraBridge) -> None:
            try:
                jpeg, _width, _height = bridge.session.snapshot(bridge.ffmpeg)
            except P2PError:
                self._json(503, {"error": "snapshot_unavailable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(jpeg)))
            self.end_headers()
            self.wfile.write(jpeg)

        def _raw_stream(self, bridge: CameraBridge) -> None:
            try:
                subscription = bridge.session.acquire(reason="bridge_http")
            except P2PError:
                self._json(503, {"error": "stream_unavailable"})
                return
            self.send_response(200)
            self.send_header("Content-Type", "video/h264")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            self.connection.settimeout(MEDIA_WRITE_TIMEOUT_SECONDS)
            try:
                for chunk in subscription:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                subscription.close()

        def _muxed_stream(self, bridge: CameraBridge) -> None:
            """Serve the live stream as MPEG-TS.

            A raw Annex-B elementary stream carries no timestamps, and Home
            Assistant's stream worker rejects it with "No dts in N consecutive
            packets", so live view never produces segments. Muxing to MPEG-TS
            with wall-clock timestamps gives it what it needs, without
            re-encoding.
            """

            request_started = time.monotonic()
            _session_diagnostic(bridge, "muxer_start")
            _session_diagnostic(bridge, "active_acquire_begin", operation="muxer")
            try:
                subscription = bridge.session.acquire(reason="bridge_http_muxed")
            except P2PError:
                _session_diagnostic(
                    bridge,
                    "camera_live_failed",
                    failure_stage="active_acquire",
                    exception_class="P2PError",
                    exception_message="redacted",
                    elapsed_ms=round((time.monotonic() - request_started) * 1000, 1),
                )
                self._json(503, {"error": "stream_unavailable"})
                return
            _session_diagnostic(
                bridge,
                "active_acquire_complete",
                active_acquire_elapsed_ms=round((time.monotonic() - request_started) * 1000, 1),
            )
            muxer: subprocess.Popen[bytes] | None = None
            writer: threading.Thread | None = None
            try:
                muxer = subprocess.Popen(
                    [
                        bridge.ffmpeg,
                        "-hide_banner",
                        "-loglevel", "error",
                        "-fflags", "+genpts",
                        "-use_wallclock_as_timestamps", "1",
                        "-f", "h264",
                        "-i", "pipe:0",
                        "-c:v", "copy",
                        "-f", "mpegts",
                        "-muxdelay", "0",
                        "-muxpreload", "0",
                        "pipe:1",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except (OSError, subprocess.SubprocessError):
                subscription.close()
                self._json(503, {"error": "stream_unavailable"})
                return
            assert muxer.stdin is not None and muxer.stdout is not None
            _session_diagnostic(bridge, "muxer_started")

            def feed() -> None:
                first_input = True
                try:
                    for chunk in subscription:
                        if first_input:
                            first_input = False
                            _session_diagnostic(bridge, "muxer_first_input", bytes=len(chunk))
                        muxer.stdin.write(chunk)  # type: ignore[union-attr]
                        muxer.stdin.flush()  # type: ignore[union-attr]
                except (BrokenPipeError, ConnectionError, OSError, ValueError):
                    pass
                finally:
                    try:
                        muxer.stdin.close()  # type: ignore[union-attr]
                    except OSError:
                        pass

            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self.connection.settimeout(MEDIA_WRITE_TIMEOUT_SECONDS)
            _session_diagnostic(
                bridge,
                "http_headers_sent",
                headers_elapsed_ms=round((time.monotonic() - request_started) * 1000, 1),
            )
            self.close_connection = True
            try:
                writer = threading.Thread(target=feed, daemon=True)
                writer.start()
                output_bytes = 0
                first_output = True
                while True:
                    piece = muxer.stdout.read(STREAM_CHUNK_BYTES)
                    if not piece:
                        break
                    output_bytes += len(piece)
                    if first_output:
                        first_output = False
                        _session_diagnostic(bridge, "muxer_first_output", bytes=len(piece))
                        _session_diagnostic(bridge, "http_first_media_byte_sent", bytes=len(piece))
                    self.wfile.write(piece)
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionError, OSError):
                pass
            finally:
                subscription.close()
                if muxer.poll() is None:
                    muxer.terminate()
                    try:
                        muxer.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        muxer.kill()
                        muxer.wait(timeout=5)
                if writer is not None:
                    writer.join(timeout=2)
                _session_diagnostic(
                    bridge,
                    "muxer_exit",
                    muxer_output_bytes_total=output_bytes if "output_bytes" in locals() else 0,
                    muxer_exit_code=muxer.returncode if muxer is not None else None,
                )
                _session_diagnostic(
                    bridge,
                    "request_total_complete",
                    request_total_elapsed_ms=round((time.monotonic() - request_started) * 1000, 1),
                )

        def _request_json(self) -> dict[str, object] | None:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_REQUEST_BYTES:
                self._json(400, {"error": "invalid_request"})
                return None
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                self._json(400, {"error": "invalid_request"})
                return None
            if not isinstance(value, dict):
                self._json(400, {"error": "invalid_request"})
                return None
            return value

        def _json(self, status: int, payload: object) -> None:
            body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            elapsed_ms = round((time.monotonic() - self._request_started) * 1000, 1)
            path = urlsplit(getattr(self, "path", "")).path
            if elapsed_ms > 1000 and (path == "/ready" or path.endswith("/status")):
                print(
                    "native_diag event=slow_status_request camera_uid=- session_id=- "
                    f"session_generation=- elapsed_ms={elapsed_ms} path={path} status={status}",
                    file=sys.stderr,
                    flush=True,
                )

        def log_message(self, format: str, *args: object) -> None:
            return

    return BridgeHandler
