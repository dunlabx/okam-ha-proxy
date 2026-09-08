"""Small, dependency-free logging helpers for add-on diagnostics."""

from __future__ import annotations

from datetime import datetime
import sys
import uuid
from typing import TextIO


PROCESS_ID = uuid.uuid4().hex


def absolute_timestamp() -> str:
    """Return local wall-clock time with milliseconds and a colonized offset."""

    value = datetime.now().astimezone()
    offset = value.strftime("%z")
    if len(offset) == 5:
        offset = f"{offset[:3]}:{offset[3:]}"
    return f"{value.strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]} {offset}"


def timestamped_print(
    *values: object,
    sep: str = " ",
    end: str = "\n",
    file: TextIO | None = None,
    flush: bool = False,
) -> None:
    """Print application text with an absolute timestamp at every line edge."""

    stream = file if file is not None else sys.stdout
    text = sep.join(str(value) for value in values)
    lines = text.split("\n")
    prefix = absolute_timestamp() + " "
    stream.write("\n".join(prefix + line for line in lines) + end)
    if flush:
        stream.flush()
