"""Privacy backstops for the generic scaffold and shipped package source.

The authoritative gate is inventory and provenance based.  It deliberately does
not encode a maintainer's vault, names, projects, or private-token denylist in a
public test fixture.
"""

from __future__ import annotations

import re
from pathlib import Path

import exomem
from exomem import semantic_authoring, workflow_skills
from exomem.public_artifact_privacy import (
    assert_public_artifacts_clean,
    repository_input_paths,
    scan_repository_inputs,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path(exomem.__file__).resolve().parent
SCAFFOLD = SOURCE / "_scaffold"
SAMPLE_VAULT = SOURCE / "_sample_vault"
PUBLIC_INPUTS = tuple(REPO_ROOT / path for path in repository_input_paths(REPO_ROOT))


def _files(root: Path) -> list[Path]:
    return [
        path
        for path in PUBLIC_INPUTS
        if path.is_file() and path.is_relative_to(root)
    ]


def _labels(root: Path, files: list[Path]) -> dict[Path, str]:
    return {
        path: f"{root.name}/{path.relative_to(root).as_posix()}"
        for path in files
    }


def test_scaffold_ships_no_personal_data() -> None:
    files = _files(SCAFFOLD)
    assert files, "bundled scaffold is missing"
    assert_public_artifacts_clean(files, labels=_labels(SCAFFOLD, files))


def test_sample_vault_ships_no_personal_data() -> None:
    files = _files(SAMPLE_VAULT)
    assert files, "bundled sample vault is missing"
    assert_public_artifacts_clean(files, labels=_labels(SAMPLE_VAULT, files))


def test_source_ships_no_personal_data() -> None:
    files = _files(SOURCE)
    assert files, "package source is missing"
    assert_public_artifacts_clean(files, labels=_labels(SOURCE, files))


def test_complete_public_inventory_ships_no_private_context() -> None:
    report = scan_repository_inputs(REPO_ROOT)
    assert report.scanned_files > len(_files(SOURCE))
    assert report.findings == ()


def test_shipped_contract_embeddings_are_canonical_and_public_safe() -> None:
    """The scaffold's shipped contract embeddings must be exactly the canonical
    projection — public-safe, generic, and free of any private vault context."""

    concise = semantic_authoring.render_concise()
    core = workflow_skills.WORKFLOW_SKILLS_DIR.parent / "SKILL.md"
    carriers = [core]
    carriers.extend(
        workflow_skills.source_dir(str(skill["name"])) / "SKILL.md"
        for skill in workflow_skills.list_skills()
        if skill.get("standalone_authoring") is True
    )

    # Core scaffold SKILL + 9 standalone authoring workflows = 10 embeddings.
    assert len(carriers) == 10

    for path in carriers:
        assert path.is_file(), f"missing shipped contract carrier {path}"
        text = path.read_text(encoding="utf-8")
        assert text.count(concise) == 1, f"{path} lost the canonical contract"

    # The embedded contract carries only invented, generic examples: the canonical
    # v4 identity and a governed-relative wikilink target, never a vault path.
    assert "<!-- exomem-semantic-authoring:v4 " in concise
    assert "[[Knowledge Base/Notes/Health/Morning training]]" in concise

    # The authoritative, provenance-based gate proves public safety without
    # encoding any maintainer name, vault, or private-token denylist here.
    assert_public_artifacts_clean(carriers, labels={p: p.name for p in carriers})


def test_source_ships_no_competitor_tokens() -> None:
    """Contender names stay in maintainer comparisons, not shipped runtime."""

    pattern = re.compile(r"\bbasic[-_ ]memory\b", re.IGNORECASE)
    offenders: list[str] = []
    for path in _files(SOURCE):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, 1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(SOURCE)}:{line_number}")
    assert not offenders, f"competitor token found in shipped source at {offenders}"
