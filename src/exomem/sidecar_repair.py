"""Repair media sidecars that accumulated copies of themselves.

A sidecar that was re-rendered as `pending` filed its previous body under a
`## Preserved notes` heading and wrote a fresh extraction above it. Because the
re-rendered result was itself not canonical-pending-shaped, the next pass did the
same thing again — one whole copy of the sidecar per reconciliation pass, without
bound. Sidecars are chunked and embedded whole, so an N-times duplicated document
contributes N near-identical chunks and crowds the rest of the corpus out of any
ranked result.

The write path is fixed in `media_processing`; this module cleans up what the old
one left behind. It is deliberately conservative:

* The longest surviving `## Extracted text` block wins. That matters — for the
  sidecars whose top-level block was blanked by a re-render, the ONLY copy of the
  extraction lives in a nested `## Preserved notes` section, so truncating at the
  first heading (the obvious repair) would destroy it.
* Prose that is not regenerated scaffolding is kept, deduplicated, under a single
  `## Preserved notes` heading.
* Frontmatter is never rewritten here. A sidecar left `extracted_by: pending`
  stays pending on purpose, so the worker still re-extracts it from the binary and
  the recovered text is only the fallback if that fails.
* `repair` refuses to shorten a transcript: it is a pure function whose output is
  checked against the input before any write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .kbdir import kb_dirname
from .vault import parse_frontmatter, walk_vault_md

PRESERVED_HEADING = "## Preserved notes"
EXTRACTED_HEADING = "## Extracted text"

# Title + locator lines that `_render_sidecar` re-emits on every render.
_BOILERPLATE_RE = re.compile(
    r"(?m)^# Evidence: .*$\n?|^Preserved under `[^`]*`\.[ \t]*$\n?"
)


def _logical_text(content: str) -> str:
    """Match ``Path.read_text`` universal-newline semantics on guarded bytes."""

    return content.replace("\r\n", "\n").replace("\r", "\n")


def _segments(body: str) -> list[tuple[str, str]]:
    """Split `body` into (prose-before-extraction, extraction) per nesting level.

    Extracted text runs to the end of its nesting level, NOT to the next `## `
    heading: markitdown emits one `## <sheet name>` per spreadsheet tab and pandoc
    emits document headings, so a "stop at the next H2" reader sees an empty block
    and would silently drop the whole table on repair.
    """
    out: list[tuple[str, str]] = []
    for segment in body.split(PRESERVED_HEADING):
        index = segment.find(EXTRACTED_HEADING)
        if index == -1:
            out.append((segment, ""))
            continue
        out.append((segment[:index], segment[index + len(EXTRACTED_HEADING) :]))
    return out


def _is_repeated_extraction_residual(residual: str, extraction: str) -> bool:
    """Whether a preserved residual is only whole copies of the extraction."""
    if not extraction:
        return False
    copy = re.escape(extraction)
    return re.fullmatch(rf"{copy}(?:\s+{copy})*", residual) is not None


@dataclass(frozen=True)
class SidecarDamage:
    """What one over-rendered sidecar contains."""

    path: Path
    depth: int
    """Number of nested `## Preserved notes` sections."""
    distinct_extractions: int
    top_level_chars: int
    recovered_chars: int
    """Length of the longest surviving extraction."""
    duplicate_chars: int
    """Bytes the repair reclaims."""

    @property
    def recovery_only(self) -> bool:
        """The only copy of the extraction is buried in a nested section.

        Truncating at the first `## Preserved notes` would destroy these.
        """
        return self.top_level_chars == 0 and self.recovered_chars > 0


def analyze(content: str, path: Path) -> SidecarDamage | None:
    """Describe the duplication in `content`, or None when it is clean."""
    logical_content = _logical_text(content)
    _frontmatter, body, raw = parse_frontmatter(logical_content)
    body = body if raw is not None else logical_content
    if PRESERVED_HEADING not in body:
        return None
    # A single `## Preserved notes` holding genuine prose is the correct end
    # state, not damage — so "damaged" means "the repair would change this",
    # which also makes a repaired vault report clean on the next pass.
    repaired = repair(content)
    if _logical_text(repaired) == logical_content:
        return None
    segments = _segments(body)
    blocks = _extraction_blocks(body)
    # Deliberately the FIRST segment's block, empty or not — an empty one is what
    # makes a sidecar recovery-only.
    top = segments[0][1].strip() if segments else ""
    best = max(blocks, key=len) if blocks else ""
    return SidecarDamage(
        path=path,
        depth=body.count(PRESERVED_HEADING),
        distinct_extractions=len(set(blocks)),
        top_level_chars=len(top),
        recovered_chars=len(best),
        duplicate_chars=max(0, len(logical_content) - len(_logical_text(repaired))),
    )


def repair(content: str) -> str:
    """Return `content` with one extraction and no nesting. Pure; idempotent.

    Keeps frontmatter verbatim so a still-`pending` sidecar stays queued for a
    real re-extraction.
    """
    frontmatter_text, body = _split_frontmatter(content)
    body = _logical_text(body)
    if PRESERVED_HEADING not in body:
        return content

    segments = _segments(body)
    blocks = [extraction.strip() for _prose, extraction in segments if extraction.strip()]
    best = max(blocks, key=len) if blocks else ""

    head_text = "\n\n".join(
        line.strip() for line in _BOILERPLATE_RE.findall(segments[0][0]) if line.strip()
    )

    notes: list[str] = []
    for index, (prose, _extraction) in enumerate(segments):
        residual = _BOILERPLATE_RE.sub("", prose).strip()
        if index and _is_repeated_extraction_residual(residual, best):
            continue
        if residual and residual not in notes:
            notes.append(residual)

    parts = [head_text] if head_text else []
    parts.append(f"{EXTRACTED_HEADING}\n\n{best}".rstrip("\n"))
    if notes:
        parts.append(f"{PRESERVED_HEADING}\n\n" + "\n\n".join(notes))
    rebuilt = "\n\n".join(parts) + "\n"
    if frontmatter_text:
        separator = "" if frontmatter_text.endswith(("\n", "\r")) else "\n"
        return f"{frontmatter_text}{separator}{rebuilt}"
    return rebuilt


def repair_is_safe(original: str, repaired: str) -> bool:
    """Whether `repaired` keeps every character of the best extraction.

    The guard that makes this pass unable to lose content: a repair that would
    leave less transcript than the original held is refused, not written.
    """
    return _longest_extraction(repaired) >= _longest_extraction(original)


def iter_media_sidecars(vault_root: Path):
    """Yield every `<binary>.md` sidecar under the governed Evidence tree."""
    root = Path(vault_root)
    evidence_prefix = f"{kb_dirname()}/Evidence/"
    for entry in walk_vault_md(root):
        try:
            relative = entry.relative_to(root).as_posix()
        except ValueError:
            continue
        if (
            relative.startswith(evidence_prefix)
            and entry.suffix.lower() == ".md"
            and entry.with_suffix("").suffix
        ):
            yield entry


def _extraction_blocks(body: str) -> list[str]:
    return [
        extraction.strip()
        for _prose, extraction in _segments(body)
        if extraction.strip()
    ]


def _longest_extraction(content: str) -> int:
    content = _logical_text(content)
    blocks = _extraction_blocks(content)
    return len(max(blocks, key=len)) if blocks else 0


def _split_frontmatter(content: str) -> tuple[str, str]:
    """Split into (verbatim frontmatter block, body). Frontmatter is never edited."""
    if not content.startswith("---"):
        return "", content
    end = content.find("\n---", 3)
    if end == -1:
        return "", content
    boundary = end + len("\n---")
    if content.startswith("\r\n", boundary):
        boundary += 2
    elif content.startswith("\n", boundary):
        boundary += 1
    return content[:boundary], content[boundary:]
