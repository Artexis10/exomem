"""Artifact renderer identity and honest PDF/PNG degradation."""

from __future__ import annotations

from membench.artifacts import image as image_renderer
from membench.artifacts import pdf as pdf_renderer
from membench.artifacts import render_artifact, renderer_versions
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
    """Byte identity holds on whichever path this environment can take.

    Determinism is the claim, and it binds equally to the rendered PNG and to
    the degraded markdown standing in for it. Asserting the PNG magic here
    instead would make the test a Pillow-presence check.
    """

    token = sentinel("SRC-TEST0001")
    first = render_artifact(_content(ArtifactKind.PNG), token)
    second = render_artifact(_content(ArtifactKind.PNG), token)
    assert first.logical_sha256 == second.logical_sha256
    assert first.bytes_sha256 == second.bytes_sha256
    if image_renderer.pillow_version() is None:
        assert first.actual_kind is ArtifactKind.PNG_UNAVAILABLE
    else:  # pragma: no cover - environment dependent
        assert first.actual_kind is ArtifactKind.PNG
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


def test_png_degrades_honestly_when_renderer_missing(monkeypatch) -> None:
    """The PDF contract, owed equally by PNG.

    Without this the media extra is a hard dependency of corpus *generation*,
    not just of the image artifacts, so a clean checkout cannot regenerate the
    corpus at all — which is exactly what the replication kit must promise.
    """

    def boom(content, token):  # type: ignore[no-untyped-def]
        raise image_renderer.ImageUnavailable("no pillow in this environment")

    monkeypatch.setattr("membench.artifacts.image_renderer.render", boom)
    token = sentinel("SRC-TEST0004")
    result = render_artifact(_content(ArtifactKind.PNG), token)
    assert result.actual_kind is ArtifactKind.PNG_UNAVAILABLE
    assert result.extension == "md"
    assert result.degradation and "image renderer unavailable" in result.degradation
    text = result.data.decode("utf-8")
    assert "[png-unavailable]" in text and token in text


def test_png_real_path_matches_environment() -> None:
    token = sentinel("SRC-TEST0005")
    result = render_artifact(_content(ArtifactKind.PNG), token)
    if image_renderer.pillow_version() is None:
        assert result.actual_kind is ArtifactKind.PNG_UNAVAILABLE
    else:  # pragma: no cover - environment dependent
        assert result.actual_kind is ArtifactKind.PNG
        assert result.data.startswith(b"\x89PNG")


def test_renderer_versions_report_absent_rather_than_raising() -> None:
    """Provenance capture must survive a missing renderer.

    ``renderer_versions()`` runs on every generation to stamp the manifest; if
    it raises when Pillow is absent, the environment record dies before the
    degradation path it exists to document can be reported.
    """

    versions = renderer_versions()
    assert versions["pillow"] == ("absent" if image_renderer.pillow_version() is None else versions["pillow"])
    assert versions["pymupdf"] == ("absent" if pdf_renderer.pymupdf_version() is None else versions["pymupdf"])
    assert all(isinstance(value, str) for value in versions.values())
