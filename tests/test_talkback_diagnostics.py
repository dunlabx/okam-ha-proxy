from pathlib import Path
import re

import yaml


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "native" / "hybris_connect" / "hybris_connect.c"


def test_diagnostic_candidate_version_and_native_helper_scope():
    config = yaml.safe_load((ROOT / "okam_native_app" / "config.yaml").read_text())
    assert config["version"] == "2.0.0-rc8-talkframe1"

    source = HELPER.read_text()
    assert "typedef bool (*client_write_fn)(void *, int, const void *, int, int);" in source
    assert "TALKBACK_NATIVE_HEADER_BYTES 32U" in source
    assert "TALKBACK_NATIVE_FRAME_BYTES (TALKBACK_NATIVE_HEADER_BYTES + TALKBACK_CHUNK_BYTES)" in source
    assert "0x55, 0xaa, 0x15, 0xa8" in source
    assert "0x08, 0x01, 0x00, 0x00" in source
    assert "memcpy(native_frame + TALKBACK_NATIVE_HEADER_BYTES, payload, TALKBACK_CHUNK_BYTES);" in source
    assert "TALKBACK_CHANNEL, native_frame, TALKBACK_NATIVE_FRAME_BYTES, 2000" in source
    assert "native_talkback_native_frame_first" in source
    assert "native_talkback_write_first_attempt" in source
    assert "native_talkback_write_first_success" in source
    assert "native_talkback_write_first_failure" in source
    assert "native_talkback_write_progress" in source
    assert "native_talkback_segmentation_first" in source
    assert "native_talkback_segmentation_progress" in source
    assert "write_call_elapsed_ms" in source
    assert "since_previous_write_ms" in source
    assert "short_header_read" in source
    assert "malformed_header" in source
    assert "incomplete_payload" in source
    assert "invalid_declared_length" in source
    assert "native_talkback_thread_started" in source
    assert "native_talkback_thread_start_failure" in source
    assert "client_write_resolved" in source
    assert "native_talkback_pacer_started" in source
    assert "native_talkback_pacing_first" in source
    assert "native_talkback_pacing_progress" in source
    assert "native_talkback_pacing_overflow" in source


def test_native_talk_frame_has_exact_official_header_and_audio_payload_contract():
    source = HELPER.read_text()
    match = re.search(
        r"talkback_native_header\[TALKBACK_NATIVE_HEADER_BYTES\]\s*=\s*\{(.*?)\};",
        source,
        re.DOTALL,
    )
    assert match is not None
    actual = bytes(int(value, 16) for value in re.findall(r"0x([0-9a-fA-F]{2})", match.group(1)))
    expected = bytes.fromhex(
        "55aa15a80801000000000000800200000000000007000000"
        "0000000000000000"
    )
    assert actual == expected
    audio = bytes(range(256)) * 2 + bytes(range(128))
    frame = actual + audio
    assert len(audio) == 640
    assert len(frame) == 672
    assert frame[:32] == expected
    assert frame[4] == 0x08
    assert frame[32:] == audio

    write_section = source[source.index("static void talkback_write_one") : source.index("static void talkback_queue_reset")]
    assert "TALKBACK_CHANNEL, native_frame, TALKBACK_NATIVE_FRAME_BYTES, 2000" in write_section
    assert "payload, 640, 2000" not in write_section


def test_camera_to_client_audio_path_does_not_use_talk_frame_header():
    source = HELPER.read_text()
    receive_section = source[source.index("static bool forward_h264_frames") : source.index("int main")]
    assert "talkback_native_header" not in receive_section
    assert "header[4] == 0x0cU" in receive_section


def test_diagnostic_segmentation_observes_existing_residual_discard():
    source = HELPER.read_text()
    assert "size_t full_chunks = length / 640;" in source
    assert "size_t residual = length % 640;" in source
    assert "talkback_diagnostics.discarded_residual_bytes_total += residual;" in source
    assert "for (size_t pos = 0; pos + TALKBACK_CHUNK_BYTES <= length; pos += TALKBACK_CHUNK_BYTES)" in source
    assert "talkback_queue_push(payload + pos)" in source
    assert "OKT1" in source
    assert "TALKBACK_CHANNEL 3" in source


def test_diagnostic_write_result_is_reported_without_payload_logging():
    source = HELPER.read_text()
    write_section = source[source.index("static void talkback_write_one") : source.index("static void *forward_talkback")]
    assert "write_result" in write_section
    assert "write_success_count" in write_section
    assert "write_failure_count" in write_section
    assert "payload + pos" not in write_section


def test_talkback_pacing_is_bounded_and_monotonic_without_changing_wire_contract():
    source = HELPER.read_text()
    assert "#define TALKBACK_CHUNK_BYTES 640U" in source
    assert "#define TALKBACK_QUEUE_CAPACITY 16U" in source
    assert "#define TALKBACK_PACING_INTERVAL_MS 40LL" in source
    assert "clock_gettime(CLOCK_MONOTONIC" in source
    assert "talkback_queue.chunks[talkback_queue.tail]" in source
    assert "talkback_queue.count >= TALKBACK_QUEUE_CAPACITY" in source
    assert "pthread_cond_signal(&talkback_queue.condition)" in source
    assert "pthread_cond_broadcast(&talkback_queue.condition)" in source
    assert "next_deadline += TALKBACK_PACING_INTERVAL_MS" in source
    assert "TALKBACK_CHANNEL, native_frame, TALKBACK_NATIVE_FRAME_BYTES, 2000" in source
    assert "TALKBACK_CHANNEL 3" in source
    assert "TALKBACK_CHUNK_BYTES <= length" in source
    assert "residual = length % 640" in source
    assert "discarded_residual_bytes_total += residual" in source


def test_pacer_is_separate_from_ipc_reader_and_is_joined_on_shutdown():
    source = HELPER.read_text()
    forward = source[source.index("static void *forward_talkback") : source.index("static bool forward_h264_frames")]
    pacer = source[source.index("static void *talkback_pacer") : source.index("static void *forward_talkback")]
    assert "talkback_write_one(payload + pos)" not in forward
    assert "talkback_queue_push(payload + pos)" in forward
    assert "talkback_queue_pop(payload)" in pacer
    assert "pthread_create(&talkback_pacer_thread" in source
    assert "pthread_join(talkback_pacer_thread" in source
    assert "talkback_queue_stop();" in source
