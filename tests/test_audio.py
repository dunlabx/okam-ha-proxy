import io
import os
import re
import threading
from types import SimpleNamespace

import pytest

from okam_native.p2p import decode_audio_header, encode_audio_frame, encode_talkback_frame
from okam_native.bridge import BridgeRegistry, CameraBridge
from okam_native.rtsp import (
    AUDIO_CLOCK,
    RTP_AUDIO_PAYLOAD_TYPE,
    RTSP_AUDIO_COMPATIBILITY_MODES,
    RTSP_BACKCHANNEL_MODES,
    _RTSPHandler,
    _sdp,
)
from okam_native.session import NativeStreamSession, SessionStatus


def test_pcma_frame_is_length_framed_and_bounded():
    frame = encode_audio_frame(b"x" * 640)
    assert frame[:4] == b"OKA1"
    assert decode_audio_header(frame[:8]) == 640


def test_talkback_frame_uses_separate_magic():
    assert encode_talkback_frame(b"x" * 640) == b"OKT1" + (640).to_bytes(4, "big") + b"x" * 640


def test_sdp_advertises_dynamic_pcma_audio_track():
    class Session:
        def parameter_sets(self):
            return b"", b""
    class Bridge:
        session = Session()
    body = _sdp(Bridge(), "127.0.0.1", 8100)
    assert b"m=audio 0 RTP/AVP 97" in body
    assert b"a=rtpmap:97 PCMA/16000/1" in body
    assert b"a=control:trackID=1" in body


def test_backchannel_sdp_is_separate_sendonly_track_and_require_gated():
    class Session:
        def parameter_sets(self):
            return b"", b""
    class Bridge:
        session = Session()

    for mode in RTSP_BACKCHANNEL_MODES:
        body = _sdp(Bridge(), "127.0.0.1", 8100, "auto_recvonly", mode, True)
        advertised = b"a=rtpmap:98 PCMA/16000/1" in body
        assert advertised is (mode != "off")
        if advertised:
            assert b"a=sendonly" in body
            assert b"a=control:audioback" in body or b"a=control:trackID=2" in body

    gated = _sdp(Bridge(), "127.0.0.1", 8100, "auto_recvonly", "onvif_require_audioback", False)
    assert b"a=rtpmap:98" not in gated


def test_backchannel_rtp_routes_only_negotiated_track_to_talkback():
    class Session:
        def __init__(self):
            self.payloads = []
        def send_talkback(self, payload):
            self.payloads.append(payload)
            return True
    class Bridge:
        session = Session()
    handler = object.__new__(_RTSPHandler)
    handler._track_channels = {1: 2, 2: 4}
    handler._bridge = Bridge()
    handler._backchannel_packets = 0
    handler._backchannel_bytes = 0
    handler._diagnostic = lambda *_args, **_kwargs: None
    payload = b"audio"
    rtp = b"\x80\x60" + b"\x00" * 10 + payload
    handler._handle_interleaved(2, rtp)
    handler._handle_interleaved(4, rtp)
    assert handler._bridge.session.payloads == [payload]


@pytest.mark.parametrize("mode", RTSP_AUDIO_COMPATIBILITY_MODES)
def test_rtsp_receive_audio_negotiation_modes_reach_audio_rtp(monkeypatch, mode):
    """Exercise DESCRIBE -> SETUP(video/audio) -> PLAY, including SDP parsing."""

    class Subscription:
        def __iter__(self):
            return iter(())

        def iter_audio(self):
            yield b"a" * 640
            yield b"b" * 640

        def close(self):
            pass

    class Session:
        def __init__(self):
            self.subscription = Subscription()

        def parameter_sets(self):
            return b"", b""

        def acquire(self, **_kwargs):
            return self.subscription

        def status(self):
            return SessionStatus(False, 0, True, None, False)

    class Request:
        def __init__(self):
            self.sent = []

        def sendall(self, payload):
            self.sent.append(payload)

    class ImmediateThread:
        def __init__(self, target, daemon=True):
            self.target = target

        def start(self):
            self.target()

        def join(self, timeout=None):
            pass

    monkeypatch.setattr("okam_native.rtsp.threading.Thread", ImmediateThread)
    session = Session()
    bridge = CameraBridge(
        camera_id="UID_NEGOTIATION",
        camera_uid="UID_NEGOTIATION",
        camera_name="Negotiation",
        api_token="token",
        session=session,
        ffmpeg="ffmpeg",
    )
    registry = BridgeRegistry()
    registry.add(bridge)
    server = SimpleNamespace(
        registry=registry,
        host="127.0.0.1",
        port=8100,
        audio_compatibility_mode=mode,
    )
    handler = object.__new__(_RTSPHandler)
    handler.server = server
    handler.request = Request()
    handler._session_id = "session"
    handler._bridge = None
    handler._subscription = None
    handler._stop_stream = threading.Event()
    handler._write_lock = threading.Lock()
    handler._stream_thread = None
    handler._audio_thread = None
    handler._rtp_channel = 0
    handler._track_channels = {}
    handler._diagnostic_started = 0.0
    handler._diagnostic_camera = None
    handler._rtsp_bytes = 0
    handler._rtsp_chunks = 0
    handler._audio_packets = 0
    handler._audio_bytes = 0
    handler._saw_standby = False
    handler._saw_live = False
    handler._media_generation = None
    handler._codec_transition_seen = False
    handler._sdp_mode_logged = False
    handler._diagnostic = lambda *_args, **_kwargs: None
    handler._stream = lambda: None
    try:
        describe = (
            b"DESCRIBE rtsp://127.0.0.1/UID_NEGOTIATION RTSP/1.0\r\n"
            b"CSeq: 1\r\n\r\n"
        )
        assert handler._handle_request(describe) is True
        sdp = handler.request.sent[-1].split(b"\r\n\r\n", 1)[1]
        video_section, audio_section = sdp.split(b"m=audio", 1)
        audio_section = b"m=audio" + audio_section
        assert b"m=video 0 RTP/AVP 96" in video_section
        assert b"a=rtpmap:96 H264/90000" in video_section
        assert b"a=control:trackID=0" in video_section
        assert b"a=rtpmap:97 PCMA/16000/1" in audio_section
        assert b"m=audio 0 RTP/AVP 97" in audio_section
        if mode == "baseline":
            assert b"a=sendrecv" in audio_section
        elif mode == "explicit_recvonly":
            assert b"a=recvonly" in audio_section
            assert b"a=sendrecv" not in audio_section
        else:
            assert b"a=sendrecv" not in audio_section
            assert b"a=recvonly" not in audio_section
        control = re.search(rb"a=control:([^\r\n]+)", audio_section).group(1)
        if mode == "compat_control":
            assert control == b"rtsp://127.0.0.1:8100/UID_NEGOTIATION/trackID=1"
            audio_target = control
        else:
            assert control == b"trackID=1"
            audio_target = b"rtsp://127.0.0.1/UID_NEGOTIATION/trackID=1"
        assert handler._handle_request(
            b"SETUP rtsp://127.0.0.1/UID_NEGOTIATION/trackID=0 RTSP/1.0\r\n"
            b"CSeq: 2\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n"
        ) is True
        assert handler._handle_request(
            b"SETUP " + audio_target + b" RTSP/1.0\r\n"
            b"CSeq: 3\r\nTransport: RTP/AVP/TCP;unicast;interleaved=2-3\r\n\r\n"
        ) is True
        assert handler._track_channels == {0: 0, 1: 2}
        assert handler._handle_request(
            b"PLAY rtsp://127.0.0.1/UID_NEGOTIATION RTSP/1.0\r\nCSeq: 4\r\n\r\n"
        ) is True
        packets = [item for item in handler.request.sent if item.startswith(b"$")]
        assert len(packets) == 2
        assert all(packet[1] == 2 for packet in packets)
        timestamps = [int.from_bytes(packet[8:12], "big") for packet in packets]
        assert (timestamps[1] - timestamps[0]) & 0xFFFFFFFF == 640
    finally:
        handler._stop_stream.set()


def test_audio_queue_is_separate_from_video_queue():
    class Process:
        stdout = io.BytesIO(b"\x00\x00\x01\x67x\x00\x00\x01\x65y")
        stderr = io.BytesIO(b"")
        returncode = 0
        pid = 1
        def poll(self): return 0
        def wait(self, timeout=None): return 0
    session = NativeStreamSession(lambda: Process())
    sub = session.acquire(passive=True)
    assert hasattr(sub, "iter_audio")
    sub.close()
    session.close()


def test_audio_rtp_payload_type_is_dynamic():
    assert RTP_AUDIO_PAYLOAD_TYPE == 97


def test_session_audio_diagnostics_are_first_and_sparse():
    lines: list[str] = []
    session = NativeStreamSession(lambda: object(), camera_uid="UID_AUDIO", logger=lines.append)
    session._process_generation = 1
    framed = b"".join(encode_audio_frame(b"x" * 640) for _ in range(250))
    session._pump_audio(io.BytesIO(framed), 1)
    diagnostics = "\n".join(lines)
    assert diagnostics.count("event=session_audio_first_frame") == 1
    assert diagnostics.count("event=session_audio_progress") == 1
    assert "password" not in diagnostics.lower()


def test_audio_pipe_write_reports_complete_frame_without_credentials():
    read_fd, write_fd = os.pipe()
    try:
        from okam_native import amd64_helper

        assert amd64_helper._write_audio(write_fd, b"x" * 640) is True
        assert os.read(read_fd, 648) == encode_audio_frame(b"x" * 640)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_helper_audio_diagnostics_are_first_and_sparse(monkeypatch):
    from okam_native import amd64_helper

    events: list[str] = []
    monkeypatch.setattr(amd64_helper, "_diag", lambda event, **_fields: events.append(event))
    counters: dict[str, int] = {}
    for _ in range(250):
        assert amd64_helper._record_native_audio(0x0C, b"x" * 640, counters)
    assert events == ["native_audio_first_frame", "native_audio_progress"]
    assert counters == {"frames": 250, "bytes": 160000}


def test_helper_audio_pipe_diagnostics_do_not_include_credentials(monkeypatch):
    from okam_native import amd64_helper

    events: list[str] = []
    monkeypatch.setattr(amd64_helper, "_diag", lambda event, **_fields: events.append(event))
    counters: dict[str, int] = {}
    amd64_helper._record_audio_pipe(True, 640, counters)
    amd64_helper._record_audio_pipe("closed", 640, counters)
    assert events == ["audio_pipe_first_write", "audio_pipe_closed"]
    assert "password" not in repr(events).lower()


def test_rtsp_audio_first_packet_diagnostic():
    class Subscription:
        def iter_audio(self):
            yield b"x" * 640

    class Request:
        def __init__(self):
            self.frames: list[bytes] = []

        def sendall(self, payload: bytes) -> None:
            self.frames.append(payload)

    handler = object.__new__(_RTSPHandler)
    handler._subscription = Subscription()
    handler._stop_stream = threading.Event()
    handler._write_lock = threading.Lock()
    handler._rtp_channel = 0
    handler._track_channels = {1: 2}
    handler._audio_packets = 0
    handler._audio_bytes = 0
    handler.request = Request()
    events: list[tuple[str, dict[str, object]]] = []
    handler._diagnostic = lambda event, **fields: events.append((event, fields))
    handler._stream_audio()
    event, fields = events[0]
    assert event == "rtsp_audio_first_packet"
    assert fields == {
        "payload_type": RTP_AUDIO_PAYLOAD_TYPE,
        "clock_rate": AUDIO_CLOCK,
        "channels": 1,
        "payload_bytes": 640,
    }
    assert handler.request.frames[0][0:2] == b"$\x02"
