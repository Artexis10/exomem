"""CSV source artifact; the sentinel travels as a trailing reference row."""

from __future__ import annotations

import csv
import io

from membench.templates.base import SourceContent


def render(content: SourceContent, sentinel: str) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["# " + content.title])
    for row in content.table or []:
        writer.writerow(row)
    for line in content.lines:
        writer.writerow(["note", line])
    writer.writerow(["ref", sentinel])
    return buffer.getvalue()
