"""Receive-only ARP observation for configured battery-camera wake events."""
from __future__ import annotations
from dataclasses import dataclass
import socket
import struct
import threading
from typing import Callable

_ETH_P_ARP = 0x0806
_BROADCAST_MAC = b"\xff" * 6
_ARP_FRAME_LENGTH = 14 + 28

@dataclass(frozen=True)
class ArpPacket:
    operation: int
    sender_ip: str
    target_ip: str
    sender_mac: str
    broadcast: bool
    interface: str | None = None

def parse_arp_packet(frame: bytes, *, interface: str | None = None) -> ArpPacket | None:
    if len(frame) < _ARP_FRAME_LENGTH or struct.unpack_from("!H", frame, 12)[0] != _ETH_P_ARP:
        return None
    hardware_type, protocol_type, hardware_len, protocol_len, operation = struct.unpack_from("!HHBBH", frame, 14)
    if hardware_type != 1 or protocol_type != 0x0800 or hardware_len != 6 or protocol_len != 4:
        return None
    sender_mac = frame[22:28]
    return ArpPacket(operation, socket.inet_ntoa(frame[28:32]), socket.inet_ntoa(frame[38:42]), ":".join(f"{b:02x}" for b in sender_mac), frame[:6] == _BROADCAST_MAC, interface)

def parse_arp_request(frame: bytes, *, interface: str | None = None) -> ArpPacket | None:
    packet = parse_arp_packet(frame, interface=interface)
    if packet is None or not packet.broadcast or packet.operation != 1:
        return None
    return packet

def is_expected_camera_arp(request: ArpPacket, camera_ip: str | None = None) -> bool:
    return request.broadcast and request.operation == 1 and camera_ip is not None and request.sender_ip == camera_ip

WakeCallback = Callable[[str, ArpPacket], None]

class ArpWakeListener:
    def __init__(self, logger: Callable[..., object] = print, *, ip_to_camera_uid: dict[str, str] | None = None, on_wake: WakeCallback | None = None) -> None:
        self._logger = logger
        self._ip_to_camera_uid = dict(ip_to_camera_uid or {})
        self._on_wake = on_wake
        self._socket: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, *, enabled: bool = True) -> bool:
        if not enabled:
            self._logger("arp_wake_listener_disabled", flush=True)
            return False
        if not hasattr(socket, "AF_PACKET"):
            self._logger("arp_wake_listener_unavailable exception=UnsupportedPlatform reason=AF_PACKET_unavailable", flush=True)
            return False
        try:
            raw_socket = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(_ETH_P_ARP))
            raw_socket.settimeout(1.0)
        except (OSError, PermissionError) as error:
            self._logger("arp_wake_listener_unavailable " f"exception={type(error).__name__} reason={_safe_reason(error)}", flush=True)
            return False
        self._socket = raw_socket
        self._logger("arp_wake_listener_started interface=all mechanism=af_packet protocol=arp mode=receive_only configured_cameras=" f"{len(self._ip_to_camera_uid)}", flush=True)
        self._thread = threading.Thread(target=self._run, name="okam-arp-listener", daemon=True)
        self._thread.start()
        return True

    def close(self) -> None:
        self._stop.set()
        raw_socket = self._socket
        self._socket = None
        if raw_socket is not None:
            raw_socket.close()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2.0)
        self._thread = None

    def _run(self) -> None:
        raw_socket = self._socket
        if raw_socket is None:
            return
        while not self._stop.is_set():
            try:
                frame, address = raw_socket.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                return
            interface = address[0] if isinstance(address, tuple) and address else None
            self._handle_packet(parse_arp_request(frame, interface=interface))

    def _handle_packet(self, packet: ArpPacket | None) -> None:
        if packet is None:
            return
        camera_uid = self._ip_to_camera_uid.get(packet.sender_ip)
        if camera_uid is None or not is_expected_camera_arp(packet, packet.sender_ip):
            return
        self._logger("camera_arp_wake_detected " f"camera={packet.sender_ip} uid={camera_uid} target={packet.target_ip}", flush=True)
        if self._on_wake is not None:
            self._on_wake(camera_uid, packet)

def _safe_reason(error: BaseException) -> str:
    reason = str(error).strip().replace(" ", "_")
    return reason.replace("=", "_") or "unspecified"

def start_arp_wake_listener(logger: Callable[..., object] = print, *, enabled: bool = True, ip_to_camera_uid: dict[str, str] | None = None, on_wake: WakeCallback | None = None) -> ArpWakeListener | None:
    listener = ArpWakeListener(logger, ip_to_camera_uid=ip_to_camera_uid, on_wake=on_wake)
    if listener.start(enabled=enabled):
        return listener
    return None
