from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from exomem import find as find_module

REPO_ROOT = Path(__file__).resolve().parents[1]
CRATE_ROOT = REPO_ROOT / "experiments" / "rust_find_keyword_lite"
SCRATCH_ROOT = CRATE_ROOT / "target" / "pytest-vaults"


def _fresh_vault(name: str) -> Path:
    root = (SCRATCH_ROOT / name).resolve()
    scratch = SCRATCH_ROOT.resolve()
    if not str(root).startswith(str(scratch)):
        raise RuntimeError(f"scratch path escaped target dir: {root}")
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    return root



@pytest.fixture
def tmp_path(request: pytest.FixtureRequest) -> Path:
    name = hashlib.sha1(request.node.nodeid.encode("utf-8")).hexdigest()[:12]
    return _fresh_vault(f"pytest-fixture-{name}")

def _write_page(
    root: Path,
    rel: str,
    body: str,
    *,
    title: str | None = None,
    updated: str = "2026-01-01",
) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    t = title or Path(rel).stem
    p.write_text(
        f"---\ntype: insight\ntitle: {t}\nupdated: {updated}\n---\n# {t}\n\n{body}\n",
        encoding="utf-8",
    )


def _parity_corpus(root: Path) -> None:
    _write_page(root, "Knowledge Base/plain.md", "employment contract terms", updated="2026-03-01")
    _write_page(root, "Knowledge Base/midword.md", "the xylophones sang loudly", updated="2026-02-01")
    _write_page(root, "Knowledge Base/title-only.md", "unrelated body", title="Budget Overview", updated="2026-04-01")
    _write_page(root, "Knowledge Base/short.md", "xq marks the spot", updated="2026-01-15")
    _write_page(root, "Knowledge Base/meta.md", "growth was 42% in snake_case", updated="2026-01-10")
    _write_page(root, "Knowledge Base/uni.md", "tere tulemast Tallinnasse sõbrad", updated="2026-01-05")
    _write_page(root, "Knowledge Base/sub/nested.md", "employment law contract precedent", updated="2026-05-01")
    _write_page(root, "Knowledge Base/index.md", "employment xylophones budget xq", updated="2026-06-01")
    _write_page(root, "Knowledge Base/punct.md", "+++ ~~~ !!!", updated="2026-01-02")
    _write_page(root, "Knowledge Base/same-date-b.md", "twin content marker", updated="2026-02-02")
    _write_page(root, "Knowledge Base/same-date-a.md", "twin content marker", updated="2026-02-02")


PARITY_QUERIES = [
    "contract employment",
    "ylophon",
    "budget",
    "xq",
    "q",
    "42%",
    "e_c",
    "sõbra",
    "tallinnasse",
    "~~~",
    "twin marker",
    "xq spot",
    "employment",
    "zzz-no-such-token",
    "",
]


@pytest.fixture(scope="session")
def rust_keyword_bin() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not installed")
    manifest = CRATE_ROOT / "Cargo.toml"
    subprocess.run(
        [cargo, "build", "--release", "--manifest-path", str(manifest)],
        check=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    exe = "rust_find_keyword_lite.exe" if os.name == "nt" else "rust_find_keyword_lite"
    return CRATE_ROOT / "target" / "release" / exe


@pytest.fixture(scope="session")
def rust_write_bin() -> Path:
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("cargo not installed")
    manifest = CRATE_ROOT / "Cargo.toml"
    subprocess.run(
        [cargo, "build", "--release", "--manifest-path", str(manifest)],
        check=True,
        cwd=REPO_ROOT,
        timeout=120,
    )
    exe = "rust_write_lite.exe" if os.name == "nt" else "rust_write_lite"
    return CRATE_ROOT / "target" / "release" / exe


def _rust_hits(bin_path: Path, vault: Path, query: str, limit: int = 100) -> list[dict]:
    out = subprocess.run(
        [str(bin_path), "--vault", str(vault), "--query", query, "--limit", str(limit)],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )
    return json.loads(out.stdout)["hits"]


@pytest.mark.parametrize("query", PARITY_QUERIES)
def test_rust_keyword_lite_matches_python_keyword_lane(
    rust_keyword_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
    query: str,
) -> None:
    name = hashlib.sha1(query.encode("utf-8")).hexdigest()[:12] or "empty"
    vault = _fresh_vault(f"parity-{name}")
    _parity_corpus(vault)
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    query_norm = query.lower().strip()
    expected = find_module._keyword_match_paths(vault, query_norm, "kb")[:100]
    got = [hit["path"] for hit in _rust_hits(rust_keyword_bin, vault, query)]
    assert got == expected


def test_rust_keyword_lite_compact_fields_match_python_find(
    rust_keyword_bin: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = _fresh_vault("compact-fields")
    _parity_corpus(vault)
    monkeypatch.setenv("EXOMEM_LEXICAL_BACKEND", "python")
    py_hits = find_module.find(
        vault,
        query="contract employment",
        mode="keyword",
        scope="kb-only",
        graph=False,
        limit=100,
    )
    expected = [
        {"path": hit.path, "title": hit.title, "updated": hit.updated}
        for hit in py_hits
    ]
    assert _rust_hits(rust_keyword_bin, vault, "contract employment") == expected


def test_rust_write_lite_edit_updates_frontmatter_and_body(rust_write_bin: Path) -> None:
    vault = _fresh_vault("write-edit")
    rel = "Knowledge Base/Notes/Insights/write-target.md"
    _write_page(vault, rel, "old marker", title="Write Target", updated="2026-01-01")

    out = subprocess.run(
        [
            str(rust_write_bin),
            "edit",
            "--vault",
            str(vault),
            "--path",
            rel,
            "--old",
            "old marker",
            "--new",
            "new marker",
            "--date",
            "2026-07-07",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )

    payload = json.loads(out.stdout)
    text = (vault / rel).read_text(encoding="utf-8")
    assert payload["op"] == "edit"
    assert "updated: 2026-07-07" in text
    assert "new marker" in text
    assert "old marker" not in text


def test_rust_write_lite_note_creates_frontmatter_page(rust_write_bin: Path) -> None:
    vault = _fresh_vault("write-note")
    rel = "Knowledge Base/Notes/Insights/rust-created-note.md"

    out = subprocess.run(
        [
            str(rust_write_bin),
            "note",
            "--vault",
            str(vault),
            "--path",
            rel,
            "--title",
            "Rust Created Note",
            "--content",
            "# Rust Created Note\n\n## Claim\n\nCreated by the lite writer.",
            "--date",
            "2026-07-07",
        ],
        check=True,
        text=True,
        capture_output=True,
        timeout=30,
    )

    payload = json.loads(out.stdout)
    text = (vault / rel).read_text(encoding="utf-8")
    assert payload["op"] == "note"
    assert "type: insight" in text
    assert "title: Rust Created Note" in text
    assert "updated: 2026-07-07" in text
    assert "Created by the lite writer." in text


def test_rust_write_lite_uses_distinct_runtime_exit_codes(rust_write_bin: Path) -> None:
    vault = _fresh_vault("write-exit-codes")
    rel = "Knowledge Base/Notes/Insights/write-target.md"
    _write_page(vault, rel, "old marker", title="Write Target", updated="2026-01-01")

    usage = subprocess.run(
        [
            str(rust_write_bin),
            "edit",
            "--vault",
            str(vault),
            "--path",
            "../escape.md",
            "--old",
            "old marker",
            "--new",
            "new marker",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert usage.returncode == 2

    data = subprocess.run(
        [
            str(rust_write_bin),
            "edit",
            "--vault",
            str(vault),
            "--path",
            rel,
            "--old",
            "missing marker",
            "--new",
            "new marker",
        ],
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert data.returncode == 3

