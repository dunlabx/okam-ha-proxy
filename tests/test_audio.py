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
    BACKCHANNEL_AUDIO_CLOCK,
    RTP_AUDIO_PAYLOAD_TYPE,
    RTSP_AUDIO_COMPATIBILITY_MODES,
    RTSP_BACKCHANNEL_MODES,
    _PCMA8To16,
    _RTSPHandler,
    _pcma_decode,
    _pcma_encode,
    _rtp_payload,
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
        advertised = b"a=rtpmap:98 PCMA/8000/1" in body
        assert advertised is (mode != "off")
        if advertised:
            backchannel = body.split(b"m=audio", 2)[2]
            assert b"PCMA/16000/1" not in backchannel
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
    payload = bytes([_pcma_encode(1200)]) * 160
    rtp = b"\x80\x60" + b"\x00" * 10 + payload
    handler._handle_interleaved(2, rtp)
    handler._handle_interleaved(4, rtp)
    assert len(handler._bridge.session.payloads) == 1
    assert len(handler._bridge.session.payloads[0]) == 320
    assert handler._bridge.session.payloads[0] != payload


def test_pcma_8k_to_16k_preserves_20ms_duration_and_signal():
    converter = _PCMA8To16()
    source = bytes([_pcma_encode(8_000 if index % 8 < 4 else -8_000) for index in range(160)])
    converted = converter.convert(source)
    assert len(converted) == 320
    decoded = [_pcma_decode(value) for value in converted]
    assert max(decoded) > 4_000
    assert min(decoded) < -4_000


def test_pcma_8k_to_16k_preserves_40ms_duration():
    converter = _PCMA8To16()
    source = bytes([_pcma_encode(index * 80 - 12_000) for index in range(320)])
    converted = converter.convert(source)
    assert len(converted) == 640
    assert len([_pcma_decode(value) for value in converted]) == 640


def test_pcma_converter_keeps_state_across_rtp_packets():
    converter = _PCMA8To16()
    first = bytes([_pcma_encode(1_000 + index * 10) for index in range(160)])
    second = bytes([_pcma_encode(8_000 + index * 10) for index in range(160)])
    first_out = converter.convert(first)
    second_out = converter.convert(second)
    assert len(first_out) == len(second_out) == 320
    boundary = [_pcma_decode(value) for value in second_out[:2]]
    assert 2_000 < boundary[0] < 8_000
    assert boundary[1] > boundary[0]


def test_go2rtc_backchannel_codec_match_requires_clock_rate():
    def matches(
        producer: tuple[str, str, str, int], consumer: tuple[str, str, str, int]
    ) -> bool:
        producer_kind, producer_direction, producer_name, producer_clock = producer
        consumer_kind, consumer_direction, consumer_name, consumer_clock = consumer
        return (
            producer_kind == consumer_kind
            and producer_direction == "sendonly"
            and consumer_direction == "recvonly"
            and producer_name == consumer_name
            and (producer_clock == consumer_clock or consumer_clock == 0)
        )

    browser_pcma = ("audio", "recvonly", "PCMA", 8_000)
    assert matches(("audio", "sendonly", "PCMA", BACKCHANNEL_AUDIO_CLOCK), browser_pcma)
    assert not matches(("audio", "sendonly", "PCMA", 16_000), browser_pcma)
    assert not matches(("audio", "recvonly", "PCMA", 8_000), browser_pcma)
    assert not matches(("audio", "sendonly", "OPUS", 8_000), browser_pcma)


def test_rtp_payload_parser_rejects_malformed_packets():
    assert _rtp_payload(b"\x80" * 11)[0] is None
    assert _rtp_payload(b"\x40" + b"\x00" * 11 + b"x")[0] is None
    assert _rtp_payload(b"\x80" + b"\x00" * 11)[0] is None
    assert _rtp_payload(b"\x80" + b"\x00" * 11 + b"\x00")[0] == b"\x00"


def test_backchannel_rtp_drops_wrong_channel_and_malformed_packets():
    class Session:
        def __init__(self):
            self.payloads = []

        def send_talkback(self, payload):
            self.payloads.append(payload)
            return True

    class Bridge:
        session = Session()

    events = []
    handler = object.__new__(_RTSPHandler)
    handler._track_channels = {2: 4}
    handler._bridge = Bridge()
    handler._diagnostic = lambda event, **fields: events.append((event, fields))
    handler._handle_interleaved(2, b"\x80" + b"\x00" * 11 + b"x")
    handler._handle_interleaved(4, b"\x40" + b"\x00" * 11 + b"x")
    assert handler._bridge.session.payloads == []
    assert events and events[0][0] == "rtsp_backchannel_drop"


def test_backchannel_rtp_routes_converted_audio_to_native_boundary():
    class Session:
        def __init__(self):
            self.payloads = []

        def send_talkback(self, payload):
            self.payloads.append(payload)
            return True

    class Bridge:
        session = Session()

    handler = object.__new__(_RTSPHandler)
    handler._track_channels = {2: 4}
    handler._bridge = Bridge()
    handler._backchannel_packets = 0
    handler._backchannel_bytes = 0
    handler._diagnostic = lambda *_args, **_kwargs: None
    payload = bytes([_pcma_encode(4_000)]) * 160
    packet = b"\x80\x60" + b"\x00" * 10 + payload
    handler._handle_interleaved(4, packet)
    assert len(handler._bridge.session.payloads) == 1
    assert len(handler._bridge.session.payloads[0]) == 320


def test_rtsp_backchannel_negotiation_keeps_reverse_track_separate():
    class Subscription:
        def __iter__(self):
            return iter(())

        def iter_audio(self):
            return iter(())

        def close(self):
            pass

    class Session:
        def parameter_sets(self):
            return b"", b""

        def acquire(self, **_kwargs):
            return Subscription()

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

    class Bridge:
        camera_uid = "UID_TALK"
        camera_id = "UID_TALK"
        battery_camera = False
        session = Session()

    class Registry:
        def get(self, identifier):
            return Bridge() if identifier == "UID_TALK" else None

    server = type("Server", (), {
        "registry": Registry(),
        "host": "127.0.0.1",
        "port": 8100,
        "audio_compatibility_mode": "auto_recvonly",
        "backchannel_mode": "onvif_require_audioback",
    })()
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
    handler._media_generation = None
    handler._sdp_mode_logged = False
    handler._diagnostic = lambda *_args, **_kwargs: None
    handler._stream = lambda: None
    handler._stream_audio = lambda: None

    old_thread = threading.Thread
    threading.Thread = ImmediateThread
    try:
        assert handler._handle_request(
            b"DESCRIBE rtsp://127.0.0.1/UID_TALK RTSP/1.0\r\n"
            b"CSeq: 1\r\nRequire: www.onvif.org/ver20/backchannel\r\n\r\n"
        )
        body = handler.request.sent[-1].split(b"\r\n\r\n", 1)[1]
        assert b"a=rtpmap:97 PCMA/16000/1" in body
        assert b"a=rtpmap:98 PCMA/8000/1" in body
        assert b"a=control:audioback" in body
        for cseq, target, channels in (
            (2, b"trackID=0", b"0-1"),
            (3, b"trackID=1", b"2-3"),
            (4, b"audioback", b"4-5"),
        ):
            assert handler._handle_request(
                b"SETUP rtsp://127.0.0.1/UID_TALK/" + target + b" RTSP/1.0\r\n"
                + b"CSeq: " + str(cseq).encode() + b"\r\n"
                + b"Transport: RTP/AVP/TCP;unicast;interleaved=" + channels + b"\r\n\r\n"
            )
        assert handler._track_channels == {0: 0, 1: 2, 2: 4}
        assert handler._handle_request(
            b"PLAY rtsp://127.0.0.1/UID_TALK RTSP/1.0\r\nCSeq: 5\r\n\r\n"
        )
    finally:
        threading.Thread = old_thread
        handler._stop_stream.set()


def test_receive_audio_and_backchannel_use_independent_paths():
    class Subscription:
        def iter_audio(self):
            yield b"r" * 320

    class Session:
        def __init__(self):
            self.talkback = []

        def send_talkback(self, payload):
            self.talkback.append(payload)
            return True

    class Request:
        def __init__(self):
            self.frames = []

        def sendall(self, payload):
            self.frames.append(payload)

    class Bridge:
        session = Session()

    handler = object.__new__(_RTSPHandler)
    handler._subscription = Subscription()
    handler._bridge = Bridge()
    handler.request = Request()
    handler._stop_stream = threading.Event()
    handler._write_lock = threading.Lock()
    handler._track_channels = {1: 2, 2: 4}
    handler._rtp_channel = 0
    handler._audio_packets = 0
    handler._audio_bytes = 0
    handler._backchannel_packets = 0
    handler._backchannel_bytes = 0
    handler._diagnostic = lambda *_args, **_kwargs: None
    handler._stream_audio()
    handler._handle_interleaved(4, b"\x80\x60" + b"\x00" * 10 + bytes([_pcma_encode(2_000)]) * 160)
    assert handler.request.frames[0][1] == 2
    assert len(handler._bridge.session.talkback) == 1
    assert len(handler._bridge.session.talkback[0]) == 320


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
