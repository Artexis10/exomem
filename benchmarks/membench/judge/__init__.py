"""Judge layer: optional, desk-side, default OFF, and blind by construction.

Policy (docs/memory-proof-benchmark.md): deterministic gates are FINAL.
Judge output is advisory — a disagreement renders as a ``gate_conflict``
annotation in reports and never overrides a deterministic verdict. Judge
requests are blinded (provider identities and provenance shapes replaced by
neutral tokens), order-randomized deterministically, and expanded to N
samples whose per-sample values are preserved end to end.
"""

from __future__ import annotations

from membench.judge.backends import (
    JUDGE_PROMPT_TEMPLATE,
    AnswerBackend,
    BackendRequestResult,
    ClaudeCliBackend,
    JudgeBackend,
    JudgeVerdict,
    NoneBackend,
    OpenAICompatBackend,
    PhaseOutcome,
    SubagentBackend,
    build_judge_prompt,
    default_backend,
    make_judge_item,
    parse_judge_verdict,
)
from membench.judge.blinding import (
    BlindingMap,
    SourceNumbering,
    deterministic_permutation,
    leakage_scan,
    normalize_for_judge,
    shuffled,
    structural_leakage_scan,
)
from membench.judge.handshake import (
    HandshakeRequest,
    HandshakeResponse,
    LeakageError,
    PairedResponse,
    RequestItem,
    append_failure,
    collect_responses,
    load_requests,
    write_requests,
)

__all__ = [
    "JUDGE_PROMPT_TEMPLATE",
    "AnswerBackend",
    "BackendRequestResult",
    "BlindingMap",
    "ClaudeCliBackend",
    "HandshakeRequest",
    "HandshakeResponse",
    "JudgeBackend",
    "JudgeVerdict",
    "LeakageError",
    "NoneBackend",
    "OpenAICompatBackend",
    "PairedResponse",
    "PhaseOutcome",
    "RequestItem",
    "SourceNumbering",
    "SubagentBackend",
    "append_failure",
    "build_judge_prompt",
    "collect_responses",
    "default_backend",
    "deterministic_permutation",
    "leakage_scan",
    "load_requests",
    "make_judge_item",
    "normalize_for_judge",
    "parse_judge_verdict",
    "shuffled",
    "structural_leakage_scan",
    "write_requests",
]
