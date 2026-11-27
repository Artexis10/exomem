"""One email message as text: its key headers and its body, for an `.eml` file and for each message of a mailbox.

A message is untrusted. Its text is read as data: an HTML body goes through `html_text`, nothing in it is followed,
and a header that cannot be read leaves that header out instead of failing the message.
"""

from __future__ import annotations

from email.message import EmailMessage, Message

from . import html_text

#: The cut-short note of a body that `.eml` extraction cuts; a mailbox entry brings its own words.
CUT_SHORT = "[text cut short]"
# nosemgrep: ep-word-set -- The RFC 5322 header names that a message carries; a closed set the standard fixes.
HEADERS = ("From", "To", "Cc", "Date", "Subject")


def header(message: Message, name: str) -> str:
    """One header as a single line, or an empty string when it is absent or cannot be read."""
    try:
        value = message[name]
    except Exception:  # noqa: BLE001 -- a header that the parser cannot decode leaves the header out, not the message
        return ""
    return " ".join(str(value).split()) if value is not None else ""


def _plain_payload(part: Message) -> str:
    """The text of a part whose charset the content manager cannot decode, with what it cannot read replaced."""
    payload = part.get_payload(decode=True)
    return payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else ""


def body(message: EmailMessage, *, cut_note: str = CUT_SHORT) -> str:
    """The message's text: its `text/plain` part, else its `text/html` part converted to text; empty when it has neither.

    An HTML body longer than `html_text.MAX_INPUT_CHARS` is converted from its start and ends with `cut_note`.
    """
    try:
        part = message.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 -- exotic MIME must not fail the whole message
        return ""
    if part is None:
        return ""
    try:
        content = part.get_content()
    except Exception:  # noqa: BLE001 -- an unknown charset or a damaged encoding still has bytes to read
        content = _plain_payload(part)
    if not isinstance(content, str):
        return ""
    if part.get_content_subtype() != "html":
        return content.replace("\r\n", "\n").replace("\r", "\n")  # a CRLF mailbox keeps CRLF in its text parts
    converted = html_text.to_text(content)
    return f"{converted.text}\n\n{cut_note}" if converted.cut else converted.text


def message_text(message: EmailMessage, *, cut_note: str = CUT_SHORT) -> str:
    """The message's key headers, a blank line, and its body."""
    head = [f"{name}: {value}" for name in HEADERS if (value := header(message, name))]
    return ("\n".join(head) + "\n\n" + body(message, cut_note=cut_note)).strip()
