"""Exercise the HACS dual-input mux topology with a real FFmpeg binary."""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest


FIXTURE = Path(__file__).parent / "fixtures" / "h264_annexb.h264"


def test_real_h264_pcma_mux_to_mpegts() -> None:
    ffmpeg = os.environ.get("FFMPEG_TEST_BINARY") or shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is not installed; image smoke covers the built binary")

    video = FIXTURE.read_bytes()
    assert video.startswith(b"\x00\x00\x00\x01") or video.startswith(b"\x00\x00\x01")
    audio_read, audio_write = os.pipe()
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "+genpts",
        "-use_wallclock_as_timestamps",
        "1",
        "-f",
        "h264",
        "-i",
        "pipe:0",
        "-f",
        "alaw",
        "-ar",
        "16000",
        "-ac",
        "1",
        "-i",
        f"pipe:{audio_read}",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-c:a",
        "aac",
        "-c:v",
        "copy",
        "-f",
        "mpegts",
        "-muxdelay",
        "0",
        "-muxpreload",
        "0",
        "pipe:1",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        pass_fds=(audio_read,),
        bufsize=0,
    )
    os.close(audio_read)
    assert process.stdin is not None and process.stdout is not None
    assert process.stderr is not None
    output = bytearray()
    errors = bytearray()

    def drain(stream, target: bytearray) -> None:
        while True:
            chunk = stream.read(65536)
            if not chunk:
                return
            target.extend(chunk)

    output_thread = threading.Thread(target=drain, args=(process.stdout, output), daemon=True)
    error_thread = threading.Thread(target=drain, args=(process.stderr, errors), daemon=True)
    output_thread.start()
    error_thread.start()
    try:
        for _ in range(4):
            process.stdin.write(video)
            process.stdin.flush()
        process.stdin.close()
        for _ in range(12):
            os.write(audio_write, b"\xd5" * 640)
        os.close(audio_write)
        audio_write = -1
        returncode = process.wait(timeout=15)
    finally:
        if audio_write >= 0:
            os.close(audio_write)
        output_thread.join(timeout=2)
        error_thread.join(timeout=2)

    assert returncode == 0, bytes(errors).decode("utf-8", "replace")
    assert output, bytes(errors).decode("utf-8", "replace")
    assert b"Unknown input format" not in errors
    assert b"No such filter" not in errors
