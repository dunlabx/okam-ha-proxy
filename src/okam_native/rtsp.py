"""Small RTSP-over-TCP server for the native H.264 camera sessions.

The native helper already emits Annex-B H.264. This module only packetizes NAL
units into RTP and fans each selected camera session out to RTSP clients; it
never decodes or re-encodes video.
"""

from __future__ import annotations

import base64
import random
import re
import socket
import socketserver
import threading
import time
from urllib.parse import unquote, urlsplit

from .bridge import BridgeRegistry, CameraBridge
from .cs2 import MAX_FRAME_BYTES
from .p2p import P2PError


RTP_PAYLOAD_TYPE = 96
RTP_AUDIO_PAYLOAD_TYPE = 97
RTP_CLOCK = 90_000
AUDIO_CLOCK = 16_000
MAX_RTP_PAYLOAD = 1_400
FRAME_TICKS = 3_000  # stable fallback clock: 30 synthetic frames per second


class _BitReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def read(self, count: int) -> int:
        if count < 0 or self._offset + count > len(self._data) * 8:
            raise ValueError("truncated H264 SPS")
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self._data[self._offset // 8] >> (7 - self._offset % 8)) & 1)
            self._offset += 1
        return value

    def ue(self) -> int:
        zeros = 0
        while self.read(1) == 0:
            zeros += 1
            if zeros > 31:
                raise ValueError("invalid H264 exp-golomb value")
        return (1 << zeros) - 1 + self.read(zeros)

    def se(self) -> int:
        value = self.ue()
        return -(value // 2) if value % 2 == 0 else (value + 1) // 2


def _nal_rbsp(nal: bytes) -> bytes:
    if not nal:
        return b""
    result = bytearray()
    zeroes = 0
    for byte in nal[1:]:
        if zeroes >= 2 and byte == 3:
            zeroes = 0
            continue
        result.append(byte)
        zeroes = zeroes + 1 if byte == 0 else 0
    return bytes(result)


def _sps_rbsp(nal: bytes) -> bytes:
    return _nal_rbsp(nal) if nal and nal[0] & 0x1F == 7 else b""


def _frame_ticks_from_sps(nal: bytes) -> int | None:
    """Read H.264 VUI timing and convert one frame to the RTP 90 kHz clock."""

    try:
        reader = _BitReader(_sps_rbsp(nal))
        profile_idc = reader.read(8)
        reader.read(8)  # constraint flags and reserved bits
        reader.read(8)  # level_idc
        reader.ue()  # seq_parameter_set_id
        if profile_idc in {44, 83, 86, 100, 110, 118, 122, 128, 134, 138, 139, 244}:
            chroma_format = reader.ue()
            if chroma_format == 3:
                reader.read(1)
            reader.ue()  # bit_depth_luma_minus8
            reader.ue()  # bit_depth_chroma_minus8
            reader.read(1)  # qpprime_y_zero_transform_bypass_flag
            if reader.read(1):  # seq_scaling_matrix_present_flag
                for index in range(8 if chroma_format != 3 else 12):
                    if reader.read(1):
                        size = 16 if index < 6 else 64
                        last_scale = 8
                        next_scale = 8
                        for _ in range(size):
                            if next_scale:
                                next_scale = (last_scale + reader.se()) % 256
                            last_scale = next_scale or last_scale
        reader.ue()  # log2_max_frame_num_minus4
        poc_type = reader.ue()
        if poc_type == 0:
            reader.ue()
        elif poc_type == 1:
            reader.read(1)
            reader.se()
            reader.se()
            for _ in range(reader.ue()):
                reader.se()
        reader.ue()  # max_num_ref_frames
        reader.read(1)  # gaps_in_frame_num_value_allowed_flag
        reader.ue()  # pic_width_in_mbs_minus1
        reader.ue()  # pic_height_in_map_units_minus1
        frame_mbs_only = reader.read(1)
        if not frame_mbs_only:
            reader.read(1)
        reader.read(1)  # direct_8x8_inference_flag
        if reader.read(1):  # frame_cropping_flag
            for _ in range(4):
                reader.ue()
        if not reader.read(1):  # vui_parameters_present_flag
            return None
        if reader.read(1):  # aspect_ratio_info_present_flag
            aspect_ratio_idc = reader.read(8)
            if aspect_ratio_idc == 255:
                reader.read(32)
        if reader.read(1):  # overscan_info_present_flag
            reader.read(1)
        if reader.read(1):  # video_signal_type_present_flag
            reader.read(3)
            if reader.read(1):
                reader.read(24)
        if reader.read(1):  # chroma_loc_info_present_flag
            reader.ue()
            reader.ue()
        if not reader.read(1):  # timing_info_present_flag
            return None
        num_units_in_tick = reader.read(32)
        time_scale = reader.read(32)
        fixed_frame_rate = reader.read(1)
        if not fixed_frame_rate or not num_units_in_tick or not time_scale:
            return None
        ticks = (RTP_CLOCK * 2 * num_units_in_tick + time_scale // 2) // time_scale
        return ticks if 1 <= ticks <= RTP_CLOCK * 10 else None
    except (IndexError, ValueError):
        return None


def _vcl_starts_new_picture(nal: bytes) -> bool | None:
    """Return whether a VCL NAL begins a picture, when its slice header parses."""

    if not nal or nal[0] & 0x1F not in (1, 5):
        return None
    try:
        # first_mb_in_slice is the first unsigned Exp-Golomb field after the
        # one-byte NAL header. A value of zero identifies the first slice.
        return _BitReader(_nal_rbsp(nal)).ue() == 0
    except (IndexError, ValueError):
        return None


def _nal_units(data: bytes, carry: bytearray) -> list[bytes]:
    """Extract complete Annex-B NAL units while retaining an incomplete tail."""

    carry.extend(data)
    if len(carry) > MAX_FRAME_BYTES + 4:
        # Keep only enough bytes to preserve a start code split across reads;
        # an unbounded incomplete NAL must never grow with a slow/malformed
        # producer.
        del carry[: len(carry) - 4]
    starts: list[int] = []
    i = 0
    while i + 3 <= len(carry):
        if carry[i : i + 3] == b"\x00\x00\x01":
            starts.append(i)
            i += 3
        elif i + 4 <= len(carry) and carry[i : i + 4] == b"\x00\x00\x00\x01":
            starts.append(i)
            i += 4
        else:
            i += 1
    if len(starts) < 2:
        return []
    result: list[bytes] = []
    for begin, end in zip(starts, starts[1:]):
        start_len = 4 if carry[begin : begin + 4] == b"\x00\x00\x00\x01" else 3
        nal = bytes(carry[begin + start_len : end])
        if nal:
            result.append(nal)
    del carry[: starts[-1]]
    if len(carry) > MAX_FRAME_BYTES + 4:
        del carry[: len(carry) - 4]
    return result


class _AccessUnitAssembler:
    """Group Annex-B NALs into access units for RTP timestamps.

    The native helper exposes byte framing but no presentation timestamps. H264
    VCL NAL boundaries are therefore used as a stable frame clock. AUDs are
    honored when present; SPS/PPS/SEI preceding a VCL NAL stay with that frame.
    """

    def __init__(self) -> None:
        self._units: list[bytes] = []
        self._has_vcl = False

    def push(self, nal: bytes) -> list[list[bytes]]:
        kind = nal[0] & 0x1F if nal else 0
        ready: list[list[bytes]] = []
        if kind == 9:  # access-unit delimiter
            if self._units and self._has_vcl:
                ready.append(self._units)
                self._units = []
            self._units.append(nal)
            self._has_vcl = False
            return ready
        if kind in (1, 5):  # non-IDR/IDR VCL NAL
            starts_picture = _vcl_starts_new_picture(nal)
            if self._has_vcl and starts_picture is not False:
                ready.append(self._units)
                self._units = []
            self._has_vcl = True
        elif kind in (7, 8) and self._has_vcl:
            # Parameter sets normally precede the next IDR/access unit.
            ready.append(self._units)
            self._units = []
            self._has_vcl = False
        self._units.append(nal)
        return ready

    def flush(self) -> list[list[bytes]]:
        if self._units and self._has_vcl:
            result = [self._units]
            self._units = []
            self._has_vcl = False
            return result
        return []


def _sdp(bridge: CameraBridge, host: str, port: int) -> bytes:
    parameter_sets = getattr(bridge.session, "parameter_sets", None)
    sps, pps = parameter_sets() if callable(parameter_sets) else (b"", b"")
    attrs = [
        "v=0",
        "o=- 0 0 IN IP4 0.0.0.0",
        "s=O-KAM Native Bridge",
        "t=0 0",
        "a=control:*",
        "m=video 0 RTP/AVP 96",
        "c=IN IP4 0.0.0.0",
        "a=rtpmap:96 H264/90000",
        "a=fmtp:96 packetization-mode=1;profile-level-id=42e01f",
        "a=control:trackID=0",
        "m=audio 0 RTP/AVP 97",
        "c=IN IP4 0.0.0.0",
        "a=rtpmap:97 PCMA/16000/1",
        "a=control:trackID=1",
        "a=sendrecv",
    ]
    if len(sps) >= 4 and len(pps) >= 4:
        sps_payload = sps[4:] if sps[:4] == b"\x00\x00\x00\x01" else sps[3:]
        pps_payload = pps[4:] if pps[:4] == b"\x00\x00\x00\x01" else pps[3:]
        profile = sps_payload[1:4].hex() if len(sps_payload) >= 4 else "42e01f"
        attrs[8] = (
            "a=fmtp:96 packetization-mode=1;"
            f"profile-level-id={profile};"
            f"sprop-parameter-sets={base64.b64encode(sps_payload).decode()},"
            f"{base64.b64encode(pps_payload).decode()}"
        )
    return ("\r\n".join(attrs) + "\r\n\r\n").encode()


class _RTSPHandler(socketserver.BaseRequestHandler):
    server: "RTSPServer"

    def setup(self) -> None:
        self.request.settimeout(1.0)
        self._session_id = f"{random.randrange(1 << 32):08x}"
        self._bridge: CameraBridge | None = None
        self._subscription = None
        self._stop_stream = threading.Event()
        self._write_lock = threading.Lock()
        self._stream_thread: threading.Thread | None = None
        self._rtp_channel = 0
        self._track_channels: dict[int, int] = {}
        self._diagnostic_started = time.monotonic()
        self._diagnostic_camera: CameraBridge | None = None
        self._rtsp_bytes = 0
        self._rtsp_chunks = 0
        self._audio_packets = 0
        self._audio_bytes = 0
        self._saw_standby = False
        self._saw_live = False
        self._media_generation: int | None = None
        self._codec_transition_seen = False

    def _diagnostic(self, event: str, **fields: object) -> None:
        bridge = self._diagnostic_camera
        if bridge is None:
            return
        emit = getattr(bridge.session, "diagnostic", None)
        if callable(emit):
            values = {
                "operation": "rtsp",
                "rtsp_elapsed_ms": round((time.monotonic() - self._diagnostic_started) * 1000, 1),
                **fields,
            }
            emit(event, **values)

    def handle(self) -> None:
        buffer = bytearray()
        try:
            while not self._stop_stream.is_set():
                try:
                    chunk = self.request.recv(16 * 1024)
                except socket.timeout:
                    continue
                except (ConnectionResetError, ConnectionAbortedError, OSError):
                    # A client disappearing is normal for live playback. The
                    # finally block below releases its shared subscription.
                    return
                if not chunk:
                    break
                buffer.extend(chunk)
                while True:
                    if buffer[:1] == b"$":
                        if len(buffer) < 4:
                            break
                        length = int.from_bytes(buffer[2:4], "big")
                        if len(buffer) < 4 + length:
                            break
                        channel = buffer[1]
                        payload = bytes(buffer[4 : 4 + length])
                        del buffer[: 4 + length]
                        self._handle_interleaved(channel, payload)
                        continue
                    marker = buffer.find(b"\r\n\r\n")
                    if marker < 0:
                        break
                    request = bytes(buffer[: marker + 4])
                    del buffer[: marker + 4]
                    if not self._handle_request(request):
                        return
        finally:
            self._stop_stream.set()
            if self._subscription is not None:
                self._subscription.close()
                self._subscription = None
            if self._stream_thread is not None:
                self._stream_thread.join(timeout=3)

    def _handle_request(self, raw: bytes) -> bool:
        lines = raw.decode("iso-8859-1", errors="replace").split("\r\n")
        if not lines or len(lines[0].split()) < 2:
            return False
        method, target, *_ = lines[0].split()
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if ":" in line:
                key, value = line.split(":", 1)
                headers[key.lower().strip()] = value.strip()
        cseq = headers.get("cseq", "0")
        parsed = urlsplit(target)
        identifier = unquote(parsed.path.strip("/").split("/")[0])
        bridge = self.server.registry.get(identifier) if identifier else None
        if bridge is not None and self._diagnostic_camera is None:
            self._diagnostic_camera = bridge
            generation = getattr(bridge.session, "media_generation", None)
            self._media_generation = generation() if callable(generation) else None
            self._diagnostic("rtsp_client_connected")

        if method == "OPTIONS":
            self._reply(200, cseq, {"Public": "OPTIONS, DESCRIBE, SETUP, PLAY, TEARDOWN, GET_PARAMETER"})
            return True
        if bridge is None:
            self._reply(404, cseq)
            return True
        if method == "DESCRIBE":
            body = _sdp(bridge, self.server.host, self.server.port)
            self._reply(200, cseq, {"Content-Type": "application/sdp", "Content-Base": target}, body)
            return True
        if method == "SETUP":
            transport = headers.get("transport", "")
            channel = self._rtp_channel
            if "RTP/AVP/TCP" not in transport.upper():
                self._reply(461, cseq)
                return True
            match = re.search(r"interleaved=(\d+)-(\d+)", transport, re.IGNORECASE)
            if match is not None:
                channel = int(match.group(1))
                if channel > 255 or int(match.group(2)) > 255:
                    self._reply(461, cseq)
                    return True
                track = 1 if re.search(r"trackID=1|audioback", target, re.I) else 0
                self._track_channels[track] = channel
                if track == 0:
                    self._rtp_channel = channel
            self._bridge = bridge
            self._reply(
                200,
                cseq,
                {
                    "Transport": f"RTP/AVP/TCP;unicast;interleaved={channel}-{channel + 1}",
                    "Session": self._session_id,
                },
            )
            return True
        if method == "PLAY":
            if self._bridge is not bridge or self._subscription is not None:
                self._reply(454, cseq)
                return True
            try:
                self._subscription = bridge.session.acquire(
                    passive=bridge.battery_camera,
                    reason="rtsp_passive" if bridge.battery_camera else "rtsp_active",
                )
            except (P2PError, OSError, RuntimeError):
                self._reply(503, cseq)
                return True
            self._diagnostic("rtsp_subscription_created")
            self._reply(200, cseq, {"Session": self._session_id, "RTP-Info": "url=trackID=0"})
            self._stream_thread = threading.Thread(target=self._stream, daemon=True)
            self._stream_thread.start()
            if 1 in self._track_channels:
                self._audio_thread = threading.Thread(target=self._stream_audio, daemon=True)
                self._audio_thread.start()
            else:
                self._diagnostic("rtsp_audio_track_not_subscribed")
            return True
        if method == "GET_PARAMETER":
            self._reply(200, cseq, {"Session": self._session_id})
            return True
        if method == "TEARDOWN":
            self._reply(200, cseq, {"Session": self._session_id})
            self._stop_stream.set()
            return False
        self._reply(501, cseq)
        return True

    def _handle_interleaved(self, channel: int, packet: bytes) -> None:
        """Forward go2rtc backchannel RTP payloads to native channel 3."""
        if channel != self._track_channels.get(1) or len(packet) < 12:
            return
        header = 12 + (packet[0] & 0x0F) * 4
        if packet[0] & 0x10:
            if len(packet) < header + 4:
                return
            header += 4 + int.from_bytes(packet[header + 2 : header + 4], "big") * 4
        if header > len(packet):
            return
        sender = getattr(getattr(self, "_bridge", None), "session", None)
        send = getattr(sender, "send_talkback", None)
        if callable(send):
            send(packet[header:])

    def _reply(self, status: int, cseq: str, headers: dict[str, str] | None = None, body: bytes = b"") -> None:
        reason = {
            200: "OK",
            404: "Not Found",
            454: "Session Not Found",
            461: "Unsupported Transport",
            501: "Not Implemented",
            503: "Service Unavailable",
        }.get(status, "Error")
        fields = {"CSeq": cseq, "Server": "okam-native-rtsp/1"}
        if headers:
            fields.update(headers)
        if body:
            fields["Content-Length"] = str(len(body))
        message = f"RTSP/1.0 {status} {reason}\r\n" + "".join(
            f"{key}: {value}\r\n" for key, value in fields.items()
        ) + "\r\n"
        with self._write_lock:
            try:
                self.request.sendall(message.encode("iso-8859-1") + body)
            except OSError:
                self._stop_stream.set()

    def _stream(self) -> None:
        assert self._subscription is not None
        sequence = random.randrange(1 << 16)
        ssrc = random.randrange(1 << 32)
        carry = bytearray()
        timestamp = random.randrange(1 << 32)
        frame_ticks = FRAME_TICKS
        assembler = _AccessUnitAssembler()
        try:
            for chunk in self._subscription:
                if self._stop_stream.is_set():
                    return
                generation_reader = getattr(self._bridge.session, "media_generation", None)
                generation = generation_reader() if callable(generation_reader) else None
                status = getattr(self._bridge.session, "status", lambda: None)()
                live_media = bool(getattr(status, "running", False) and getattr(status, "media_ready", False))
                codec_changed = (
                    generation is not None
                    and self._media_generation is not None
                    and generation != self._media_generation
                )
                if (
                    codec_changed
                    and not self._codec_transition_seen
                    and ((self._saw_standby and live_media) or self._saw_live)
                ):
                    self._codec_transition_seen = True
                    self._diagnostic(
                        "rtsp_codec_transition_reconnect",
                        from_generation=self._media_generation,
                        to_generation=generation,
                    )
                    self._stop_stream.set()
                    try:
                        self.request.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    return
                self._rtsp_chunks += 1
                self._rtsp_bytes += len(chunk)
                if not self._saw_standby:
                    self._saw_standby = True
                    self._diagnostic("rtsp_first_standby_frame", bytes=len(chunk))
                if not self._saw_live:
                    if getattr(status, "running", False) and getattr(status, "media_ready", False):
                        self._saw_live = True
                        self._diagnostic("rtsp_first_live_chunk", bytes=len(chunk))
                if self._rtsp_chunks == 1 or self._rtsp_chunks % 100 == 0:
                    self._diagnostic("rtsp_progress", rtsp_chunks=self._rtsp_chunks, rtsp_live_bytes_total=self._rtsp_bytes)
                for nal in _nal_units(chunk, carry):
                    if not nal:
                        continue
                    if nal[0] & 0x1F == 7:
                        frame_ticks = _frame_ticks_from_sps(nal) or frame_ticks
                    for access_unit in assembler.push(nal):
                        sequence = self._send_access_unit(
                            access_unit, sequence, timestamp, ssrc
                        )
                        timestamp = (timestamp + frame_ticks) & 0xFFFFFFFF
            for access_unit in assembler.flush():
                self._send_access_unit(access_unit, sequence, timestamp, ssrc)
        except (OSError, ConnectionError):
            pass
        finally:
            self._stop_stream.set()

    def _stream_audio(self) -> None:
        """Send queued PCMA frames on the negotiated audio interleaving."""
        subscription = self._subscription
        iterator = getattr(subscription, "iter_audio", None)
        if not callable(iterator):
            return
        sequence = random.randrange(1 << 16)
        timestamp = random.randrange(1 << 32)
        ssrc = random.randrange(1 << 32)
        channel = self._track_channels.get(1, self._rtp_channel + 2)
        try:
            for payload in iterator():
                if self._stop_stream.is_set():
                    return
                header = bytes((0x80, RTP_AUDIO_PAYLOAD_TYPE)) + sequence.to_bytes(2, "big") + timestamp.to_bytes(4, "big") + ssrc.to_bytes(4, "big")
                frame = bytes((0x24, channel)) + (len(header) + len(payload)).to_bytes(2, "big") + header + payload
                with self._write_lock:
                    self.request.sendall(frame)
                self._audio_packets += 1
                self._audio_bytes += len(payload)
                if self._audio_packets == 1:
                    self._diagnostic(
                        "rtsp_audio_first_packet",
                        payload_type=RTP_AUDIO_PAYLOAD_TYPE,
                        clock_rate=AUDIO_CLOCK,
                        channels=1,
                        payload_bytes=len(payload),
                    )
                elif self._audio_packets % 250 == 0:
                    self._diagnostic(
                        "rtsp_audio_progress",
                        rtp_audio_packet_count=self._audio_packets,
                        rtp_audio_payload_bytes_total=self._audio_bytes,
                    )
                sequence = (sequence + 1) & 0xFFFF
                timestamp = (timestamp + len(payload)) & 0xFFFFFFFF
        except (OSError, ConnectionError):
            return

    def _send_access_unit(
        self, access_unit: list[bytes], sequence: int, timestamp: int, ssrc: int
    ) -> int:
        for index, nal in enumerate(access_unit):
            packets = _rtp_packets(
                nal,
                sequence,
                timestamp,
                ssrc,
                marker=index == len(access_unit) - 1,
            )
            for packet, sequence in packets:
                frame = (
                    bytes((0x24, self._rtp_channel))
                    + len(packet).to_bytes(2, "big")
                    + packet
                )
                with self._write_lock:
                    self.request.sendall(frame)
        return sequence


def _rtp_packets(
    nal: bytes,
    sequence: int,
    timestamp: int,
    ssrc: int,
    *,
    marker: bool = True,
) -> list[tuple[bytes, int]]:
    def header(seq: int, marker: int) -> bytes:
        return bytes([0x80, (marker << 7) | RTP_PAYLOAD_TYPE]) + seq.to_bytes(2, "big") + timestamp.to_bytes(4, "big") + ssrc.to_bytes(4, "big")

    if len(nal) <= MAX_RTP_PAYLOAD:
        return [(header(sequence, int(marker)) + nal, (sequence + 1) & 0xFFFF)]
    indicator = (nal[0] & 0xE0) | 28
    nal_type = nal[0] & 0x1F
    result: list[tuple[bytes, int]] = []
    offset = 1
    first = True
    while offset < len(nal):
        end = min(offset + MAX_RTP_PAYLOAD - 2, len(nal))
        last = end == len(nal)
        fu_header = nal_type | (0x80 if first else 0) | (0x40 if last else 0)
        result.append((header(sequence, int(marker and last)) + bytes([indicator, fu_header]) + nal[offset:end], (sequence + 1) & 0xFFFF))
        sequence = (sequence + 1) & 0xFFFF
        offset = end
        first = False
    return result


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class RTSPServer(_ThreadingTCPServer):
    """RTSP/TCP listener routing `/<camera_uid>` to the registry."""

    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], registry: BridgeRegistry) -> None:
        self.registry = registry
        super().__init__(server_address, _RTSPHandler)
        self.host, self.port = self.server_address
