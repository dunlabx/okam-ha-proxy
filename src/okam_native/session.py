"""Reference-counted on-demand native H.264 stream session."""

from __future__ import annotations

import json
import queue
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .p2p import MAX_RESPONSE_BYTES, P2PError, _jpeg_dimensions
from .logging import PROCESS_ID


StreamStarter = Callable[[], subprocess.Popen[bytes]]
_END = object()
ANNEX_B_START = b"\x00\x00\x01"
# A keyframe access unit plus its parameter sets. Anything larger is treated as
# unparsable rather than buffered indefinitely.
MAX_PREAMBLE_BYTES = 1024 * 1024
MAX_SCAN_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class SessionStatus:
    running: bool
    viewers: int
    clean_disconnect: bool | None
    last_error: str | None
    media_ready: bool = False
    standby: bool = False
    state: str = "IDLE"


class StreamSubscription:
    def __init__(
        self,
        owner: "NativeStreamSession",
        subscription_id: str,
        chunks: queue.Queue[bytes | object],
    ) -> None:
        self._owner = owner
        self._subscription_id = subscription_id
        self._chunks = chunks
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        while not self._closed:
            chunk = self._chunks.get()
            if chunk is _END:
                return
            assert isinstance(chunk, bytes)
            yield chunk

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            try:
                self._chunks.put_nowait(_END)
            except queue.Full:
                try:
                    self._chunks.get_nowait()
                    self._chunks.put_nowait(_END)
                except (queue.Empty, queue.Full):
                    pass
            self._owner.release(self._subscription_id)


class _StartupOperation:
    """Stable outcome shared by every waiter for one startup generation."""

    def __init__(self, generation: int, token: object) -> None:
        self.generation = generation
        self.token = token
        self.done = threading.Event()
        self.outcome: str | None = None
        self.error: BaseException | None = None


class NativeStreamSession:
    def __init__(
        self,
        starter: StreamStarter,
        *,
        idle_timeout: float = 120.0,
        standby_frame: bytes | None = None,
        standby_interval: float = 1.0,
        camera_uid: str | None = None,
        transport_uid: str | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self._starter = starter
        self._camera_uid = camera_uid
        self._transport_uid = transport_uid
        self._logger = logger
        self._session_id = uuid.uuid4().hex
        self._diagnostic_started = time.monotonic()
        self._session_generation = 0
        self._state = "IDLE"
        self.idle_timeout = idle_timeout
        self._lock = threading.RLock()
        self._startup_condition = threading.Condition(self._lock)
        self._startup_token: object | None = None
        self._startup_operation: _StartupOperation | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._process_generation: int | None = None
        self._cleanup_complete = threading.Event()
        self._cleanup_complete.set()
        self._subscribers: dict[str, tuple[queue.Queue[bytes | object], bool]] = {}
        self._idle_timer: threading.Timer | None = None
        self._stderr = bytearray()
        self._clean_disconnect: bool | None = None
        self._last_error: str | None = None
        self._media_ready = False
        self._scan = bytearray()
        self._sps = b""
        self._pps = b""
        self._keyframe = b""
        self._standby_frame = standby_frame or b""
        self._standby_interval = max(0.1, standby_interval)
        self._standby_thread: threading.Thread | None = None
        self._closed = False
        self._wake_before_connect = True
        self._helper_stdout_bytes = 0
        self._helper_stdout_chunks = 0
        self._h264_frame_count = 0
        self._diagnostic_events: set[str] = set()
        self._deferred_activation: Callable[[], None] | None = None
        if self._standby_frame:
            self._note_media(self._standby_frame + ANNEX_B_START)
            self._standby_media = (self._sps, self._pps, self._keyframe)
            # Standby media is a synthetic placeholder.  Do not let it consume
            # the first-live SPS/PPS/IDR diagnostics or frame counters.
            self._h264_frame_count = 0
            self._diagnostic_events.clear()
        else:
            self._standby_media = (b"", b"", b"")

    def acquire(
        self,
        *,
        passive: bool = False,
        reason: str = "active",
        wake_before_connect: bool = True,
    ) -> StreamSubscription:
        wait_begin = time.monotonic()
        self._diagnostic("session_lock_wait_begin", reason=reason, passive=passive)
        while True:
            cleanup_wait: threading.Event | None = None
            wait_generation: int | None = None
            wait_operation: _StartupOperation | None = None
            startup_generation: int | None = None
            startup_token: object | None = None
            startup_operation: _StartupOperation | None = None
            with self._lock:
                lock_acquired = time.monotonic()
                self._diagnostic(
                    "session_lock_acquired",
                    reason=reason,
                    passive=passive,
                    session_lock_wait_ms=round((lock_acquired - wait_begin) * 1000, 1),
                )
                if self._closed:
                    raise P2PError("native stream session is closed")
                process = self._process
                if not passive and self._state == "STOPPING":
                    # A wake arriving during termination belongs to the next
                    # generation; wait for the current cleanup rather than
                    # attaching to a process that is being stopped.
                    cleanup_wait = self._cleanup_complete
                elif process is not None and process.poll() is not None:
                    # The helper has exited, but its pump still owns the
                    # final disconnect/standby cleanup. Never overlap the
                    # next native generation with that teardown.
                    cleanup_wait = self._cleanup_complete
                else:
                    if self._idle_timer is not None:
                        self._idle_timer.cancel()
                        self._idle_timer = None
                    if self._startup_token is not None:
                        if passive:
                            return self._subscribe_locked(passive=True)
                        wait_generation = self._session_generation
                        wait_operation = self._startup_operation
                    elif not passive and process is None:
                        self._wake_before_connect = wake_before_connect
                        start_begin = time.monotonic()
                        self._diagnostic("session_start_begin", reason=reason)
                        startup_generation = self._session_generation + 1
                        startup_token = object()
                        self._session_generation = startup_generation
                        self._startup_token = startup_token
                        startup_operation = _StartupOperation(
                            startup_generation, startup_token
                        )
                        self._startup_operation = startup_operation
                        self._prepare_start_locked(reason)
                    elif passive:
                        return self._subscribe_locked(passive=True)
                    else:
                        return self._subscribe_locked(passive=False)
            if wait_generation is not None:
                if wait_operation is None:
                    raise P2PError("native stream startup operation unavailable")
                wait_operation.done.wait()
                if wait_operation.outcome == "failure":
                    if isinstance(wait_operation.error, BaseException):
                        raise wait_operation.error
                    raise P2PError("native stream startup failed")
                if wait_operation.outcome in {"closed", "superseded"}:
                    raise P2PError("native stream startup was superseded")
                if wait_operation.outcome != "success":
                    raise P2PError("native stream startup had no outcome")
                with self._lock:
                    if (
                        self._closed
                        or self._process is None
                        or self._process_generation != wait_generation
                    ):
                        raise P2PError("native stream startup generation ended")
                    return self._subscribe_locked(passive=False)
            if cleanup_wait is not None:
                if not cleanup_wait.wait(timeout=15.0):
                    raise P2PError("native stream cleanup did not complete")
                continue
            if startup_generation is not None and startup_token is not None:
                try:
                    process = self._starter()
                except Exception as error:
                    self._publish_start_failure(startup_generation, startup_token, error)
                    raise
                stale = self._publish_start_success(
                    startup_generation, startup_token, process, reason, start_begin
                )
                if stale:
                    self._terminate(process)
                    with self._lock:
                        if startup_operation is not None and isinstance(
                            startup_operation.error, BaseException
                        ):
                            raise startup_operation.error
                    raise P2PError("native stream startup was superseded")
                return self._subscribe_after_start(startup_generation)

    def release(self, subscription_id: str) -> None:
        with self._lock:
            self._subscribers.pop(subscription_id, None)
            if (
                not self._active_subscribers_locked()
                and self._process is not None
                and not self._closed
            ):
                if self._idle_timer is not None:
                    self._idle_timer.cancel()
                generation = self._process_generation
                self._idle_timer = threading.Timer(
                    self.idle_timeout, self._stop_if_idle, args=(generation, self._process)
                )
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def _active_subscribers_locked(self) -> bool:
        return any(not passive for _chunks, passive in self._subscribers.values())

    def _subscribe_locked(self, *, passive: bool) -> StreamSubscription:
        subscription_id = uuid.uuid4().hex
        chunks: queue.Queue[bytes | object] = queue.Queue(maxsize=32)
        preamble = self._preamble()
        if preamble:
            chunks.put_nowait(preamble)
        self._subscribers[subscription_id] = (chunks, passive)
        if passive and self._process is None:
            if self._state != "STARTING":
                self._set_state_locked("STANDBY")
            self._put_chunk(chunks, self._standby_frame)
            self._ensure_standby_thread_locked()
        return StreamSubscription(self, subscription_id, chunks)

    def _subscribe_after_start(self, generation: int) -> StreamSubscription:
        with self._lock:
            if self._closed or self._process_generation != generation:
                raise P2PError("native stream session is closed")
            return self._subscribe_locked(passive=False)

    def _active_count_locked(self) -> int:
        return sum(not passive for _chunks, passive in self._subscribers.values())

    def _passive_count_locked(self) -> int:
        return sum(passive for _chunks, passive in self._subscribers.values())

    def _emit(self, message: str) -> None:
        if self._logger is not None:
            self._logger(f"process_id={PROCESS_ID} {message}")

    def diagnostic(self, event: str, **fields: object) -> None:
        """Emit a credential-free diagnostic event for an outer protocol layer."""
        with self._lock:
            self._diagnostic(event, **fields)

    def _diagnostic(self, event: str, **fields: object) -> None:
        repeatable = {
            "session_lock_wait_begin",
            "session_lock_acquired",
            "stream_http_request_received",
            "active_acquire_begin",
            "active_acquire_complete",
            "http_headers_sent",
            "request_total_complete",
            "muxer_start",
            "muxer_started",
            "muxer_exit",
        }
        if event in self._diagnostic_events and event not in repeatable:
            return
        if event not in repeatable:
            self._diagnostic_events.add(event)
        values = {
            "event": event,
            "process_id": PROCESS_ID,
            "camera_uid": self._camera_uid or "-",
            "session_id": self._session_id,
            "session_generation": self._session_generation,
            "elapsed_ms": round((time.monotonic() - self._diagnostic_started) * 1000, 1),
        }
        values.update(fields)
        self._emit("native_diag " + " ".join(f"{key}={value}" for key, value in values.items()))

    def _set_state_locked(self, state: str) -> None:
        previous = self._state
        self._state = state
        if previous != state:
            self._diagnostic(
                "state_transition",
                from_state=previous,
                to_state=state,
                media_ready=self._media_ready,
                process_running=str(self._process is not None and self._process.poll() is None).lower(),
                active_consumers=self._active_count_locked(),
                passive_consumers=self._passive_count_locked(),
            )

    def _ensure_standby_thread_locked(self) -> None:
        if not self._standby_frame or self._closed:
            return
        if self._standby_thread is None or not self._standby_thread.is_alive():
            self._standby_thread = threading.Thread(
                target=self._standby_loop, name="okam-standby", daemon=True
            )
            self._standby_thread.start()

    def snapshot(self, ffmpeg: str, *, timeout: float = 90.0) -> tuple[bytes, int, int]:
        subscription = self.acquire(reason="bridge_snapshot")
        decoder: subprocess.Popen[bytes] | None = None
        writer: threading.Thread | None = None
        try:
            decoder = subprocess.Popen(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "h264",
                    "-i",
                    "pipe:0",
                    "-frames:v",
                    "1",
                    "-f",
                    "image2pipe",
                    "-c:v",
                    "mjpeg",
                    "-q:v",
                    "3",
                    "pipe:1",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert decoder.stdin is not None
            decoder_input = decoder.stdin
            decoder.stdin = None

            def feed() -> None:
                try:
                    for chunk in subscription:
                        decoder_input.write(chunk)
                except (BrokenPipeError, OSError):
                    pass
                finally:
                    try:
                        decoder_input.close()
                    except OSError:
                        pass

            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            jpeg, _stderr = decoder.communicate(timeout=timeout)
            if decoder.returncode != 0:
                raise P2PError("native on-demand snapshot decoder failed")
            width, height = _jpeg_dimensions(jpeg)
            return jpeg, width, height
        except P2PError:
            raise
        except (OSError, subprocess.SubprocessError):
            raise P2PError("native on-demand snapshot failed") from None
        finally:
            subscription.close()
            if decoder is not None and decoder.poll() is None:
                decoder.kill()
                try:
                    decoder.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._emit(
                        "native_snapshot_decoder_reap_timeout "
                        f"process_pid={getattr(decoder, 'pid', None) or '-'}",
                        flush=True,
                    )
            if writer is not None:
                writer.join(timeout=5)

    def _note_media(self, chunk: bytes) -> None:
        """Remember the parameter sets and newest keyframe from the stream.

        Chunks are arbitrary reads rather than NAL-aligned, so units are
        reassembled across boundaries before being classified.
        """

        scan = self._scan
        scan.extend(chunk)
        if len(scan) > MAX_SCAN_BYTES:
            del scan[: len(scan) - MAX_SCAN_BYTES]
        starts = []
        position = scan.find(ANNEX_B_START)
        while position >= 0:
            starts.append(position)
            position = scan.find(ANNEX_B_START, position + len(ANNEX_B_START))
        if len(starts) < 2:
            return
        for begin, end in zip(starts, starts[1:]):
            self._record_unit(bytes(scan[begin:end]))
        # Keep the trailing unit: it is still incomplete.
        del scan[: starts[-1]]

    def _record_unit(self, unit: bytes) -> None:
        if len(unit) <= len(ANNEX_B_START) or len(unit) > MAX_PREAMBLE_BYTES:
            return
        prefix = 4 if unit.startswith(b"\x00\x00\x00\x01") else len(ANNEX_B_START)
        if len(unit) <= prefix:
            return
        kind = unit[prefix] & 0x1F
        if kind == 7:
            self._sps = unit
            self._diagnostic("first_h264_sps", bytes=len(unit))
        elif kind == 8:
            self._pps = unit
            self._diagnostic("first_h264_pps", bytes=len(unit))
        elif kind == 5:
            self._keyframe = unit
            self._h264_frame_count += 1
            self._diagnostic("first_h264_idr", bytes=len(unit))
        elif kind == 1:
            self._h264_frame_count += 1
        if kind in (1, 5) and (
            self._h264_frame_count == 1 or self._h264_frame_count % 100 == 0
        ):
            self._diagnostic("h264_frame_progress", h264_frame_count=self._h264_frame_count)

    def _preamble(self) -> bytes:
        """The bytes a new viewer needs before live data makes sense."""

        if not self._sps or not self._pps:
            return b""
        preamble = self._sps + self._pps + self._keyframe
        return preamble if len(preamble) <= MAX_PREAMBLE_BYTES else b""

    def status(self) -> SessionStatus:
        with self._lock:
            running = self._process is not None and self._process.poll() is None
            return SessionStatus(
                running=running,
                viewers=len(self._subscribers),
                clean_disconnect=self._clean_disconnect,
                last_error=self._last_error,
                media_ready=self._media_ready,
                standby=(
                    not running
                    and any(passive for _chunks, passive in self._subscribers.values())
                ),
                state=self._state,
            )

    def parameter_sets(self) -> tuple[bytes, bytes]:
        """Return the latest SPS/PPS, if a stream has already started."""

        with self._lock:
            return self._sps, self._pps

    def close(self) -> None:
        with self._lock:
            self._closed = True
            operation = self._startup_operation
            if operation is not None and operation.outcome is None:
                operation.outcome = "closed"
                operation.done.set()
            self._startup_token = None
            self._startup_operation = None
            self._deferred_activation = None
            self._startup_condition.notify_all()
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            process = self._process
            if process is not None and process.poll() is None:
                self._set_state_locked("STOPPING")
            subscribers = tuple(chunks for chunks, _passive in self._subscribers.values())
            self._subscribers.clear()
        for chunks in subscribers:
            self._put_chunk(chunks, _END)
        self._terminate(process)

    def _prepare_start_locked(self, reason: str) -> None:
        generation = self._session_generation
        state_before = self._state
        self._set_state_locked("STARTING")
        self._diagnostic("native_start_begin", reason=reason)
        self._emit(
            "native_session_start "
            f"camera_uid={self._camera_uid or '-'} "
            f"transport_uid={self._transport_uid or '-'} "
            f"session_id={self._session_id} session_generation={generation} "
            f"start_reason={reason} state_before={state_before} state_after=STARTING "
            f"active_consumers={self._active_count_locked()} "
            f"passive_consumers={self._passive_count_locked()}"
        )
        self._stderr.clear()
        self._clean_disconnect = None
        self._last_error = None
        self._media_ready = False
        # A new helper means a new encoder state, so cached units are stale.
        self._scan.clear()
        self._sps = self._pps = self._keyframe = b""
        self._helper_stdout_bytes = 0
        self._helper_stdout_chunks = 0
        self._h264_frame_count = 0
        self._diagnostic_events.clear()
        self._set_state_locked("AUTHENTICATING")
        self._emit(
            "native_session_auth "
            f"camera_uid={self._camera_uid or '-'} transport_uid={self._transport_uid or '-'} "
            f"session_id={self._session_id} session_generation={generation} "
            f"start_reason={reason} state_before=STARTING state_after=AUTHENTICATING"
        )
        self._diagnostic("native_start_unlocked", reason=reason)

    def _publish_start_failure(
        self, generation: int, token: object, error: BaseException
    ) -> None:
        with self._startup_condition:
            if self._startup_token is not token or self._session_generation != generation:
                self._startup_condition.notify_all()
                return
            operation = self._startup_operation
            if operation is not None and operation.generation == generation:
                operation.error = error
                operation.outcome = "failure"
                operation.done.set()
            self._startup_token = None
            self._startup_operation = None
            if self._passive_count_locked() and self._standby_frame:
                self._restore_standby_media_locked()
                self._set_state_locked("STANDBY")
                self._ensure_standby_thread_locked()
            else:
                self._set_state_locked("FAILED")
            self._diagnostic(
                "camera_live_failed",
                failure_stage="native_start",
                exception_class=type(error).__name__,
                exception_message="redacted",
            )
            self._startup_condition.notify_all()

    def _publish_start_success(
        self,
        generation: int,
        token: object,
        process: subprocess.Popen[bytes],
        reason: str,
        start_begin: float,
    ) -> bool:
        with self._startup_condition:
            if (
                self._closed
                or self._startup_token is not token
                or self._session_generation != generation
            ):
                self._startup_condition.notify_all()
                operation = self._startup_operation
                if operation is not None and operation.generation == generation:
                    operation.outcome = "closed" if self._closed else "superseded"
                    operation.done.set()
                return True
            if process.stdout is None or process.stderr is None:
                operation = self._startup_operation
                error = P2PError("native stream helper pipes are unavailable")
                if operation is not None and operation.generation == generation:
                    operation.error = error
                    operation.outcome = "failure"
                    operation.done.set()
                self._startup_token = None
                self._startup_operation = None
                if self._passive_count_locked() and self._standby_frame:
                    self._restore_standby_media_locked()
                    self._set_state_locked("STANDBY")
                    self._ensure_standby_thread_locked()
                else:
                    self._set_state_locked("FAILED")
                self._startup_condition.notify_all()
                return True
            self._process = process
            self._process_generation = generation
            self._cleanup_complete = threading.Event()
            self._set_state_locked("CONNECTED")
            self._startup_token = None
            operation = self._startup_operation
            self._startup_operation = None
            if operation is not None and operation.generation == generation:
                operation.outcome = "success"
                operation.done.set()
            self._diagnostic(
                "session_start_complete",
                reason=reason,
                session_lock_hold_ms=0,
                session_start_elapsed_ms=round((time.monotonic() - start_begin) * 1000, 1),
            )
            self._emit(
                "native_session_process "
                f"camera_uid={self._camera_uid or '-'} transport_uid={self._transport_uid or '-'} "
                f"session_id={self._session_id} session_generation={generation} "
                f"process_pid={getattr(process, 'pid', None) or '-'} start_reason={reason} "
                "state_before=AUTHENTICATING state_after=CONNECTED"
            )
            stderr_thread = threading.Thread(
                target=self._drain_stderr, args=(process,), daemon=True
            )
            stderr_thread.start()
            threading.Thread(
                target=self._pump,
                args=(process, stderr_thread, generation, self._cleanup_complete),
                daemon=True,
            ).start()
            self._startup_condition.notify_all()
            return False

    def _pump(
        self,
        process: subprocess.Popen[bytes],
        stderr_thread: threading.Thread,
        generation: int,
        cleanup_event: threading.Event,
    ) -> None:
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                with self._lock:
                    self._helper_stdout_chunks += 1
                    self._helper_stdout_bytes += len(chunk)
                    if self._helper_stdout_chunks == 1:
                        self._diagnostic("first_helper_stdout_chunk", bytes=len(chunk))
                        self._diagnostic("pump_first_chunk", bytes=len(chunk))
                    if self._helper_stdout_chunks == 1 or self._helper_stdout_chunks % 100 == 0:
                        self._diagnostic(
                            "pump_progress",
                            pump_bytes_total=self._helper_stdout_bytes,
                            helper_stdout_bytes_total=self._helper_stdout_bytes,
                            subscriber_count=len(self._subscribers),
                            active_subscriber_count=self._active_count_locked(),
                            passive_subscriber_count=self._passive_count_locked(),
                        )
                    self._media_ready = True
                    if self._state == "CONNECTED":
                        self._set_state_locked("STREAMING")
                    self._note_media(chunk)
                    subscribers = tuple(
                        chunks for chunks, _passive in self._subscribers.values()
                    )
                for chunks in subscribers:
                    self._put_chunk(chunks, chunk)
        finally:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._emit(
                            "native_process_reap_timeout "
                            f"process_pid={getattr(process, 'pid', None) or '-'}"
                        )
            stderr_thread.join(timeout=2)
            with self._lock:
                current = self._process is process and self._process_generation == generation
                active_subscribers = tuple(
                    chunks for chunks, passive in self._subscribers.values() if not passive
                ) if current else ()
                if current:
                    for subscription_id, (_chunks, passive) in tuple(self._subscribers.items()):
                        if not passive:
                            self._subscribers.pop(subscription_id, None)
                    self._process = None
                    self._process_generation = None
                    self._restore_standby_media_locked()
                    self._parse_summary_locked(process.returncode)
                    state_before = self._state
                    self._set_state_locked("STANDBY" if any(
                        passive for _chunks, passive in self._subscribers.values()
                    ) else "IDLE")
                    self._emit(
                        "native_session_end "
                        f"camera_uid={self._camera_uid or '-'} transport_uid={self._transport_uid or '-'} "
                        f"session_id={self._session_id} session_generation={self._session_generation} "
                        f"process_pid={getattr(process, 'pid', None) or '-'} "
                        f"returncode={process.returncode} state_before={state_before} "
                        f"state_after={self._state} active_consumers={self._active_count_locked()} "
                        f"passive_consumers={self._passive_count_locked()}"
                    )
            for chunks in active_subscribers:
                self._put_chunk(chunks, _END)
            with self._lock:
                if self._subscribers:
                    self._ensure_standby_thread_locked()
            cleanup_event.set()
            deferred: Callable[[], None] | None = None
            closed = False
            with self._lock:
                if current:
                    deferred = self._deferred_activation
                    self._deferred_activation = None
                    closed = self._closed
            with self._startup_condition:
                self._startup_condition.notify_all()
            if deferred is not None and not closed:
                deferred()

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        assert process.stderr is not None
        while True:
            chunk = process.stderr.read(4096)
            if not chunk:
                return
            with self._lock:
                remaining = MAX_RESPONSE_BYTES - len(self._stderr)
                if remaining > 0:
                    self._stderr.extend(chunk[:remaining])

    def _parse_summary_locked(self, returncode: int) -> None:
        payload = None
        for line in reversed(bytes(self._stderr).splitlines()):
            try:
                candidate = json.loads(line.decode("utf-8"))
            except (UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict) and "disconnected" in candidate:
                payload = candidate
                break
        if payload is not None and isinstance(payload.get("disconnected"), bool):
            self._clean_disconnect = payload["disconnected"]
        if returncode != 0:
            self._last_error = "native_stream_ended"

    def _stop_if_idle(
        self,
        expected_generation: int | None = None,
        expected_process: subprocess.Popen[bytes] | None = None,
    ) -> None:
        should_terminate = False
        with self._lock:
            self._idle_timer = None
            if self._active_subscribers_locked() or self._closed:
                return
            process = self._process
            if (
                process is not None
                and process.poll() is None
                and self._process_generation == expected_generation
                and process is expected_process
            ):
                should_terminate = True
                self._set_state_locked("STOPPING")
                self._emit(
                    "native_session_stop "
                    f"camera_uid={self._camera_uid or '-'} transport_uid={self._transport_uid or '-'} "
                    f"session_id={self._session_id} session_generation={self._session_generation} "
                    f"state_before=STREAMING state_after=STOPPING active_consumers={self._active_count_locked()} "
                    f"passive_consumers={self._passive_count_locked()}"
                )
        if should_terminate:
            self._terminate(process)

    def defer_active_after_cleanup(self, callback: Callable[[], None]) -> bool:
        """Retain one activation until a stopped helper has fully cleaned up."""

        with self._lock:
            if self._closed:
                return False
            process = self._process
            cleanup_pending = self._state == "STOPPING" or (
                process is not None and process.poll() is not None
            )
            if not cleanup_pending:
                return False
            if self._deferred_activation is None:
                self._deferred_activation = callback
            return True

    def _standby_loop(self) -> None:
        while True:
            with self._lock:
                if self._closed or not self._standby_frame:
                    return
                if self._process is not None and self._process.poll() is None:
                    should_emit = False
                    passive_subscribers: tuple[queue.Queue[bytes | object], ...] = ()
                else:
                    passive_subscribers = tuple(
                        chunks for chunks, passive in self._subscribers.values() if passive
                    )
                    if not passive_subscribers:
                        return
                    should_emit = True
            if should_emit:
                for chunks in passive_subscribers:
                    self._put_chunk(chunks, self._standby_frame)
            time.sleep(self._standby_interval)

    @staticmethod
    def _put_chunk(chunks: queue.Queue[bytes | object], value: bytes | object) -> None:
        try:
            chunks.put_nowait(value)
        except queue.Full:
            try:
                chunks.get_nowait()
                chunks.put_nowait(value)
            except (queue.Empty, queue.Full):
                pass

    def _restore_standby_media_locked(self) -> None:
        self._scan.clear()
        self._sps, self._pps, self._keyframe = self._standby_media
        self._media_ready = False

    def _terminate(self, process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._emit(
                    "native_process_reap_timeout "
                    f"process_pid={getattr(process, 'pid', None) or '-'}"
                )
