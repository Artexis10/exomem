"""Deterministic semantic block extraction over ordinary Markdown.

Semantic blocks are source-spanned knowledge units derived from files. This
module deliberately stays parser-only: it reads Markdown/frontmatter, returns
structured measurements, and never writes notes or invokes a model by default.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import vault as vault_module

BLOCK_KINDS: frozenset[str] = frozenset({
    "source",
    "evidence",
    "claim",
    "finding",
    "decision",
    "assumption",
    "constraint",
    "risk",
    "failure",
    "experiment",
    "result",
    "pattern",
    "requirement",
    "action",
    "entity",
    "project",
    "case",
    "timeline_event",
    "media_segment",
})

_PAGE_TYPE_TO_KIND: dict[str, str] = {
    "source": "source",
    "insight": "claim",
    "failure": "failure",
    "pattern": "pattern",
    "experiment": "experiment",
    "entity": "entity",
    "research-note": "finding",
    "production-log": "result",
}

_LABEL_TO_KIND: dict[str, str] = {
    "source": "source",
    "sources": "source",
    "evidence": "evidence",
    "proof": "evidence",
    "claim": "claim",
    "claims": "claim",
    "finding": "finding",
    "findings": "finding",
    "decision": "decision",
    "decisions": "decision",
    "assumption": "assumption",
    "assumptions": "assumption",
    "constraint": "constraint",
    "constraints": "constraint",
    "risk": "risk",
    "risks": "risk",
    "failure": "failure",
    "failures": "failure",
    "experiment": "experiment",
    "experiments": "experiment",
    "result": "result",
    "results": "result",
    "outcome": "result",
    "outcomes": "result",
    "pattern": "pattern",
    "patterns": "pattern",
    "requirement": "requirement",
    "requirements": "requirement",
    "action": "action",
    "actions": "action",
    "todo": "action",
    "todos": "action",
    "next action": "action",
    "next actions": "action",
    "entity": "entity",
    "entities": "entity",
    "project": "project",
    "projects": "project",
    "case": "case",
    "cases": "case",
    "timeline": "timeline_event",
    "timeline event": "timeline_event",
    "timeline events": "timeline_event",
    "event": "timeline_event",
    "events": "timeline_event",
    "media segment": "media_segment",
    "media segments": "media_segment",
    "segment": "media_segment",
    "segments": "media_segment",
}

_FM_PATTERN = re.compile(r"^---\n(.*?)\n---\n?(.*)", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_LIST_LABEL_RE = re.compile(
    r"^\s*[-*+]\s+(?:\[(?P<bracket>[A-Za-z][A-Za-z0-9 _-]{0,40})\]|"
    r"(?P<label>[A-Za-z][A-Za-z0-9 _-]{1,40}):)\s+(?P<text>.+?)\s*$"
)
_H1_RE = re.compile(r"^#\s+(.+?)\s*#*\s*$", re.MULTILINE)


@dataclass(frozen=True)
class SemanticBlock:
    key: str
    kind: str
    path: str
    text: str
    source_hash: str
    anchor: str | None
    heading: str | None
    line_start: int | None
    line_end: int | None
    metadata: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "kind": self.kind,
            "path": self.path,
            "text": self.text,
            "source_hash": self.source_hash,
            "anchor": self.anchor,
            "heading": self.heading,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class SemanticBlockExtraction:
    blocks: tuple[SemanticBlock, ...]
    suggestions: tuple[SemanticBlock, ...] = ()
    warnings: tuple[str, ...] = ()
    model_suggestions_available: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "blocks": [b.as_dict() for b in self.blocks],
            "suggestions": [b.as_dict() for b in self.suggestions],
            "warnings": list(self.warnings),
            "model_suggestions_available": self.model_suggestions_available,
        }


ModelSuggestor = Callable[[str, str, dict[str, Any]], Iterable[SemanticBlock]]


def extract_semantic_blocks(
    path: Path | str,
    *,
    vault_root: Path | str | None = None,
    text: str | None = None,
    include_model_suggestions: bool = False,
    model_suggestor: ModelSuggestor | None = None,
) -> SemanticBlockExtraction:
    """Extract deterministic semantic blocks from one Markdown file.

    `include_model_suggestions` is a response-only seam. With no injected
    suggestor, it soft-fails with a warning and does not import optional model
    packages. Callers that later wire a local classifier can pass a suggestor;
    its results are returned as suggestions, never accepted blocks.
    """
    source_path = Path(path)
    raw_text = text if text is not None else source_path.read_text(encoding="utf-8")
    root = Path(vault_root) if vault_root is not None else None
    rel_path = _rel_path(source_path, root)
    fm, body, body_line_offset = _split_frontmatter(raw_text)
    title = _title(body, source_path)
    file_hash = _hash(raw_text)

    blocks: list[SemanticBlock] = []
    page_kind = _page_kind(fm)
    if page_kind is not None:
        page_text = _page_text(title, body)
        if page_text:
            blocks.append(
                _make_block(
                    kind=page_kind,
                    rel_path=rel_path,
                    text=page_text,
                    anchor="page",
                    heading=None,
                    line_start=_h1_line(body, body_line_offset) or body_line_offset,
                    line_end=None,
                    metadata={
                        "origin": "frontmatter",
                        "page_type": fm.get("type"),
                        "title": title,
                        "file_hash": file_hash,
                        "wikilinks": _wikilinks(page_text),
                    },
                )
            )

    blocks.extend(
        _section_blocks(
            body=body,
            body_line_offset=body_line_offset,
            rel_path=rel_path,
            file_hash=file_hash,
        )
    )
    blocks.extend(
        _list_label_blocks(
            body=body,
            body_line_offset=body_line_offset,
            rel_path=rel_path,
            file_hash=file_hash,
        )
    )

    warnings: list[str] = []
    suggestions: tuple[SemanticBlock, ...] = ()
    model_available = False
    if include_model_suggestions:
        if model_suggestor is None:
            warnings.append("model-backed semantic block suggestions unavailable")
        else:
            try:
                suggestions = tuple(model_suggestor(body, rel_path, fm))
                model_available = True
            except Exception as exc:  # noqa: BLE001 - optional response-only seam
                warnings.append(
                    f"model-backed semantic block suggestions unavailable: {exc}"
                )

    deduped = tuple(_dedupe_blocks(blocks))
    return SemanticBlockExtraction(
        blocks=deduped,
        suggestions=suggestions,
        warnings=tuple(warnings),
        model_suggestions_available=model_available,
    )


def _rel_path(path: Path, vault_root: Path | None) -> str:
    if vault_root is not None:
        try:
            return path.resolve().relative_to(vault_root.resolve()).as_posix()
        except (ValueError, OSError):
            pass
    return path.as_posix().replace("\\", "/")


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str, int]:
    m = _FM_PATTERN.match(text)
    if not m:
        return {}, text, 1
    fm, body, _ = vault_module.parse_frontmatter(text)
    raw_body = m.group(2)
    body_start = m.start(2) + (1 if raw_body.startswith("\n") else 0)
    return fm, body, text[:body_start].count("\n") + 1


def _page_kind(fm: dict[str, Any]) -> str | None:
    raw_type = fm.get("type")
    if raw_type is None:
        return None
    kind = _PAGE_TYPE_TO_KIND.get(str(raw_type).strip().lower())
    return kind if kind in BLOCK_KINDS else None


def _title(body: str, path: Path) -> str:
    m = _H1_RE.search(body)
    return m.group(1).strip() if m else path.stem


def _h1_line(body: str, body_line_offset: int) -> int | None:
    for idx, line in enumerate(body.splitlines(), start=body_line_offset):
        if _H1_RE.match(line):
            return idx
    return None


def _page_text(title: str, body: str) -> str:
    lede = _first_paragraph(body)
    if title and lede:
        return f"{title}\n\n{lede}"
    return title or lede


def _first_paragraph(body: str) -> str:
    buf: list[str] = []
    in_fence = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            if buf:
                break
            continue
        if _HEADING_RE.match(stripped):
            if buf:
                break
            continue
        buf.append(stripped.lstrip("-*+ ").strip())
    return _collapse(" ".join(buf))


def _section_blocks(
    *,
    body: str,
    body_line_offset: int,
    rel_path: str,
    file_hash: str,
) -> list[SemanticBlock]:
    lines = body.splitlines()
    headings: list[tuple[int, int, str, str]] = []
    in_fence = False
    for idx, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(line.strip())
        if not m:
            continue
        heading = m.group(2).strip()
        kind = _kind_for_label(heading)
        if kind is not None:
            headings.append((idx, len(m.group(1)), heading, kind))

    out: list[SemanticBlock] = []
    for pos, (idx, _level, heading, kind) in enumerate(headings):
        next_idx = len(lines)
        for later_idx, line in enumerate(lines[idx + 1:], start=idx + 1):
            if _FENCE_RE.match(line):
                # Do not split on headings inside fenced code; the loop below
                # strips matching fence lines from block text separately.
                continue
            if _HEADING_RE.match(line.strip()):
                next_idx = later_idx
                break
        section_lines = lines[idx + 1:next_idx]
        text = _block_text(section_lines)
        if not text:
            continue
        line_start = body_line_offset + idx
        line_end = body_line_offset + max(idx, next_idx - 1)
        out.append(
            _make_block(
                kind=kind,
                rel_path=rel_path,
                text=text,
                anchor=_anchor(heading),
                heading=heading,
                line_start=line_start,
                line_end=line_end,
                metadata={
                    "origin": "heading",
                    "heading_level": _level,
                    "file_hash": file_hash,
                    "wikilinks": _wikilinks(text),
                },
            )
        )
        # Prevent duplicate headings from producing duplicate identical blocks
        # when headings list includes two recognized headings at the same index.
        if pos >= len(headings):
            break
    return out


def _list_label_blocks(
    *,
    body: str,
    body_line_offset: int,
    rel_path: str,
    file_hash: str,
) -> list[SemanticBlock]:
    out: list[SemanticBlock] = []
    in_fence = False
    for idx, line in enumerate(body.splitlines(), start=body_line_offset):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _LIST_LABEL_RE.match(line)
        if not m:
            continue
        label = (m.group("bracket") or m.group("label") or "").strip()
        kind = _kind_for_label(label)
        if kind is None:
            continue
        text = _collapse(m.group("text"))
        if not text:
            continue
        out.append(
            _make_block(
                kind=kind,
                rel_path=rel_path,
                text=text,
                anchor=f"line-{idx}",
                heading=None,
                line_start=idx,
                line_end=idx,
                metadata={
                    "origin": "list_label",
                    "label": label,
                    "file_hash": file_hash,
                    "wikilinks": _wikilinks(text),
                },
            )
        )
    return out


def _block_text(lines: list[str]) -> str:
    kept: list[str] = []
    in_fence = False
    for line in lines:
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            kept.append(line.rstrip())
            continue
        stripped = line.strip()
        if not stripped:
            if kept and kept[-1]:
                kept.append("")
            continue
        kept.append(stripped.lstrip("-*+ ").strip())
    return _collapse(" ".join(part for part in kept if part))


def _kind_for_label(label: str) -> str | None:
    key = " ".join(
        str(label)
        .strip()
        .lower()
        .replace("-", " ")
        .replace("_", " ")
        .rstrip(":")
        .split()
    )
    kind = _LABEL_TO_KIND.get(key)
    return kind if kind in BLOCK_KINDS else None


def _anchor(heading: str) -> str:
    text = heading.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_]+", "-", text)
    return text.strip("-") or "section"


def _make_block(
    *,
    kind: str,
    rel_path: str,
    text: str,
    anchor: str | None,
    heading: str | None,
    line_start: int | None,
    line_end: int | None,
    metadata: dict[str, Any],
) -> SemanticBlock:
    normalized_text = _collapse(text)
    source_hash = _hash(normalized_text)
    identity = "\n".join([rel_path, kind, anchor or "", normalized_text])
    return SemanticBlock(
        key=f"{kind}:{_hash(identity)}",
        kind=kind,
        path=rel_path,
        text=normalized_text,
        source_hash=source_hash,
        anchor=anchor,
        heading=heading,
        line_start=line_start,
        line_end=line_end,
        metadata=metadata,
    )


def _wikilinks(text: str) -> list[str]:
    return [m.group(1).strip() for m in vault_module.find_body_wikilinks(text)]


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "")).strip()


def _dedupe_blocks(blocks: Iterable[SemanticBlock]) -> list[SemanticBlock]:
    out: list[SemanticBlock] = []
    seen: set[str] = set()
    for block in blocks:
        if block.key in seen:
            continue
        seen.add(block.key)
        out.append(block)
    return out
