"""Defect 5a: durable adoption run state is operational state, not knowledge.

`Knowledge Base/_Adoption/` holds the run objects and the run manifest for a
governed multi-step operation. It names sources, targets and hashes whose own
disclosure decisions may be restrictive, so it must not enter the content
corpus. The oracle is a count oracle: a recall query whose terms match run
state must return exactly what the same vault returns with no run present —
not merely "no run item ranked highly".

There are TWO walkers and they must not disagree. `scope="kb"`/`"kb-only"` walk
`find_corpus.EXCLUDED_DIR_NAMES`; `scope="vault"` reaches `vault.walk_vault_md`
via `bm25`, which filters the separate `vault.VAULT_SCAN_SKIP_DIRS`. On
unmodified `main` that second set names neither `_Adoption` nor `_Governance`,
so `find(scope="vault")` walked straight past both exclusions — a caller-selected
bypass, and `commands.py:580` actively suggests widening to it. Both names are
now in both sets, and this suite asserts ABSENCE at every scope (not merely
parity between the two trees, which a shared leak would also satisfy).
"""

from __future__ import annotations

import shutil
from pathlib import Path

from exomem import bm25, commands
from exomem import find as find_module

ADOPTION_DIR = "Knowledge Base/_Adoption"
GOVERNANCE_DIR = "Knowledge Base/_Governance"

# The recall scopes the corpus exclusion governs: the default and its strict form.
CORPUS_SCOPES = ("kb", "kb-only")

# Terms that match durable run state: the manifest heading/type, the title of
# the adopted item the run names, and the bare topic word.
RUN_STATE_QUERIES = (
    "adoption run manifest",
    "quarterly planning",
    "adoption",
)


def _seed_legacy(vault: Path) -> None:
    old = vault / "Old Notes"
    old.mkdir(parents=True, exist_ok=True)
    (old / "quarterly-planning.md").write_text(
        "# Quarterly Planning Notes\n\nShip the adoption studio this quarter.\n",
        encoding="utf-8",
    )
    (old / "standup.txt").write_text("standup: nothing blocking\n", encoding="utf-8")
    find_module.clear_cache()


def _run_to_done(vault: Path) -> str:
    started = commands.op_adoption_studio(vault, action="start", path="Old Notes")
    run_id = started["run_id"]
    commands.op_adoption_studio(
        vault, action="select", run_id=run_id, include=["Old Notes"]
    )
    planned = commands.op_adoption_studio(vault, action="plan", run_id=run_id)
    commands.op_adoption_studio(
        vault, action="apply", run_id=run_id, plan_id=planned["plan"]["plan_id"]
    )
    finished = commands.op_adoption_studio(vault, action="finish", run_id=run_id)
    assert finished["phase"] == "done"
    return run_id


def _recall(vault: Path, query: str, *, scope: str) -> list[str]:
    find_module.clear_cache()
    bm25.clear_cache()
    hits = find_module.find(
        vault, query=query, mode="keyword", limit=20, graph=False, scope=scope
    )
    return [h.path for h in hits]


def test_run_state_is_not_recalled_and_counts_match_a_vault_with_no_run(
    vault: Path,
) -> None:
    _seed_legacy(vault)
    _run_to_done(vault)
    assert sorted((vault / ADOPTION_DIR).glob("*.md")), "finish wrote no run manifest"

    with_run = {
        (scope, q): _recall(vault, q, scope=scope)
        for scope in CORPUS_SCOPES
        for q in RUN_STATE_QUERIES
    }
    leaked = [p for paths in with_run.values() for p in paths if p.startswith(ADOPTION_DIR)]
    assert leaked == [], f"run state reachable by recall: {leaked}"

    # The count oracle: dropping the run entirely must be indistinguishable.
    shutil.rmtree(vault / ADOPTION_DIR)
    without_run = {
        (scope, q): _recall(vault, q, scope=scope)
        for scope in CORPUS_SCOPES
        for q in RUN_STATE_QUERIES
    }

    assert {k: len(v) for k, v in with_run.items()} == {
        k: len(v) for k, v in without_run.items()
    }
    assert with_run == without_run


def test_adoption_tree_has_the_same_corpus_treatment_as_the_governance_tree(
    vault: Path,
) -> None:
    """`_Adoption/` is excluded on the same reasoning as `_Governance/`, so every
    walk must treat the two identically AND must reach neither.

    Parity alone is too weak — two trees leaking equally satisfies it. The
    `scope="vault"` case is the one that regressed historically: it reaches
    `vault.walk_vault_md`/`VAULT_SCAN_SKIP_DIRS`, a different set from the corpus
    exclusion, and named neither directory."""
    marker = "zzoperationalstatemarkerzz"
    for directory in (ADOPTION_DIR, GOVERNANCE_DIR):
        target = vault / directory
        target.mkdir(parents=True, exist_ok=True)
        (target / "operational.md").write_text(
            f"---\ntags: []\n---\n# Operational state\n\n{marker}\n", encoding="utf-8"
        )

    for scope in (*CORPUS_SCOPES, "vault"):
        hits = _recall(vault, marker, scope=scope)
        adoption_hits = [p for p in hits if p.startswith(ADOPTION_DIR)]
        governance_hits = [p for p in hits if p.startswith(GOVERNANCE_DIR)]
        assert len(adoption_hits) == len(governance_hits), (
            f"scope={scope!r}: run state and policy state are not treated alike "
            f"(adoption={adoption_hits}, governance={governance_hits})"
        )
        assert adoption_hits == [] and governance_hits == [], (
            f"scope={scope!r}: operational state is reachable by recall "
            f"(adoption={adoption_hits}, governance={governance_hits})"
        )


def test_both_walkers_exclude_operational_state() -> None:
    """The corpus walk and the full-vault walk use SEPARATE exclusion sets. A name
    excluded from one and indexed by the other is a caller-selected bypass, so pin
    both — this is the assertion that fails if someone adds a name to only one."""
    from exomem import find_corpus
    from exomem import vault as vault_module

    for name in ("_Adoption", "_Governance"):
        assert name in find_corpus.EXCLUDED_DIR_NAMES, f"{name} missing from corpus walk"
        assert name in vault_module.VAULT_SCAN_SKIP_DIRS, f"{name} missing from vault walk"


def test_stateless_save_manifest_embeds_no_paths_or_report_dump(vault: Path) -> None:
    """`adopt(mode="save-manifest")` writes a released page too, and it carried the
    same class of dump as the run manifest — a `_compact_report` JSON fence whose
    `overview`/`refs`/`copy`/`compile_plan` enumerate every scanned path. Unlike
    the run manifest there is no run object to defer to, so the body carries the
    human-readable sections only; the full report stays available live from
    `adopt(mode="scan-only")`, where a disclosure decision applies."""
    _seed_legacy(vault)
    result = commands.op_adopt_vault(vault, mode="save-manifest")
    rel = result["manifest"]["path"]
    body = (vault / rel).read_text(encoding="utf-8")

    assert "```json" not in body, "released manifest still embeds a machine-readable dump"
    assert "Machine-Readable" not in body

    leaked = [
        token
        for token in ("Old Notes/quarterly-planning.md", "Old Notes/standup.txt")
        if token in body
    ]
    assert leaked == [], f"released manifest enumerates scanned paths: {leaked}"

    # Still useful: the human-readable summary survives.
    assert "## Scan Summary" in body
    assert "## Safe Next Actions" in body


def test_adoption_dir_is_in_the_corpus_exclusion_set() -> None:
    from exomem import find_corpus

    assert "_Adoption" in find_corpus.EXCLUDED_DIR_NAMES
