"""CLI-compatible pure native helper for amd64 Home Assistant hosts."""

from __future__ import annotations

import json
import os
import signal
import struct
import sys
import time

from .cs2 import (
    LIVE_STREAM_RESPONSE_COMMANDS,
    LOGIN_RESPONSE_COMMAND,
    CameraLoginRejected,
    CS2Error,
    CS2Session,
    authenticate_camera,
    inspect_h264,
    login_candidates,
    make_cgi_request,
    read_command_result,
    read_video_frame,
    write_command,
)
from .logging import PROCESS_ID, timestamped_print


# The live-start acknowledgement is read before the first media read so it is
# recorded as evidence instead of sitting unclaimed in the channel-0 buffer.
LIVE_START_RESPONSE_SECONDS = 10.0


_running = True
_diagnostic_started = time.monotonic()


def _diag(event: str, **fields: object) -> None:
    values = {
        "event": event,
        "process_id": os.environ.get("OKAM_PROCESS_ID", PROCESS_ID),
        "camera_uid": os.environ.get("OKAM_DIAG_CAMERA_UID", "-"),
        "session_id": os.environ.get("OKAM_DIAG_SESSION_ID", "-"),
        "session_generation": os.environ.get("OKAM_DIAG_SESSION_GENERATION", "-"),
        "elapsed_ms": round((time.monotonic() - _diagnostic_started) * 1000, 1),
        **fields,
    }
    timestamped_print(
        "native_diag " + " ".join(f"{key}={value}" for key, value in values.items()),
        file=sys.stderr,
        flush=True,
    )


def _debug_credentials_enabled() -> bool:
    import os

    return os.environ.get("OKAM_DEBUG_CREDENTIALS") == "1"


def _credential_debug_line(stage: str, password: str, uid: str | None = None) -> str:
    uid_field = f"uid={uid} " if uid is not None else ""
    return (
        f"{stage} {uid_field}username_present=true password_present=true "
        f"username='admin' password={password!r} "
        f"password_length={len(password)} "
        f"password_hex={password.encode('utf-8').hex()}"
    )


def _stop(_signum: int, _frame: object) -> None:
    global _running
    _running = False


def _read_field(*, allow_empty: bool = False) -> str:
    size_bytes = sys.stdin.buffer.read(4)
    if len(size_bytes) != 4:
        raise CS2Error("native P2P input is invalid")
    size = struct.unpack(">I", size_bytes)[0]
    if size > 4096 or (size == 0 and not allow_empty):
        raise CS2Error("native P2P input is invalid")
    value = sys.stdin.buffer.read(size)
    if len(value) != size or any(byte < 0x20 for byte in value):
        raise CS2Error("native P2P input is invalid")
    try:
        return value.decode("utf-8")
    except UnicodeError:
        raise CS2Error("native P2P input is invalid") from None


def _summary(**values: object) -> dict[str, object]:
    defaults: dict[str, object] = {
        "connected": False,
        "connect_state": -1,
        "login_sent": False,
        "login_response_received": False,
        "authenticated": False,
        "login_command": 0,
        "login_result": -1,
        "login_candidate": -1,
        "login_attempts": [],
        "connect_path": "",
        "counters": {},
        "stream_start_command": 0,
        "stream_start_result": -1,
        "stream_start_sent": False,
        "stream_stop_sent": False,
        "h264_received": False,
        "h264_frames": 0,
        "h264_bytes": 0,
        "keyframe_seen": False,
        "h265_frames": 0,
        "disconnected": False,
    }
    defaults.update(values)
    return defaults


def _finish(
    session: CS2Session, result: dict[str, object], code: int
) -> tuple[int, dict[str, object]]:
    """Attach the sanitized transport counters to every exit path."""

    result["counters"] = dict(sorted(session.counters.items()))
    return code, result


def run(
    mode: str,
    uid: str,
    service: str,
    device_password: str | None,
    *,
    credential_index: int | None = None,
    substream: int = 2,
    prefer_relay: bool = True,
) -> tuple[int, dict[str, object]]:
    """Run one probe stage.

    `credential_index` forces a specific login candidate and skips the login
    probe. It exists to test whether a command that the login probe accepts is
    the same one the camera will honour for media.
    """

    global _running
    _running = True
    session = CS2Session(uid, service, prefer_relay=prefer_relay)
    result = _summary()
    accepted_user = "admin"
    accepted_password = device_password or ""
    stdout_chunks = 0
    native_video_packets = 0
    if mode != "connect":
        if _debug_credentials_enabled():
            timestamped_print(_credential_debug_line("native_login_input", accepted_password, uid), file=sys.stderr, flush=True)
        else:
            length = " password_length=0" if not accepted_password else ""
            timestamped_print(
                "native_login_input username_present=true "
                f"password_present={str(device_password is not None).lower()}" + length,
                file=sys.stderr,
                flush=True,
            )
    try:
        session.connect(timeout=55.0)
        result.update(connected=True, connect_state=3, connect_path=session.connect_path)
        _diag("native_connect_result", connect_state=3, connected=True)
        if mode == "connect":
            result["disconnected"] = session.close()
            return _finish(session, result, 0)
        assert device_password is not None
        if credential_index is not None:
            if credential_index < 0:
                raise CS2Error("credential candidate is out of range")
            result["login_candidate"] = credential_index
            result["login_sent"] = True
            write_command(
                session,
                make_cgi_request(
                    "get_status.cgi?name=admin&", accepted_user, accepted_password
                ),
            )
            answer = read_command_result(
                session, (LOGIN_RESPONSE_COMMAND,), timeout=45.0
            )
            if answer is not None:
                result.update(
                    login_response_received=True,
                    authenticated=answer[1] == 0,
                    login_command=answer[0],
                    login_result=answer[1],
                )
                _diag("native_login_result", login_result=answer[1], authenticated=answer[1] == 0)
        else:
            result["login_sent"] = True
            login = authenticate_camera(session, device_password)
            accepted_user, accepted_password = login.user, login.password
            result.update(
                login_response_received=True,
                authenticated=True,
                login_command=LOGIN_RESPONSE_COMMAND,
                login_result=login.result,
                login_candidate=login.candidate,
                login_attempts=list(login.attempts),
            )
        if mode == "authenticate":
            result["disconnected"] = session.close()
            return _finish(session, result, 0)

        if mode == "stream-stdout":
            print(
                json.dumps(
                    {
                        "okam_auth": True,
                        "connected": result["connected"],
                        "connect_state": result["connect_state"],
                        "login_sent": result["login_sent"],
                        "login_response_received": result["login_response_received"],
                        "authenticated": result["authenticated"],
                        "login_command": result["login_command"],
                        "login_result": result["login_result"],
                    },
                    separators=(",", ":"),
                ),
                file=sys.stderr,
                flush=True,
            )

        write_command(
            session,
            make_cgi_request(
                f"livestream.cgi?streamid=10&substream={substream}&",
                accepted_user,
                accepted_password,
            ),
        )
        result["stream_start_sent"] = True
        _diag("livestream_command_sent", stream_start_sent=True)
        # Media buffers normally while this bounded read waits, so claiming the
        # acknowledgement here costs no frames.
        answer = read_command_result(
            session,
            LIVE_STREAM_RESPONSE_COMMANDS,
            timeout=LIVE_START_RESPONSE_SECONDS,
        )
        if answer is not None:
            result.update(stream_start_command=answer[0], stream_start_result=answer[1])
        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)
        while _running:
            payload, frame_type = read_video_frame(session, timeout=45.0)
            native_video_packets += 1
            if native_video_packets == 1:
                _diag("first_native_video_packet", bytes=len(payload), frame_type=frame_type)
            if native_video_packets == 1 or native_video_packets % 100 == 0:
                _diag(
                    "native_video_packet_progress",
                    native_video_packet_count=native_video_packets,
                    frame_type=frame_type,
                    bytes=len(payload),
                )
            if frame_type in (0x10, 0x11):
                result["h265_frames"] = int(result["h265_frames"]) + 1
                continue
            valid, keyframe = inspect_h264(payload)
            if not valid:
                continue
            result["h264_received"] = True
            result["h264_frames"] = int(result["h264_frames"]) + 1
            result["h264_bytes"] = int(result["h264_bytes"]) + len(payload)
            result["keyframe_seen"] = bool(result["keyframe_seen"]) or keyframe
            if mode == "stream-stdout":
                try:
                    sys.stdout.buffer.write(payload)
                    sys.stdout.buffer.flush()
                    stdout_chunks += 1
                    if stdout_chunks == 1 or stdout_chunks % 100 == 0:
                        _diag(
                            "helper_stdout_progress",
                            first_helper_stdout_chunk=stdout_chunks == 1,
                            helper_stdout_bytes_total=int(result["h264_bytes"]),
                        )
                except (BrokenPipeError, OSError):
                    break
            elif (
                int(result["h264_frames"]) >= 3
                and int(result["h264_bytes"]) >= 1024
                and bool(result["keyframe_seen"])
            ):
                break
        write_command(
            session,
            make_cgi_request(
                "livestream.cgi?streamid=16&substream=0&",
                accepted_user,
                accepted_password,
            ),
        )
        result["stream_stop_sent"] = True
        result["disconnected"] = session.close()
        ok = bool(result["h264_received"]) and bool(result["stream_stop_sent"])
        return _finish(session, result, 0 if ok else 6)
    except CS2Error as error:
        if isinstance(error, CameraLoginRejected):
            # A rejection is evidence too: record which candidates answered.
            result["login_attempts"] = list(error.attempts)
            result["login_response_received"] = any(
                item is not None for item in error.attempts
            )
        if result["stream_start_sent"] and not result["stream_stop_sent"]:
            try:
                write_command(
                    session,
                    make_cgi_request(
                        "livestream.cgi?streamid=16&substream=0&",
                        accepted_user,
                        accepted_password,
                    ),
                )
                result["stream_stop_sent"] = True
            except CS2Error:
                pass
        result["disconnected"] = session.close()
        if not result["connected"]:
            return _finish(session, result, 4)
        if not result["authenticated"]:
            return _finish(session, result, 5)
        return _finish(session, result, 6)


def main() -> int:
    args = sys.argv[2:]
    if not args or len(args) not in (1, 3):
        timestamped_print(
            "usage: okam-amd64-connect ignored-library "
            "[--authenticate|--stream-test|--stream-stdout] "
            "[--credential-index N]",
            file=sys.stderr,
        )
        return 2
    option = args[0]
    modes = {
        "": "connect",
        "--authenticate": "authenticate",
        "--stream-test": "stream-test",
        "--stream-stdout": "stream-stdout",
    }
    if option not in modes or (len(args) == 3 and args[1] != "--credential-index"):
        return 2
    credential_index = None
    if len(args) == 3:
        try:
            credential_index = int(args[2])
        except ValueError:
            return 2
        if credential_index < 0:
            return 2
    try:
        uid = _read_field()
        service = _read_field()
        password = _read_field(allow_empty=True) if modes[option] != "connect" else None
        if password is not None and _debug_credentials_enabled():
            timestamped_print(_credential_debug_line("ipc_read", password, uid), file=sys.stderr, flush=True)
        code, result = run(
            modes[option], uid, service, password, credential_index=credential_index
        )
    except CS2Error:
        return 3
    output = json.dumps(result, separators=(",", ":"), sort_keys=True)
    if modes[option] == "stream-stdout":
        print(output, file=sys.stderr, flush=True)
    else:
        print(output, flush=True)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
