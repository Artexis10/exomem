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
