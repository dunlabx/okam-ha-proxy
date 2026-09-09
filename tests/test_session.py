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


def test_separate_camera_sessions_start_concurrently() -> None:
    entered_a = threading.Event()
    entered_b = threading.Event()
    release = threading.Event()

    def starter(entered: threading.Event) -> FakeProcess:
        entered.set()
        assert release.wait(2)
        return FakeProcess()

    first = NativeStreamSession(lambda: starter(entered_a))  # type: ignore[arg-type]
    second = NativeStreamSession(lambda: starter(entered_b))  # type: ignore[arg-type]
    results: list[object] = []
    threads = [
        threading.Thread(target=lambda: results.append(first.acquire())),
        threading.Thread(target=lambda: results.append(second.acquire())),
    ]
    for thread in threads:
        thread.start()
    assert entered_a.wait(2) and entered_b.wait(2)
    release.set()
    for thread in threads:
        thread.join(timeout=2)
    assert len(results) == 2
    for subscription in results:
        subscription.close()  # type: ignore[union-attr]
    first.close()
    second.close()


def test_status_remains_responsive_while_startup_owner_is_blocked() -> None:
    entered = threading.Event()
    release = threading.Event()

    def start() -> FakeProcess:
        entered.set()
        assert release.wait(2)
        return FakeProcess()

    session = NativeStreamSession(start)  # type: ignore[arg-type]
    result: list[object] = []
    owner = threading.Thread(target=lambda: result.append(session.acquire()))
    owner.start()
    assert entered.wait(2)
    started = time.monotonic()
    status = session.status()
    assert time.monotonic() - started < 0.1
    assert status.state == "AUTHENTICATING"
    release.set()
    owner.join(timeout=2)
    assert len(result) == 1
    result[0].close()  # type: ignore[union-attr]
    session.close()


def test_passive_acquire_during_startup_receives_standby_without_waiting() -> None:
    entered = threading.Event()
    release = threading.Event()
    frame = _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"standby")

    def start() -> FakeProcess:
        entered.set()
        assert release.wait(2)
        return FakeProcess()

    session = NativeStreamSession(start, standby_frame=frame)  # type: ignore[arg-type]
    result: list[object] = []
    owner = threading.Thread(target=lambda: result.append(session.acquire()))
    owner.start()
    assert entered.wait(2)
    passive = session.acquire(passive=True)
    assert session.status().viewers == 1
    passive.close()
    release.set()
    owner.join(timeout=2)
    assert len(result) == 1
    result[0].close()  # type: ignore[union-attr]
    session.close()


def test_passive_subscriber_survives_startup_failure() -> None:
    frame = _unit(7, b"sps") + _unit(8, b"pps") + _unit(5, b"standby")
    entered = threading.Event()

    def start() -> FakeProcess:
        entered.set()
        raise P2PError("startup failed")

    session = NativeStreamSession(start, standby_frame=frame)  # type: ignore[arg-type]
    owner_result: list[object] = []
    owner = threading.Thread(target=lambda: owner_result.append(_capture(session.acquire)))
    owner.start()
    assert entered.wait(2)
    passive = session.acquire(passive=True)
    owner.join(timeout=2)
    assert isinstance(owner_result[0], P2PError)
    assert session.status().standby is True
    passive.close()
    session.close()


def test_startup_failure_outcome_is_stable_for_waiter_before_next_generation() -> None:
    first_started = threading.Event()
    fail = threading.Event()
    starts: list[int] = []
    first = FakeProcess()
    second = FakeProcess()

    def start() -> FakeProcess:
        starts.append(len(starts) + 1)
        if len(starts) == 1:
            first_started.set()
            assert fail.wait(2)
            raise P2PError("generation one failed")
        return second

    session = NativeStreamSession(start)  # type: ignore[arg-type]
    owner_result: list[object] = []
    waiter_result: list[object] = []
    owner = threading.Thread(target=lambda: owner_result.append(_capture(session.acquire)))
    owner.start()
    assert first_started.wait(2)
    operation = session._startup_operation
    assert operation is not None

    class DelayedEvent:
        def __init__(self) -> None:
            self.ready = threading.Event()
            self.release = threading.Event()
            self.wait_started = threading.Event()

        def set(self) -> None:
            self.ready.set()

        def wait(self) -> bool:
            self.wait_started.set()
            self.ready.wait(2)
            return self.release.wait(2)

    delayed = DelayedEvent()
    operation.done = delayed  # type: ignore[assignment]
    waiter = threading.Thread(target=lambda: waiter_result.append(_capture(session.acquire)))
    waiter.start()
    assert delayed.wait_started.wait(2)
    fail.set()
    owner.join(timeout=2)
    assert isinstance(owner_result[0], P2PError)
    third = session.acquire()
    assert starts == [1, 2]
    delayed.release.set()
    waiter.join(timeout=2)
    assert isinstance(waiter_result[0], P2PError)
    third.close()
    session.close()


def test_deferred_activation_survives_cleanup_and_runs_once() -> None:
    old = FakeProcess()
    new = FakeProcess()
    starts = 0

    def start() -> FakeProcess:
        nonlocal starts
        starts += 1
        return old if starts == 1 else new

    session = NativeStreamSession(start, idle_timeout=10)  # type: ignore[arg-type]
    active = session.acquire()
    with session._lock:
        session._set_state_locked("STOPPING")
    called = threading.Event()

    def deferred() -> None:
        subscription = session.acquire(reason="arp_wake", wake_before_connect=False)
        subscription.close()
        called.set()

    assert session.defer_active_after_cleanup(deferred) is True
    old.terminate()
    active.close()
    assert called.wait(2)
    assert starts == 2
    session.close()


def test_final_kill_wait_timeout_is_reported_without_raising() -> None:
    lines: list[str] = []

    class NeverReaps:
        pid = 123

        def poll(self):
            return None

        def terminate(self):
            pass

        def kill(self):
            pass

        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("never-reaps", timeout)

    session = NativeStreamSession(lambda: FakeProcess(), logger=lines.append)  # type: ignore[arg-type]
    session._terminate(NeverReaps())  # type: ignore[arg-type]
    assert any("native_process_reap_timeout process_pid=123" in line for line in lines)
    session.close()


def test_shutdown_wakes_waiters_during_startup_without_publishing_process() -> None:
    entered = threading.Event()
    release = threading.Event()

    def start() -> FakeProcess:
        entered.set()
        release.wait(2)
        return FakeProcess()

    session = NativeStreamSession(start)  # type: ignore[arg-type]
    owner_result: list[object] = []
    waiter_result: list[object] = []
    owner = threading.Thread(target=lambda: owner_result.append(_capture(session.acquire)))
    owner.start()
    assert entered.wait(2)
    waiter = threading.Thread(target=lambda: waiter_result.append(_capture(session.acquire)))
    waiter.start()
    session.close()
    waiter.join(timeout=1)
    assert not waiter.is_alive()
    assert isinstance(waiter_result[0], P2PError)
    release.set()
    owner.join(timeout=2)
    assert not owner.is_alive()
    assert isinstance(owner_result[0], P2PError)


def _capture(call):
    try:
        return call()
    except Exception as error:  # pragma: no cover - assertion helper
        return error


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

    diagnostics = "\n".join(line for line in lines if "native_diag " in line)
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


def test_reconnect_waits_for_previous_helper_cleanup() -> None:
    cleanup_gate = threading.Event()

    class DelayedCleanupProcess(FakeProcess):
        def __init__(self) -> None:
            super().__init__()
            self.stderr = self.GatedPipe(cleanup_gate)

        class GatedPipe(BlockingPipe):
            def __init__(self, gate: threading.Event) -> None:
                super().__init__()
                self._gate = gate

            def read(self, _size: int = -1) -> bytes:
                self._gate.wait(2)
                return b""

    processes = [DelayedCleanupProcess(), FakeProcess()]
    starts: list[bool] = []

    def start() -> FakeProcess:
        starts.append(True)
        return processes[len(starts) - 1]

    session = NativeStreamSession(start)  # type: ignore[arg-type]
    first = session.acquire()
    processes[0].terminate()
    deadline = time.monotonic() + 1
    while processes[0].poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    second_result: list[object] = []
    waiter = threading.Thread(target=lambda: second_result.append(session.acquire()))
    waiter.start()
    time.sleep(0.05)
    assert len(starts) == 1
    assert waiter.is_alive()
    cleanup_gate.set()
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert len(starts) == 2
    first.close()
    second = second_result[0]
    assert hasattr(second, "close")
    second.close()  # type: ignore[union-attr]
    session.close()


def test_acquire_during_stopping_waits_then_starts_one_pending_generation():
    class SlowTerminationProcess(FakeProcess):
        def terminate(self) -> None:
            self.returncode = 0
            self.stderr.chunks.put(b'{"disconnected":true}\n')
            self.stderr.chunks.put(None)
            self._done.set()

    first = SlowTerminationProcess()
    second = FakeProcess()
    processes = [first, second]
    starts: list[bool] = []

    def start() -> FakeProcess:
        starts.append(True)
        return processes[len(starts) - 1]

    session = NativeStreamSession(start, idle_timeout=0.01)  # type: ignore[arg-type]
    active = session.acquire()
    active.close()
    deadline = time.monotonic() + 1
    while session.status().state != "STOPPING" and time.monotonic() < deadline:
        time.sleep(0.01)
    assert session.status().state == "STOPPING"
    result: list[object] = []
    waiter = threading.Thread(target=lambda: result.append(session.acquire(reason="arp_wake")))
    waiter.start()
    time.sleep(0.03)
    assert waiter.is_alive()
    first.stdout.chunks.put(None)
    waiter.join(timeout=2)
    assert not waiter.is_alive()
    assert len(starts) == 2
    result[0].close()  # type: ignore[union-attr]
    session.close()


def test_stale_idle_timer_cannot_terminate_newer_process():
    old = FakeProcess()
    current = FakeProcess()
    session = NativeStreamSession(lambda: current)  # type: ignore[arg-type]
    with session._lock:
        session._process = current
        session._process_generation = 2
    session._stop_if_idle(1, old)  # type: ignore[arg-type]
    assert current.poll() is None
    session.close()


def test_stale_pump_cleanup_cannot_mutate_newer_generation():
    old = FakeProcess()
    old.terminate()
    current = FakeProcess()
    session = NativeStreamSession(lambda: current)  # type: ignore[arg-type]
    with session._lock:
        session._process = current
        session._process_generation = 2
        session._state = "CONNECTED"
    old.stdout.chunks.put(None)
    class NoJoin:
        def join(self, timeout=None):
            pass

    session._pump(old, NoJoin(), 1, threading.Event())  # type: ignore[arg-type]
    assert session._process is current
    assert session._process_generation == 2
    session.close()


def test_shutdown_discards_deferred_activation():
    session = NativeStreamSession(lambda: FakeProcess())  # type: ignore[arg-type]
    with session._lock:
        session._state = "STOPPING"
    called = threading.Event()
    assert session.defer_active_after_cleanup(called.set) is True
    session.close()
    assert not called.is_set()


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
