"""Lane 4 — deferred write advisory custody and exact result retrieval.

Every test here drives the real accepted Lane 1 receipt/advisory store for
claim, restart, generation proof, publication and completion. Only the
terminal handoff is faked, and it is faked through Lane 1's own committed
``DerivedReceiptProtocolFake`` so no invented wire shape can pass.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import inspect
import json
import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from derived_receipt_fakes import DerivedReceiptProtocolFake

from exomem import commands, corpus_aware, derived_receipts, embeddings
from exomem import find as find_module
from exomem import vault as vault_module
from exomem.governance import egress as egress_module
from exomem.governance.principal import (
    RequestPrincipal,
    owner_principal,
    request_scope,
)

_REVIEW_REF_RE = re.compile(r"^exomem://review/write-advisory/[0-9a-f]{24}$")
_RESULT_REF_RE = re.compile(r"^exomem://write-advisory-result/[0-9a-f]{32}$")
_WORD_RE = re.compile(r"[a-z0-9]+")

_SCOPE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
_RULE_ID = "01ARZ3NDEKTSV4RRFFQ69G5FB0"
_EXTERNAL = "external"


# ---------------------------------------------------------------------------
# Lane boundaries: the implementation under test is resolved lazily so a
# missing leaf fails its own node instead of aborting collection.
# ---------------------------------------------------------------------------


def _advisory():
    spec = importlib.util.find_spec("exomem.deferred_write_advisory")
    assert spec is not None, "the deferred write advisory leaf is missing"
    return importlib.import_module("exomem.deferred_write_advisory")


def _generation_seam():
    assert hasattr(embeddings, "prepare_generation_vectors"), (
        "the exact-generation vector handoff seam is missing from embeddings"
    )
    return embeddings.prepare_generation_vectors


def _scoring_seam():
    assert hasattr(corpus_aware, "best_cosine_per_file_for_vectors"), (
        "the precomputed-vector advisory scoring seam is missing from corpus_aware"
    )
    return corpus_aware.best_cosine_per_file_for_vectors


def _emitter_seam():
    assert hasattr(corpus_aware, "emitted_write_advisory_groups"), (
        "the structured write-advisory emitter seam is missing from corpus_aware"
    )
    return corpus_aware.emitted_write_advisory_groups


# ---------------------------------------------------------------------------
# Deterministic, model-free encoder
# ---------------------------------------------------------------------------


def _vector_for(text: str) -> np.ndarray:
    """A hashed set-of-words unit vector: no model, no network, no download."""
    vector = np.zeros(embeddings.VECTOR_DIM, dtype=np.float32)
    for word in sorted(set(_WORD_RE.findall(text.lower()))):
        bucket = int(hashlib.sha256(word.encode("utf-8")).hexdigest()[:8], 16)
        vector[bucket % embeddings.VECTOR_DIM] = 1.0
    norm = float(np.linalg.norm(vector))
    if norm == 0.0:
        return vector
    return (vector / norm).astype(np.float32)


class _DeterministicEncoder:
    """Records every encode call so a second encode of one generation shows."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, texts, *, is_query: bool = False) -> np.ndarray:
        recorded = tuple(texts)
        self.calls.append(recorded)
        if not recorded:
            return np.zeros((0, embeddings.VECTOR_DIM), dtype=np.float32)
        return np.stack([_vector_for(text) for text in recorded]).astype(np.float32)

    def count_for(self, texts) -> int:
        wanted = tuple(texts)
        return sum(call == wanted for call in self.calls)

    def reset(self) -> None:
        self.calls.clear()


@pytest.fixture
def encoder(monkeypatch: pytest.MonkeyPatch) -> _DeterministicEncoder:
    fake = _DeterministicEncoder()
    monkeypatch.setenv("EXOMEM_DISABLE_EMBEDDINGS", "")
    monkeypatch.setattr(embeddings, "embed_texts", fake)
    monkeypatch.setattr(embeddings, "get_model", lambda: object())
    monkeypatch.setattr(embeddings, "_IMPORT_FAILED", False)
    return fake


# ---------------------------------------------------------------------------
# Vault, custody and claim helpers — all through the real accepted store
# ---------------------------------------------------------------------------


def _seed_page(vault: Path, name: str, body: str, *, folder: str = "Insights") -> str:
    rel = f"Knowledge Base/Notes/{folder}/{name}.md"
    target = vault / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: insight\nstatus: active\ncreated: 2026-08-16\n"
        "updated: 2026-08-16\ntags: []\n---\n"
        f"## Observations\n\n- [test] {body} ^{name}\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    return rel


def _fingerprint(vault: Path, rel: str) -> str:
    return vault_module.content_hash((vault / rel).read_text(encoding="utf-8"))


def _prepare_custody(
    vault: Path,
    *,
    batch_id: str,
    target_rel: str,
    generation: str = "generation-1",
    now: float = 10.0,
    terminal_replay_until: float = 4_000_000_000.0,
    advisory_retention_until: float | None = None,
):
    """Prepare, prove and publish real Lane 1 custody for one advisory target."""
    fingerprint = _fingerprint(vault, target_rel)
    receipt = derived_receipts.prepare_batch(
        vault,
        batch_id=batch_id,
        mutation_attempt_digest=hashlib.sha256(batch_id.encode("utf-8")).hexdigest(),
        canonical_generation=generation,
        checkpoint_id=f"checkpoint-{batch_id}",
        paths=(
            derived_receipts.DerivedBatchPath(
                rel_path=target_rel,
                before_hash=None,
                after_hash=fingerprint,
            ),
        ),
        required_components=frozenset({derived_receipts.DerivedComponent.WRITE_ADVISORY}),
        advisory_target_rel_path=target_rel,
        advisory_target_fingerprint=fingerprint,
        terminal_replay_until=terminal_replay_until,
        advisory_retention_until=advisory_retention_until,
        now=now,
    )
    proof = derived_receipts.prove_committed(
        vault, receipt, current_generation=generation, now=now + 0.1
    )
    assert proof.outcome == "ready"
    assert derived_receipts.publish_pending_visibility(
        vault,
        receipt,
        publisher=lambda _root, _receipt: True,
        now=now + 0.2,
    )
    return receipt, fingerprint


def _claim(vault: Path, *, owner: str = "lane4-worker", now: float = 20.0, lease: float = 60.0):
    claims = derived_receipts.claim_ready_components(
        vault, owner=owner, limit=8, lease_seconds=lease, now=now
    )
    advisory = [
        status
        for status in claims
        if status.component is derived_receipts.DerivedComponent.WRITE_ADVISORY
    ]
    assert advisory, "the accepted store did not offer the advisory component"
    return advisory[0]


def _observer(generation: str):
    return lambda _vault_root: generation


def _run(vault: Path, *, generation: str = "generation-1", owner: str = "lane4-worker", now: float = 20.0):
    return _advisory().run_pending_write_advisories(
        vault,
        observe_current_generation=_observer(generation),
        owner=owner,
        now=now,
    )


def _resolve(vault: Path, ref):
    return commands.op_review_memory(vault, mode="write-advisory-result", ref=ref)


def _wire_candidates(
    monkeypatch: pytest.MonkeyPatch,
    duplicates: list[corpus_aware.DupCandidate],
    overlaps: list[corpus_aware.DupCandidate] | None = None,
) -> None:
    """Pin the frozen detectors so the lane's own behaviour is what is measured."""
    monkeypatch.setattr(corpus_aware, "detect_duplicates", lambda *a, **k: list(duplicates))
    monkeypatch.setattr(
        corpus_aware, "detect_contradictions", lambda *a, **k: list(overlaps or [])
    )


def _candidate(vault: Path, name: str) -> corpus_aware.DupCandidate:
    rel = _seed_page(vault, name, f"Counterpart body for {name}.")
    return corpus_aware.DupCandidate(path=rel, title=f"Existing {name}", cosine=0.93)


# ---------------------------------------------------------------------------
# Governance helpers — a real policy, not a stubbed release plane
# ---------------------------------------------------------------------------


def _govern(vault: Path, *, glob: str, ceiling: int) -> None:
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "rules").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "lane4.yaml").write_text(
        f'governance_version: 1\nid: {_SCOPE_ID}\nname: Lane4\npaths: ["{glob}"]\n',
        encoding="utf-8",
    )
    (gov / "rules" / "lane4.yaml").write_text(
        f'governance_version: 1\nid: {_RULE_ID}\nscope_ids: ["{_SCOPE_ID}"]\n'
        f"audience: {_EXTERNAL}\nceiling: {ceiling}\n",
        encoding="utf-8",
    )
    from exomem.governance import membership, policy

    policy._CACHE.clear()
    membership.clear_memo()
    egress_module.clear_decision_memo()
    find_module.clear_cache()


def _external() -> RequestPrincipal:
    return RequestPrincipal(audience_id=_EXTERNAL, surface="mcp")


def _review_state_bytes(vault: Path) -> str:
    """The whole review-state payload: first-surfaced ledger and quiet offers.

    Snapshotting all of it, rather than one key, is deliberate: the guard is
    that a retryable pass performs no once-only mutation at all.
    """
    from exomem import review_state as review_state_module

    return json.dumps(
        review_state_module.ReviewStateStore(vault).load(), sort_keys=True, default=str
    )


def _carrier_bytes(vault: Path) -> str:
    from exomem import due_state as due_state_module

    payload = {
        "attention": commands.op_review_memory(vault, mode="attention", limit=25, state="all"),
        "activation": commands.op_review_memory(vault, mode="activation", limit=25, state="all"),
        "dispositions": commands.op_review_memory(vault, mode="dispositions"),
        "due_state": due_state_module.recompute(vault),
    }
    return json.dumps(payload, sort_keys=True, default=str)


# ---------------------------------------------------------------------------
# Receipt-owned component execution
# ---------------------------------------------------------------------------


def test_advisory_worker_runs_from_receipt_without_foreground_writer(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    module = _advisory()
    target = _seed_page(vault, "lane4-target", "Deferred advisory target body.")
    receipt, _fingerprint = _prepare_custody(vault, batch_id="b-worker", target_rel=target)

    # No writer, no lease holder, no scheduler: only the durable receipt.
    executions = _run(vault)

    assert len(executions) == 1
    assert executions[0].outcome == "published"
    status = derived_receipts.component_status(
        vault, receipt, derived_receipts.DerivedComponent.WRITE_ADVISORY
    )
    assert status.state == "completed"
    result = derived_receipts.read_advisory_result(
        vault, derived_receipts.advisory_result_ref(vault, receipt), now=30.0
    )
    assert result is not None and result.state in {"ready", "failed"}

    source = inspect.getsource(module)
    for forbidden in ("threading", "Thread(", "while True", "time.sleep"):
        assert forbidden not in source, f"Lane 4 must own no scheduler ({forbidden})"


def test_advisory_result_ref_is_stable_opaque_and_replayable(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    module = _advisory()
    target = _seed_page(vault, "lane4-stable", "Stable reference body.")
    receipt, fingerprint = _prepare_custody(vault, batch_id="b-stable", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)

    assert _RESULT_REF_RE.match(ref or ""), ref
    assert not (ref or "").startswith(corpus_aware._WRITE_ADVISORY_REF_PREFIX)

    # Exact replay of the same mutation identity reproduces the same reference.
    replayed, _ = _prepare_custody(vault, batch_id="b-stable", target_rel=target, now=11.0)
    assert derived_receipts.advisory_result_ref(vault, replayed) == ref

    # The same identity through the frozen Lane 1 handoff fake agrees.
    fake = DerivedReceiptProtocolFake()
    faked = fake.prepare_batch(
        vault,
        batch_id="b-stable",
        mutation_attempt_digest=hashlib.sha256(b"b-stable").hexdigest(),
        canonical_generation="generation-1",
        checkpoint_id="checkpoint-b-stable",
        paths=receipt.paths,
        required_components=frozenset({derived_receipts.DerivedComponent.WRITE_ADVISORY}),
        advisory_target_rel_path=target,
        advisory_target_fingerprint=fingerprint,
        terminal_replay_until=4_000_000_000.0,
        now=10.0,
    )
    assert module.advisory_custody(vault, faked, handoff=fake).result_ref == ref

    # Opaque: no path, title, batch row, principal, or candidate count leaks.
    opaque = ref.rsplit("/", 1)[-1]
    assert re.fullmatch(r"[0-9a-f]{32}", opaque)
    for secret in (target, "lane4-stable", "b-stable", "Insights", "owner"):
        assert secret.lower() not in opaque

    other = _seed_page(vault, "lane4-other", "Another body.")
    other_receipt, _ = _prepare_custody(vault, batch_id="b-other", target_rel=other, now=12.0)
    assert derived_receipts.advisory_result_ref(vault, other_receipt) != ref

    # Retention keeps it resolvable for as long as the terminal replays.
    assert _resolve(vault, ref)["status"] == "pending"


def test_advisory_result_lifecycle_success_restart_and_replay(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _seed_page(vault, "lane4-lifecycle", "Lifecycle target body.")
    _wire_candidates(monkeypatch, [_candidate(vault, "lane4-lifecycle-counterpart")])
    receipt, _ = _prepare_custody(vault, batch_id="b-life", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)

    assert _resolve(vault, ref)["status"] == "pending"

    # A worker dies holding a short lease without publishing anything.
    dead = _claim(vault, owner="dead-worker", now=20.0, lease=1.0)
    assert dead.state == "claimed"
    assert _resolve(vault, ref)["status"] == "pending"

    # A later process resumes from durable state alone.
    executions = _run(vault, owner="live-worker", now=100.0)
    assert [execution.outcome for execution in executions] == ["published"]
    ready = _resolve(vault, ref)
    assert ready["status"] == "ready"
    assert ready["advisories"]

    # Replay is idempotent and reports the same current state.
    assert _run(vault, owner="live-worker", now=200.0) == ()
    assert _resolve(vault, ref) == ready


def test_advisory_result_failure_is_visible_and_not_mutation_failure(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _advisory()
    target = _seed_page(vault, "lane4-failure", "Failure target body.")
    before = (vault / target).read_bytes()
    receipt, _ = _prepare_custody(vault, batch_id="b-fail", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)

    monkeypatch.setattr(embeddings, "prepare_generation_vectors", lambda *a, **k: None)
    executions = _run(vault)

    assert [execution.outcome for execution in executions] == ["published"]
    assert executions[0].state == "failed"
    failed = _resolve(vault, ref)
    assert failed["status"] == "failed"
    assert failed["code"] in derived_receipts._ADVISORY_FAILURE_CODES
    assert failed["status"] != "pending"
    # The committed canonical mutation is untouched by an optional failure.
    assert (vault / target).read_bytes() == before
    assert module.advisory_custody(vault, receipt).status.state == "completed"


def test_advisory_result_material_target_change_is_superseded(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _seed_page(vault, "lane4-superseded", "Original target body.")
    _wire_candidates(monkeypatch, [_candidate(vault, "lane4-superseded-counterpart")])
    receipt, _ = _prepare_custody(vault, batch_id="b-super", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert _run(vault)[0].outcome == "published"
    assert _resolve(vault, ref)["status"] == "ready"

    (vault / target).write_text("---\ntype: insight\n---\n\nMaterially different.\n", encoding="utf-8")
    find_module.clear_cache()

    superseded = _resolve(vault, ref)
    assert superseded == {
        "mode": "write-advisory-result",
        "ref": ref,
        "status": "superseded",
    }

    # A worker that reaches publication after the change supersedes the row.
    other = _seed_page(vault, "lane4-superseded-two", "Second target body.")
    second, _ = _prepare_custody(
        vault, batch_id="b-super-2", target_rel=other, generation="generation-2", now=40.0
    )
    second_ref = derived_receipts.advisory_result_ref(vault, second)
    claimed = _claim(vault, owner="racing-worker", now=50.0)
    outcome = derived_receipts.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(),
        observed_target_fingerprint=hashlib.sha256(b"a different generation").hexdigest(),
        now=51.0,
    )
    assert outcome.outcome == "superseded"
    assert _resolve(vault, second_ref)["status"] == "superseded"


def test_advisory_worker_revalidates_the_target_fingerprint_before_publish(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    """What proves an advisory current is its target's bytes, not a counter.

    This node used to pin a refusal on the vault-global canonical generation.
    Rulings R1 and R3 removed that comparison: the generation is a single graph
    checkpoint that advances on every write to *any* page, so as a per-page
    freshness test it refused writes that were entirely sound -- in a burst,
    every batch but the last-written one, until its attempts ran out. The
    fingerprint asks the question the counter could not, and it is what this
    node pins now. The claim CAS below is unchanged and still refuses a stale
    worker outright.
    """
    module = _advisory()

    # 1. The target's bytes moved after custody was prepared. Whatever this
    #    result would have said is about content the vault no longer holds, so
    #    it must publish its own supersession rather than current-looking output.
    moved = _seed_page(vault, "lane4-fingerprint", "Fingerprint proof body.")
    moved_receipt, _ = _prepare_custody(
        vault, batch_id="b-fingerprint", target_rel=moved
    )
    moved_ref = derived_receipts.advisory_result_ref(vault, moved_receipt)
    _seed_page(vault, "lane4-fingerprint", "Rewritten body this result never saw.")

    executions = module.run_pending_write_advisories(
        vault,
        observe_current_generation=_observer("generation-1"),
        owner="fingerprint-worker",
        now=20.0,
    )
    assert [execution.outcome for execution in executions] == ["superseded"], executions
    assert [execution.state for execution in executions] == [None], executions
    assert [execution.candidate_count for execution in executions] == [0], executions
    # The stale result is retired as superseded -- never surfaced as an
    # advisory about bytes the vault no longer holds.
    assert _resolve(vault, moved_ref)["status"] == "superseded"

    # 2. The vault-global generation moving is no longer a refusal on its own.
    #    This batch's target is untouched, so its result is still about the
    #    bytes on disk and publishing it is correct.
    intact = _seed_page(vault, "lane4-generation", "Generation proof body.")
    intact_receipt, _ = _prepare_custody(
        vault, batch_id="b-gen", target_rel=intact
    )
    intact_ref = derived_receipts.advisory_result_ref(vault, intact_receipt)

    executions = module.run_pending_write_advisories(
        vault,
        observe_current_generation=_observer("generation-9"),
        owner="later-worker",
        now=40.0,
    )
    by_batch = {execution.batch_id: execution for execution in executions}
    assert by_batch["b-gen"].outcome == "published", executions
    assert by_batch["b-gen"].state == "ready", executions
    assert _resolve(vault, intact_ref)["status"] == "ready"

    # 3. An older claim revision still cannot publish current output. This is
    #    the guard that actually stops a stale worker, and it is untouched.
    stale_target = _seed_page(vault, "lane4-stale", "Stale claim body.")
    stale_receipt, _ = _prepare_custody(
        vault, batch_id="b-stale", target_rel=stale_target
    )
    stale_ref = derived_receipts.advisory_result_ref(vault, stale_receipt)
    claimed = _claim(vault, owner="older-worker", now=200.0)
    stale = module.execute_write_advisory(
        vault,
        replace(claimed, revision=claimed.revision + 1),
        now=201.0,
    )
    assert stale.outcome == "stale_claim"
    assert _resolve(vault, stale_ref)["status"] == "pending"


# ---------------------------------------------------------------------------
# One-generation vector reuse
# ---------------------------------------------------------------------------


def test_embedding_and_advisory_encode_generation_once(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    _generation_seam()
    target = _seed_page(vault, "lane4-once", "Shared generation body for one encode.")
    page = find_module._CACHE.get(vault / target, vault)
    assert page is not None
    chunks = embeddings._chunks_for_page(vault, page)
    assert chunks

    # The embedding consumer publishes this generation's page vectors.
    status = embeddings.upsert_after_write_status(vault, [vault / target])
    assert status.status in {"completed", "degraded"}
    assert encoder.count_for(chunks) == 1

    _prepare_custody(vault, batch_id="b-once", target_rel=target)
    assert _run(vault)[0].outcome == "published"

    # The advisory consumer reuses them: exactly one encode for the generation.
    assert encoder.count_for(chunks) == 1


def test_published_vectors_are_reused_after_worker_restart_without_reencoding(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    prepare = _generation_seam()
    scoring = _scoring_seam()
    target = _seed_page(vault, "lane4-replay", "Replayed generation body for reuse.")
    page = find_module._CACHE.get(vault / target, vault)
    assert page is not None
    chunks = embeddings._chunks_for_page(vault, page)
    embeddings.upsert_after_write_status(vault, [vault / target])
    published = np.stack([_vector_for(chunk) for chunk in chunks]).astype(np.float32)

    # A fresh process: no in-memory index cache, no in-flight handoff.
    embeddings.clear_embedding_indexes()
    find_module.clear_cache()
    encoder.reset()

    fingerprint = _fingerprint(vault, target)
    reused = prepare(vault, target, expected_fingerprint=fingerprint)
    assert reused is not None
    assert reused.reused is True
    assert encoder.calls == []
    assert np.allclose(np.asarray(reused.vectors, dtype=np.float32), published)

    # Self is excluded from the advisory comparison over those exact vectors.
    scores = scoring(vault, reused.vectors, self_path=target)
    assert target not in scores
    assert encoder.calls == []

    _prepare_custody(vault, batch_id="b-replay", target_rel=target)
    assert _run(vault)[0].outcome == "published"
    assert encoder.count_for(chunks) == 0


# ---------------------------------------------------------------------------
# Exact-only retrieval surface
# ---------------------------------------------------------------------------


def test_write_advisory_result_requires_ref_and_has_no_list_form(vault: Path) -> None:
    module = _advisory()
    with pytest.raises(ValueError) as missing:
        commands.op_review_memory(vault, mode="write-advisory-result")
    assert str(missing.value).startswith("INVALID_REVIEW")

    for kwargs in (
        {"limit": 25},
        {"state": "all"},
        {"query": "anything"},
        {"categories": ["write_advisory"]},
        {"ref": ""},
        {"ref": None},
    ):
        with pytest.raises(ValueError) as guarded:
            commands.op_review_memory(vault, mode="write-advisory-result", **kwargs)
        assert str(guarded.value).startswith("INVALID_REVIEW")

    # No enumeration seam anywhere in the lane or the frozen store.
    exported = {name for name in vars(module) if not name.startswith("_")}
    for banned in ("list_results", "search_results", "latest_result", "recent_results"):
        assert banned not in exported
    assert not any("advisory" in command.name for command in commands.PRODUCT_COMMANDS)
    assert len(commands.PRODUCT_COMMANDS) == 29


def test_malformed_unknown_unauthorized_and_expired_result_refs_are_indistinguishable(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    expired_target = _seed_page(vault, "lane4-expired", "Expired retention body.")
    expired_receipt, _ = _prepare_custody(
        vault,
        batch_id="b-expired",
        target_rel=expired_target,
        now=10.0,
        terminal_replay_until=11.0,
        advisory_retention_until=11.0,
    )
    expired_ref = derived_receipts.advisory_result_ref(vault, expired_receipt)

    restricted_target = _seed_page(
        vault, "lane4-unauthorized", "Unauthorized target body.", folder="Patterns"
    )
    restricted_receipt, _ = _prepare_custody(
        vault,
        batch_id="b-unauthorized",
        target_rel=restricted_target,
        generation="generation-2",
        now=20.0,
    )
    unauthorized_ref = derived_receipts.advisory_result_ref(vault, restricted_receipt)

    malformed = "not-an-exomem-reference"
    wrong_namespace = "exomem://review/write-advisory/" + "a" * 24
    unknown = "exomem://write-advisory-result/" + "b" * 32

    outcomes = []
    for ref in (malformed, wrong_namespace, unknown, expired_ref):
        with pytest.raises(ValueError) as error:
            commands.op_review_memory(vault, mode="write-advisory-result", ref=ref)
        outcomes.append((type(error.value), str(error.value)))

    _govern(vault, glob="Notes/Patterns/**", ceiling=0)
    with request_scope(_external()):
        with pytest.raises(ValueError) as unauthorized:
            commands.op_review_memory(
                vault, mode="write-advisory-result", ref=unauthorized_ref
            )
    outcomes.append((type(unauthorized.value), str(unauthorized.value)))

    assert len(set(outcomes)) == 1, outcomes
    assert outcomes[0][1].startswith("REVIEW_ITEM_NOT_FOUND")
    for leak in (expired_target, restricted_target, "b-expired", expired_ref, unauthorized_ref):
        assert leak not in outcomes[0][1]


def test_advisory_result_pending_ready_failed_superseded_wire_shapes_are_closed(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "confidentialpayloadtoken"
    target = _seed_page(vault, "lane4-shapes", f"Shapes target body {secret}.")
    counterpart = _candidate(vault, "lane4-shapes-counterpart")
    _wire_candidates(monkeypatch, [counterpart])
    receipt, _ = _prepare_custody(vault, batch_id="b-shapes", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)

    pending = _resolve(vault, ref)
    assert pending == {"mode": "write-advisory-result", "ref": ref, "status": "pending"}

    assert _run(vault)[0].outcome == "published"
    ready = _resolve(vault, ref)
    assert set(ready) == {"mode", "ref", "status", "advisories"}
    assert ready["status"] == "ready"
    for advisory in ready["advisories"]:
        assert set(advisory) == {"warning", "ref", "fingerprint"}
        assert _REVIEW_REF_RE.match(advisory["ref"]), advisory["ref"]
        assert re.fullmatch(r"[0-9a-f]{24}", advisory["fingerprint"])
        assert isinstance(advisory["warning"], str) and advisory["warning"]
    json.dumps(ready)

    # A failed result on separate custody carries only its closed code.
    failing = _seed_page(vault, "lane4-shapes-failed", "Failing target body.")
    failed_receipt, _ = _prepare_custody(
        vault, batch_id="b-shapes-failed", target_rel=failing, generation="generation-2", now=40.0
    )
    failed_ref = derived_receipts.advisory_result_ref(vault, failed_receipt)
    monkeypatch.setattr(embeddings, "prepare_generation_vectors", lambda *a, **k: None)
    assert _run(vault, generation="generation-2", owner="failing-worker", now=50.0)[0].state == "failed"
    failed = _resolve(vault, failed_ref)
    assert set(failed) == {"mode", "ref", "status", "code"}
    assert failed["status"] == "failed"
    assert secret not in json.dumps(failed)

    (vault / target).write_text("---\ntype: insight\n---\n\nRewritten.\n", encoding="utf-8")
    find_module.clear_cache()
    superseded = _resolve(vault, ref)
    assert set(superseded) == {"mode", "ref", "status"}
    assert superseded["status"] == "superseded"
    assert secret not in json.dumps(superseded)


def test_advisory_ready_warnings_are_bounded(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _emitter_seam()
    target = _seed_page(vault, "lane4-bounded", "Bounded warnings target body.")
    many = [_candidate(vault, f"lane4-bounded-{index}") for index in range(12)]
    _wire_candidates(monkeypatch, many)
    receipt, _ = _prepare_custody(vault, batch_id="b-bounded", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)

    assert _run(vault)[0].outcome == "published"
    ready = _resolve(vault, ref)

    assert ready["status"] == "ready"
    assert 0 < len(ready["advisories"]) <= derived_receipts._MAX_ADVISORY_CANDIDATES
    for advisory in ready["advisories"]:
        assert len(advisory["warning"]) <= corpus_aware._WRITE_ADVISORY_WARNING_CHARS
    refs = [advisory["ref"] for advisory in ready["advisories"]]
    assert len(set(refs)) == len(refs)


def test_write_advisory_result_mode_has_mcp_rest_cli_surface_parity(vault: Path) -> None:
    entry = next(
        command for command in commands.PRODUCT_COMMANDS if command.name == "review_memory"
    )
    assert entry.leaf is commands.op_review_memory
    assert entry.surfaces == frozenset({"mcp", "rest", "cli"})

    mode_param = next(param for param in entry.params if param.name == "mode")
    ref_param = next(param for param in entry.params if param.name == "ref")
    assert "write-advisory-result" in (mode_param.help or "")
    assert "write-advisory-result" in (ref_param.help or "")
    assert not any("advisory" in command.name for command in commands.PRODUCT_COMMANDS)

    # One shared leaf: the surfaces project the same callable, not a new route.
    with pytest.raises(ValueError):
        entry.leaf(vault, mode="write-advisory-result")
    with pytest.raises(ValueError):
        commands.op_review_memory(vault, mode="write-advisory-result")

    schemas = json.loads(
        (Path(__file__).resolve().parents[1] / "tests/fixtures/mcp_tool_schemas.json").read_text(
            encoding="utf-8"
        )
    )
    assert "write-advisory-result" in schemas["review_memory"]["inputSchema"]["properties"]["mode"][
        "description"
    ]
    assert "write-advisory-result" in schemas["review_memory"]["inputSchema"]["properties"]["ref"][
        "description"
    ]


# ---------------------------------------------------------------------------
# Current-authority privacy
# ---------------------------------------------------------------------------


def test_advisory_result_withheld_candidate_is_absent(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _seed_page(vault, "lane4-withheld", "Withheld projection target body.")
    restricted = _seed_page(
        vault, "lane4-restricted", "Restricted counterpart.", folder="Patterns"
    )
    released = _candidate(vault, "lane4-released")
    _wire_candidates(
        monkeypatch,
        [corpus_aware.DupCandidate(path=restricted, title="Restricted", cosine=0.95), released],
    )
    receipt, _ = _prepare_custody(vault, batch_id="b-withheld", target_rel=target)
    assert _run(vault)[0].outcome == "published"
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert len(_resolve(vault, ref)["advisories"]) == 2

    _govern(vault, glob="Notes/Patterns/**", ceiling=0)
    with request_scope(_external()):
        withheld = _resolve(vault, ref)

    assert withheld["status"] == "ready"
    payload = json.dumps(withheld)
    assert restricted not in payload
    assert "lane4-restricted" not in payload
    assert released.path in payload

    # Byte-identical to a job that never found that counterpart at all.
    (vault / restricted).unlink()
    find_module.clear_cache()
    with request_scope(_external()):
        never_found = _resolve(vault, ref)

    assert withheld == never_found


def test_advisory_result_deleted_stale_candidate_is_absent(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _seed_page(vault, "lane4-stale", "Stale counterpart target body.")
    deleted = _candidate(vault, "lane4-deleted")
    stale = _candidate(vault, "lane4-stale-counterpart")
    kept = _candidate(vault, "lane4-kept")
    _wire_candidates(monkeypatch, [deleted, stale, kept])
    receipt, _ = _prepare_custody(vault, batch_id="b-stale", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert _run(vault)[0].outcome == "published"
    assert len(_resolve(vault, ref)["advisories"]) == 3

    (vault / deleted.path).unlink()
    (vault / stale.path).write_text(
        "---\ntype: insight\nstatus: active\n---\n\nRewritten counterpart.\n", encoding="utf-8"
    )
    find_module.clear_cache()

    projected = _resolve(vault, ref)
    payload = json.dumps(projected)
    assert projected["status"] == "ready"
    assert len(projected["advisories"]) == 1
    assert deleted.path not in payload
    assert stale.path not in payload
    assert kept.path in payload


def test_advisory_lookup_reauthorizes_current_target_and_counterparts(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _seed_page(vault, "lane4-reauth", "Reauthorized target body.")
    counterpart = _candidate(vault, "lane4-reauth-counterpart")
    _wire_candidates(monkeypatch, [counterpart])
    receipt, _ = _prepare_custody(vault, batch_id="b-reauth", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert _run(vault)[0].outcome == "published"

    seen: list[list[str]] = []
    real = egress_module.annotate_hits

    def _spy(vault_root, hits, **kwargs):
        seen.append([getattr(hit, "path", "") for hit in hits])
        return real(vault_root, hits, **kwargs)

    monkeypatch.setattr(egress_module, "annotate_hits", _spy)
    projected = _resolve(vault, ref)

    assert projected["status"] == "ready"
    # The written target is reauthorized first, then every counterpart.
    assert [path for batch in seen for path in batch] == [target, counterpart.path]

    # Every lookup revalidates, so a later target change is seen immediately.
    (vault / target).write_text("---\ntype: insight\n---\n\nChanged.\n", encoding="utf-8")
    find_module.clear_cache()
    assert _resolve(vault, ref)["status"] == "superseded"


# ---------------------------------------------------------------------------
# Review-carrier isolation
# ---------------------------------------------------------------------------


def test_advisory_result_never_joins_review_carriers(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    target = _seed_page(vault, "lane4-carriers", "Carrier isolation target body.")
    counterpart_rel = _seed_page(vault, "lane4-carriers-counterpart", "Carrier counterpart.")
    _carrier_bytes(vault)
    baseline = _carrier_bytes(vault)

    receipt, fingerprint = _prepare_custody(vault, batch_id="b-carriers", target_rel=target)
    claimed = _claim(vault, owner="carrier-worker", now=20.0)
    review_id = hashlib.sha256(b"lane4-carriers").hexdigest()[:24]
    candidate = derived_receipts.DerivedAdvisoryCandidate(
        counterpart_rel_path=counterpart_rel,
        counterpart_fingerprint=_fingerprint(vault, counterpart_rel),
        warning="Carrier isolation counterpart warning.",
        advisory_ref=derived_receipts.advisory_result_ref(vault, receipt),
        review_ref=f"exomem://review/write-advisory/{review_id}",
        triage_fingerprint=hashlib.sha256(b"carrier-triage").hexdigest()[:24],
    )
    assert derived_receipts.publish_advisory_result(
        vault,
        claimed,
        state="ready",
        candidates=(candidate,),
        observed_target_fingerprint=fingerprint,
        now=21.0,
    ).outcome == "published"
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert _resolve(vault, ref)["status"] == "ready"

    assert _carrier_bytes(vault) == baseline


def test_ready_advisory_review_ref_keeps_existing_triage_semantics(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _seed_page(vault, "lane4-triage", "Triage semantics target body.")
    counterpart = _candidate(vault, "lane4-triage-counterpart")
    _wire_candidates(monkeypatch, [counterpart])
    receipt, _ = _prepare_custody(vault, batch_id="b-triage", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)
    assert _run(vault)[0].outcome == "published"

    [advisory] = _resolve(vault, ref)["advisories"]
    before = _carrier_bytes(vault)
    decision = corpus_aware.triage_write_advisory(
        vault,
        ref=advisory["ref"],
        action="dismiss",
        why="lane 4 keeps the existing fingerprint-bound decision namespace",
        expected_fingerprint=advisory["fingerprint"],
    )

    assert decision["state"] == "dismissed"
    assert decision["ref"] == advisory["ref"]
    assert _carrier_bytes(vault) == before

    # The existing suppression still consumes that exact decision.
    suppressed = corpus_aware.emit_write_advisory_groups(
        vault,
        self_path=target,
        groups=[("near-duplicate", [counterpart])],
        apply_declared_pair_filter=True,
    )
    assert suppressed == []


def test_advisory_operational_status_is_content_free(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "operationalsecrettoken"
    target = _seed_page(vault, "lane4-content-free", f"Body carrying {secret}.")
    counterpart = _candidate(vault, "lane4-content-free-counterpart")
    _wire_candidates(monkeypatch, [counterpart])
    receipt, _ = _prepare_custody(vault, batch_id="b-content-free", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)

    pending = json.dumps(_resolve(vault, ref))
    executions = _run(vault)
    telemetry = json.dumps([str(execution) for execution in executions])
    custody = json.dumps(str(_advisory().advisory_custody(vault, receipt)))

    for payload in (pending, telemetry, custody):
        for leak in (secret, target, counterpart.path, "Body carrying", "withheld"):
            assert leak not in payload
    assert "pending" in pending


# ---------------------------------------------------------------------------
# Preservation nodes — recorded, and required to stay green
# ---------------------------------------------------------------------------


def test_explicit_suggestions_remain_synchronous_and_unchanged(
    vault: Path, encoder: _DeterministicEncoder
) -> None:
    import inspect as inspect_module

    from exomem import add as add_module
    from exomem import edit as edit_module
    from exomem import note as note_module
    from exomem import replace as replace_module

    # The explicit opt-in is still an explicit, default-off writer parameter on
    # every leaf that offers it, and on the public command that projects them.
    for writer in (note_module.note, replace_module.replace, commands.op_remember):
        parameter = inspect_module.signature(writer).parameters.get("suggestions")
        assert parameter is not None, writer
        assert parameter.default is False

    # It still resolves inside the same call, with no deferral and no result ref.
    _seed_page(vault, "lane4-suggestion-neighbour", "Retrieval neighbour body.")
    suggestions = corpus_aware.suggest_related(
        vault,
        title="Lane 4 explicit suggestions",
        body="Retrieval neighbour body for the explicit synchronous opt-in.",
        limit=6,
    )
    assert isinstance(suggestions, list)
    rendered = json.dumps([item.as_dict() for item in suggestions])
    assert "write-advisory-result" not in rendered

    # No writer or terminal route was moved into this lane's allowlist.
    for module in (note_module, add_module, edit_module, replace_module):
        assert "deferred_write_advisory" not in inspect_module.getsource(module)


def test_lane1_terminal_handoff_fake_is_used_without_shape_translation(vault: Path) -> None:
    module = _advisory()
    target = _seed_page(vault, "lane4-handoff", "Handoff target body.")
    fingerprint = _fingerprint(vault, target)
    fake = DerivedReceiptProtocolFake()
    receipt = fake.prepare_batch(
        vault,
        batch_id="b-handoff",
        mutation_attempt_digest=hashlib.sha256(b"b-handoff").hexdigest(),
        canonical_generation="generation-1",
        checkpoint_id="checkpoint-b-handoff",
        paths=(
            derived_receipts.DerivedBatchPath(
                rel_path=target, before_hash=None, after_hash=fingerprint
            ),
        ),
        required_components=frozenset({derived_receipts.DerivedComponent.WRITE_ADVISORY}),
        advisory_target_rel_path=target,
        advisory_target_fingerprint=fingerprint,
        terminal_replay_until=4_000_000_000.0,
        now=10.0,
    )

    custody = module.advisory_custody(vault, receipt, handoff=fake)

    assert isinstance(receipt, derived_receipts.DerivedBatchReceipt)
    assert isinstance(custody.status, derived_receipts.DerivedComponentStatus)
    assert custody.status is fake.component_status(
        vault, receipt, derived_receipts.DerivedComponent.WRITE_ADVISORY
    )
    assert custody.result_ref == fake.advisory_result_ref(vault, receipt)
    assert fake.call_count("component_status") == 2
    assert fake.call_count("advisory_result_ref") == 2
    assert "component_status" in fake.call_order and "advisory_result_ref" in fake.call_order


# ---------------------------------------------------------------------------
# Correction round 1 — convergence, authority ordering, once-only side effects
# ---------------------------------------------------------------------------


def test_crash_after_publication_completes_without_recomputation(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker that died after publishing converges on the next pass.

    Design Decision 6: a crash after result publication reuses the stored
    result and completes the component without recomputation. Ordinary corpus
    drift between the crash and the replay must not change that, which is
    exactly the case a recomputing replay cannot survive: its freshly computed
    candidate tuple no longer matches the published one, so the store refuses
    the write and the component never completes.
    """
    module = _advisory()
    target = _seed_page(vault, "lane4-crash", "Crash after publication body.")
    drifting = _candidate(vault, "lane4-crash-drifting")
    steady = _candidate(vault, "lane4-crash-steady")
    _wire_candidates(monkeypatch, [drifting, steady])
    receipt, _ = _prepare_custody(vault, batch_id="b-crash", target_rel=target)
    ref = derived_receipts.advisory_result_ref(vault, receipt)

    # Worker A publishes, then dies before completing the component.
    worker_a = _claim(vault, owner="worker-a", now=20.0, lease=5.0)
    published = module.execute_write_advisory(
        vault,
        worker_a,
        now=21.0,
    )
    assert published.outcome == "published"
    assert published.state == "ready"
    stored_before = derived_receipts.read_advisory_result(vault, ref)
    assert stored_before is not None and len(stored_before.candidates) == 2
    assert (
        derived_receipts.component_status(
            vault, receipt, derived_receipts.DerivedComponent.WRITE_ADVISORY
        ).state
        == "claimed"
    )

    # An ordinary write edits one counterpart before the replay.
    (vault / drifting.path).write_text(
        "---\ntype: insight\nstatus: active\n---\n\nDrifted counterpart body.\n",
        encoding="utf-8",
    )
    find_module.clear_cache()
    encoder.reset()
    review_state_before = _review_state_bytes(vault)

    executions = _run(vault, owner="worker-b", now=100.0)

    assert [execution.outcome for execution in executions] == ["already_published"]
    assert executions[0].completed is True
    assert (
        derived_receipts.component_status(
            vault, receipt, derived_receipts.DerivedComponent.WRITE_ADVISORY
        ).state
        == "completed"
    )
    # No recomputation: no encoder call and no once-only review-state mutation.
    assert encoder.calls == []
    assert _review_state_bytes(vault) == review_state_before
    # The stored result is reused, not rewritten.
    stored_after = derived_receipts.read_advisory_result(vault, ref)
    assert stored_after is not None
    assert stored_after.state == "ready"
    assert stored_after.candidates == stored_before.candidates
    assert stored_after.publication_revision == stored_before.publication_revision

    # Benign control: the same replay with no drift also converges.
    control_target = _seed_page(vault, "lane4-crash-control", "Control target body.")
    _wire_candidates(monkeypatch, [steady])
    control_receipt, _ = _prepare_custody(
        vault,
        batch_id="b-crash-control",
        target_rel=control_target,
        generation="generation-2",
        now=200.0,
    )
    control_claim = _claim(vault, owner="worker-c", now=210.0, lease=5.0)
    assert (
        module.execute_write_advisory(
            vault,
            control_claim,
            now=211.0,
        ).outcome
        == "published"
    )
    control = _run(vault, generation="generation-2", owner="worker-d", now=300.0)
    assert [execution.outcome for execution in control] == ["already_published"]
    assert control[0].completed is True
    assert (
        derived_receipts.component_status(
            vault, control_receipt, derived_receipts.DerivedComponent.WRITE_ADVISORY
        ).state
        == "completed"
    )


def test_retryable_publication_burns_no_once_only_review_state(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass whose publication is refused leaves the ledger untouched.

    The first-surfaced ledger measures when a signal reached somebody. A
    candidate set that was computed and then refused by the store reached
    nobody, so stamping it would both burn the once-only record and change the
    warning text a later successful pass renders.
    """
    module = _advisory()
    target = _seed_page(vault, "lane4-ledger", "Ledger guard target body.")
    counterpart = _candidate(vault, "lane4-ledger-counterpart")
    _wire_candidates(monkeypatch, [counterpart])
    receipt, _ = _prepare_custody(vault, batch_id="b-ledger", target_rel=target)
    before = _review_state_bytes(vault)

    # A real refusal: the claim's lease has expired by publication time.
    expired = _claim(vault, owner="expired-worker", now=20.0, lease=1.0)
    refused = module.execute_write_advisory(
        vault,
        expired,
        now=500.0,
    )

    assert refused.outcome == "stale_claim"
    assert _review_state_bytes(vault) == before

    # The control: a publication the store accepts does record the surfacing.
    assert _run(vault, owner="live-worker", now=600.0)[0].outcome == "published"
    assert _review_state_bytes(vault) != before


def test_unauthorized_and_deleted_target_states_stay_indistinguishable(
    vault: Path, encoder: _DeterministicEncoder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Authority is decided before any status, including for a deleted target.

    A reference addresses operational state and authorizes nothing, so an
    unauthorized caller must not be able to separate a real reference from an
    unknown one by watching whether the answer is `superseded` or not-found.
    An authorized caller keeps `superseded`, because that is the true state.
    """
    module = _advisory()
    unchanged = _seed_page(vault, "lane4-auth-unchanged", "Unchanged body.", folder="Patterns")
    changed = _seed_page(vault, "lane4-auth-changed", "Changed body.", folder="Patterns")
    deleted = _seed_page(vault, "lane4-auth-deleted", "Deleted body.", folder="Patterns")
    _wire_candidates(monkeypatch, [])

    refs = {}
    for index, rel in enumerate((unchanged, changed, deleted)):
        receipt, _ = _prepare_custody(
            vault,
            batch_id=f"b-auth-{index}",
            target_rel=rel,
            generation=f"generation-{index}",
            now=10.0 + index,
        )
        assert (
            _run(vault, generation=f"generation-{index}", owner=f"w-{index}", now=50.0 + index)[
                0
            ].outcome
            == "published"
        )
        refs[rel] = derived_receipts.advisory_result_ref(vault, receipt)

    (vault / changed).write_text("---\ntype: insight\n---\n\nRewritten.\n", encoding="utf-8")
    (vault / deleted).unlink()
    find_module.clear_cache()

    _govern(vault, glob="Notes/Patterns/**", ceiling=0)

    unknown = "exomem://write-advisory-result/" + "b" * 32
    outcomes = []
    with request_scope(_external()):
        for ref in (refs[unchanged], refs[changed], refs[deleted], unknown):
            with pytest.raises(ValueError) as error:
                commands.op_review_memory(vault, mode="write-advisory-result", ref=ref)
            outcomes.append((type(error.value), str(error.value)))

    assert len(set(outcomes)) == 1, outcomes
    assert outcomes[0][1].startswith("REVIEW_ITEM_NOT_FOUND")

    # The owner is authorized, so the true state survives — including for the
    # deleted target, whose authority is decided without any content.
    with request_scope(owner_principal()):
        assert module.resolve_result(vault, refs[unchanged])["status"] == "ready"
        assert module.resolve_result(vault, refs[changed])["status"] == "superseded"
        assert module.resolve_result(vault, refs[deleted])["status"] == "superseded"
