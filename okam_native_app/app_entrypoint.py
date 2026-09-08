#!/usr/bin/env python3
"""Run the native O-KAM bridge for the current Home Assistant architecture."""

from __future__ import annotations

import asyncio
import json
import os
import platform
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from okam_native.account import (
    AccountDevice,
    AccountError,
    CameraSelection,
    Eye4AccountClient,
    configured_camera_selections,
)
from okam_native.auth import (
    AuthenticationRejected,
    AuthenticationTransportError,
    CameraAuthenticator,
    CredentialSourceCache,
    build_candidates,
)
from okam_native.bridge import (
    BridgeRegistry,
    CameraBridge,
    QuietThreadingHTTPServer,
    make_handler,
)
from okam_native.p2p import (
    P2PError,
    diagnostic_line,
    get_service_parameter,
    open_authenticated_stream_process,
    resolve_client_id,
    run_authentication_probe,
    run_connect_probe,
    run_snapshot_probe,
    run_stream_probe,
)
from okam_native.session import NativeStreamSession
from okam_native.wakeup import WakeError, load_wake_credentials, wake_camera
from okam_native.rtsp import RTSPServer


DATA = Path("/data")
VENDOR = DATA / "vendor"
RUNTIME_ARCH = os.environ.get("OKAM_RUNTIME_ARCH", platform.machine()).lower()
if RUNTIME_ARCH in {"x86_64", "x64"}:
    RUNTIME_ARCH = "amd64"
elif RUNTIME_ARCH in {"arm64", "armv8"}:
    RUNTIME_ARCH = "aarch64"
PROBE = Path("/opt/okam/okam-hybris-probe")
CONNECT_HELPER = Path(
    "/opt/okam/okam-amd64-connect"
    if RUNTIME_ARCH == "amd64"
    else "/opt/okam/okam-hybris-connect"
)
FFMPEG = Path("/usr/bin/ffmpeg")
STANDBY_FRAME = Path("/opt/okam/standby.h264")
LIBRARY = Path("/dev/null") if RUNTIME_ARCH == "amd64" else VENDOR / "libOKSMARTPPCS.so"
STATUS: dict[str, object] = {
    "service": "okam-native-bridge",
    "loader_ready": False,
    "account_ready": False,
    "p2p_ready": False,
    "camera_ready": False,
    "connect_test_enabled": False,
    "auth_test_enabled": False,
    "camera_authenticated": False,
    "stream_test_enabled": False,
    "h264_ready": False,
    "snapshot_test_enabled": False,
    "snapshot_ready": False,
    "configuration_required": True,
    "phase": "starting",
}
LOCK = threading.Lock()
BRIDGES = BridgeRegistry()
ACCOUNT_DEVICES: tuple[AccountDevice, ...] = ()
AUTHENTICATOR = CameraAuthenticator(
    CredentialSourceCache(DATA / "camera_auth_cache.json")
)


def log_build_fingerprint() -> None:
    """Emit the immutable build/runtime identity before serving requests."""

    print(
        "build_fingerprint "
        f"build_version={os.environ.get('OKAM_BUILD_VERSION', 'unknown')} "
        f"build_commit={os.environ.get('OKAM_BUILD_COMMIT', 'unknown')} "
        f"architecture={RUNTIME_ARCH}",
        flush=True,
    )
    print(
        "runtime_module "
        f"bridge={__import__('okam_native.bridge', fromlist=['__file__']).__file__} "
        f"session={__import__('okam_native.session', fromlist=['__file__']).__file__} "
        f"auth={__import__('okam_native.auth', fromlist=['__file__']).__file__} "
        f"rtsp={__import__('okam_native.rtsp', fromlist=['__file__']).__file__} "
        f"p2p={__import__('okam_native.p2p', fromlist=['__file__']).__file__}",
        flush=True,
    )


def set_status(**values: object) -> None:
    with LOCK:
        STATUS.update(values)


def get_status() -> dict[str, object]:
    with LOCK:
        payload = dict(STATUS)
    aggregate = BRIDGES.status()
    # /ready is unauthenticated and must remain a health signal, not a camera
    # inventory endpoint. Detailed per-camera state is available through the
    # token-protected /api/cameras/<id>/status route.
    aggregate.pop("cameras", None)
    payload.update(aggregate)
    payload["camera_ready"] = aggregate["ready_camera_count"] > 0
    payload["p2p_ready"] = aggregate["ready_camera_count"] > 0
    payload["phase"] = (
        "streaming"
        if aggregate["streaming_camera_count"]
        else "bridge_ready"
        if aggregate["ready_camera_count"]
        else str(payload.get("phase", "starting"))
    )
    return payload


def get_bridge() -> BridgeRegistry:
    return BRIDGES


def load_vendor_runtime() -> None:
    set_status(phase="fetching_vendor_sdk")
    VENDOR.mkdir(parents=True, exist_ok=True)
    required = [VENDOR / "device_wakeup_server.dart"]
    if RUNTIME_ARCH == "aarch64":
        required.extend((LIBRARY, VENDOR / "libvp_log.so"))
    if any(not path.exists() for path in required):
        command = [
            sys.executable,
            "/opt/okam/tools/fetch_official_sdk.py",
            "--destination",
            str(VENDOR),
        ]
        if RUNTIME_ARCH == "amd64":
            command.append("--wake-only")
        subprocess.run(
            command,
            check=True,
            timeout=180,
        )

    if RUNTIME_ARCH == "amd64":
        if not CONNECT_HELPER.is_file():
            raise RuntimeError("native amd64 P2P helper is unavailable")
        set_status(loader_ready=True, phase="native_loader_ready", runtime_arch=RUNTIME_ARCH)
        print("native_loader_ready=true", flush=True)
        return
    if RUNTIME_ARCH != "aarch64":
        raise RuntimeError("Home Assistant architecture is unsupported")

    environment = os.environ.copy()
    environment.update(
        {
            "LD_LIBRARY_PATH": "/opt/hybris/lib",
            "HYBRIS_LINKER_DIR": "/opt/hybris/lib/libhybris/linker",
            "HYBRIS_ANDROID_SDK_VERSION": "28",
            "HYBRIS_LD_LIBRARY_PATH": "/opt/android-stubs:/opt/bionic:/data/vendor",
        }
    )
    set_status(phase="loading_arm64_sdk")
    completed = subprocess.run(
        [str(PROBE), str(LIBRARY)],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    result = json.loads(completed.stdout)
    if result.get("hybris_load") is not True or not all(result["symbols"].values()):
        raise RuntimeError("native loader did not satisfy every required symbol")
    set_status(loader_ready=True, phase="native_loader_ready", runtime_arch=RUNTIME_ARCH)
    print("native_loader_ready=true", flush=True)


def load_options() -> dict[str, object]:
    path = DATA / "options.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise RuntimeError("Home Assistant app options are invalid") from None
    if not isinstance(value, dict):
        raise RuntimeError("Home Assistant app options are invalid")
    return value


def enumerate_account() -> list[CameraSelection] | None:
    global ACCOUNT_DEVICES
    options = load_options()
    username = options.get("account_username")
    password = options.get("account_password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        set_status(configuration_required=True)
        return None
    set_status(phase="enumerating_account", configuration_required=False)
    debug_credentials = options.get("debug_credentials") is True
    client = (
        Eye4AccountClient(debug_credentials=True)
        if debug_credentials
        else Eye4AccountClient()
    )
    try:
        devices = client.enumerate(username, password)
    finally:
        username = ""
        password = ""
    ACCOUNT_DEVICES = tuple(devices)
    selected = configured_camera_selections(devices, options)
    set_status(
        account_ready=True,
        raw_device_count=client.last_raw_device_count,
        parsed_device_count=len(devices),
        selected_device_count=len(selected),
        device_count=len(selected),
        phase="account_enumerated",
    )
    print(
        "account_enumerated=true "
        f"raw_count={client.last_raw_device_count} "
        f"parsed_count={len(devices)} selected_count={len(selected)}",
        flush=True,
    )
    for item in devices:
        print(
            f"account_device_parsed uid={item.uid} nickname={item.name} "
            f"password_present={str(item.password_present).lower()} "
            f"password_nonempty={str(bool(item.device_password)).lower()} "
            f"password_length={len(item.device_password)}",
            flush=True,
        )
        if debug_credentials:
            print(
                f"api_device_parsed uid={item.uid} "
                f"password={item.device_password!r} "
                "password_type=str "
                f"password_length={len(item.device_password)}",
                flush=True,
            )
    for item in selected:
        print(
            f"camera_registered uid={item.device.uid} "
            f"alias={item.alias or item.device.name} "
            f"auth_method={item.auth_method}",
            flush=True,
        )
    return selected


def p2p_environment(debug_credentials: bool = False) -> dict[str, str]:
    environment = os.environ.copy()
    if debug_credentials:
        environment["OKAM_DEBUG_CREDENTIALS"] = "1"
    else:
        environment.pop("OKAM_DEBUG_CREDENTIALS", None)
    if RUNTIME_ARCH == "amd64":
        return environment
    environment.update(
        {
            "LD_LIBRARY_PATH": "/opt/hybris/lib",
            "HYBRIS_LINKER_DIR": "/opt/hybris/lib/libhybris/linker",
            "HYBRIS_ANDROID_SDK_VERSION": "28",
            "HYBRIS_LD_LIBRARY_PATH": "/opt/android-stubs:/opt/bionic:/data/vendor",
        }
    )
    return environment


def _terminate_stream_process(process: subprocess.Popen[bytes]) -> None:
    """Close a rejected candidate's native session before trying the next."""

    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def _camera_password_override(
    selection: CameraSelection, options: dict[str, object]
) -> tuple[object, bool]:
    """Return the per-camera override and whether it is strict.

    Camera credentials are only read from the selected ``cameras`` entry.
    Stale top-level ``camera_password`` values from older options are ignored.
    """

    password = getattr(selection, "password", None)
    if getattr(selection, "auth_method", "automatic") == "password" or (
        getattr(selection, "auth_mode", "automatic") == "automatic" and password is not None
    ):
        if password is None:
            raise AccountError("password auth_method requires a password")
        return password, True
    return None, False


def run_p2p_acceptance(selection: CameraSelection) -> None:
    device = selection.device
    options = load_options()
    enabled = options.get("run_connect_test") is True
    auth_enabled = options.get("run_auth_test") is True
    stream_enabled = options.get("run_stream_test") is True
    snapshot_enabled = options.get("run_snapshot_test") is True
    stream_enabled = stream_enabled or snapshot_enabled
    auth_enabled = auth_enabled or stream_enabled
    set_status(
        connect_test_enabled=enabled or auth_enabled,
        auth_test_enabled=auth_enabled,
        stream_test_enabled=stream_enabled,
        snapshot_test_enabled=snapshot_enabled,
    )
    if not enabled and not auth_enabled and not stream_enabled:
        return
    configured_password, strict_configured = _camera_password_override(selection, options)
    debug_credentials = options.get("debug_credentials") is True
    candidates = (
        build_candidates(
            device,
            configured_password,
            account_device_uid=device.uid,
            account_devices=ACCOUNT_DEVICES,
            strict_configured=strict_configured,
            debug_credentials=debug_credentials,
        )
        if auth_enabled
        else ()
    )
    credentials = load_wake_credentials(VENDOR / "device_wakeup_server.dart")
    if credentials is None:
        raise WakeError("official wake configuration was unavailable")
    client_id = resolve_client_id(device.uid)
    service_parameter = get_service_parameter(client_id)
    wake_requested = False
    responsive_servers = 0
    last_state = -1
    for attempt in range(1, 4):
        set_status(phase="waking_camera", connect_attempt=attempt)
        try:
            wake = asyncio.run(wake_camera(device.uid, credentials, timeout=12.0))
            wake_requested = wake_requested or wake.requested
            responsive_servers = max(responsive_servers, wake.responsive_servers)
        except WakeError:
            pass
        set_status(
            wake_requested=wake_requested,
            wake_responsive_servers=responsive_servers,
            phase="connecting_p2p",
        )
        try:
            if auth_enabled:
                def attempt_candidate(candidate):
                    if snapshot_enabled:
                        return run_snapshot_probe(
                            str(CONNECT_HELPER), str(LIBRARY), str(FFMPEG),
                            client_id, service_parameter, candidate.password,
                            environment=p2p_environment(debug_credentials), credential_index=0,
                        )
                    if stream_enabled:
                        return run_stream_probe(
                            str(CONNECT_HELPER), str(LIBRARY), client_id,
                            service_parameter, candidate.password,
                            environment=p2p_environment(debug_credentials), credential_index=0,
                        )
                    return run_authentication_probe(
                        str(CONNECT_HELPER), str(LIBRARY), client_id,
                        service_parameter, candidate.password,
                        environment=p2p_environment(debug_credentials), credential_index=0,
                    )

                _selected, result = AUTHENTICATOR.authenticate(
                    device.uid, candidates, attempt_candidate,
                    debug_credentials=debug_credentials,
                )
            else:
                result = run_connect_probe(
                    str(CONNECT_HELPER),
                    str(LIBRARY),
                    client_id,
                    service_parameter,
                    environment=p2p_environment(),
                )
        except AuthenticationRejected:
            set_status(phase="camera_authentication_rejected", connect_attempt=attempt)
            raise
        except AuthenticationTransportError:
            if attempt < 3:
                time.sleep(5)
                continue
            raise
        except P2PError:
            if attempt < 3:
                time.sleep(5)
                continue
            raise
        last_state = result.connect_state
        if auth_enabled:
            set_status(
                connect_state=result.connect_state,
                login_sent=result.login_sent,
                login_response_received=result.login_response_received,
                login_result=result.login_result,
                clean_disconnect=result.disconnected,
            )
            if result.authenticated:
                set_status(p2p_ready=True, camera_authenticated=True)
            if not (result.connected and result.authenticated and result.disconnected):
                print(
                    "camera_authentication=false "
                    f"connect_state={result.connect_state} "
                    f"login_sent={str(result.login_sent).lower()} "
                    f"login_response_received={str(result.login_response_received).lower()} "
                    f"login_command={result.login_command} "
                    f"login_result={result.login_result} "
                    f"clean_disconnect={str(result.disconnected).lower()}",
                    flush=True,
                )
                print(diagnostic_line(result), flush=True)
        if stream_enabled:
            set_status(
                stream_start_sent=result.stream_start_sent,
                stream_stop_sent=result.stream_stop_sent,
                h264_frames=result.h264_frames,
                h264_bytes=result.h264_bytes,
                keyframe_seen=result.keyframe_seen,
                h265_frames=result.h265_frames,
            )
            if snapshot_enabled and result.h264_received and result.disconnected:
                set_status(
                    h264_ready=True,
                    snapshot_ready=True,
                    snapshot_bytes=len(result.jpeg),
                    snapshot_width=result.width,
                    snapshot_height=result.height,
                    phase="snapshot_created",
                    connect_attempt=attempt,
                )
                print(
                    f"snapshot_created=true width={result.width} "
                    f"height={result.height} bytes={len(result.jpeg)} "
                    "clean_disconnect=true",
                    flush=True,
                )
                return
            if not snapshot_enabled and result.h264_received and result.disconnected:
                set_status(
                    h264_ready=True,
                    phase="h264_received",
                    connect_attempt=attempt,
                )
                print(
                    f"h264_received=true frames={result.h264_frames} "
                    f"bytes={result.h264_bytes} clean_disconnect=true",
                    flush=True,
                )
                return
            print(
                "h264_received=false "
                f"stream_start_sent={str(result.stream_start_sent).lower()} "
                f"stream_stop_sent={str(result.stream_stop_sent).lower()} "
                f"frames={result.h264_frames} bytes={result.h264_bytes} "
                f"keyframe_seen={str(result.keyframe_seen).lower()} "
                f"h265_frames={result.h265_frames} "
                f"clean_disconnect={str(result.disconnected).lower()}",
                flush=True,
            )
            print(diagnostic_line(result), flush=True)
        if not stream_enabled and auth_enabled and result.connected and result.authenticated and result.disconnected:
            set_status(
                p2p_ready=True,
                camera_authenticated=True,
                phase="camera_authenticated",
                connect_attempt=attempt,
                login_command=result.login_command,
                login_result=result.login_result,
            )
            print("camera_authenticated=true clean_disconnect=true", flush=True)
            return
        if not auth_enabled and result.connected and result.disconnected:
            set_status(p2p_ready=True, phase="p2p_connected", connect_attempt=attempt)
            print("p2p_connected=true clean_disconnect=true", flush=True)
            return
        if attempt < 3:
            time.sleep(5)
    set_status(connect_state=last_state)
    raise P2PError("camera did not establish a native P2P session")


def configure_bridge(
    selection: CameraSelection, *, selected_count: int,
    account_devices: tuple[AccountDevice, ...] | None = None,
) -> CameraBridge | None:
    """Prepare the long-lived, on-demand runtime without waking the camera."""

    device = selection.device
    options = load_options()
    api_token = options.get("api_token")
    alias = selection.alias
    idle_timeout = options.get("idle_timeout_seconds", 120)
    configured_password, strict_configured = _camera_password_override(selection, options)
    debug_credentials = options.get("debug_credentials") is True
    candidates = build_candidates(
        device,
        configured_password,
        account_device_uid=device.uid,
        account_devices=account_devices if account_devices is not None else ACCOUNT_DEVICES,
        strict_configured=strict_configured,
        debug_credentials=debug_credentials,
    )
    if not isinstance(api_token, str) or not 16 <= len(api_token) <= 1024:
        set_status(configuration_required=True, camera_ready=False, phase="api_token_required")
        print("bridge_ready=false configuration_required=api_token", flush=True)
        return None
    if not isinstance(idle_timeout, int) or not 10 <= idle_timeout <= 600:
        raise RuntimeError("idle timeout is invalid")
    credentials = load_wake_credentials(VENDOR / "device_wakeup_server.dart")
    if credentials is None:
        raise WakeError("official wake configuration was unavailable")
    client_id = resolve_client_id(device.uid)
    service_parameter = get_service_parameter(client_id)
    session_ref: list[NativeStreamSession | None] = [None]

    def session_diag(event: str, **fields: object) -> None:
        session = session_ref[0]
        if session is not None:
            session.diagnostic(event, **fields)

    def start_stream() -> subprocess.Popen[bytes]:
        started = time.monotonic()
        session_diag("native_start_begin", reason="active_consumer")
        session_diag("wake_begin")
        set_status(phase="waking_camera_on_demand")
        try:
            wake = asyncio.run(wake_camera(device.uid, credentials, timeout=12.0))
            session_diag(
                "wake_result",
                requested=wake.requested,
                responsive_servers=wake.responsive_servers,
                wake_elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            )
            set_status(
                wake_requested=wake.requested,
                wake_responsive_servers=wake.responsive_servers,
                phase="starting_native_stream",
            )
        except WakeError as error:
            session_diag(
                "wake_result",
                result="failed",
                exception_class=type(error).__name__,
                wake_elapsed_ms=round((time.monotonic() - started) * 1000, 1),
            )
            set_status(phase="starting_native_stream")
        auth_started = time.monotonic()
        session_diag("auth_begin")
        try:
            def open_candidate(candidate):
                session_diag("native_process_spawn_begin", candidate=candidate.source)
                environment = p2p_environment(debug_credentials)
                runtime_session = session_ref[0]
                if runtime_session is not None:
                    environment.update(
                        {
                            "OKAM_DIAG_CAMERA_UID": device.uid,
                            "OKAM_DIAG_SESSION_ID": runtime_session._session_id,
                            "OKAM_DIAG_SESSION_GENERATION": str(runtime_session._session_generation),
                        }
                    )
                result, process = open_authenticated_stream_process(
                    str(CONNECT_HELPER),
                    str(LIBRARY),
                    client_id,
                    service_parameter,
                    candidate.password,
                    environment=environment,
                    credential_index=0,
                )
                session_diag(
                    "native_process_spawned",
                    process_pid=getattr(process, "pid", None) or "-",
                )
                session_diag(
                    "native_connect_result",
                    connect_state=result.connect_state,
                    connected=result.connected,
                )
                session_diag(
                    "native_login_result",
                    login_response_received=result.login_response_received,
                    login_result=result.login_result,
                    authenticated=result.authenticated,
                )
                return result, process

            _selected, _auth_result, process = AUTHENTICATOR.authenticate_resource(
                device.uid,
                candidates,
                open_candidate,
                discard=lambda failed: _terminate_stream_process(failed),
                debug_credentials=debug_credentials,
            )
        except Exception as error:
            session_diag(
                "auth_result",
                result="failed",
                exception_class=type(error).__name__,
                auth_elapsed_ms=round((time.monotonic() - auth_started) * 1000, 1),
            )
            session_diag(
                "camera_live_failed",
                failure_stage="authentication",
                exception_class=type(error).__name__,
                exception_message="redacted",
                auth_elapsed_ms=round((time.monotonic() - auth_started) * 1000, 1),
            )
            raise
        session_diag(
            "auth_result",
            result="success",
            auth_elapsed_ms=round((time.monotonic() - auth_started) * 1000, 1),
        )
        session_diag("livestream_command_sent")
        set_status(
            camera_authenticated=True,
            camera_authentication=True,
            login_result=_auth_result.login_result,
            phase="native_stream_started",
        )
        return process

    try:
        standby_frame = STANDBY_FRAME.read_bytes()
    except (OSError, ValueError):
        standby_frame = None
    session = NativeStreamSession(
        start_stream,
        idle_timeout=float(idle_timeout),
        standby_frame=standby_frame,
        camera_uid=device.uid,
        transport_uid=client_id,
        logger=print,
    )
    session_ref[0] = session
    camera_id = alias or device.uid
    bridge = CameraBridge(
        camera_id=camera_id,
        camera_uid=device.uid,
        camera_name=device.name,
        api_token=api_token,
        session=session,
        ffmpeg=str(FFMPEG),
    )
    BRIDGES.add(bridge)
    set_status(
        camera_ready=True,
        configuration_required=False,
        phase="bridge_ready",
        idle_timeout_seconds=idle_timeout,
    )
    print("bridge_ready=true", flush=True)
    return bridge


def initialize_camera_runtimes(
    selections: list[CameraSelection],
    account_devices: tuple[AccountDevice, ...] | None = None,
) -> int:
    """Run the production per-camera setup, isolating failures by UID."""

    registered = 0
    for selection in selections:
        try:
            # Startup only registers lazy runtimes. Explicit diagnostic probes
            # remain available through run_p2p_acceptance(), but are never
            # promoted into mandatory camera login by persisted options.
            if configure_bridge(
                selection,
                selected_count=len(selections),
                account_devices=account_devices,
            ) is not None:
                registered += 1
        except Exception as error:
            print(
                f"camera_runtime_failed uid={selection.device.uid} "
                f"error={type(error).__name__}",
                flush=True,
            )
    return registered


def main() -> int:
    options = load_options()
    log_build_fingerprint()
    debug_credentials = options.get("debug_credentials") is True
    print(f"credential_debug_enabled={str(debug_credentials).lower()}", flush=True)
    if debug_credentials:
        print("WARNING credential_debug_enabled=true", flush=True)
        print("WARNING camera/device passwords from the vendor API are being printed in plaintext", flush=True)
        print("WARNING disable debug_credentials after diagnosis", flush=True)
    api_port = options.get("api_port", 8099)
    rtsp_port = options.get("rtsp_port", 8100)
    if (
        type(api_port) is not int
        or not 1 <= api_port <= 65535
        or type(rtsp_port) is not int
        or not 1 <= rtsp_port <= 65535
        or api_port == rtsp_port
    ):
        raise RuntimeError("api_port and rtsp_port must be valid and different")
    server = QuietThreadingHTTPServer(
        ("0.0.0.0", api_port), make_handler(get_status, get_bridge)
    )
    rtsp_server = RTSPServer(("0.0.0.0", rtsp_port), BRIDGES)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=rtsp_server.serve_forever, daemon=True).start()
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        load_vendor_runtime()
        selections = enumerate_account()
        if selections is not None:
            initialize_camera_runtimes(selections, ACCOUNT_DEVICES)
            if not BRIDGES.values():
                raise RuntimeError("no selected camera runtime is available")
            print("startup_ready=true", flush=True)
    except Exception as error:
        phase = "startup_error" if STATUS["loader_ready"] else "native_loader_error"
        detail = (
            str(error).replace(" ", "_")
            if isinstance(error, (P2PError, AccountError))
            else None
        )
        set_status(phase=phase, error=type(error).__name__, error_detail=detail)
        suffix = f" detail={detail}" if detail else ""
        print(f"startup_ready=false error={type(error).__name__}{suffix}", flush=True)
    stop.wait()
    BRIDGES.close()
    rtsp_server.shutdown()
    rtsp_server.server_close()
    server.shutdown()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
