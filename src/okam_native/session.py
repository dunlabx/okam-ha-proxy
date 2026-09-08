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
        self._session_generation = 0
        self._state = "IDLE"
        self.idle_timeout = idle_timeout
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
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
        if self._standby_frame:
            self._note_media(self._standby_frame + ANNEX_B_START)
            self._standby_media = (self._sps, self._pps, self._keyframe)
        else:
            self._standby_media = (b"", b"", b"")

    def acquire(self, *, passive: bool = False, reason: str = "active") -> StreamSubscription:
        with self._lock:
            if self._closed:
                raise P2PError("native stream session is closed")
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            if not passive and (self._process is None or self._process.poll() is not None):
                self._start_locked(reason)
            subscription_id = uuid.uuid4().hex
            chunks: queue.Queue[bytes | object] = queue.Queue(maxsize=32)
            # Start the viewer on a decodable boundary. Without this it waits
            # for the camera's next keyframe, which is the whole open latency.
            preamble = self._preamble()
            if preamble:
                chunks.put_nowait(preamble)
            self._subscribers[subscription_id] = (chunks, passive)
            if passive and self._process is None:
                self._state = "STANDBY"
                self._put_chunk(chunks, self._standby_frame)
                self._ensure_standby_thread_locked()
            return StreamSubscription(self, subscription_id, chunks)

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
                self._idle_timer = threading.Timer(self.idle_timeout, self._stop_if_idle)
                self._idle_timer.daemon = True
                self._idle_timer.start()

    def _active_subscribers_locked(self) -> bool:
        return any(not passive for _chunks, passive in self._subscribers.values())

    def _active_count_locked(self) -> int:
        return sum(not passive for _chunks, passive in self._subscribers.values())

    def _passive_count_locked(self) -> int:
        return sum(passive for _chunks, passive in self._subscribers.values())

    def _emit(self, message: str) -> None:
        if self._logger is not None:
            self._logger(message)

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
                decoder.wait(timeout=5)
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
        elif kind == 8:
            self._pps = unit
        elif kind == 5:
            self._keyframe = unit

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
            if self._idle_timer is not None:
                self._idle_timer.cancel()
                self._idle_timer = None
            process = self._process
            if process is not None and process.poll() is None:
                self._state = "STOPPING"
            subscribers = tuple(chunks for chunks, _passive in self._subscribers.values())
            self._subscribers.clear()
        for chunks in subscribers:
            self._put_chunk(chunks, _END)
        self._terminate(process)

    def _start_locked(self, reason: str = "active") -> None:
        self._session_generation += 1
        generation = self._session_generation
        state_before = self._state
        self._state = "STARTING"
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
        self._state = "AUTHENTICATING"
        self._emit(
            "native_session_auth "
            f"camera_uid={self._camera_uid or '-'} transport_uid={self._transport_uid or '-'} "
            f"session_id={self._session_id} session_generation={generation} "
            f"start_reason={reason} state_before=STARTING state_after=AUTHENTICATING"
        )
        try:
            process = self._starter()
        except Exception:
            self._state = "FAILED"
            self._emit(
                "native_session_failed "
                f"camera_uid={self._camera_uid or '-'} transport_uid={self._transport_uid or '-'} "
                f"session_id={self._session_id} session_generation={generation} "
                f"start_reason={reason} state_before=AUTHENTICATING state_after=FAILED"
            )
            raise
        if process.stdout is None or process.stderr is None:
            self._state = "FAILED"
            process.kill()
            process.wait(timeout=5)
            raise P2PError("native stream helper pipes are unavailable")
        self._process = process
        self._state = "CONNECTED"
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
            target=self._pump, args=(process, stderr_thread), daemon=True
        ).start()

    def _pump(
        self, process: subprocess.Popen[bytes], stderr_thread: threading.Thread
    ) -> None:
        assert process.stdout is not None
        try:
            while True:
                chunk = process.stdout.read(64 * 1024)
                if not chunk:
                    break
                with self._lock:
                    self._media_ready = True
                    if self._state == "CONNECTED":
                        self._state = "STREAMING"
                    self._note_media(chunk)
                    subscribers = tuple(
                        chunks for chunks, _passive in self._subscribers.values()
                    )
                for chunks in subscribers:
                    self._put_chunk(chunks, chunk)
        finally:
            process.wait()
            stderr_thread.join(timeout=2)
            with self._lock:
                active_subscribers = tuple(
                    chunks for chunks, passive in self._subscribers.values() if not passive
                )
                for subscription_id, (_chunks, passive) in tuple(self._subscribers.items()):
                    if not passive:
                        self._subscribers.pop(subscription_id, None)
                if self._process is process:
                    self._process = None
                self._restore_standby_media_locked()
                self._parse_summary_locked(process.returncode)
                state_before = self._state
                self._state = "STANDBY" if any(
                    passive for _chunks, passive in self._subscribers.values()
                ) else "IDLE"
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

    def _stop_if_idle(self) -> None:
        with self._lock:
            self._idle_timer = None
            if self._active_subscribers_locked() or self._closed:
                return
            process = self._process
            if process is not None and process.poll() is None:
                self._state = "STOPPING"
                self._emit(
                    "native_session_stop "
                    f"camera_uid={self._camera_uid or '-'} transport_uid={self._transport_uid or '-'} "
                    f"session_id={self._session_id} session_generation={self._session_generation} "
                    f"state_before=STREAMING state_after=STOPPING active_consumers={self._active_count_locked()} "
                    f"passive_consumers={self._passive_count_locked()}"
                )
        self._terminate(process)

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

    @staticmethod
    def _terminate(process: subprocess.Popen[bytes] | None) -> None:
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
