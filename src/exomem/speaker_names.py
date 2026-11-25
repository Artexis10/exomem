"""Speaker labels as the lines of an extraction carry them, and the names that replace them.

The sidecar stores no turn list: the labelled lines are the stored turns. A line carries its
label in one of the two grammars that the renderers write (`speaker_assignment.label_transcript`):
a timed line, `[m:ss] [Speaker B]: text`, or a merged turn, `[Speaker B]: text`. This module reads
and rewrites only that label span, so every other byte of a page stays as it was.

`speaker_names` is the extraction field that records each name with the label it replaced and
who gave it. A reprocess carries the names of the replaced extraction to the new labels by the
lines they overlap (`carry`).
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass

from .semantic_segments import TIMED_LINE_RE, ts_from_match

#: Who gave a name: a person (told the agent, or named it) or a voice profile that matched.
BASIS_PERSON = "person"
BASIS_PROFILE = "profile"

#: A name is one short line: a long one would swamp every line that carries it.
MAX_NAME_CHARS = 80

#: The renderer's merged-turn grammar, `[Speaker B]: text`. A timed line never matches it, because
#: its first bracket is a time that no `:` follows directly.
_MERGED_TURN_RE = re.compile(r"^\[([^\]\n]+)\]:")


def name_problem(name: object, noun: str = "a name") -> str | None:
    """Why `name` cannot stand in a label span, or None. The rule is the label syntax's own.

    A speaker's name and a track both go into the span, so both take this check; `noun` words the reason.
    """
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        return f"{noun} must be text with no space at its start or end"
    if len(name.splitlines()) != 1:
        return f"{noun} must be one line"
    if any(character in name for character in "[]:"):
        return f"{noun} must not hold [, ] or :, which the label syntax uses"
    if len(name) > MAX_NAME_CHARS:
        return f"{noun} is at most {MAX_NAME_CHARS} characters"
    return None


def label_span(line: str) -> tuple[int, int] | None:
    """Where the speaker label sits in `line` (without its line ending), or None for an unlabelled line."""
    timed = TIMED_LINE_RE.match(line)
    if timed is not None:
        return timed.span(4) if timed.group(4) is not None else None
    merged = _MERGED_TURN_RE.match(line)
    return merged.span(1) if merged is not None else None


def _lines(text: str) -> Iterable[tuple[str, str]]:
    """Each line of `text` as `(content, line ending)`."""
    for raw in text.splitlines(keepends=True):
        content = raw.rstrip("\r\n")
        yield content, raw[len(content):]


def labels_in(text: str) -> list[str]:
    """The labels that the lines of `text` carry, in order of first appearance."""
    found: list[str] = []
    for content, _ending in _lines(text):
        span = label_span(content)
        if span is not None and content[span[0]:span[1]] not in found:
            found.append(content[span[0]:span[1]])
    return found


def lines_of(text: str, label: str) -> list[str]:
    """The lines of `text` that carry `label`, without their line endings."""
    found = []
    for content, _ending in _lines(text):
        span = label_span(content)
        if span is not None and content[span[0]:span[1]] == label:
            found.append(content)
    return found


def has_timed_lines(text: str) -> bool:
    """Whether any line of `text` starts with a time: false for merged turns and for a plain transcript."""
    return any(TIMED_LINE_RE.match(content) is not None for content, _ending in _lines(text))


def rename_labels(text: str, names: Mapping[str, str]) -> str:
    """`text` with each label in `names` replaced by its name, in the label span of the line alone."""
    out: list[str] = []
    for content, ending in _lines(text):
        span = label_span(content)
        if span is not None and content[span[0]:span[1]] in names:
            content = content[: span[0]] + names[content[span[0]:span[1]]] + content[span[1]:]
        out.append(content + ending)
    return "".join(out)


@dataclass(frozen=True)
class Carried:
    """The names of a replaced extraction that reached the new one, and those that did not."""

    #: New label to the name it takes.
    labels: dict[str, str]
    #: Each carried name with the new label it replaced and its basis, as `speaker_names` records it.
    entries: tuple[dict[str, str], ...]
    #: The names of the replaced extraction that no new label took.
    not_carried: tuple[str, ...]


def timed_spans(text: str) -> list[tuple[float, float, str]]:
    """`(start, end, label)` of each timed labelled line, in start order; a line runs to the next later start.

    The spans never overlap, so a caller can sweep them in one pass. The last line has no next
    start, so it has no span. A merged turn has no start at all.
    """
    starts: list[tuple[float, str | None]] = []
    for content, _ending in _lines(text):
        timed = TIMED_LINE_RE.match(content)
        if timed is not None:
            starts.append((ts_from_match(timed), timed.group(4)))
    starts.sort(key=lambda item: item[0])  # a no-op for the renderers' own order, and Timsort is linear on it
    return [
        (start, following, label)
        for (start, label), (following, _next_label) in zip(starts, starts[1:], strict=False)
        if label is not None and following > start
    ]


def _shared_time(
    spans: list[tuple[float, float, str]], named: list[tuple[float, float, str]]
) -> dict[str, float]:
    """For each label of `spans`, its seconds inside `named`: one sweep over both start-ordered, disjoint lists."""
    shared: dict[str, float] = defaultdict(float)
    i = j = 0
    while i < len(spans) and j < len(named):
        start, end, label = spans[i]
        named_start, named_end, _name = named[j]
        overlap = min(end, named_end) - max(start, named_start)
        if overlap > 0:
            shared[label] += overlap
        if end <= named_end:
            i += 1
        else:
            j += 1
    return shared


def carry(
    old_text: str,
    old_names: Iterable[Mapping[str, str]],
    new_text: str,
    *,
    keep_labels: Collection[str] = (),
) -> Carried:
    """Carry each recorded name of `old_text` to the label of `new_text` whose lines overlap its lines most.

    A name carries only where the overlap covers more than half of that label's speech time, so a
    majority needs no tuning. A name never reaches two labels, and a label never takes two names.
    A label in `keep_labels` already has the name that its caller gave, which no carried name
    replaces: the old name it would have taken is listed as not carried.

    The cost is one sweep of the new lines for each recorded name, so it grows with the length of
    the recording and the few names it has.
    """
    named = [(entry["name"], entry) for entry in old_names]
    names = {name for name, _entry in named}
    old_by_name: dict[str, list[tuple[float, float, str]]] = defaultdict(list)
    for span in timed_spans(old_text):
        if span[2] in names:
            old_by_name[span[2]].append(span)
    new_spans = [span for span in timed_spans(new_text) if span[2] not in keep_labels]
    speech: dict[str, float] = defaultdict(float)
    for start, end, label in new_spans:
        speech[label] += end - start

    overlaps: list[tuple[float, str, str]] = []
    for name, old_spans in old_by_name.items():
        shared = _shared_time(new_spans, old_spans)
        overlaps += [(seconds, label, name) for label, seconds in shared.items() if seconds * 2 > speech[label]]
    labels: dict[str, str] = {}
    taken: set[str] = set()
    for _shared, label, name in sorted(overlaps, key=lambda item: (-item[0], item[1], item[2])):
        if label not in labels and name not in taken:
            labels[label] = name
            taken.add(name)
    entries = tuple(
        {"name": name, "label": label, "basis": entry.get("basis", BASIS_PERSON)}
        for label, name in labels.items()
        for old_name, entry in named
        if old_name == name
    )
    return Carried(labels, entries, tuple(name for name, _entry in named if name not in taken))


def with_names(
    text_labels: Iterable[str], entries: Iterable[Mapping[str, str]], current: str | None
) -> str | None:
    """The `speaker_verification` after naming: `human-verified` only when a person named every label."""
    labels = list(text_labels)
    given = {entry["name"] for entry in entries if entry.get("basis") == BASIS_PERSON}
    if labels and all(label in given for label in labels):
        return "human-verified"
    return current


def rename_entries(
    entries: Iterable[Mapping[str, str]], renames: Mapping[str, str]
) -> list[dict[str, str]]:
    """`speaker_names` after a person renamed labels: each name keeps the label it first replaced."""
    result = [dict(entry) for entry in entries]
    for current, new in renames.items():
        for entry in result:
            if entry["name"] == current:
                entry["name"], entry["basis"] = new, BASIS_PERSON
                break
        else:
            result.append({"name": new, "label": current, "basis": BASIS_PERSON})
    return result
