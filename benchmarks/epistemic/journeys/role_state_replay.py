"""Immutable ordinary-language episodes for artifact-role and current-state replay."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .collection_replay import StoreBearingUtterance, assert_no_store_bearing_utterance

ORIGIN = "Knowledge Base/Notes/Experiments/tea-trials.md"
SOURCE_A = "Knowledge Base/Sources/Articles/steep-temperature.md"
SOURCE_B = "Knowledge Base/Sources/Articles/leaf-size.md"


@dataclass(frozen=True)
class ReplayTurn:
    turn_id: str
    text: str
    quiet: bool = False


@dataclass(frozen=True)
class ReplayCorpus:
    corpus_id: str
    family_id: str
    turns: tuple[ReplayTurn, ...]
    seed_pages: tuple[tuple[str, str], ...]
    expected_roles: tuple[str, ...] = ()
    expect_transient: bool = False
    resolution_boundary: int | None = None
    expert_origin: str = ""

    def digest(self) -> str:
        encoded = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode()).hexdigest()


def build_corpus(corpus: ReplayCorpus) -> ReplayCorpus:
    assert_no_store_bearing_utterance(tuple((turn.turn_id, turn.text) for turn in corpus.turns))
    if not corpus.turns or len({turn.turn_id for turn in corpus.turns}) != len(corpus.turns):
        raise ValueError("replay requires unique, nonempty turns")
    if corpus.family_id not in {"f30", "f31"}:
        raise ValueError("sequence five contains f30 and f31 only")
    if corpus.resolution_boundary is not None and not 0 < corpus.resolution_boundary < len(
        corpus.turns
    ):
        raise ValueError("resolution boundary must follow evidence and precede consent")
    return corpus


def _page(body: str) -> str:
    return (
        "---\n"
        "type: experiment\n"
        "title: Tea trials\n"
        "status: active\n"
        "exomem_id: 1f6d297a-28e7-4aa2-8cc7-62b0da294200\n"
        "sources:\n"
        f"- '[[{SOURCE_A}]]'\n"
        f"- '[[{SOURCE_B}]]'\n"
        "---\n\n" + body
    )


_SOURCES = (
    (
        SOURCE_A,
        "---\ntype: source\ntitle: Steep temperature\nstatus: active\n---\n\n"
        "Lower temperatures preserve aroma.\n",
    ),
    (
        SOURCE_B,
        "---\ntype: source\ntitle: Leaf size\nstatus: active\n---\n\n"
        "Larger leaves slow extraction.\n",
    ),
)
_METHOD_A = (
    "## Procedure\n- id: method-a\n- context: trial A\n\n"
    "Reusable method: steep at 82 C for four minutes.\n"
    "  Hold the strainer steady.\n\n"
)
_RESULT_A = (
    "## Result\n- id: outcome-a\n- context: trial A\n\nTaste results: worked well for trial A.\n\n"
)
_METHOD_B = (
    "## Procedure\n- id: method-b\n- context: trial B\n\n"
    "Repeatable method: cool the infusion for six minutes.\n\n"
)
_RESULT_B = (
    "## Result\n- id: outcome-b\n- context: trial B\n\nTaste results: worked well for trial B.\n\n"
)
_SYNTHESIS = (
    "## Finding\n- id: synthesis\n- context: tea extraction\n"
    f"- relations: cites: [[{SOURCE_A}]], cites: [[{SOURCE_B}]]\n\n"
    "Taken together, temperature and leaf size explain the slower extraction.\n"
)
_PENDING = "## Claim\n- id: pending\n- context: trial A\n\nNo taste results yet.\n\n"


def replay_corpora() -> tuple[ReplayCorpus, ...]:
    role_seed = _SOURCES + ((ORIGIN, _page(_METHOD_A + _METHOD_B)),)
    role_evidence = _page(_METHOD_A + _RESULT_A + _METHOD_B + _RESULT_B + _SYNTHESIS)
    protocol_seed = _SOURCES + (
        (
            ORIGIN,
            _page(
                _METHOD_A.replace("Reusable method: ", "Protocol: ")
                + _RESULT_A
                + _METHOD_B.replace("Repeatable method: ", "Protocol: ")
                + _RESULT_B
                + _SYNTHESIS.replace("Taken together, ", "Source list: ")
            ),
        ),
    )

    def extracted_method(method: str, method_ref: str, outcome_ref: str) -> str:
        return (
            "## Procedure\n- id: extracted\n- relations: "
            f"derived_from: [[{ORIGIN}#{method_ref}]], "
            f"supports: [[{ORIGIN}#{outcome_ref}]]\n\n" + method.split("\n\n", 1)[1]
        )

    extracted_synthesis = (
        "## Finding\n- id: extracted\n- relations: "
        f"derived_from: [[{ORIGIN}#synthesis]], "
        f"cites: [[{SOURCE_A}]], cites: [[{SOURCE_B}]]\n\n" + _SYNTHESIS.split("\n\n", 1)[1]
    )
    promoted_seed = (
        _SOURCES
        + ((ORIGIN, role_evidence),)
        + (
            (
                "Knowledge Base/Notes/Patterns/steeping-method.md",
                "---\ntype: pattern\nstatus: active\ntitle: Steeping method\n---\n\n"
                + extracted_method(_METHOD_A, "method-a", "outcome-a"),
            ),
            (
                "Knowledge Base/Notes/Patterns/cooling-method.md",
                "---\ntype: pattern\nstatus: active\ntitle: Cooling method\n---\n\n"
                + extracted_method(_METHOD_B, "method-b", "outcome-b"),
            ),
            (
                "Knowledge Base/Notes/Research/tea-extraction.md",
                "---\ntype: research-note\nstatus: active\ntitle: Tea extraction\n---\n\n"
                + extracted_synthesis,
            ),
        )
    )
    pending_seed = _SOURCES + ((ORIGIN, _page(_PENDING)),)
    historical_seed = _SOURCES + (
        (ORIGIN, _page(_PENDING.replace("No taste", "Previously, no taste") + _RESULT_A)),
    )
    different_seed = _SOURCES + (
        (
            ORIGIN,
            _page(
                _PENDING
                + _RESULT_A.replace("trial A", "trial B").replace(
                    "context: trial A", "context: trial B"
                )
            ),
        ),
    )
    role_turns = (
        ReplayTurn(
            "t01-result-a",
            "Trial A's steeping method worked well and I can reuse those exact steps.",
        ),
        ReplayTurn("t02-result-b", "Trial B's cooling method also worked well and is repeatable."),
        ReplayTurn(
            "t03-synthesis",
            "Taken together, the temperature and leaf-size articles explain why extraction slowed.",
        ),
        ReplayTurn(
            "t04-tentative", "A third variation might work, but I have not tried it.", quiet=True
        ),
        ReplayTurn(
            "t05-consent",
            "Yes, go ahead with the changes you proposed, keeping the trial history intact.",
        ),
    )
    protocol_turns = (
        ReplayTurn(
            "t01-protocol-a",
            "Trial A followed the written protocol; its tasting outcome is already in the log.",
        ),
        ReplayTurn(
            "t02-protocol-b",
            "Trial B followed its written protocol; its tasting outcome is already in the log.",
        ),
        ReplayTurn(
            "t03-sources",
            "The two articles remain separate references; I have no combined finding.",
        ),
        ReplayTurn(
            "t04-tentative", "A third variation might work, but I have not tried it.", quiet=True
        ),
    )
    state_turns = (
        ReplayTurn("t01-result", "Trial A now has a taste result: it worked well."),
        ReplayTurn("t02-elapsed", "A week has passed since trial A.", quiet=True),
        ReplayTurn(
            "t03-consent", "Yes, make the wording accurate while keeping what happened earlier."
        ),
    )
    historical_turns = (
        ReplayTurn(
            "t01-history",
            "The no-results line describes the time before trial A was tasted; "
            "the result is in the log.",
        ),
        ReplayTurn("t02-elapsed", "A week has passed since trial A.", quiet=True),
    )
    different_turns = (
        ReplayTurn(
            "t01-other-trial",
            "Trial B's tasting result is in the log; trial A still has no taste result.",
        ),
        ReplayTurn("t02-elapsed", "A week has passed since trial B.", quiet=True),
    )
    return tuple(
        map(
            build_corpus,
            (
                ReplayCorpus(
                    "f30-role-positive-v1",
                    "f30",
                    role_turns,
                    role_seed,
                    ("method-a", "method-b", "synthesis"),
                    resolution_boundary=4,
                    expert_origin=role_evidence,
                ),
                ReplayCorpus("f30-protocol-only-v1", "f30", protocol_turns, protocol_seed),
                ReplayCorpus("f30-already-promoted-v1", "f30", role_turns[:4], promoted_seed),
                ReplayCorpus(
                    "f31-current-pending-v1",
                    "f31",
                    state_turns,
                    pending_seed,
                    expect_transient=True,
                    resolution_boundary=2,
                    expert_origin=_page(_PENDING + _RESULT_A),
                ),
                ReplayCorpus("f31-historical-v1", "f31", historical_turns, historical_seed),
                ReplayCorpus("f31-different-trial-v1", "f31", different_turns, different_seed),
            ),
        )
    )


def corpus_for(subject: str | None) -> ReplayCorpus:
    for corpus in replay_corpora():
        if corpus.corpus_id == subject:
            return corpus
    raise ValueError(f"unknown role/state replay corpus: {subject!r}")


def seed_inputs(corpus: ReplayCorpus, root: Path) -> None:
    """Lay authored history and sources; the client owns subsequent changes."""
    from exomem.init import init_vault

    init_vault(root)
    for relative, body in corpus.seed_pages:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")


def fixture_payload(corpus: ReplayCorpus) -> dict[str, Any]:
    phases: list[dict[str, Any]] = []
    for arm, prominence in (("hookless", "maximal"), ("hooked", "balanced")):
        chunks = (
            (
                ("evidence", corpus.turns[: corpus.resolution_boundary]),
                ("settlement", corpus.turns[corpus.resolution_boundary :]),
            )
            if corpus.resolution_boundary is not None
            else (("control", corpus.turns),)
        )
        for stage, turns in chunks:
            phase = f"{arm}-{stage}"
            ops: list[dict[str, str]] = []
            if stage in {"evidence", "control"}:
                ops.append({"op": "configure", "ref": f"{arm}-{prominence}"})
            ops.append({"op": "snapshot", "ref": f"s-{phase}-seed"})
            ops.extend(
                {"op": "agent_turn", "ref": turn.turn_id, "detail": turn.text} for turn in turns
            )
            ops.append({"op": "snapshot", "ref": f"s-{phase}-end"})
            assertions = (
                ["role_signal_delivered_after_write", "role_state_settled_with_provenance"]
                if corpus.family_id == "f30"
                else [
                    "transient_signal_delivered_after_write",
                    "transient_state_settled_without_dismissal",
                ]
            )
            if stage == "evidence":
                assertions = assertions[:1]
            elif stage == "settlement":
                assertions = assertions[1:]
            phases.append(
                {
                    "phase_id": phase,
                    "ops": ops,
                    "expect": [
                        {"assert": name, "subject": corpus.corpus_id} for name in assertions
                    ],
                }
            )
    return {
        "scenario_id": corpus.corpus_id,
        "family_id": corpus.family_id,
        "kind": "operational",
        "public_coverage": "none",
        "phases": phases,
        "fairness": {
            "why_neutral": (
                "Ordinary work language tests delivered advice and represented state "
                "without naming a store."
            ),
            "public_coverage_subtraction": "No public suite covers these operational journeys.",
            "mechanisms": [
                {
                    "provider_role": "subject",
                    "mechanism": "Documented client carrier and canonical page projection",
                    "verdict": "satisfiable",
                    "evidence": "benchmarks/epistemic/PREREGISTRATION.md:1",
                }
            ],
            "privileged_endpoint_matrix": [
                {
                    "driver_surface_id": "state.read",
                    "provider": "exomem",
                    "variant": "native",
                    "disposition": "equivalent",
                    "audit_scope": "Read-only canonical pages and delivered client responses",
                    "evidence": "benchmarks/epistemic/PREREGISTRATION.md:1",
                    "reason": "Both surfaces are visible to the client.",
                    "competitor_surface": "Documented file and client response",
                }
            ],
            "acceptance_predicate": (
                "PREREGISTRATION.md section 4, "
                "sequence-five replay predicates."
            ),
        },
    }


__all__ = [
    "ORIGIN",
    "ReplayCorpus",
    "ReplayTurn",
    "StoreBearingUtterance",
    "build_corpus",
    "corpus_for",
    "fixture_payload",
    "replay_corpora",
    "seed_inputs",
]
