import io
import os
import threading

from okam_native.p2p import decode_audio_header, encode_audio_frame, encode_talkback_frame
from okam_native.rtsp import AUDIO_CLOCK, RTP_AUDIO_PAYLOAD_TYPE, _RTSPHandler, _sdp
from okam_native.session import NativeStreamSession


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
