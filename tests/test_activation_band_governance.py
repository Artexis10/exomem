"""The semantic band under a governed policy (step 4, round 2, R7).

The band is an aggregate over the whole anchor catalogue: its population,
its floor, its median and spread, and its width rule all count every anchor,
withheld ones included. So for a caller who may not see some anchor, the band
itself would be a channel: whether an anchor bands, whether the catalogue
calibrates, and whether a band is too wide can each depend on a page withheld
from that caller. Under a non-empty governed policy the band therefore runs
only for the owner. Every other principal gets semantic state
`audience_restricted` and no `vector_band` contact at all, decided before any
similarity is computed; with an empty policy, or for the owner, nothing
changes.

Twin vaults that differ only by one withheld anchor W must look identical to
the restricted caller (the reviewer's probe: floor, width, a band on W alone,
and a median shift).
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from test_governance_egress import _external, write_rule, write_scope
from test_working_set_egress import _prepare_end_to_end_vault, _reset_governance_state
from test_working_set_index import _seed_structure, _write

from exomem import commands, embeddings, readiness, working_set_index, working_set_runtime
from exomem.governance.principal import owner_principal, request_scope

pytestmark = pytest.mark.timeout(600)

DIM = 64
E0 = np.eye(DIM, dtype=np.float32)[0]
E1 = np.eye(DIM, dtype=np.float32)[1]
FINGERPRINT = "planted-governed|cls|l2|0000"
W_REL = "Products/Wintergreen Relay.md"
TURN = "Could the sled cope?"

#: Each case plants cosines against the turn's direction; every other
#: signature is a seeded random direction.
CASES = {
    # Visible 49 plus W: the floor of 50 is reached only because of W.
    "floor": dict(visible=49, planted={"Cargo Sled": 1.0}),
    # Three visible outliers plus W: W makes the band too wide to serve.
    "width": dict(
        visible=70,
        planted={"Cargo Sled": 1.0, "Cedar Carrier": 1.0, "Birch Hauler": 1.0, "Wintergreen Relay": 1.0},
        extras=("Cedar Carrier", "Birch Hauler"),
    ),
    # W alone is similar to the turn: a band on it would mark it withheld.
    "w_partial": dict(visible=70, planted={"Wintergreen Relay": 1.0}),
    # W is ordinary, but it moves the median and the spread.
    "median": dict(visible=60, planted={"Cargo Sled": 0.99, "Wintergreen Relay": 0.3}),
}


def _vector(cosine: float) -> np.ndarray:
    return (cosine * E0 + math.sqrt(max(0.0, 1 - cosine * cosine)) * E1).astype(np.float32)


def _encoder(planted: dict[str, float]):
    def passages(texts):
        rows = []
        for text in texts:
            title = text.split("\n", 1)[0].strip()
            if title in planted:
                rows.append(_vector(planted[title]))
                continue
            seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "little")
            vector = np.random.default_rng(seed).standard_normal(DIM).astype(np.float32)
            rows.append(vector / np.linalg.norm(vector))
        return np.vstack(rows)

    return passages


def _page(vault: Path, rel: str, title: str) -> None:
    _write(vault / "Knowledge Base" / rel, f"---\ntype: note\nstatus: active\n---\n# {title}\n\nPlanted item {title}.\n")


def _embed_all(vault: Path) -> int:
    index = working_set_index.WorkingSetIndex(vault)
    index.update(load_encoder=True)
    count = len(index.vector_matrix(FINGERPRINT)[0])
    index.close()
    return count


def _governed_vault(vault: Path, monkeypatch: pytest.MonkeyPatch, spec: dict) -> None:
    passages = _encoder(spec["planted"])
    monkeypatch.delenv("EXOMEM_DISABLE_EMBEDDINGS", raising=False)
    monkeypatch.setattr(embeddings, "embed_activation_passages", passages)
    monkeypatch.setattr(embeddings, "embed_activation_passages_if_loaded", passages)
    monkeypatch.setattr(embeddings, "activation_fingerprint", lambda: FINGERPRINT)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: E0)
    monkeypatch.setattr(readiness, "should_defer", lambda component: False)
    _seed_structure(vault)
    for extra in spec.get("extras", ()):
        _page(vault, f"Products/{extra}.md", extra)
    working_set_runtime.reset_caches_for_tests()
    working_set_index.WorkingSetIndex(vault).rebuild()
    for i in range(max(0, spec["visible"] - _embed_all(vault))):
        _page(vault, f"Products/Zorvath {i:03d}.md", f"Zorvath {i:03d}")
    write_scope(vault, paths=W_REL)
    write_rule(vault, ceiling=0, audience="external")


def _serve(vault: Path, principal) -> dict:
    _prepare_end_to_end_vault(vault)
    _embed_all(vault)
    _reset_governance_state(vault)
    with request_scope(principal):
        return commands.op_activate_context(vault, turn=TURN)


def _shape(packet: dict) -> dict:
    return {
        "abstained": packet.get("abstained"),
        "abstention": packet.get("abstention"),
        "semantic_evidence": (packet.get("generation") or {}).get("semantic_evidence"),
        "anchors": sorted(
            (item.get("path"), item.get("status"), tuple(item.get("evidence") or ())) for item in packet.get("anchors") or ()
        ),
        "missing": packet.get("missing"),
        "units": sorted(unit.get("ref") for unit in packet.get("units") or ()),
    }


@pytest.mark.parametrize("case", sorted(CASES))
def test_a_withheld_anchor_is_invisible_to_a_restricted_caller_through_the_band(
    vault: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _governed_vault(vault, monkeypatch, CASES[case])
    _page(vault, W_REL, "Wintergreen Relay")
    with_w = _serve(vault, _external())
    (vault / "Knowledge Base" / W_REL).unlink()
    without_w = _serve(vault, _external())

    assert _shape(with_w) == _shape(without_w)
    assert _shape(with_w)["semantic_evidence"] == "audience_restricted"
    assert not any("vector_band" in evidence for _path, _status, evidence in _shape(with_w)["anchors"])
    assert "Wintergreen" not in json.dumps(with_w, default=str)


def test_a_restricted_caller_is_never_encoded_for(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Decided before any similarity or statistic: the turn is not even encoded."""
    _governed_vault(vault, monkeypatch, CASES["floor"])
    _page(vault, W_REL, "Wintergreen Relay")
    encodes: list[str] = []
    _prepare_end_to_end_vault(vault)
    _embed_all(vault)
    _reset_governance_state(vault)
    monkeypatch.setattr(embeddings, "embed_activation_query_if_loaded", lambda text: encodes.append(text) or E0)
    monkeypatch.setattr(
        working_set_index.WorkingSetIndex,
        "vector_matrix",
        lambda *_a, **_k: pytest.fail("the band's population was read for a restricted caller"),
    )

    with request_scope(_external()):
        packet = commands.op_activate_context(vault, turn=TURN)

    assert packet["generation"]["semantic_evidence"] == "audience_restricted"
    assert encodes == []


def test_the_owner_still_gets_the_band_under_governance(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _governed_vault(vault, monkeypatch, CASES["floor"])
    _page(vault, W_REL, "Wintergreen Relay")

    packet = _serve(vault, owner_principal())

    assert packet["generation"]["semantic_evidence"] == "ready"
    sled = next(item for item in packet["anchors"] if item["title"] == "Cargo Sled")
    assert "vector_band" in sled["evidence"]


def test_an_ungoverned_vault_bands_for_every_caller(vault: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = CASES["floor"]
    _governed_vault(vault, monkeypatch, spec)
    _page(vault, W_REL, "Wintergreen Relay")
    governance = vault / "Knowledge Base" / "_Governance"
    for path in sorted(governance.rglob("*.yaml")):
        path.unlink()

    packet = _serve(vault, _external())

    assert packet["generation"]["semantic_evidence"] == "ready"
    sled = next(item for item in packet["anchors"] if item["title"] == "Cargo Sled")
    assert "vector_band" in sled["evidence"]


def test_the_packet_cache_never_serves_one_audiences_band_to_another(
    vault: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _governed_vault(vault, monkeypatch, CASES["floor"])
    _page(vault, W_REL, "Wintergreen Relay")
    _prepare_end_to_end_vault(vault)
    _embed_all(vault)
    _reset_governance_state(vault)

    with request_scope(owner_principal()):
        owner = commands.op_activate_context(vault, turn=TURN)
    with request_scope(_external()):
        restricted = commands.op_activate_context(vault, turn=TURN)

    assert owner["generation"]["semantic_evidence"] == "ready"
    assert restricted["generation"]["semantic_evidence"] == "audience_restricted"
