"""Artifact renderer identity and honest PDF degradation."""

from __future__ import annotations

from membench.artifacts import pdf as pdf_renderer
from membench.artifacts import render_artifact
from membench.ids import sentinel
from membench.schema import ArtifactKind
from membench.templates.base import SourceContent


def _content(kind: ArtifactKind) -> SourceContent:
    return SourceContent(
        kind=kind,
        title="Quarterly reading",
        lines=["The drift index measured 41.3 points."],
        table=[["metric", "value"], ["drift index", "41.3"]],
    )


def test_png_render_is_stable_within_session() -> None:
    token = sentinel("SRC-TEST0001")
    first = render_artifact(_content(ArtifactKind.PNG), token)
    second = render_artifact(_content(ArtifactKind.PNG), token)
    assert first.logical_sha256 == second.logical_sha256
    assert first.bytes_sha256 == second.bytes_sha256
    assert first.data.startswith(b"\x89PNG")
    assert first.degradation is None


def test_logical_hash_tracks_content_not_bytes() -> None:
    token = sentinel("SRC-TEST0001")
    md = render_artifact(_content(ArtifactKind.MARKDOWN), token)
    changed = _content(ArtifactKind.MARKDOWN)
    changed.lines = ["The drift index measured 99.9 points."]
    other = render_artifact(changed, token)
    assert md.logical_sha256 != other.logical_sha256


def test_sentinel_is_visible_in_every_text_kind() -> None:
    token = sentinel("SRC-TEST0001")
    for kind in (ArtifactKind.MARKDOWN, ArtifactKind.CSV, ArtifactKind.TRANSCRIPT):
        result = render_artifact(_content(kind), token)
        assert token in result.data.decode("utf-8")


def test_pdf_degrades_honestly_when_renderer_missing(monkeypatch) -> None:
    def boom(content, token):  # type: ignore[no-untyped-def]
        raise pdf_renderer.PdfUnavailable("no pymupdf in this environment")

    monkeypatch.setattr("membench.artifacts.pdf_renderer.render", boom)
    token = sentinel("SRC-TEST0002")
    result = render_artifact(_content(ArtifactKind.PDF), token)
    assert result.actual_kind is ArtifactKind.PDF_UNAVAILABLE
    assert result.extension == "md"
    assert result.degradation and "pdf renderer unavailable" in result.degradation
    text = result.data.decode("utf-8")
    assert "[pdf-unavailable]" in text and token in text


def test_pdf_real_path_matches_environment() -> None:
    token = sentinel("SRC-TEST0003")
    result = render_artifact(_content(ArtifactKind.PDF), token)
    if pdf_renderer.pymupdf_version() is None:
        assert result.actual_kind is ArtifactKind.PDF_UNAVAILABLE
    else:  # pragma: no cover - environment dependent
        assert result.actual_kind is ArtifactKind.PDF
        assert result.data.startswith(b"%PDF")
