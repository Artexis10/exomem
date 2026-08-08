"""v0.1 release manifest: the committed corpus identity is reproducible.

The committed file under ``benchmarks/corpus/releases/`` is the manifest of
the full 17-template suite generated at master seed 1; regenerating must
reproduce it byte-identically (corpus identity without committing artifacts).
"""

from __future__ import annotations

import json
from pathlib import Path

from membench.generate import generate_corpus

RELEASE_MANIFEST = (
    Path(__file__).resolve().parents[1]
    / "benchmarks"
    / "corpus"
    / "releases"
    / "v0.1-seed1.manifest.json"
)


def test_release_manifest_is_committed() -> None:
    assert RELEASE_MANIFEST.is_file(), f"missing release manifest: {RELEASE_MANIFEST}"


def test_full_suite_seed1_reproduces_committed_manifest(tmp_path: Path) -> None:
    generate_corpus(1, tmp_path / "s1")  # full template suite, master seed 1
    generated = (tmp_path / "s1" / "manifest.json").read_bytes()
    assert generated == RELEASE_MANIFEST.read_bytes(), (
        "seed-1 full-suite manifest drifted from the committed release identity"
    )


def test_release_manifest_covers_the_declared_suite() -> None:
    payload = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    assert payload["master_seed"] == 1
    assert len(payload["templates"]) == 24  # T00 smoke + T01..T23 families
    assert payload["counts"]["queries"] >= 200
    assert payload["artifacts"], "artifact hash inventory must be present"
    # The compiled tier ships with the corpus, so a plan missing from the
    # release identity would let a compiled run be reproduced against bytes
    # that never carried one.
    assert payload["counts"]["conclusions"] == payload["counts"]["claims"]
