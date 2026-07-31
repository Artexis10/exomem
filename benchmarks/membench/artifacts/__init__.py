"""Artifact renderers: stdlib + Pillow only; PDF degrades honestly.

Every renderer returns bytes plus the canonical *logical* payload whose hash
is the artifact's primary identity (byte hashes are secondary and scoped to
recorded renderer versions).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from membench.artifacts import image as image_renderer
from membench.artifacts import markdown as markdown_renderer
from membench.artifacts import pdf as pdf_renderer
from membench.artifacts import tabular as tabular_renderer
from membench.artifacts import transcript as transcript_renderer
from membench.schema import ArtifactKind
from membench.templates.base import SourceContent

_EXTENSIONS = {
    ArtifactKind.MARKDOWN: "md",
    ArtifactKind.CSV: "csv",
    ArtifactKind.PNG: "png",
    ArtifactKind.PDF: "pdf",
    ArtifactKind.PDF_UNAVAILABLE: "md",
    ArtifactKind.TRANSCRIPT: "txt",
}


@dataclass(frozen=True)
class RenderResult:
    data: bytes
    extension: str
    actual_kind: ArtifactKind
    logical_sha256: str
    bytes_sha256: str
    degradation: str | None = None


def logical_payload(content: SourceContent, sentinel: str, actual_kind: ArtifactKind) -> dict:
    return {
        "kind": actual_kind.value,
        "title": content.title,
        "lines": list(content.lines),
        "table": content.table,
        "sentinel": sentinel,
    }


def renderer_versions() -> dict[str, str]:
    versions = {"membench-artifacts": "0.1.0", "pillow": image_renderer.pillow_version()}
    versions["pymupdf"] = pdf_renderer.pymupdf_version() or "absent"
    return versions


def render_artifact(content: SourceContent, sentinel: str) -> RenderResult:
    kind = content.kind
    degradation: str | None = None
    if kind is ArtifactKind.MARKDOWN:
        data = markdown_renderer.render(content, sentinel).encode("utf-8")
    elif kind is ArtifactKind.CSV:
        data = tabular_renderer.render(content, sentinel).encode("utf-8")
    elif kind is ArtifactKind.TRANSCRIPT:
        data = transcript_renderer.render(content, sentinel).encode("utf-8")
    elif kind is ArtifactKind.PNG:
        data = image_renderer.render(content, sentinel)
    elif kind is ArtifactKind.PDF:
        try:
            data = pdf_renderer.render(content, sentinel)
        except pdf_renderer.PdfUnavailable as exc:
            kind = ArtifactKind.PDF_UNAVAILABLE
            degradation = f"{content.title}: pdf renderer unavailable ({exc}); emitted markdown"
            data = markdown_renderer.render(content, sentinel, banner="[pdf-unavailable]").encode(
                "utf-8"
            )
    else:  # pragma: no cover - enum is closed
        raise ValueError(f"unsupported artifact kind {kind}")
    logical = json.dumps(
        logical_payload(content, sentinel, kind), sort_keys=True, ensure_ascii=False
    ).encode("utf-8")
    return RenderResult(
        data=data,
        extension=_EXTENSIONS[kind],
        actual_kind=kind,
        logical_sha256=hashlib.sha256(logical).hexdigest(),
        bytes_sha256=hashlib.sha256(data).hexdigest(),
        degradation=degradation,
    )
