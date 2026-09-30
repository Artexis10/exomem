"""The `conversation` argument of `activate_context`: bounds and evidence.

A caller may hand the compiler a small, bounded view of the conversation it is
in. Three optional fields, each capped by the SERVER, deterministically:

* `focus`: the agent's one-line statement of what the turn is about, at most
  `FOCUS_MAX_CHARS`. It is resolved as a second segment of the current turn.
* `recent`: the tail of earlier turns, newest last. At most `RECENT_MAX_ENTRIES`
  entries, a user entry cut to `USER_ENTRY_MAX_CHARS` and an assistant entry to
  `ASSISTANT_ENTRY_MAX_CHARS`, `RECENT_MAX_TOTAL_CHARS` in all. It yields the
  subordinate `conversation` qualifier, never a candidate.
* `refs`: canonical page refs the conversation already read, at most
  `REFS_MAX`. Also a qualifier.

Nothing here refuses: a malformed object is `absent`, and a bound that cuts
anything reports `truncated`. Every text field passes the shared egress
scrubber first (`governance.scrubber`): a credential is removed, never
matched, and its removal is a cut. Nothing here is stored: the conversation lives in
the request and is matched against the caller's own view of the catalogue.
Matching runs the turn's own token and stopword rules over ONE entry at a time
(never across entries) by alias and lexical kinds only. No recall query, no
embedding, no model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

FOCUS_MAX_CHARS = 240
USER_ENTRY_MAX_CHARS = 600
ASSISTANT_ENTRY_MAX_CHARS = 300
RECENT_MAX_ENTRIES = 6
RECENT_MAX_TOTAL_CHARS = 2400
REFS_MAX = 12
#: A ref longer than this names no page. Dropped like any other non-ref.
REF_MAX_CHARS = 1024

#: How many entries are read as evidence, newest first. Only these are ever
#: matched, so a longer conversation cannot raise what an older subject
#: contributes.
READ_USER_ENTRIES = 3
READ_ASSISTANT_ENTRIES = 2

ROLES: tuple[str, ...] = ("user", "assistant")
#: The three values of `generation.conversation`.
ABSENT, APPLIED, TRUNCATED = "absent", "applied", "truncated"

#: The evidence kinds an earlier entry may lend an anchor. Worded contact only.
ENTRY_KINDS: frozenset[str] = frozenset({"exact_alias", "lexical_overlap"})
#: The contact kinds `focus` may lend. `rare_term` is deliberately absent: a
#: single shared rare word is too weak a fact to take from the agent's own line.
FOCUS_KINDS: frozenset[str] = frozenset({"exact_alias", "lexical_overlap", "claims_match"})


@dataclass(frozen=True, slots=True)
class Entry:
    role: str
    text: str


@dataclass(frozen=True, slots=True)
class Conversation:
    """One bounded conversation. `state` is the value `generation.conversation` reports."""

    focus: str = ""
    recent: tuple[Entry, ...] = ()
    refs: tuple[str, ...] = ()
    state: str = ABSENT

    @property
    def present(self) -> bool:
        return self.state != ABSENT

    @property
    def counts(self) -> dict[str, int]:
        """What the activation log may record: presence and counts, never text."""
        return {"state": self.state, "recent": len(self.recent), "refs": len(self.refs)}

    def user_entries(self) -> tuple[Entry, ...]:
        """The user entries that carry a carry, newest first."""
        return tuple(reversed([e for e in self.recent if e.role == "user"]))[:READ_USER_ENTRIES]

    def read_entries(self) -> tuple[Entry, ...]:
        """The newest three user and newest two assistant entries, newest first."""
        assistant = tuple(reversed([e for e in self.recent if e.role == "assistant"]))
        return (*self.user_entries(), *assistant[:READ_ASSISTANT_ENTRIES])


NONE = Conversation()


def _cut(text: str, limit: int) -> tuple[str, bool]:
    """Keep the head, cut at the last whitespace at or before `limit`."""
    if len(text) <= limit:
        return text, False
    head = text[: limit + 1]
    if head[-1].isspace():
        return head.rstrip(), True
    space = max((i for i, ch in enumerate(head) if ch.isspace()), default=-1)
    return (head[:space] if space > 0 else head[:limit]).rstrip(), True


def _scrubbed(text: str) -> tuple[str, bool]:
    """`text` with every credential the shared egress scrubber recognises
    removed, and whether it removed one. The scrubber's fixed notice is taken
    out again, so it can never be matched as the user's words."""
    from .governance import scrubber

    cleaned, blocked = scrubber.scrub_text(text)
    if not blocked:
        return text, False
    return " ".join(cleaned.replace(scrubber.NOTICE, " ").split()), True


def bound(raw: Any) -> Conversation:
    """The bounded conversation for a caller's raw `conversation` argument.

    Never raises. A value that is not a mapping, or whose every field is empty
    after bounding, is `absent`. Deduplicating refs is normalisation, not loss,
    so it does not by itself report `truncated`.
    """
    if not isinstance(raw, Mapping):
        return NONE
    cut = False

    focus = raw.get("focus")
    focus_text = ""
    if isinstance(focus, str) and focus.strip():
        focus_text, was_cut = _cut(focus.strip(), FOCUS_MAX_CHARS)
        focus_text, was_scrubbed = _scrubbed(focus_text)
        cut = cut or was_cut or was_scrubbed
    elif focus not in (None, ""):
        cut = True

    entries: list[Entry] = []
    recent = raw.get("recent")
    if isinstance(recent, Sequence) and not isinstance(recent, (str, bytes)):
        for item in recent:
            role = item.get("role") if isinstance(item, Mapping) else None
            text = item.get("text") if isinstance(item, Mapping) else None
            if role not in ROLES or not isinstance(text, str) or not text.strip():
                cut = True
                continue
            limit = USER_ENTRY_MAX_CHARS if role == "user" else ASSISTANT_ENTRY_MAX_CHARS
            body, was_cut = _cut(text.strip(), limit)
            body, was_scrubbed = _scrubbed(body)
            cut = cut or was_cut or was_scrubbed
            if not body:
                continue
            entries.append(Entry(role, body))
    elif recent not in (None, "", []):
        cut = True
    if len(entries) > RECENT_MAX_ENTRIES:
        entries = entries[-RECENT_MAX_ENTRIES:]
        cut = True
    while entries and sum(len(entry.text) for entry in entries) > RECENT_MAX_TOTAL_CHARS:
        entries.pop(0)
        cut = True

    refs: list[str] = []
    raw_refs = raw.get("refs")
    if isinstance(raw_refs, Sequence) and not isinstance(raw_refs, (str, bytes)):
        for ref in raw_refs:
            if not isinstance(ref, str) or not ref.strip() or len(ref) > REF_MAX_CHARS:
                cut = True
                continue
            if ref.strip() not in refs:
                refs.append(ref.strip())
        if len(refs) > REFS_MAX:
            refs = refs[:REFS_MAX]
            cut = True
    elif raw_refs not in (None, "", []):
        cut = True

    if not focus_text and not entries and not refs:
        return NONE
    return Conversation(
        focus=focus_text,
        recent=tuple(entries),
        refs=tuple(refs),
        state=TRUNCATED if cut else APPLIED,
    )


# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #

#: Separates the turn's tokens from `focus`'s in the token list `resolve` reads
#: for its free-standing-mention exception, so no phrase can span the two.
SEGMENT_BREAK = "\x00"

#: Origin labels: whose words reached an anchor.
ORIGIN_TURN, ORIGIN_FOCUS, ORIGIN_BOTH, ORIGIN_CONVERSATION = (
    "turn",
    "focus",
    "turn_and_focus",
    "conversation",
)


@dataclass(frozen=True, slots=True)
class Analyzed:
    """The conversation's own segments, each analysed on its own."""

    focus: Any = None
    entries: tuple[tuple[Entry, Any], ...] = ()

    def union(self, analysis: Any) -> Any:
        """The turn's analysis widened with every segment's words, for the ONE
        job of deciding which anchors the caller may see (`audience_view`).
        Never used to resolve: segments never pair."""
        from dataclasses import replace

        analyses = [a for a in (self.focus, *(item for _entry, item in self.entries)) if a is not None]
        if not analyses:
            return analysis
        tokens = analysis.tokens + tuple(t for a in analyses for t in a.tokens)
        ngrams = tuple(dict.fromkeys((*analysis.ngrams, *(g for a in analyses for g in a.ngrams))))
        return replace(analysis, tokens=tokens, ngrams=ngrams)

    def turn_tokens(self, analysis: Any) -> tuple[str, ...]:
        """The tokens `resolve` reads: the turn's, then `focus`'s behind a break."""
        if self.focus is None:
            return tuple(analysis.tokens)
        return (*analysis.tokens, SEGMENT_BREAK, *self.focus.tokens)


def analyze(conversation: Conversation, *, vocabulary: Any = None) -> Analyzed:
    """Analyse `focus` and each read entry once."""
    from . import working_set_resolve

    def one(text: str) -> Any:
        return working_set_resolve.analyze_turn(text, vocabulary=vocabulary)

    return Analyzed(
        focus=one(conversation.focus) if conversation.focus else None,
        entries=tuple((entry, one(entry.text)) for entry in conversation.read_entries()),
    )


def apply(
    candidates: Sequence[Any],
    segments: Analyzed,
    conversation: Conversation,
    *,
    rows: Sequence[Any],
    routing_targets: Sequence[Any] = (),
    term_anchor_counts: Mapping[str, int] | None = None,
    stopwords: frozenset[str] | None = None,
    rare_term_max_anchors: int | None = None,
) -> tuple[tuple[Any, ...], dict[str, str], tuple[tuple[Entry, Any, tuple[Any, ...]], ...]]:
    """Fold `focus` and the earlier turns into the turn's candidates.

    Returns `(candidates, origins, entries)`: the merged candidates, an origin
    label per anchor id that `focus` touched, and each read entry with its own
    analysis and the candidates its words alone reached (the carry walks these,
    so no entry is matched twice).

    * `focus` is a second segment of the CURRENT turn: worded contact kinds
      only (`FOCUS_KINDS`), no recall and no embedding, and it may create a
      candidate. Nothing pairs across segments.
    * An earlier entry, or a visible ref, only ever QUALIFIES an anchor the
      turn or `focus` already reached (`conversation`, a qualifier). It never
      creates a candidate.
    """
    from dataclasses import replace

    from . import working_set_resolve as resolve_module

    keywords: dict[str, Any] = {"term_anchor_counts": term_anchor_counts}
    if stopwords is not None:
        keywords["stopwords"] = stopwords
    if rare_term_max_anchors is not None:
        keywords["rare_term_max_anchors"] = rare_term_max_anchors

    turn_reached = {
        item.anchor_id for item in candidates if item.evidence & resolve_module.CONTACT_KINDS
    }
    origins: dict[str, str] = {}
    merged: dict[str, Any] = {item.anchor_id: item for item in candidates}
    if segments.focus is not None:
        for item in resolve_module.candidates_for(
            segments.focus, rows, bands={}, routing_targets=routing_targets, **keywords
        ):
            worded = item.evidence & FOCUS_KINDS
            if not worded:
                continue
            existing = merged.get(item.anchor_id)
            if existing is None:
                merged[item.anchor_id] = replace(item, evidence=frozenset(worded))
                origins[item.anchor_id] = ORIGIN_FOCUS
            else:
                merged[item.anchor_id] = replace(
                    existing,
                    evidence=existing.evidence | worded,
                    exact_alias_phrases=existing.exact_alias_phrases | item.exact_alias_phrases,
                )
                origins[item.anchor_id] = (
                    ORIGIN_BOTH if item.anchor_id in turn_reached else ORIGIN_FOCUS
                )

    named: set[str] = set()
    refs = frozenset(conversation.refs)
    if refs:
        named.update(row.anchor_id for row in rows if resolve_module.names_row(refs, row))
    entry_candidates: list[tuple[Entry, Any, tuple[Any, ...]]] = []
    for entry, analysis in segments.entries:
        drawn = resolve_module.candidates_for(analysis, rows, bands={}, **keywords)
        entry_candidates.append((entry, analysis, drawn))
        for item in drawn:
            if item.evidence & ENTRY_KINDS:
                named.add(item.anchor_id)
    for anchor_id in named & merged.keys():
        merged[anchor_id] = replace(
            merged[anchor_id], evidence=merged[anchor_id].evidence | {"conversation"}
        )
    ordered = sorted(merged.values(), key=resolve_module._candidate_order)
    return tuple(ordered[: resolve_module.MAX_CANDIDATES]), origins, tuple(entry_candidates)


@dataclass(frozen=True, slots=True)
class Carry:
    """The conversation carry's verdict for an anaphoric turn."""

    status: str = "none"  # none | one | ambiguous
    anchors: tuple[Any, ...] = ()


def carry(entries: Sequence[tuple[Entry, Any, Sequence[Any]]]) -> Carry:
    """Walk the user entries newest to oldest and stop at the first in which at
    least one anchor resolves under the ordinary rules applied to that entry's
    own text (no recall query, no embedding).

    Exactly one anchor is carried. Two or more make the turn ambiguous: the
    compiler does not choose. Assistant entries are never walked (the subject
    must have been the user's), and neither are refs (a read is not a referent).
    """
    from . import working_set_resolve as resolve_module

    for entry, analysis, drawn in entries:
        if entry.role != "user":
            continue
        resolution = resolve_module.resolve(tuple(drawn), turn_tokens=analysis.tokens)
        found = resolution.resolved_anchors
        if not found:
            continue
        if len(found) == 1 and resolution.status == "resolved":
            return Carry("one", found)
        return Carry("ambiguous", found)
    return Carry()
