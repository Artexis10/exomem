"""Natural-prompt driver: fresh ``claude -p`` sessions against an isolated
exomem stdio server.

Per query the driver (1) writes an ``--mcp-config`` JSON pointing at an
isolated exomem stdio server (fresh vault + the deterministic lexical
profile), (2) builds the exact fresh-session invocation::

    claude -p "<prompt>" --mcp-config <cfg> --strict-mcp-config \
        --output-format stream-json --verbose

with prompt-prefix variants for the two activation modes (``natural`` = the
query verbatim; ``explicit`` = a memory-check instruction prepended), and
(3) parses the stream-json transcript into an :class:`AnswerRecord` (final
text, sentinel citations, token/latency accounting) plus the witness inputs
:mod:`membench.trackc.witness_join` consumes.

EXECUTION IS USER-RUN: the default runner shells out to the real ``claude``
binary, which needs network and credentials this sandbox does not have. Tests
(and offline replays) inject ``runner=``; nothing is ever fabricated when the
binary is absent — the subprocess error propagates to the caller. A run that
DID execute but failed (non-zero exit: auth failure, rate limit, rejected
mcp-config; or an error-subtype transcript) is a HARNESS fault: the
:class:`CaseExecution` carries ``harness_fault=True`` with the reason and NO
``answer`` — an empty transcript can never flow into an AnswerRecord for the
gates to score as a product failure.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from membench.adapters.exomem_local import lexical_profile
from membench.scoring.answer_contract import AnswerRecord, extract_structure
from membench.trackc.witness_join import Transcript, parse_stream_json_transcript

MODES = ("natural", "explicit")
EXPLICIT_PREFIX = (
    "Before answering, check the connected memory server for stored knowledge "
    "relevant to this question.\n\n"
)
DEFAULT_SERVER_NAME = "exomem"
RUN_TIMEOUT_SECONDS = 300.0


class _ProcLike(Protocol):
    returncode: int
    stdout: str
    stderr: str


Runner = Callable[[list[str]], _ProcLike]


def _subprocess_runner(argv: list[str]) -> _ProcLike:
    import subprocess

    return subprocess.run(  # noqa: S603 - benchmark-owned argv; user-run path
        argv, capture_output=True, text=True, timeout=RUN_TIMEOUT_SECONDS
    )


def write_mcp_config(
    path: Path,
    *,
    vault: Path,
    workdir: Path,
    python_executable: str | None = None,
    server_name: str = DEFAULT_SERVER_NAME,
) -> Path:
    """Write an isolated exomem stdio server ``--mcp-config`` file.

    The server env pins the deterministic lexical profile and points every
    state root (vault, config, leases, logs) under benchmark-owned dirs — a
    real vault can never be touched (``EXOMEM_VAULT_PATH`` is mandatory with
    no fallback).
    """

    path = Path(path)
    workdir = Path(workdir)
    env = {
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_CONFIG_PATH": str(workdir / "exomem-config.json"),
        "EXOMEM_WRITER_LEASE_STATE_DIR": str(workdir / "leases"),
        "EXOMEM_LOG_DIR": str(workdir / "logs"),
        **lexical_profile().settings,
    }
    payload = {
        "mcpServers": {
            server_name: {
                "command": python_executable or sys.executable,
                "args": ["-m", "exomem", "--transport", "stdio"],
                "env": env,
            }
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def build_argv(
    prompt: str,
    *,
    mcp_config: Path,
    mode: str = "natural",
    claude_executable: str = "claude",
    model: str | None = None,
) -> list[str]:
    """The exact fresh-session invocation (never ``--continue``/``--resume``)."""

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    full_prompt = prompt if mode == "natural" else EXPLICIT_PREFIX + prompt
    argv = [
        claude_executable,
        "-p",
        full_prompt,
        "--mcp-config",
        str(mcp_config),
        "--strict-mcp-config",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if model:
        argv.extend(["--model", model])
    return argv


@dataclass(frozen=True)
class CaseExecution:
    """One executed prompt case: witness-join inputs plus, when the execution
    was healthy, the scoring answer.

    ``harness_fault=True`` means the execution itself failed (non-zero exit
    or an error-subtype transcript); ``answer`` is then ``None`` so the case
    is structurally unscorable — invalid-case semantics, never a contender
    loss (the same contract as :class:`AdapterEnvironmentError` runs).
    """

    transcript: Transcript
    argv: tuple[str, ...]
    returncode: int
    stderr_tail: str = ""
    answer: AnswerRecord | None = None
    harness_fault: bool = False
    harness_fault_reason: str | None = None

    @property
    def tool_uses(self) -> list[tuple[str, dict]]:
        return [(t.name, t.input) for t in self.transcript.tool_uses]


def transcript_to_answer(
    transcript: Transcript, *, query_id: str, provider_token: str | None = None
) -> AnswerRecord:
    """Fold a parsed transcript into the scoring answer envelope.

    The extractor only ADDS structure (sentinel citations found in the final
    text); token/latency accounting rides in ``raw`` and ``latency_ms``.
    """

    raw: dict = {
        "usage": transcript.usage,
        "total_cost_usd": transcript.total_cost_usd,
        "num_turns": transcript.num_turns,
        "session_id": transcript.session_id,
        "tool_use_names": [t.name for t in transcript.tool_uses],
        "is_error": transcript.is_error,
    }
    record = AnswerRecord(
        query_id=query_id,
        provider_token=provider_token,
        answer_text=transcript.result_text,
        latency_ms=transcript.duration_ms,
        raw=raw,
    )
    return extract_structure(record)


def run_case(
    prompt: str,
    *,
    query_id: str,
    mcp_config: Path,
    mode: str = "natural",
    runner: Runner | None = None,
    claude_executable: str = "claude",
    model: str | None = None,
    server_name: str = DEFAULT_SERVER_NAME,
) -> CaseExecution:
    """Execute one fresh-session prompt case and parse its transcript.

    A non-zero exit or an error-subtype transcript marks the case a harness
    fault: no :class:`AnswerRecord` is produced, so the failure can never be
    silently scored by the gates as a product loss.
    """

    argv = build_argv(
        prompt,
        mcp_config=mcp_config,
        mode=mode,
        claude_executable=claude_executable,
        model=model,
    )
    proc = (runner or _subprocess_runner)(argv)
    transcript = parse_stream_json_transcript(proc.stdout.splitlines())
    stderr_tail = (proc.stderr or "")[-800:]

    fault_reason: str | None = None
    if proc.returncode != 0:
        fault_reason = (
            f"claude exited with exit code {proc.returncode}"
            + (f": {stderr_tail.strip()}" if stderr_tail.strip() else "")
        )
    elif transcript.is_error:
        fault_reason = "transcript reports an error result (non-success subtype)"

    if fault_reason is not None:
        return CaseExecution(
            transcript=transcript,
            argv=tuple(argv),
            returncode=proc.returncode,
            stderr_tail=stderr_tail,
            answer=None,  # structurally unscorable — harness fault, not a loss
            harness_fault=True,
            harness_fault_reason=fault_reason,
        )

    answer = transcript_to_answer(transcript, query_id=query_id)
    return CaseExecution(
        transcript=transcript,
        argv=tuple(argv),
        returncode=proc.returncode,
        stderr_tail=stderr_tail,
        answer=answer,
    )


__all__ = [
    "EXPLICIT_PREFIX",
    "MODES",
    "CaseExecution",
    "build_argv",
    "run_case",
    "transcript_to_answer",
    "write_mcp_config",
]
