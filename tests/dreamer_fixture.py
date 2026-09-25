"""A small generated vault for the dreamer suites (invented, generic names).

Pages:

* three Sources (`field-report-one/two/three`);
* one entity, `orbit-pump`, last updated before the notes that describe it;
* insights that cite the sources and link the entity:
  - `pump-cavitation` (field-report-one) and `pump-seal-wear` (field-report-two)
    are two independent origins with newer facts linking the entity;
  - `pump-inlet-pressure` cites field-report-one too, so it shares a source with
    `pump-cavitation` (a structural `shared_sources` relation candidate).

`build` seeds both freshness scopes the way the watcher does and publishes the
epistemic graph, so the dreamer's delta and read snapshots are both live.
"""

from __future__ import annotations

from pathlib import Path

from exomem import epistemic_graph, freshness
from exomem import find as find_module
from exomem import vault as vault_module

KB = "Knowledge Base"
ENTITY = f"{KB}/Notes/Entities/orbit-pump.md"
CAVITATION = f"{KB}/Notes/Insights/pump-cavitation.md"
SEAL_WEAR = f"{KB}/Notes/Insights/pump-seal-wear.md"
INLET = f"{KB}/Notes/Insights/pump-inlet-pressure.md"
SOURCE_ONE = f"{KB}/Sources/field-report-one.md"
SOURCE_TWO = f"{KB}/Sources/field-report-two.md"
SOURCE_THREE = f"{KB}/Sources/field-report-three.md"


def source(title: str) -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: source\n"
        "captured: 2026-04-01\n"
        "---\n\n"
        f"# {title}\n\nRaw notes from the field.\n"
    )


def entity(*, updated: str = "2026-01-10", status: str = "active", extra: str = "") -> str:
    return (
        "---\n"
        "title: Orbit Pump\n"
        "type: entity\n"
        "entity_type: concept\n"
        f"status: {status}\n"
        "created: 2026-01-01\n"
        f"updated: {updated}\n"
        "---\n\n"
        "# Orbit Pump\n\nA circulation pump used in the test rig.\n"
        f"{extra}"
    )


def insight(
    title: str,
    *,
    sources: list[str],
    updated: str,
    links: str = "",
    status: str = "active",
    observation: str = "The measurement repeats across runs.",
    extra: str = "",
) -> str:
    source_lines = "".join(f'  - "[[Sources/{name}]]"\n' for name in sources)
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        f"status: {status}\n"
        "created: 2026-04-02\n"
        f"updated: {updated}\n"
        f"sources:\n{source_lines}"
        "---\n\n"
        f"# {title}\n\n"
        f"{links}\n\n"
        "## Observations\n\n"
        f"- [finding] {observation}\n"
        f"{extra}"
    )


def write(vault: Path, rel: str, text: str) -> Path:
    path = vault / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    find_module.clear_cache()
    return path


def seed(vault: Path) -> None:
    freshness.seed(
        vault,
        "vault",
        [(str(p), freshness.stat_signature(p)) for p in vault_module.walk_vault_md(vault)],
    )
    freshness.seed(
        vault,
        "kb",
        [(str(p), freshness.stat_signature(p)) for p in find_module._walk_md(vault / KB)],
    )


def publish_graph(vault: Path) -> None:
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()


def build(root: Path, *, with_graph: bool = True) -> Path:
    vault = root / "vault"
    write(vault, SOURCE_ONE, source("Field report one"))
    write(vault, SOURCE_TWO, source("Field report two"))
    write(vault, SOURCE_THREE, source("Field report three"))
    write(vault, ENTITY, entity())
    write(
        vault,
        CAVITATION,
        insight(
            "Pump cavitation",
            sources=["field-report-one"],
            updated="2026-05-01",
            links="Cavitation shows up on the [[Notes/Entities/orbit-pump]] above 40 litres "
            "a minute.",
            observation="Cavitation starts above 40 litres a minute.",
        ),
    )
    write(
        vault,
        SEAL_WEAR,
        insight(
            "Pump seal wear",
            sources=["field-report-two"],
            updated="2026-05-02",
            links="Seal wear on the [[Notes/Entities/orbit-pump]] doubles after a dry start.",
            observation="Seal wear doubles after a dry start.",
        ),
    )
    write(
        vault,
        INLET,
        insight(
            "Pump inlet pressure",
            sources=["field-report-one"],
            updated="2026-05-03",
            observation="Inlet pressure falls before cavitation begins.",
        ),
    )
    seed(vault)
    if with_graph:
        publish_graph(vault)
    return vault


def edit(vault: Path, rel: str, text: str, *, graph: bool = True) -> None:
    """Change one page the way a governed write would be observed."""
    path = write(vault, rel, text)
    freshness.on_files_changed(vault, changed=[path])
    if graph:
        publish_graph(vault)


def remove(vault: Path, rel: str, *, graph: bool = True) -> None:
    path = vault / rel
    path.unlink()
    find_module.clear_cache()
    freshness.on_files_changed(vault, deleted=[path])
    if graph:
        publish_graph(vault)


def tree_state(vault: Path) -> dict[str, tuple[int, bytes]]:
    """Every file under the vault with its mtime and bytes: a write detector."""
    return {
        path.relative_to(vault).as_posix(): (path.stat().st_mtime_ns, path.read_bytes())
        for path in sorted(vault.rglob("*"))
        if path.is_file()
    }


def run_to_quiet(vault: Path, *, now: float | None = None, limit: int = 60) -> list:
    """Tick until nothing is left, across budget stops. Returns every result."""
    from exomem import dreamer

    clock = dreamer.Clock() if now is None else dreamer.Clock(time=lambda: now)
    results = []
    for _ in range(limit):
        result = dreamer.run_once(vault, clock=clock)
        results.append(result)
        if result.stop_reason not in {"pages", "cpu", "wall"}:
            break
    return results


def plant_deliverable(vault: Path, subject: str, contributors: list[str], *, now: float) -> str:
    """Store one settled, deliverable hydration-shaped item over existing pages.

    For suites that measure the carrier on a vault the worker never ran on:
    the evidence signatures are the live ones, two contributors carry two
    origins, and the review-state token is current. Returns the candidate id.
    """
    from exomem import dreamer_delta, dreamer_families, dreamer_store, relation_queue

    def entry(path: str, role: str, origin: str) -> dict:
        return {
            "path": path,
            "ref": relation_queue._fallback_ref(path),
            "sig": dreamer_store.encode_sig(dreamer_delta.live_signature(vault, path)),
            "role": role,
            "origin": origin,
            "title": Path(path).stem.replace("-", " ").title(),
        }

    evidence = [entry(subject, "subject", "")] + [
        entry(path, "contributor", f"origin-{index}") for index, path in enumerate(contributors)
    ]
    assert all(item["sig"] for item in evidence), "plant needs a live freshness registry"
    store = dreamer_store.DreamerStore(vault)
    conn = store.connect()
    try:
        with store.write(conn):
            cid = store.upsert_proposal(
                conn,
                family=dreamer_families.HYDRATION_FAMILY,
                kind=dreamer_families.HYDRATION_KIND,
                subject_path=subject,
                subject_ref=evidence[0]["ref"],
                proposal_key="",
                evidence=evidence,
                route={
                    "tool": "maintain_memory",
                    "args": {
                        "mode": "curation",
                        "curation_action": "work-item",
                        "paths": [subject, *contributors],
                    },
                },
                reason_code="newer_linked_facts",
                producer=dreamer_families.HYDRATION_FAMILY,
                signal_version="planted",
                now=now - 2 * dreamer_families.SETTLE_SECONDS,
            )
            store.set_deliverable(
                conn,
                cid,
                deliverable=True,
                token=dreamer_families.review_state_token(vault),
                settled_at=now - dreamer_families.SETTLE_SECONDS,
            )
    finally:
        conn.close()
    dreamer_store.clear_reader_memo()
    return cid


# ----------------------------------------------------------------------
# realistic shapes: `## Relations` lines, `exomem_id` pages, one-Source clusters
# ----------------------------------------------------------------------

#: Deterministic ids, so a rebuilt fixture carries the same identities.
_ID_NAMESPACE = "5f1c0a52-3e2d-4d5b-9a0e-6b7c8d9e0f10"


def page_id(rel: str) -> str:
    import uuid

    return str(uuid.uuid5(uuid.UUID(_ID_NAMESPACE), rel))


def with_id(text: str, rel: str) -> str:
    """The page text with an `exomem_id` as its first frontmatter key."""
    return text.replace("---\n", f"---\nexomem_id: {page_id(rel)}\n", 1)


def relations(*lines: tuple[str, str]) -> str:
    """A `## Relations` section: `(kind, KB-relative target without .md)` pairs."""
    body = "".join(f"- {kind} [[{target}]]\n" for kind, target in lines)
    return f"\n## Relations\n\n{body}"


def cluster(vault: Path, count: int, source: str, *, stem: str = "cluster-note") -> list[str]:
    """`count` insights citing one Source, each with a `## Relations` line."""
    written = []
    for index in range(count):
        rel = f"{KB}/Notes/Insights/{stem}-{index:03d}.md"
        neighbour = f"Notes/Insights/{stem}-{(index + 1) % count:03d}"
        text = insight(
            f"{stem.replace('-', ' ').title()} {index:03d}",
            sources=[source],
            updated="2026-05-01",
            observation=f"Fact {index} from the shared report.",
            extra=relations(("relates_to", neighbour)) if count > 1 else "",
        )
        write(vault, rel, with_id(text, rel))
        written.append(rel)
    return written


def warm_identity(vault: Path, monkeypatch) -> None:
    """Warm the reference-identity snapshot the way a live service's writes do.

    Id-bearing pages need it for exact relation refs. The suites disable the
    corpus-context cache by default; this turns it on for the calling test.
    """
    from exomem import semantic_contract

    monkeypatch.delenv("EXOMEM_DISABLE_CORPUS_CACHE", raising=False)
    semantic_contract.reset_corpus_context_cache()
    semantic_contract.build_corpus_context(vault)
    assert semantic_contract.current_reference_identity_snapshot(vault) is not None


def build_realistic(root: Path, *, with_graph: bool = True) -> Path:
    """`build` plus the shapes real vaults have.

    Every compiled page and the entity carry an `exomem_id`; the cavitation and
    seal-wear notes author a `## Relations` line to the entity, and the inlet
    note one to the seal-wear note. The cavitation/inlet pair stays unauthored,
    so it remains the link positive, and the hydration positive is unchanged.
    """
    vault = build(root, with_graph=False)
    extras = {
        CAVITATION: relations(("relates_to", "Notes/Entities/orbit-pump")),
        SEAL_WEAR: relations(("relates_to", "Notes/Entities/orbit-pump")),
        INLET: relations(("relates_to", "Notes/Insights/pump-seal-wear")),
        ENTITY: "",
    }
    for rel, extra in extras.items():
        path = vault / rel
        write(vault, rel, with_id(path.read_text(encoding="utf-8") + extra, rel))
    freshness.clear()
    seed(vault)
    if with_graph:
        publish_graph(vault)
    return vault
