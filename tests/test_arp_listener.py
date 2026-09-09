from __future__ import annotations
import queue
import socket
import struct
import threading
from pathlib import Path
import yaml
from okam_native.arp_listener import ArpWakeListener, is_expected_camera_arp, parse_arp_packet, parse_arp_request

def _arp_frame(*, sender: str, target: str, operation: int = 1, broadcast: bool = True) -> bytes:
    dst = b"\xff" * 6 if broadcast else bytes.fromhex("001122334455")
    src = bytes.fromhex("aabbccddeeff")
    arp = struct.pack("!HHBBH6s4s6s4s", 1, 0x0800, 6, 4, operation, src, socket.inet_aton(sender), b"\x00" * 6, socket.inet_aton(target))
    return dst + src + struct.pack("!H", 0x0806) + arp

def _collect(lines):
    return lambda message, **_kwargs: lines.append(message)

def test_broadcast_sender_ip_routes_configured_camera():
    packet = parse_arp_request(_arp_frame(sender="192.168.1.140", target="10.0.0.1"), interface="eth0")
    assert packet and is_expected_camera_arp(packet, "192.168.1.140")
    lines=[]; routed=[]
    ArpWakeListener(_collect(lines), ip_to_camera_uid={"192.168.1.140":"UID_A"}, on_wake=lambda uid, p: routed.append((uid,p)))._handle_packet(packet)
    assert routed[0][0] == "UID_A"
    assert lines[0].startswith("camera_arp_wake_detected camera=192.168.1.140 uid=UID_A")


def test_arp_callback_uses_existing_session_without_second_wake():
    packet = parse_arp_request(_arp_frame(sender="192.168.1.140", target="10.0.0.1"))
    calls = []
    listener = ArpWakeListener(
        ip_to_camera_uid={"192.168.1.140": "UID_A"},
        on_wake=lambda uid, _packet: calls.append((uid, "arp_wake", False)),
    )
    listener._handle_packet(packet)
    assert calls == [("UID_A", "arp_wake", False)]

def test_unknown_ip_and_nonbroadcast_are_ignored():
    packet=parse_arp_request(_arp_frame(sender="192.168.1.50", target="10.0.0.1"))
    assert packet and not is_expected_camera_arp(packet, "192.168.1.140")
    lines=[]; listener=ArpWakeListener(_collect(lines), ip_to_camera_uid={"192.168.1.140":"UID_A"})
    listener._handle_packet(packet)
    listener._handle_packet(parse_arp_packet(_arp_frame(sender="192.168.1.140", target="10.0.0.1", broadcast=False)))
    assert lines == []

def test_arp_reply_is_ignored():
    assert parse_arp_request(_arp_frame(sender="192.168.1.1", target="192.168.1.140", operation=2)) is None

def test_listener_disabled_does_not_create_socket(monkeypatch, capsys):
    monkeypatch.setattr(socket, "socket", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("socket created")))
    assert ArpWakeListener().start(enabled=False) is False
    assert capsys.readouterr().out.strip() == "arp_wake_listener_disabled"

def test_listener_start_failure_is_non_fatal(monkeypatch, capsys):
    monkeypatch.delattr(socket, "AF_PACKET", raising=False)
    assert ArpWakeListener().start() is False
    assert "arp_wake_listener_unavailable" in capsys.readouterr().out

def test_listener_is_receive_only():
    source=Path(__file__).parents[1]/"src/okam_native/arp_listener.py"
    text=source.read_text()
    assert ".send(" not in text and ".sendto(" not in text and ".sendmsg(" not in text


def test_dispatch_coalesces_bursts_per_camera_without_blocking_receiver():
    packet = parse_arp_request(_arp_frame(sender="192.168.1.140", target="10.0.0.1"))
    assert packet is not None
    entered = threading.Event()
    release = threading.Event()
    calls = []

    listener = ArpWakeListener(
        ip_to_camera_uid={"192.168.1.140": "UID_A"},
        on_wake=lambda uid, _packet: (calls.append(uid), entered.set(), release.wait(2)),
    )
    channel = queue.Queue(maxsize=1)
    listener._queues["UID_A"] = channel
    worker = threading.Thread(target=listener._dispatch_loop, args=("UID_A", channel), daemon=True)
    worker.start()
    listener._handle_packet(packet, dispatch=True)
    assert entered.wait(1)
    for _ in range(10):
        listener._handle_packet(packet, dispatch=True)
    release.set()
    worker.join(timeout=2)
    assert calls == ["UID_A"]
    listener.close()

def test_addon_defaults_and_minimum_network_permission():
    root=Path(__file__).parents[1]
    config=yaml.safe_load((root/"okam_native_app/config.yaml").read_text())
    assert config["options"]["arp_wake_listener"] is True
    assert "arp_wake_log_all_camera_arp" not in config["options"]
    assert config["schema"]["arp_wake_listener"] == "bool"
    assert "arp_wake_log_all_camera_arp" not in config["schema"]
    assert config["host_network"] is True and config["privileged"] == ["NET_RAW"]
