"""Text matching, excerpting, and transcript localization for find results."""

from __future__ import annotations

import re

from .find_types import ParsedPage

EXCERPT_RADIUS = 100  # chars on each side of the match
EXCERPT_MAX_LEN = 220


def transcript_ts_for_hit(
    page: ParsedPage, chunk: str | None, query_norm: str
) -> float | None:
    """Localize a text match inside a TIMED transcript -> seconds, or None.

    Vector lane: the matched chunk is a semantic segment whose first timed line
    carries the segment start. BM25/keyword (no chunk, or a chunk without
    markers): anchor on the first query token's position in the body and take
    the nearest PRECEDING timestamp marker. Only timed audio/video sidecars
    ever return a value; flat pages cost one media_type check.
    """
    if page.media_type not in ("audio", "video"):
        return None
    from . import semantic_segments as ss

    if chunk:
        for line in chunk.splitlines():
            m = ss.TIMED_LINE_RE.match(line)
            if m and m.group(2) is not None:
                return ss.ts_from_match(m)
    tokens = query_norm.split() if query_norm else []
    if not tokens:
        return None
    body = page.body or ""
    if "[" not in body:
        return None
    pos = body.lower().find(tokens[0])
    if pos == -1:
        return None
    offset = 0
    best: float | None = None
    for line in body.splitlines(keepends=True):
        if offset > pos:
            break
        m = ss.TIMED_LINE_RE.match(line)
        if m and m.group(2) is not None:
            best = ss.ts_from_match(m)
        offset += len(line)
    return best


def stem_tokens_present(page: ParsedPage, query_norm: str) -> bool:
    """All-tokens-present check using Snowball stems on both sides.

    Recovers morphological matches that the literal substring gate misses.
    Used only as a fallback in hybrid mode; keyword mode keeps the strict
    substring gate.
    """
    if not query_norm:
        return True
    from . import bm25 as bm25_module

    text_stems = page.stem_set
    for tok in query_norm.split():
        if not tok:
            continue
        if bm25_module.stem_word(tok) not in text_stems:
            return False
    return True


def stem_anchored_excerpt(page: ParsedPage, query_norm: str) -> str:
    """Snippet anchored on the first body word whose stem matches the query."""
    from . import bm25 as bm25_module

    body = page.body.strip()
    if not body:
        return ""
    query_stems = {bm25_module.stem_word(t) for t in query_norm.split() if t}
    if not query_stems:
        return collapse(body[:EXCERPT_MAX_LEN])
    anchor_idx = -1
    anchor_len = 0
    for m in re.finditer(r"[A-Za-z0-9]+", body):
        word = m.group(0)
        if bm25_module.stem_word(word.lower()) in query_stems:
            anchor_idx = m.start()
            anchor_len = len(word)
            break
    if anchor_idx == -1:
        return collapse(body[:EXCERPT_MAX_LEN])
    start = max(0, anchor_idx - EXCERPT_RADIUS)
    end = min(len(body), anchor_idx + anchor_len + EXCERPT_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return collapse(snippet)


def semantic_excerpt(
    page: ParsedPage,
    query_norm: str,
    best_chunk: str | None,
    keyword_excerpt: str | None,
) -> str:
    """Prefer the matching chunk text, then fall back to the keyword excerpt."""
    if best_chunk:
        body = best_chunk
        title_prefix = (page.title or "").strip()
        if title_prefix and body.startswith(title_prefix + "\n\n"):
            body = body[len(title_prefix) + 2:]
        snippet = body.strip()[:EXCERPT_MAX_LEN].strip()
        if len(body) > EXCERPT_MAX_LEN:
            snippet = snippet.rstrip() + "…"
        return collapse(snippet)
    return keyword_excerpt or ""


def make_excerpt(page: ParsedPage, query_norm: str) -> str | None:
    """Return a short snippet anchored to the query, or None when no token matches."""
    body = page.body_stripped
    if not query_norm:
        snippet = body[:EXCERPT_MAX_LEN]
        return collapse(snippet)
    title_norm = page.title_norm
    body_norm = page.body_norm
    tokens = query_norm.split()
    if not tokens:
        snippet = body[:EXCERPT_MAX_LEN]
        return collapse(snippet)
    for tok in tokens:
        if tok not in title_norm and tok not in body_norm:
            return None
    anchor_idx = -1
    anchor_len = 0
    for tok in tokens:
        idx = body_norm.find(tok)
        if idx != -1:
            anchor_idx = idx
            anchor_len = len(tok)
            break
    if anchor_idx == -1:
        snippet = body[:EXCERPT_MAX_LEN]
        return collapse(snippet)
    start = max(0, anchor_idx - EXCERPT_RADIUS)
    end = min(len(body), anchor_idx + anchor_len + EXCERPT_RADIUS)
    snippet = body[start:end]
    if start > 0:
        snippet = "…" + snippet.lstrip()
    if end < len(body):
        snippet = snippet.rstrip() + "…"
    return collapse(snippet)


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
