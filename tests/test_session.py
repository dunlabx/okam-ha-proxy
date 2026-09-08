import queue
import subprocess
import threading
import time

import pytest

from okam_native.p2p import P2PError
from okam_native.session import NativeStreamSession


class BlockingPipe:
    def __init__(self) -> None:
        self.chunks: queue.Queue[bytes | None] = queue.Queue()

    def read(self, _size: int = -1) -> bytes:
        value = self.chunks.get(timeout=3)
        return b"" if value is None else value


class FakeProcess:
    def __init__(self) -> None:
        self.stdout = BlockingPipe()
        self.stderr = BlockingPipe()
        self._done = threading.Event()
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.returncode = 0
        self.stderr.chunks.put(b'{"disconnected":true}\n')
        self.stderr.chunks.put(None)
        self.stdout.chunks.put(None)
        self._done.set()

    def kill(self) -> None:
        self.terminate()

    def wait(self, timeout=None):
        if not self._done.wait(timeout):
            raise subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def test_session_default_keeps_camera_warm_for_two_minutes() -> None:
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    assert session.idle_timeout == 120.0


def test_session_reuses_process_and_stops_after_last_viewer() -> None:
    process = FakeProcess()
    starts = []

    def start():
        starts.append(True)
        return process

    session = NativeStreamSession(start, idle_timeout=0.02)  # type: ignore[arg-type]
    first = session.acquire()
    second = session.acquire()
    assert len(starts) == 1
    assert session.status().viewers == 2
    assert session.status().media_ready is False

    process.stdout.chunks.put(b"\x00\x00\x00\x01h264")
    deadline = time.monotonic() + 2
    while not session.status().media_ready and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.status().media_ready is True

    first.close()
    assert process.poll() is None
    second.close()
    deadline = time.monotonic() + 2
    while session.status().running and time.monotonic() < deadline:
        time.sleep(0.01)

    assert process.poll() == 0
    deadline = time.monotonic() + 2
    while session.status().clean_disconnect is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.status().clean_disconnect is True


def test_simultaneous_active_acquires_share_one_start_operation() -> None:
    entered = threading.Event()
    release = threading.Event()
    starts = []
    process = FakeProcess()

    def start():
        starts.append(True)
        entered.set()
        assert release.wait(2)
        return process

    session = NativeStreamSession(start)  # type: ignore[arg-type]
    subscriptions = []
    first = threading.Thread(target=lambda: subscriptions.append(session.acquire()))
    second = threading.Thread(target=lambda: subscriptions.append(session.acquire()))
    first.start()
    assert entered.wait(2)
    second.start()
    time.sleep(0.05)
    assert len(starts) == 1
    release.set()
    first.join(timeout=2)
    second.join(timeout=2)
    assert not first.is_alive() and not second.is_alive()
    assert len(starts) == 1
    assert session.status().viewers == 2
    for subscription in subscriptions:
        subscription.close()
    session.close()


def test_session_lifecycle_logs_identify_camera_and_process_generation() -> None:
    lines: list[str] = []
    process = FakeProcess()
    session = NativeStreamSession(
        lambda: process,
        camera_uid="UID_FRONT",
        transport_uid="transport-front",
        logger=lines.append,
    )  # type: ignore[arg-type]
    subscription = session.acquire(reason="ha_live")
    subscription.close()
    session.close()
    assert any(
        "native_session_start camera_uid=UID_FRONT transport_uid=transport-front" in line
        and "session_generation=1" in line
        and "start_reason=ha_live" in line
        and "active_consumers=0 passive_consumers=0" in line
        for line in lines
    )
    assert any("native_session_process" in line and "session_generation=1" in line for line in lines)


def test_session_diagnostics_identify_live_boundary_without_credentials() -> None:
    lines: list[str] = []
    process = FakeProcess()
    session = NativeStreamSession(
        lambda: process,
        camera_uid="UID_FRONT",
        logger=lines.append,
    )  # type: ignore[arg-type]
    subscription = session.acquire(reason="ha_live")
    process.stdout.chunks.put(
        _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"idr") + _unit(1, b"frame")
    )
    deadline = time.monotonic() + 2
    while not session.status().media_ready and time.monotonic() < deadline:
        time.sleep(0.01)
    subscription.close()
    session.close()

    diagnostics = "\n".join(line for line in lines if line.startswith("native_diag "))
    assert "event=session_lock_acquired" in diagnostics
    assert "event=first_helper_stdout_chunk" in diagnostics
    assert "event=first_h264_sps" in diagnostics
    assert "event=first_h264_pps" in diagnostics
    assert "event=first_h264_idr" in diagnostics
    assert "camera_uid=UID_FRONT" in diagnostics
    assert "session_generation=1" in diagnostics
    assert "password" not in diagnostics.lower()


def test_transport_failure_returns_to_retryable_state_without_overlap() -> None:
    starts = []
    process = FakeProcess()

    def start():
        starts.append(True)
        if len(starts) == 1:
            raise P2PError("transport unavailable")
        return process

    session = NativeStreamSession(start)  # type: ignore[arg-type]
    with pytest.raises(P2PError):
        session.acquire(reason="ha_live")
    assert session.status().state == "FAILED"
    subscription = session.acquire(reason="retry")
    try:
        assert len(starts) == 2
        assert session.status().state == "CONNECTED"
    finally:
        subscription.close()
        session.close()


def test_clean_disconnect_allows_a_fresh_reconnect_generation() -> None:
    processes = [FakeProcess(), FakeProcess()]
    starts = []

    def start():
        starts.append(True)
        return processes[len(starts) - 1]

    session = NativeStreamSession(start)  # type: ignore[arg-type]
    first = session.acquire(reason="ha_live")
    processes[0].terminate()
    deadline = time.monotonic() + 2
    while session.status().running and time.monotonic() < deadline:
        time.sleep(0.01)
    first.close()
    second = session.acquire(reason="recovery")
    try:
        assert len(starts) == 2
        assert session.status().state == "CONNECTED"
    finally:
        second.close()
        session.close()


def _unit(kind: int, body: bytes = b"\x00") -> bytes:
    return b"\x00\x00\x01" + bytes([kind]) + body


def test_new_viewer_starts_on_a_decodable_boundary() -> None:
    # Without a cached keyframe a viewer waits for the camera's next one,
    # which is the entire delay when opening a live view.
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    sps, pps, idr = _unit(7, b"sps"), _unit(8, b"pps"), _unit(5, b"idr")
    session._note_media(sps + pps + idr + _unit(1, b"inter") + _unit(1, b"tail"))

    assert session._preamble() == sps + pps + idr


def test_media_units_are_reassembled_across_chunk_boundaries() -> None:
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    stream = _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"idr") + _unit(1, b"x")
    for index in range(0, len(stream), 3):
        session._note_media(stream[index : index + 3])

    assert session._preamble() == _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"idr")


def test_only_the_newest_keyframe_is_kept() -> None:
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    session._note_media(_unit(7) + _unit(8) + _unit(5, b"old") + _unit(1))
    session._note_media(_unit(5, b"new") + _unit(1) + _unit(1))

    assert session._preamble().endswith(_unit(5, b"new"))
    assert b"old" not in session._preamble()


def test_later_viewer_receives_the_preamble_before_live_media() -> None:
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    first = session.acquire()
    # Media observed while the first viewer is watching.
    session._note_media(_unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"idr") + _unit(1))
    second = session.acquire()
    try:
        assert next(iter(second)) == session._preamble()
    finally:
        second.close()
        first.close()
        session.close()


def test_restarting_the_helper_discards_stale_media_units() -> None:
    # A new helper means a new encoder state, so a cached keyframe from the
    # previous session must not be handed to the next viewer.
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    session._note_media(_unit(7) + _unit(8) + _unit(5, b"idr") + _unit(1))
    assert session._preamble() != b""

    subscription = session.acquire()
    try:
        assert session._preamble() == b""
    finally:
        subscription.close()
        session.close()


def test_no_preamble_is_sent_before_parameter_sets_are_seen() -> None:
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    session._note_media(_unit(1, b"inter") + _unit(1, b"more"))

    assert session._preamble() == b""


def test_passive_subscription_never_starts_native_helper_and_emits_standby() -> None:
    starts = []
    frame = _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"standby")
    session = NativeStreamSession(
        lambda: (starts.append(True) or FakeProcess()),
        standby_frame=frame,
        standby_interval=0.01,
    )  # type: ignore[arg-type]
    subscription = session.acquire(passive=True)
    try:
        assert starts == []
        assert session.status().standby is True
        assert next(iter(subscription)) == frame
    finally:
        subscription.close()
        session.close()


def test_active_subscription_promotes_shared_passive_session_and_cleanup_restores_standby() -> None:
    processes: list[FakeProcess] = []
    frame = _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"standby")

    def start() -> FakeProcess:
        process = FakeProcess()
        processes.append(process)
        return process

    session = NativeStreamSession(start, idle_timeout=0.02, standby_frame=frame, standby_interval=0.01)  # type: ignore[arg-type]
    passive = session.acquire(passive=True)
    active = session.acquire()
    assert len(processes) == 1
    assert session.status().standby is False
    active.close()
    deadline = time.monotonic() + 2
    while processes[0].poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert processes[0].poll() == 0
    assert session.status().standby is True
    passive.close()
    session.close()


def test_native_process_end_keeps_passive_subscriber_alive() -> None:
    process = FakeProcess()
    frame = _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"standby")
    session = NativeStreamSession(lambda: process, idle_timeout=0.02, standby_frame=frame, standby_interval=0.01)  # type: ignore[arg-type]
    passive = session.acquire(passive=True)
    active = session.acquire()
    process.stdout.chunks.put(b"live")
    process.stdout.chunks.put(None)
    active.close()
    deadline = time.monotonic() + 2
    while not session.status().standby and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.status().standby is True
    passive.close()
    session.close()
