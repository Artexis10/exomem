"""Turn an upload reply's one-line `curl` send into the request that curl would make.

An upload reply gives its send as one `curl -sS -H ... -F ... <url>` line with placeholders such as
`<token>` and `<file>`. A test fills them in as the step's text tells the agent, then posts the
result with httpx.
"""

from __future__ import annotations

import mimetypes
import shlex
from pathlib import Path


def curl_request(shell: str, **placeholders: str) -> tuple[str, dict]:
    """The URL and the `httpx` post arguments of a send line, with each `<name>` replaced by its value."""

    def filled(word: str) -> str:
        for name, value in placeholders.items():
            word = word.replace(f"<{name}>", value)
        return word

    words = iter(shlex.split(shell))
    assert next(words) == "curl"
    headers: dict[str, str] = {}
    fields: dict[str, str] = {}
    files: dict[str, tuple[str, bytes, str]] = {}
    url = ""
    for word in words:
        if word == "-H":
            name, _, value = filled(next(words)).partition(": ")
            headers[name] = value
        elif word == "-F":
            name, _, value = filled(next(words)).partition("=")
            if value.startswith("@"):
                path = Path(value[1:])
                # curl names the part after the file and guesses its type from the extension.
                media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
                files[name] = (path.name, path.read_bytes(), media_type)
            else:
                fields[name] = value
        elif word != "-sS":
            url = word
    return url, {"headers": headers, "data": fields, "files": files}
