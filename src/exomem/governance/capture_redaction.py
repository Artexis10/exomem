"""Remove payment card numbers and labelled credentials from text before a capture keeps it.

Evidence is append-only, so a card number or a password that reaches a stored
page stays there. Every capture door that writes caller text (a text original,
a caption, an extracted text) passes it through `redact` first, and reports
what it removed so the agent can tell the person.

Two shapes are removed, and nothing else:

- A payment card number: 14 to 19 digits, unbroken or in a grouping that card
  schemes print, whose issuer prefix and length belong to a card scheme and
  whose Luhn check digit holds. The last four digits stay, so the page can still say
  which card it was.
- A machine credential, in the shapes the egress scrubber already names: a
  bearer token, a private key block, a known provider key prefix, or a
  labelled value of 16 or more characters (`api_key=...`).

A short human password, a PIN or a card security code has no shape that
tells it apart from other text, so it stays the agent's judgement, which the
guidance covers: never keep one, and ask for a covered copy.

The egress scrubber's high-entropy heuristic is left out on purpose: evidence
often quotes hashes, serial numbers and identifiers, which that heuristic
would mangle. It still runs on every reply that leaves the server.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import scrubber

#: What replaces a labelled credential in a kept text.
CREDENTIAL_NOTICE = "[credential removed before saving]"

# A card number as people write it: one unbroken run of digits, or the groupings card schemes print
# (4-4-4-4 with an optional 3-digit tail, 4-6-5 and 4-6-4). Other groupings, such as order numbers, are
# left alone. A digit, or a decimal or thousands separator between digits, bounds the match on either
# side, so a sentence's full stop or comma after the number does not hide it.
_CARD_CANDIDATE = re.compile(
    r"(?<!\d)(?<!\d[.,])(?:"
    r"\d{14,19}"
    r"|\d{4}([ \u00a0-])\d{4}\1\d{4}\1\d{4}(?:\1\d{3})?"
    r"|\d{4}([ \u00a0-])\d{6}\2\d{4,5}"
    r")(?!\d|[.,]\d)"
)

# Issuer prefix ranges and lengths of the major card schemes (ISO/IEC 7812 issuer
# identification numbers, as each scheme publishes them). This is a closed set
# fixed by the schemes, not a vocabulary, so it lives in code (registry C6).
# Each row is (lowest prefix, highest prefix, allowed lengths).
_SCHEMES: tuple[tuple[int, int, frozenset[int]], ...] = (
    # Visa's retired 13-digit numbers are left out: EAN-13 barcodes that start with 4 would match them.
    (4, 4, frozenset({16, 19})),                         # Visa
    (51, 55, frozenset({16})),                           # Mastercard
    (2221, 2720, frozenset({16})),                       # Mastercard 2-series
    (34, 34, frozenset({15})),                           # American Express
    (37, 37, frozenset({15})),                           # American Express
    (6011, 6011, frozenset({16, 17, 18, 19})),           # Discover
    (644, 649, frozenset({16, 17, 18, 19})),             # Discover
    (65, 65, frozenset({16, 17, 18, 19})),               # Discover
    (3528, 3589, frozenset({16, 17, 18, 19})),           # JCB
    (300, 305, frozenset({14, 15, 16, 17, 18, 19})),     # Diners Club
    (36, 36, frozenset({14, 15, 16, 17, 18, 19})),       # Diners Club
    (38, 39, frozenset({16, 17, 18, 19})),               # Diners Club
    (62, 62, frozenset({16, 17, 18, 19})),               # UnionPay
)


@dataclass(frozen=True)
class Redaction:
    """The kept text and the kinds of value removed from it (`payment_card`, `credential`)."""

    text: str
    removed: tuple[str, ...]


def _luhn_valid(digits: str) -> bool:
    total = 0
    for position, char in enumerate(reversed(digits)):
        value = int(char)
        if position % 2:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return total % 10 == 0


def _is_card_number(digits: str) -> bool:
    for low, high, lengths in _SCHEMES:
        width = len(str(low))
        if len(digits) in lengths and low <= int(digits[:width]) <= high:
            return _luhn_valid(digits)
    return False


def _mask_cards(text: str) -> tuple[str, int]:
    count = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal count
        found = match.group(0)
        digits = re.sub(r"[ \u00a0-]", "", found)
        if _is_card_number(digits):
            count += 1
            return f"[card number removed, ends {digits[-4:]}]"
        if len(digits) == 19 and found[4] in " \u00a0-" and _is_card_number(digits[:16]):
            # A 4-4-4-4 card followed by a 3-digit group is a card and its security code, so both go.
            count += 1
            return f"[card number removed, ends {digits[12:16]}]"
        return found

    return _CARD_CANDIDATE.sub(replace, text), count


def redact(text: str) -> Redaction:
    """Return `text` with card numbers and labelled credentials removed, naming what was removed."""
    if not text:
        return Redaction(text, ())
    cleaned, cards = _mask_cards(text)
    cleaned, credentials = scrubber.scrub_labelled(cleaned, notice=CREDENTIAL_NOTICE)
    removed = (("payment_card",) if cards else ()) + (("credential",) if credentials else ())
    return Redaction(cleaned, removed)


def warning(field: str, removed: tuple[str, ...]) -> str:
    """The plain sentence a capture result carries for one redacted field."""
    names = {"payment_card": "a card number", "credential": "a password or key"}
    what = " and ".join(names[kind] for kind in removed)
    return (f"Removed {what} from the {field} before saving. Tell the person; "
            "the kept text shows where it was.")
