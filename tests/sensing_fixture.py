"""A stub instrument and a small sensed vault for the sensing suites (invented names).

The stub is a deterministic function of the two texts it is handed. It answers
from a table of text pairs and falls back to `neutral`, and carries its own
identity, so readings made with it are unmistakably stub readings.

Pages (all with one in-scope compact unit):

* `seal-wear` (2026-04-01): the claim other pages talk about;
* `seal-wear-winter` (2026-05-01): links it, and REFINES its unit;
* `seal-wear-denial` (2026-05-02): links it, and CONTRADICTS its unit;
* `inlet-pressure` (2026-05-03): links it, stays neutral.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import dreamer_fixture as fx

from exomem import dreamer, sensed_model, sensing, sensor_worker

KB = fx.KB
SEAL = f"{KB}/Notes/Insights/seal-wear.md"
WINTER = f"{KB}/Notes/Insights/seal-wear-winter.md"
DENIAL = f"{KB}/Notes/Insights/seal-wear-denial.md"
INLET = f"{KB}/Notes/Insights/inlet-pressure.md"

SEAL_TEXT = "Seal wear doubles after a dry start."
WINTER_TEXT = "Seal wear doubles after a dry start, most of all in winter."
DENIAL_TEXT = "Seal wear never changes after a dry start."
INLET_TEXT = "Inlet pressure falls before cavitation begins."

STUB = sensing.InstrumentIdentity(
    model="stub/pair-relation",
    revision="1",
    weights_sha256="0" * 64,
    runtime="stub",
    runtime_version="1",
    template_version="nli-pair-v1",
    label_map_version="relation-v1",
    fixture_set="relation-v1-multilingual",
)

#: The served bge-m3 fingerprint (`EncoderProfile.fingerprint()` of the pinned int8
#: artefact), the one space `sensed_model.COSINE_THETA` calibrates.
ENCODER = "BAAI/bge-m3|cls|l2|74068c180d6514e8"

CONTRA = [0.01, 0.02, 0.97]
ENTAIL = [0.97, 0.02, 0.01]
WEAK = [0.30, 0.60, 0.10]
NEUTRAL = [0.20, 0.70, 0.10]


class StubInstrument:
    """`table[(x, y)] = label`, where for `refines` x is the refining text."""

    def __init__(self, table: dict[tuple[str, str], str] | None = None, identity=STUB) -> None:
        self.identity = identity
        self.table = dict(table or {})
        self.calls: list[tuple[str, str]] = []

    def judge(self, pairs):
        out = []
        for first, second in pairs:
            self.calls.append((first, second))
            label = self.table.get((first, second)) or self.table.get((second, first))
            if label == "contradicts":
                out.append((CONTRA, CONTRA, None))
            elif label == "restates":
                out.append((ENTAIL, ENTAIL, None))
            elif label == "refines":
                first_refines = (first, second) in self.table
                out.append((ENTAIL, WEAK, None) if first_refines else (WEAK, ENTAIL, None))
            elif label == "asymmetric":
                out.append((CONTRA, WEAK, None))
            else:
                out.append((NEUTRAL, NEUTRAL, None))
        return out


def default_table() -> dict[tuple[str, str], str]:
    return {(WINTER_TEXT, SEAL_TEXT): "refines", (DENIAL_TEXT, SEAL_TEXT): "contradicts"}


def note(title: str, created: str, unit: str, *, links: str = "", status: str = "active",
         anchor: str = "", extra: str = "") -> str:
    return (
        "---\n"
        f"title: {title}\n"
        "type: insight\n"
        f"status: {status}\n"
        f"created: {created}\n"
        f"updated: {created}\n"
        "---\n\n"
        f"# {title}\n\n"
        f"{links}\n\n"
        "## Observations\n\n"
        f"- [finding] {unit}{(' ^' + anchor) if anchor else ''}\n"
        f"{extra}"
    )


def build(root: Path, *, with_denial: bool = True, with_winter: bool = True) -> Path:
    vault = root / "vault"
    fx.write(vault, SEAL, note("Seal wear", "2026-04-01", SEAL_TEXT))
    if with_winter:
        fx.write(
            vault,
            WINTER,
            note("Seal wear in winter", "2026-05-01", WINTER_TEXT,
                 links="Builds on [[Notes/Insights/seal-wear]].", anchor="winter"),
        )
    if with_denial:
        fx.write(
            vault,
            DENIAL,
            note("Seal wear denial", "2026-05-02", DENIAL_TEXT,
                 links="Disputes [[Notes/Insights/seal-wear]]."),
        )
    fx.write(
        vault,
        INLET,
        note("Inlet pressure", "2026-05-03", INLET_TEXT, links="See [[Notes/Insights/seal-wear]]."),
    )
    fx.seed(vault)
    fx.publish_graph(vault)
    return vault


def enable(monkeypatch, *identities: sensing.InstrumentIdentity, vectors=None) -> None:
    """Sensing on, with stub instruments active and stored vectors from `vectors`."""
    monkeypatch.setenv("EXOMEM_SENSING", "on")
    active = {identity.instrument_id: identity for identity in (identities or (STUB,))}
    monkeypatch.setattr(sensed_model, "active_instruments", lambda: dict(active))
    table = vectors or {}
    monkeypatch.setattr(
        sensed_model, "encoder_fingerprint", lambda vault_root: ENCODER if table else None
    )
    monkeypatch.setattr(
        sensed_model,
        "stored_unit_vectors",
        lambda vault_root, rel, fingerprint: dict(table.get(rel, {})) if fingerprint else {},
    )


BIG = dreamer.Budget(pages=500, cpu=120.0, wall=120.0)


def settle(vault: Path, *, rounds: int = 6) -> None:
    """Tick until the dreamer and the projection have nothing left."""
    for _ in range(rounds):
        dreamer.run_once(vault, budget=BIG)


def sense(vault: Path, instrument) -> int:
    """Run the sensor child's loop in-process until the queue is empty."""
    return sensor_worker.run_child(
        vault,
        parent_pid=0,
        cpu_allotment=1e9,
        judgement_allotment=10**6,
        idle_seconds=0.0,
        instrument=instrument,
        sleep=lambda _seconds: None,
    )


def converge(vault: Path, instrument) -> None:
    settle(vault)
    sense(vault, instrument)
    settle(vault)


def edges(vault: Path) -> list[tuple]:
    conn = sensed_model.open_readonly(vault)
    assert conn is not None
    try:
        return conn.execute(
            "SELECT pair_key, path_a, unit_a, path_b, unit_b, state, verdict, direction, p, "
            "reading_id, fingerprint, selected, queued FROM pairs ORDER BY pair_key"
        ).fetchall()
    finally:
        conn.close()


def with_identity(**changes) -> sensing.InstrumentIdentity:
    return dataclasses.replace(STUB, **changes)
