"""File handshake: the RUNNER NEVER HOLDS MODEL CREDENTIALS.

The runner (or a backend) serializes blinded request batches under the run
directory; an external executor — a human, an orchestrating subagent
session, or a CLI backend — executes them and writes response batches back.
Pairing is by ``(request_id, sample_index)``. Unmatched or malformed lines
are appended to ``failures.jsonl`` and KEPT in every denominator — a lost
sample is a recorded failure, never a silently shrunk denominator.

Writing is fail-closed: every serialized request line is leakage-scanned,
and one leaky request aborts the whole batch before anything touches disk.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError

from membench.judge.blinding import leakage_scan, shuffled
from membench.schema import StrictModel

KINDS = ("answer", "judge")
BATCH_NAME = "batch-001.jsonl"
HANDOFF_NAME = "HANDOFF.md"


class HandshakeRequest(StrictModel):
    """One line of ``<kind>-requests/batch-*.jsonl`` (already blinded)."""

    request_id: str
    sample_index: int = Field(ge=0)
    blinded_provider_token: str
    payload: dict[str, Any]


class HandshakeResponse(StrictModel):
    """One line of ``<kind>-responses/batch-*.jsonl`` written by the executor."""

    request_id: str
    sample_index: int = Field(ge=0)
    model_id: str
    response: str
    meta: dict[str, Any] | None = None


@dataclass(frozen=True)
class RequestItem:
    """One logical item to execute; expanded to N sampled requests."""

    item_id: str
    blinded_provider_token: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class PairedResponse:
    request: HandshakeRequest
    response: HandshakeResponse


class LeakageError(ValueError):
    """A request line still contains provider-identifying tokens."""

    def __init__(self, leaks: dict[str, list[str]]) -> None:
        self.leaks = leaks
        details = "; ".join(
            f"{request_id}: {tokens}" for request_id, tokens in sorted(leaks.items())
        )
        super().__init__(f"refusing to write leaky request(s) — {details}")


def requests_dir(run_dir: Path, kind: str) -> Path:
    _check_kind(kind)
    return Path(run_dir) / f"{kind}-requests"


def responses_dir(run_dir: Path, kind: str) -> Path:
    _check_kind(kind)
    return Path(run_dir) / f"{kind}-responses"


def _check_kind(kind: str) -> None:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}, got {kind!r}")


def append_failure(run_dir: Path, record: dict[str, Any]) -> None:
    """Append one record to the run's ``failures.jsonl`` (runner format)."""

    path = Path(run_dir) / "failures.jsonl"
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def write_requests(
    run_dir: Path,
    kind: str,
    items: list[RequestItem] | tuple[RequestItem, ...],
    *,
    samples: int = 1,
    seed: str = "membench",
) -> Path:
    """Serialize blinded requests to ``<kind>-requests/batch-001.jsonl``.

    Each item expands to ``samples`` requests sharing its ``request_id``
    (``sample_index`` 0..N-1); per-sample identity is preserved so N-sample
    judge variance survives end to end. Line order is a deterministic
    seed-derived permutation (order randomization without ``random``).

    Fail-closed leakage gate: every serialized line is scanned BEFORE any
    directory or file is created; one leaky request raises
    :class:`LeakageError` and nothing is written. Batches are immutable —
    an existing batch file is never overwritten.
    """

    _check_kind(kind)
    if samples < 1:
        raise ValueError("samples must be >= 1")
    requests = [
        HandshakeRequest(
            request_id=item.item_id,
            sample_index=sample_index,
            blinded_provider_token=item.blinded_provider_token,
            payload=item.payload,
        )
        for item in items
        for sample_index in range(samples)
    ]
    requests = shuffled(requests, f"{seed}:{kind}:order")

    lines: list[str] = []
    leaks: dict[str, list[str]] = {}
    for request in requests:
        line = json.dumps(request.model_dump(), ensure_ascii=False, sort_keys=True)
        found = leakage_scan(line)
        if found:
            merged = leaks.setdefault(request.request_id, [])
            merged.extend(token for token in found if token not in merged)
        lines.append(line)
    if leaks:
        raise LeakageError(leaks)

    directory = requests_dir(run_dir, kind)
    directory.mkdir(parents=True, exist_ok=True)
    batch = directory / BATCH_NAME
    if batch.exists():
        raise FileExistsError(f"{batch} already exists; request batches are immutable")
    batch.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    (directory / HANDOFF_NAME).write_text(_handoff_text(kind), encoding="utf-8")
    return batch


def load_requests(run_dir: Path, kind: str) -> list[HandshakeRequest]:
    """All request lines for ``kind`` in on-disk (shuffled) order."""

    directory = requests_dir(run_dir, kind)
    if not directory.is_dir():
        return []
    requests: list[HandshakeRequest] = []
    for batch in sorted(directory.glob("batch-*.jsonl")):
        for raw in batch.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                requests.append(HandshakeRequest.model_validate_json(raw))
    return requests


def collect_responses(
    run_dir: Path, kind: str
) -> tuple[list[PairedResponse], dict[str, int]]:
    """Pair responses to requests; failures are recorded, never dropped.

    Malformed response lines, responses without a matching request,
    duplicates, and requests with no response are each appended to
    ``failures.jsonl`` and counted in ``stats`` — they all stay in the
    denominators downstream. Returns ``(paired, stats)`` with ``paired``
    sorted by ``(request_id, sample_index)`` for determinism.
    """

    req_dir = requests_dir(run_dir, kind)
    if not req_dir.is_dir():
        raise FileNotFoundError(f"no {kind}-requests directory under {run_dir}")
    requests = {
        (request.request_id, request.sample_index): request
        for request in load_requests(run_dir, kind)
    }
    stats = {
        "requests": len(requests),
        "response_lines": 0,
        "paired": 0,
        "malformed": 0,
        "duplicate": 0,
        "unmatched_response": 0,
        "missing_response": 0,
    }
    paired: list[PairedResponse] = []
    seen: set[tuple[str, int]] = set()
    resp_dir = responses_dir(run_dir, kind)
    if resp_dir.is_dir():
        for batch in sorted(resp_dir.glob("batch-*.jsonl")):
            for raw in batch.read_text(encoding="utf-8").splitlines():
                if not raw.strip():
                    continue
                stats["response_lines"] += 1
                try:
                    response = HandshakeResponse.model_validate_json(raw)
                except ValidationError as exc:
                    stats["malformed"] += 1
                    append_failure(
                        run_dir,
                        {
                            "phase": f"{kind}-collect",
                            "detail": f"malformed response line (kept in denominators): {exc.error_count()} validation error(s)",
                            "raw_prefix": raw[:200],
                        },
                    )
                    continue
                key = (response.request_id, response.sample_index)
                if key not in requests:
                    stats["unmatched_response"] += 1
                    append_failure(
                        run_dir,
                        {
                            "phase": f"{kind}-collect",
                            "request_id": response.request_id,
                            "sample_index": response.sample_index,
                            "detail": "response without matching request",
                        },
                    )
                    continue
                if key in seen:
                    stats["duplicate"] += 1
                    append_failure(
                        run_dir,
                        {
                            "phase": f"{kind}-collect",
                            "request_id": response.request_id,
                            "sample_index": response.sample_index,
                            "detail": "duplicate response ignored (first one kept)",
                        },
                    )
                    continue
                seen.add(key)
                paired.append(PairedResponse(request=requests[key], response=response))
                stats["paired"] += 1
    for key in sorted(requests):
        if key not in seen:
            stats["missing_response"] += 1
            append_failure(
                run_dir,
                {
                    "phase": f"{kind}-collect",
                    "request_id": key[0],
                    "sample_index": key[1],
                    "detail": "missing response (kept in denominators)",
                },
            )
    paired.sort(key=lambda pair: (pair.request.request_id, pair.request.sample_index))
    return paired, stats


def _handoff_text(kind: str) -> str:
    judge_extra = ""
    if kind == "judge":
        judge_extra = (
            "\n"
            "Judge requests demand STRICT JSON model output — one object, no prose:\n"
            "\n"
            '    {"semantic_match": true|false, "explanation_quality": 1-5, "reason": "short reason"}\n'
            "\n"
            "Judge output is advisory only: deterministic gates are final and a\n"
            "judge disagreement is rendered as a conflict annotation, never an\n"
            "override.\n"
        )
    return (
        f"# {kind} handshake — instructions for the external executor\n"
        "\n"
        "The benchmark runner never holds model credentials. It serialized\n"
        "blinded requests here; an external executor (a human, an orchestrating\n"
        "agent session, or a CLI backend) runs them and writes responses back.\n"
        "\n"
        f"1. Read `{kind}-requests/{BATCH_NAME}`. Each line is one request:\n"
        "\n"
        '       {"request_id": "...", "sample_index": 0, "blinded_provider_token": "system-A", "payload": {...}}\n'
        "\n"
        "   Send `payload.prompt` to the model verbatim. Execute each\n"
        "   (request_id, sample_index) pair independently — samples exist to\n"
        "   measure model variance and must not share context.\n"
        "\n"
        f"2. Append one line per completed request to `{kind}-responses/{BATCH_NAME}`\n"
        "   (create the directory next to the requests directory):\n"
        "\n"
        '       {"request_id": "...", "sample_index": 0, "model_id": "<model used>", "response": "<raw model output>"}\n'
        "\n"
        "   `response` is the model's RAW text output — do not repair,\n"
        "   summarize, or fill in missing output. A request you could not run\n"
        "   gets no response line; it is recorded as a failure and stays in\n"
        "   every denominator. Do not add provider-identifying strings.\n"
        + judge_extra
    )
