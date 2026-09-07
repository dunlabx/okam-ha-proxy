from okam_native.bridge import BridgeRegistry, CameraBridge
from okam_native.rtsp import _AccessUnitAssembler, _frame_ticks_from_sps, _nal_units, _rtp_packets, _sdp
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
