import re

from okam_native.logging import PROCESS_ID, timestamped_print


_LINE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3} [+-]\d{2}:\d{2} ")


def test_timestamped_print_prefixes_each_application_log_line(capsys) -> None:
    timestamped_print("first\nsecond", flush=True)
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert all(_LINE.match(line) for line in lines)


def test_runtime_process_id_is_stable_and_nonempty() -> None:
    assert re.fullmatch(r"[0-9a-f]{32}", PROCESS_ID)
