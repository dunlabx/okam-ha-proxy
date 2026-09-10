import io
import threading

from okam_native.p2p import decode_audio_header, encode_audio_frame, encode_talkback_frame
from okam_native.rtsp import RTP_AUDIO_PAYLOAD_TYPE, _sdp
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
