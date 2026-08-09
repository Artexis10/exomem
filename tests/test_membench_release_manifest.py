"""v0.1 release manifest: the committed corpus identity is reproducible.

The committed file under ``benchmarks/corpus/releases/`` is the manifest of
the full 17-template suite generated at master seed 1; regenerating must
reproduce it byte-identically (corpus identity without committing artifacts).
"""

from __future__ import annotations

import json
from pathlib import Path

from membench.artifacts.image import pillow_version
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
    generated_path = tmp_path / "s1" / "manifest.json"
    committed = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    generated = json.loads(generated_path.read_text(encoding="utf-8"))

    # Corpus identity gates; environment provenance never does (ledger
    # 4b.7): `renderer_versions` is excluded, because a Pillow patch bump
    # must not report corpus drift while every artifact hash is identical —
    # exactly the failure this test used to produce. In a lean environment
    # (no Pillow) image artifacts deliberately degrade to markdown
    # placeholders, so the media artifacts are checked structurally there
    # and by hash everywhere else. Failures name only what drifted: the
    # old whole-manifest byte comparison sent pytest's difflib rendering
    # past the 60s timeout and killed the whole CI run.
    problems = _manifest_diff(
        committed, generated, allow_media_degradation=pillow_version() is None
    )
    assert not problems, (
        "seed-1 manifest drifted from the committed release identity: "
        + "; ".join(problems)
    )


_MEDIA_SUFFIXES = (".png", ".pdf")


def _manifest_diff(
    committed: dict, generated: dict, *, allow_media_degradation: bool = False
) -> list[str]:
    """Compact, name-only differences — safe to embed in an assertion."""
    problems: list[str] = []
    for field in ("master_seed", "generator_version", "counts", "templates"):
        if committed.get(field) != generated.get(field):
            problems.append(f"{field} differs")

    committed_by_id = {a["source_id"]: a for a in committed["artifacts"]}
    generated_by_id = {a["source_id"]: a for a in generated["artifacts"]}
    if set(committed_by_id) != set(generated_by_id):
        missing = sorted(set(committed_by_id) - set(generated_by_id))[:6]
        extra = sorted(set(generated_by_id) - set(committed_by_id))[:6]
        problems.append(f"artifact source ids differ (missing={missing}, extra={extra})")
        return problems

    for source_id, expected in sorted(committed_by_id.items()):
        actual = generated_by_id[source_id]
        is_media = expected["path"].endswith(_MEDIA_SUFFIXES)
        if allow_media_degradation and is_media:
            stem_ok = actual["path"] == expected["path"].rsplit(".", 1)[0] + ".md"
            if not stem_ok:
                problems.append(
                    f"{source_id}: degraded artifact at unexpected path {actual['path']!r}"
                )
            continue
        for key in ("path", "bytes_sha256", "logical_sha256"):
            if actual[key] != expected[key]:
                problems.append(f"{source_id}: {key} differs ({expected['path']})")
                break

    if allow_media_degradation:
        media_ids = {
            s for s, a in committed_by_id.items() if a["path"].endswith(_MEDIA_SUFFIXES)
        }
        degraded = generated.get("degradations", [])
        if len(degraded) != len(media_ids):
            problems.append(
                f"degradations recorded for {len(degraded)} artifacts, "
                f"expected exactly the {len(media_ids)} committed media artifacts"
            )
    elif committed.get("degradations") != generated.get("degradations"):
        problems.append("degradations differ")

    return problems


def test_release_manifest_covers_the_declared_suite() -> None:
    payload = json.loads(RELEASE_MANIFEST.read_text(encoding="utf-8"))
    assert payload["master_seed"] == 1
    assert len(payload["templates"]) == 24  # T00 smoke + T01..T23 families
    assert payload["counts"]["queries"] >= 200
    assert payload["artifacts"], "artifact hash inventory must be present"
    # The compiled tier ships with the corpus, so a plan missing from the
    # release identity would let a compiled run be reproduced against bytes
    # that never carried one. Conclusions *exceed* claims because the plan is
    # bitemporal (4b.39): a claim whose basis grew over time yields one
    # conclusion per point at which it changed.
    assert payload["counts"]["conclusions"] >= payload["counts"]["claims"]
