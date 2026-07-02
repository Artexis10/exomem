"""Write-boundary guard: reject base64-encoded binaries shoved into text tools.

The expensive failure mode is Claude base64-encoding a binary (image, PDF, …)
and passing it as a text tool argument. Those characters are *model output
tokens* — a few-MB file becomes millions of tokens before the request even
reaches us, so this guard CANNOT refund the cost. What it does do: stop the
blob from being written into a markdown note (vault hygiene) and return a clear
error that points at the proper out-of-band path (`POST /upload`) so the model
learns the right move. The real prevention lives in SKILL.md + the tool
descriptions, which steer the model *before* it generates.

The heuristic is deliberately conservative: it only fires on the pathological
"big unbroken run of base64 with effectively no whitespace" shape. Ordinary
markdown — even a very large compiled note — is full of spaces and punctuation,
so it sails through.
"""

from __future__ import annotations

import os

# A genuine markdown note never approaches 1 MB; a base64 image easily exceeds it.
MAX_TEXT_CONTENT_BYTES = int(os.environ.get("EXOMEM_MAX_TEXT_BYTES", str(1024 * 1024)))
# Smallest base64 run we treat as "this is a binary blob, not prose" (~20 KB).
BASE64_RUN_THRESHOLD = int(os.environ.get("EXOMEM_BASE64_RUN_THRESHOLD", str(20 * 1024)))

_B64_CHARS = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/="
)


def _looks_like_base64_blob(content: str) -> bool:
    """True when `content` is overwhelmingly base64 with no prose whitespace.

    Catches three shapes: data URIs (`data:image/png;base64,…`), one giant
    unbroken base64 string, and newline-wrapped base64 (the 76-col convention).
    Prose/markdown carries spaces and punctuation and is rejected by the
    whitespace check, so it never trips this.
    """
    head = content[:256].lower()
    if "data:" in head and "base64," in head:
        return True
    if len(content) < BASE64_RUN_THRESHOLD:
        return False
    # base64 is often wrapped at 76 cols — ignore line breaks before measuring.
    compact = content.replace("\n", "").replace("\r", "")
    if len(compact) < BASE64_RUN_THRESHOLD:
        return False
    spaces = compact.count(" ") + compact.count("\t")
    if spaces / len(compact) > 0.01:
        return False  # real whitespace → prose/markdown, not a blob
    b64 = sum(1 for c in compact if c in _B64_CHARS)
    return b64 / len(compact) > 0.97


def guard_text_content(content: str | None, *, tool: str, field: str = "content") -> None:
    """Raise ValueError if `content` looks like a binary blob, else no-op.

    Called at the top of every text-write tool. Raising ValueError matches the
    existing tool→ValueError error convention, so the message reaches the client.
    """
    if not content:
        return
    n = len(content)
    if n > MAX_TEXT_CONTENT_BYTES:
        raise ValueError(
            f"BINARY_BLOB_REJECTED: `{field}` for `{tool}` is {n:,} chars "
            f"(> {MAX_TEXT_CONTENT_BYTES:,} limit). Text tools are for markdown, not "
            "binaries. If this is a real (huge) note, raise EXOMEM_MAX_TEXT_BYTES; if "
            "it's an encoded file, use the POST /upload endpoint instead."
        )
    if _looks_like_base64_blob(content):
        raise ValueError(
            f"BINARY_BLOB_REJECTED: `{field}` for `{tool}` looks like base64-encoded "
            "binary. Do NOT push binaries through text tools — they are billed as model "
            "output tokens. Use the out-of-band POST /upload endpoint (no token cost), "
            "or `preserve` for genuinely small artifacts; to keep the original file, "
            "drop it into Evidence/ via Obsidian Sync and link it from the note."
        )
