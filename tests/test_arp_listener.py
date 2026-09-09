from __future__ import annotations
import queue
import socket
import struct
import threading
import importlib.util
from pathlib import Path
import yaml
from okam_native.arp_listener import ArpWakeListener, is_expected_camera_arp, parse_arp_packet, parse_arp_request
from okam_native.bridge import BridgeRegistry, CameraBridge
from okam_native.session import NativeStreamSession, SessionStatus

_ENTRYPOINT_SPEC = importlib.util.spec_from_file_location(
    "okam_test_arp_entrypoint", Path(__file__).parents[1] / "okam_native_app" / "app_entrypoint.py"
)
assert _ENTRYPOINT_SPEC and _ENTRYPOINT_SPEC.loader
_ENTRYPOINT = importlib.util.module_from_spec(_ENTRYPOINT_SPEC)
_ENTRYPOINT_SPEC.loader.exec_module(_ENTRYPOINT)
handle_arp_wake = _ENTRYPOINT.handle_arp_wake
configured_arp_routes = _ENTRYPOINT.configured_arp_routes

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


def test_raw_receiver_dispatches_other_camera_while_one_worker_is_blocked(monkeypatch):
    frame_a = _arp_frame(sender="192.168.1.140", target="10.0.0.1")
    frame_b = _arp_frame(sender="192.168.1.141", target="10.0.0.1")
    frames = [(frame_a, ("eth0",)), (frame_b, ("eth0",))]
    closed = threading.Event()

    class RawSocket:
        def settimeout(self, _timeout):
            pass

        def recvfrom(self, _size):
            if frames:
                return frames.pop(0)
            if closed.wait(0.01):
                raise OSError("closed")
            raise socket.timeout

        def close(self):
            closed.set()

    monkeypatch.setattr(socket, "socket", lambda *_args, **_kwargs: RawSocket())
    monkeypatch.setattr(socket, "AF_PACKET", 17, raising=False)
    a_entered = threading.Event()
    release_a = threading.Event()
    b_called = threading.Event()

    def on_wake(uid, _packet):
        if uid == "UID_A":
            a_entered.set()
            assert release_a.wait(2)
        else:
            b_called.set()

    listener = ArpWakeListener(
        ip_to_camera_uid={"192.168.1.140": "UID_A", "192.168.1.141": "UID_B"},
        on_wake=on_wake,
    )
    assert listener.start() is True
    assert a_entered.wait(1)
    assert b_called.wait(1)
    release_a.set()
    listener.close()


def test_stopping_arp_is_deferred_once_until_cleanup():
    class Subscription:
        def close(self):
            pass

    class Session:
        idle_timeout = 120.0

        def __init__(self):
            self.deferred = None
            self.defer_calls = 0
            self.acquire_calls = []

        def status(self):
            return SessionStatus(False, 0, None, None, False, False, "STOPPING")

        def defer_active_after_cleanup(self, callback):
            self.defer_calls += 1
            if self.deferred is None:
                self.deferred = callback
            return True

        def acquire(self, **kwargs):
            self.acquire_calls.append(kwargs)
            return Subscription()

    session = Session()
    registry = BridgeRegistry()
    registry.add(CameraBridge(
        camera_id="front", camera_uid="UID_FRONT", camera_name="Front",
        api_token="x" * 16, session=session, ffmpeg="ffmpeg",
    ))
    for _ in range(10):
        handle_arp_wake("UID_FRONT", registry)
    assert session.defer_calls == 10
    assert len(session.acquire_calls) == 0
    assert session.deferred is not None
    session.deferred()
    assert len(session.acquire_calls) == 1
    assert session.acquire_calls[0]["wake_before_connect"] is False


def test_stale_connected_process_is_deferred_instead_of_dropped():
    class Session:
        idle_timeout = 120.0

        def status(self):
            return SessionStatus(False, 0, None, None, False, False, "STREAMING")

        def defer_active_after_cleanup(self, callback):
            self.callback = callback
            return True

    session = Session()
    registry = BridgeRegistry()
    registry.add(CameraBridge(
        camera_id="front", camera_uid="UID_FRONT", camera_name="Front",
        api_token="x" * 16, session=session, ffmpeg="ffmpeg",
    ))
    handle_arp_wake("UID_FRONT", registry)
    assert hasattr(session, "callback")


def test_ha_startup_then_arp_uses_one_native_generation():
    entered = threading.Event()
    release = threading.Event()
    starts = []

    class Pipe:
        def __init__(self):
            self.queue = queue.Queue()

        def read(self, _size=-1):
            return self.queue.get(timeout=2) or b""

    class Process:
        def __init__(self):
            self.stdout, self.stderr = Pipe(), Pipe()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.stdout.queue.put(None)
            self.stderr.queue.put(None)

        kill = terminate

        def wait(self, timeout=None):
            return self.returncode

    process = Process()

    def starter():
        starts.append(True)
        entered.set()
        assert release.wait(2)
        return process

    session = NativeStreamSession(starter)  # type: ignore[arg-type]
    registry = BridgeRegistry()
    registry.add(CameraBridge(
        camera_id="front", camera_uid="UID_FRONT", camera_name="Front",
        api_token="x" * 16, session=session, ffmpeg="ffmpeg",
    ))
    owner = threading.Thread(target=session.acquire)
    owner.start()
    assert entered.wait(1)
    handle_arp_wake("UID_FRONT", registry)
    release.set()
    owner.join(timeout=2)
    assert not owner.is_alive()
    assert len(starts) == 1
    session.close()


def test_arp_startup_then_ha_joins_one_native_generation():
    entered = threading.Event()
    release = threading.Event()
    starts = []

    class Pipe:
        def __init__(self):
            self.queue = queue.Queue()

        def read(self, _size=-1):
            return self.queue.get(timeout=2) or b""

    class Process:
        def __init__(self):
            self.stdout, self.stderr = Pipe(), Pipe()
            self.returncode = None

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = 0
            self.stdout.queue.put(None)
            self.stderr.queue.put(None)

        kill = terminate

        def wait(self, timeout=None):
            return self.returncode

    process = Process()

    def starter():
        starts.append(True)
        entered.set()
        assert release.wait(2)
        return process

    session = NativeStreamSession(starter)  # type: ignore[arg-type]
    registry = BridgeRegistry()
    registry.add(CameraBridge(
        camera_id="front", camera_uid="UID_FRONT", camera_name="Front",
        api_token="x" * 16, session=session, ffmpeg="ffmpeg",
    ))
    arp = threading.Thread(target=lambda: handle_arp_wake("UID_FRONT", registry))
    arp.start()
    assert entered.wait(1)
    ha = threading.Thread(target=session.acquire)
    ha.start()
    release.set()
    arp.join(timeout=2)
    ha.join(timeout=2)
    assert not arp.is_alive() and not ha.is_alive()
    assert len(starts) == 1
    session.close()


def test_arp_activation_skips_wake_and_live_state_is_coalesced():
    class Subscription:
        def close(self):
            pass

    class Session:
        idle_timeout = 120.0

        def __init__(self, state, running):
            self.state = state
            self.running = running
            self.calls = []

        def status(self):
            return SessionStatus(self.running, 1, None, None, False, False, self.state)

        def acquire(self, **kwargs):
            self.calls.append(kwargs)
            return Subscription()

        def defer_active_after_cleanup(self, _callback):
            return False

    idle = Session("IDLE", False)
    registry = BridgeRegistry()
    registry.add(CameraBridge(
        camera_id="front", camera_uid="UID_IDLE", camera_name="Front",
        api_token="x" * 16, session=idle, ffmpeg="ffmpeg",
    ))
    handle_arp_wake("UID_IDLE", registry)
    assert idle.calls[0]["wake_before_connect"] is False

    starting = Session("STARTING", True)
    registry = BridgeRegistry()
    registry.add(CameraBridge(
        camera_id="starting", camera_uid="UID_STARTING", camera_name="Starting",
        api_token="x" * 16, session=starting, ffmpeg="ffmpeg",
    ))
    handle_arp_wake("UID_STARTING", registry)
    assert starting.calls == []

    live = Session("STREAMING", True)
    registry = BridgeRegistry()
    registry.add(CameraBridge(
        camera_id="live", camera_uid="UID_LIVE", camera_name="Live",
        api_token="x" * 16, session=live, ffmpeg="ffmpeg",
    ))
    handle_arp_wake("UID_LIVE", registry)
    assert live.calls == []


def test_duplicate_configured_camera_ip_is_disabled_deterministically(capsys):
    bridges = []
    for uid in ("UID_A", "UID_B"):
        bridges.append(CameraBridge(
            camera_id=uid,
            camera_uid=uid,
            camera_name=uid,
            api_token="x" * 16,
            session=object(),  # type: ignore[arg-type]
            ffmpeg="ffmpeg",
            battery_camera=True,
            camera_ip="192.168.1.140",
        ))
    assert configured_arp_routes(tuple(bridges)) == {}
    assert "arp_wake_duplicate_camera_ip ip=192.168.1.140" in capsys.readouterr().out


def test_addon_defaults_and_minimum_network_permission():
    root=Path(__file__).parents[1]
    config=yaml.safe_load((root/"okam_native_app/config.yaml").read_text())
    assert config["options"]["arp_wake_listener"] is True
    assert "arp_wake_log_all_camera_arp" not in config["options"]
    assert config["schema"]["arp_wake_listener"] == "bool"
    assert "arp_wake_log_all_camera_arp" not in config["schema"]
    assert config["host_network"] is True and config["privileged"] == ["NET_RAW"]
