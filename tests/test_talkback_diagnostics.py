from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "native" / "hybris_connect" / "hybris_connect.c"


def test_diagnostic_candidate_version_and_native_helper_scope():
    config = yaml.safe_load((ROOT / "okam_native_app" / "config.yaml").read_text())
    assert config["version"] == "2.0.0-rc8-talkdiag2"

    source = HELPER.read_text()
    assert "typedef bool (*client_write_fn)(void *, int, const void *, int, int);" in source
    assert "bool write_result = talkback_write(talkback_client, TALKBACK_CHANNEL, payload, 640, 2000);" in source
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


def test_diagnostic_segmentation_observes_existing_residual_discard():
    source = HELPER.read_text()
    assert "size_t full_chunks = length / 640;" in source
    assert "size_t residual = length % 640;" in source
    assert "talkback_diagnostics.discarded_residual_bytes_total += residual;" in source
    assert "for (size_t pos = 0; pos + 640 <= length; pos += 640)" in source
    assert "talkback_write_one(payload + pos);" in source
    assert "OKT1" in source
    assert "TALKBACK_CHANNEL 3" in source


def test_diagnostic_write_result_is_reported_without_payload_logging():
    source = HELPER.read_text()
    write_section = source[source.index("static void talkback_write_one") : source.index("static void *forward_talkback")]
    assert "write_result" in write_section
    assert "write_success_count" in write_section
    assert "write_failure_count" in write_section
    assert "payload + pos" not in write_section
