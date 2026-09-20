"""Contract tests for the CPU-only document/OCR container image."""

from __future__ import annotations

import runpy
import tomllib
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

import pytest

from exomem import extract

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def document_fixtures(tmp_path: Path) -> Path:
    for module in ("fitz", "markitdown", "openpyxl", "PIL", "pptx"):
        pytest.importorskip(module)
    generate = runpy.run_path(str(ROOT / "tests/fixtures/documents_cpu/generate.py"))["generate"]
    generate(tmp_path)
    return tmp_path


def test_cpu_document_profile_installs_only_document_and_ocr_dependencies() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    extras = project["project"]["optional-dependencies"]

    assert extras["documents"] == [
        "pymupdf>=1.24",
        "markitdown[docx,xlsx,pptx]>=0.1.1",
    ]
    assert extras["ocr"] == [
        "pytesseract>=0.3.10",
        "pillow>=10.0",
    ]
    assert "faster-whisper" not in "\n".join(extras["documents"] + extras["ocr"])
    assert "ctranslate2" not in "\n".join(extras["documents"] + extras["ocr"])


def test_dockerfile_defines_an_unpublished_cpu_document_target_before_default_lean() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "FROM builder-lean AS builder-documents-cpu" in text
    assert "uv sync --frozen --no-dev --no-editable --extra documents --extra ocr" in text
    assert "FROM python:3.12-slim AS documents-cpu" in text
    documents = text.split("FROM python:3.12-slim AS documents-cpu", 1)[1].split(
        "FROM python:3.12-slim AS lean", 1
    )[0]
    assert "apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng" in documents
    assert "USER 10001:10001" in documents
    assert 'ENTRYPOINT ["exomem"]' in documents
    assert 'CMD ["--help"]' in documents
    assert "VOLUME" not in documents
    assert "EXPOSE" not in documents
    assert text.index("FROM python:3.12-slim AS documents-cpu") < text.index(
        "FROM python:3.12-slim AS lean"
    )


def test_document_cpu_acceptance_uses_a_unique_host_run_directory() -> None:
    script = (ROOT / "scripts/verify-document-cpu-image.sh").read_text(encoding="utf-8")

    assert "run_dir=$(mktemp -d /tmp/exomem-documents-cpu.XXXXXX)" in script
    assert 'tag="exomem-documents-cpu:${run_dir##*/}"' in script
    assert 'export BUILDX_CONFIG="${BUILDX_CONFIG:-$run_dir/buildx}"' in script
    assert 'before="$run_dir/input-hashes-before"' in script
    assert 'docker image rm "$tag"' in script
    assert 'docker image rm -f "$tag"' not in script


def test_document_fixture_generator_is_checked_in_as_readable_source() -> None:
    generator = ROOT / "tests/fixtures/documents_cpu/generate.py"

    assert generator.is_file()
    text = generator.read_text(encoding="utf-8")
    assert "def generate" in text
    assert "ZipFile" in text
    assert "fitz" in text


def test_document_fixture_has_a_referenced_external_link_without_fetching(
    document_fixtures: Path,
) -> None:
    docx = document_fixtures / "table.docx"

    with ZipFile(docx) as archive:
        document = archive.read("word/document.xml").decode()
        relationships = archive.read("word/_rels/document.xml.rels").decode()

    assert 'r:id="rIdExternal"' in document
    assert 'Target="https://external-link.invalid/synthetic?source=fixture"' in relationships
    assert 'TargetMode="External"' in relationships
    assert "External Fixture Link" in extract.extract_text(docx).text


def test_document_converter_uses_markitdown_local_conversion(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class LocalOnlyMarkItDown:
        def __init__(self, *, enable_plugins: bool) -> None:
            assert enable_plugins is False

        def convert(self, _path: str) -> None:
            raise AssertionError("generic conversion must not be used for a local file")

        def convert_local(self, path: Path) -> SimpleNamespace:
            assert path.name == "report.docx"
            return SimpleNamespace(text_content="local document text")

    monkeypatch.setitem(
        __import__("sys").modules,
        "markitdown",
        SimpleNamespace(MarkItDown=LocalOnlyMarkItDown),
    )

    result = extract._extract_document(tmp_path / "report.docx", "docx")

    assert result.text == "local document text"


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("corrupt.docx", "markitdown could not convert"),
        ("unsupported.bin", "no extractor"),
        ("protected.pdf", "password-protected"),
    ],
)
def test_document_profile_reports_unavailable_inputs_truthfully(
    document_fixtures: Path, name: str, message: str
) -> None:
    with pytest.raises(extract.ExtractionUnavailable, match=message):
        extract.extract_text(document_fixtures / name)
