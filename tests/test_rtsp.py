import socket
import threading
import time
import pytest

from okam_native.bridge import BridgeRegistry, CameraBridge
from okam_native.rtsp import MAX_FRAME_BYTES, RTSPServer, _AccessUnitAssembler, _RTSPHandler, _frame_ticks_from_sps, _nal_units, _rtp_packets, _sdp
from okam_native.session import SessionStatus


class FakeSession:
    idle_timeout = 120.0

    def status(self) -> SessionStatus:
        return SessionStatus(False, 0, True, None, False)

    def parameter_sets(self):
        return (b"\x00\x00\x01\x67\x42\x00\x1f", b"\x00\x00\x01\x68\xce")


def test_annex_b_parser_keeps_incomplete_tail() -> None:
    carry = bytearray()
    assert _nal_units(b"\x00\x00\x01\x67sps\x00\x00", carry) == []
    assert _nal_units(b"\x01\x68pps\x00\x00\x01\x65idr", carry) == [b"\x67sps", b"\x68pps"]


def test_annex_b_incomplete_carry_is_bounded():
    carry = bytearray()
    assert _nal_units(b"x" * (MAX_FRAME_BYTES + 1024), carry) == []
    assert len(carry) <= 4


def test_annex_b_accepts_a_valid_nal_just_below_the_safety_bound():
    carry = bytearray()
    payload = b"x" * (MAX_FRAME_BYTES - 32)
    result = _nal_units(
        b"\x00\x00\x01\x65" + payload + b"\x00\x00\x01\x41x", carry
    )
    assert result and result[0] == b"\x65" + payload


def test_h264_rtp_packetization_sets_marker_and_fragments_large_nal() -> None:
    packets = _rtp_packets(b"\x65" + b"x" * 3000, 100, 1234, 5678)
    assert len(packets) > 1
    assert packets[-1][0][1] & 0x80
    assert packets[0][0][12] & 0x1F == 28
    assert packets[0][1] == 101


def test_access_units_share_timestamp_and_sps_timing_sets_90khz_step() -> None:
    assembler = _AccessUnitAssembler()
    assert assembler.push(b"\x67sps") == []
    assert assembler.push(b"\x68pps") == []
    assert assembler.push(b"\x65\x80") == []  # first_mb_in_slice = 0
    assert assembler.push(b"\x41\x50") == []  # continuation slice, first_mb_in_slice = 1
    assert assembler.push(b"\x41\x80") == [
        [b"\x67sps", b"\x68pps", b"\x65\x80", b"\x41\x50"]
    ]
    # This SPS advertises num_units_in_tick=1 and time_scale=20, i.e. 10 fps.
    sps = bytes.fromhex("6764000aacb20417f2e022000003000200000300291e244c90")
    assert _frame_ticks_from_sps(sps) == 9_000


def test_rtp_marker_is_only_on_last_packet_of_access_unit() -> None:
    packets = _rtp_packets(b"\x65" + b"x" * 3000, 100, 1234, 5678, marker=False)
    assert not any(packet[0][1] & 0x80 for packet in packets)


def test_slow_rtsp_client_does_not_block_another_client() -> None:
    entered = threading.Event()
    release = threading.Event()

    class SlowSocket:
        def sendall(self, _payload):
            entered.set()
            assert release.wait(2)

    class FastSocket:
        def __init__(self):
            self.sent = []

        def sendall(self, payload):
            self.sent.append(payload)

    slow = object.__new__(_RTSPHandler)
    slow.request = SlowSocket()
    slow._write_lock = threading.Lock()
    slow._rtp_channel = 0
    fast_socket = FastSocket()
    fast = object.__new__(_RTSPHandler)
    fast.request = fast_socket
    fast._write_lock = threading.Lock()
    fast._rtp_channel = 0
    slow_thread = threading.Thread(
        target=slow._send_access_unit, args=([b"\x65" + b"x" * 20], 1, 2, 3)
    )
    slow_thread.start()
    assert entered.wait(1)
    fast._send_access_unit([b"\x65fast"], 1, 2, 3)
    assert fast_socket.sent
    release.set()
    slow_thread.join(timeout=2)
    assert not slow_thread.is_alive()


def test_registry_routes_uid_and_sdp_advertises_h264() -> None:
    bridge = CameraBridge(
        camera_id="front",
        camera_uid="UID_FRONT",
        camera_name="Front",
        api_token="x" * 16,
        session=FakeSession(),  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )
    registry = BridgeRegistry()
    registry.add(bridge)
    assert registry.get("UID_FRONT") is bridge
    assert b"H264/90000" in _sdp(bridge, "127.0.0.1", 8100)


def test_registry_routes_trimmed_case_sensitive_alias_to_same_bridge():
    bridge = CameraBridge(
        camera_id="Front Door",
        camera_uid="UID_FRONT",
        camera_name="Front",
        api_token="x" * 16,
        session=FakeSession(),  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )
    registry = BridgeRegistry()
    registry.add(bridge)
    assert registry.get("UID_FRONT") is bridge
    assert registry.get("  Front Door ") is bridge
    assert registry.get("front door") is None


def test_registry_rejects_alias_uid_and_alias_collisions():
    first = CameraBridge(
        camera_id="front",
        camera_uid="UID_FRONT",
        camera_name="Front",
        api_token="x" * 16,
        session=FakeSession(),  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )
    registry = BridgeRegistry()
    registry.add(first)
    with pytest.raises(ValueError):
        registry.add(CameraBridge(
            camera_id="UID_FRONT",
            camera_uid="UID_OTHER",
            camera_name="Other",
            api_token="x" * 16,
            session=FakeSession(),  # type: ignore[arg-type]
            ffmpeg="ffmpeg",
        ))
    with pytest.raises(ValueError):
        registry.add(CameraBridge(
            camera_id=" front ",
            camera_uid="UID_OTHER",
            camera_name="Other",
            api_token="x" * 16,
            session=FakeSession(),  # type: ignore[arg-type]
            ffmpeg="ffmpeg",
        ))


def test_empty_alias_does_not_create_a_route():
    bridge = CameraBridge(
        camera_id="",
        camera_uid="UID_EMPTY_ALIAS",
        camera_name="Camera",
        api_token="x" * 16,
        session=FakeSession(),  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
    )
    registry = BridgeRegistry()
    registry.add(bridge)
    assert registry.get("") is None
    assert registry.get("UID_EMPTY_ALIAS") is bridge


def test_case_sensitive_aliases_can_be_distinct_and_invalid_aliases_are_rejected():
    registry = BridgeRegistry()
    for alias, uid in (("camera1", "UID_ONE"), ("Camera1", "UID_TWO")):
        registry.add(CameraBridge(
            camera_id=alias,
            camera_uid=uid,
            camera_name=alias,
            api_token="x" * 16,
            session=FakeSession(),  # type: ignore[arg-type]
            ffmpeg="ffmpeg",
        ))
    assert registry.get("camera1").camera_uid == "UID_ONE"  # type: ignore[union-attr]
    assert registry.get("Camera1").camera_uid == "UID_TWO"  # type: ignore[union-attr]
    with pytest.raises(ValueError):
        registry.add(CameraBridge(
            camera_id="bad/alias",
            camera_uid="UID_THREE",
            camera_name="Bad",
            api_token="x" * 16,
            session=FakeSession(),  # type: ignore[arg-type]
            ffmpeg="ffmpeg",
        ))


def test_registry_keeps_camera_identifiers_and_runtimes_independent() -> None:
    registry = BridgeRegistry()
    sessions = []
    for uid in ("UID_A", "UID_B"):
        session = FakeSession()
        sessions.append(session)
        registry.add(
            CameraBridge(
                camera_id=uid,
                camera_uid=uid,
                camera_name=uid,
                api_token="x" * 16,
                session=session,  # type: ignore[arg-type]
                ffmpeg="ffmpeg",
            )
        )
    assert registry.get("UID_A") is not registry.get("UID_B")
    assert registry.status()["camera_count"] == 2


@pytest.mark.parametrize(("battery_camera", "expected_passive"), [(True, True), (False, False)])
def test_rtsp_play_uses_mode_specific_shared_camera_subscription(
    battery_camera: bool, expected_passive: bool
) -> None:
    class Subscription:
        def __init__(self) -> None:
            self.closed = False

        def __iter__(self):
            yield b"\x00\x00\x01\x67sps\x00\x00\x01\x68pps\x00\x00\x01\x65\x80"

        def close(self) -> None:
            self.closed = True

    class Session:
        idle_timeout = 120.0

        def __init__(self) -> None:
            self.acquires = 0
            self.passive = None
            self.subscription = Subscription()

        def status(self) -> SessionStatus:
            return SessionStatus(True, self.acquires, True, None, True)

        def parameter_sets(self):
            return (b"", b"")

        def acquire(self, *, passive=False, reason="active"):
            self.acquires += 1
            self.passive = passive
            return self.subscription

    session = Session()
    bridge = CameraBridge(
        camera_id="UID_RTSP",
        camera_uid="UID_RTSP",
        camera_name="RTSP",
        api_token="token",
        session=session,  # type: ignore[arg-type]
        ffmpeg="ffmpeg",
        battery_camera=battery_camera,
    )
    registry = BridgeRegistry()
    registry.add(bridge)
    server = RTSPServer(("127.0.0.1", 0), registry)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = socket.create_connection(("127.0.0.1", server.port), timeout=3)
    client.settimeout(3)
    try:
        client.sendall(b"OPTIONS rtsp://127.0.0.1/UID_RTSP RTSP/1.0\r\nCSeq: 1\r\n\r\n")
        assert b"RTSP/1.0 200" in client.recv(4096)
        client.sendall(
            b"SETUP rtsp://127.0.0.1/UID_RTSP/trackID=0 RTSP/1.0\r\n"
            b"CSeq: 2\r\nTransport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n"
        )
        assert b"RTSP/1.0 200" in client.recv(4096)
        client.sendall(b"PLAY rtsp://127.0.0.1/UID_RTSP RTSP/1.0\r\nCSeq: 3\r\n\r\n")
        assert b"RTSP/1.0 200" in client.recv(4096)
        deadline = time.monotonic() + 1
        while session.acquires < 1 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert session.acquires == 1
        assert session.passive is expected_passive
        client.sendall(b"TEARDOWN rtsp://127.0.0.1/UID_RTSP RTSP/1.0\r\nCSeq: 4\r\n\r\n")
    finally:
        client.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
    assert session.subscription.closed is True
