"""Native development replay and public-file projection for sequence five."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from membench.trackc.natural_prompt_driver import write_mcp_config
from membench.trackc.witness_join import parse_stream_json_transcript

from ..amendments import withheld_family_ids
from ..assertions import AssertionContext
from ..projectors.exomem_vault import VaultProjector
from ..registry import resolve
from ..snapshot import EpistemicStateSnapshot, StateItem
from . import f27_replay as client
from .role_state_replay import ReplayCorpus, fixture_payload, seed_inputs

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/sequence5"

WRITE_TOOLS = frozenset(
    {
        "remember",
        "edit_memory",
        "replace_memory",
        "compile_source",
        "restructure_memory",
        "observe_memory",
    }
)


def _tool_payload(content: Any) -> dict[str, Any] | None:
    if isinstance(content, list):
        content = "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return None
    return content if isinstance(content, dict) else None


def carrier_items_from_transcript(
    stream: str, *, turn_id: str, review_index: Mapping[str, Mapping[str, Any]]
) -> tuple[StateItem, ...]:
    """Project only successful write results actually returned on the client stream."""
    from membench.trackc.witness_join import parse_stream_json_transcript

    from . import f27_replay as client

    parsed = parse_stream_json_transcript(stream.splitlines())
    if parsed.malformed_lines or parsed.is_error or client.result_subtype(stream) != "success":
        return ()
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    items: list[StateItem] = []
    for line in stream.splitlines():
        payload = json.loads(line)
        blocks = payload.get("message", {}).get("content", []) if isinstance(payload, dict) else []
        if not isinstance(blocks, list):
            continue
        if payload.get("type") == "assistant":
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_use":
                    continue
                name, call_id = block.get("name"), block.get("id")
                if isinstance(name, str) and isinstance(call_id, str):
                    calls[call_id] = (
                        name,
                        block.get("input") if isinstance(block.get("input"), dict) else {},
                    )
        elif payload.get("type") == "user":
            for block in blocks:
                if not isinstance(block, dict) or block.get("type") != "tool_result":
                    continue
                call = calls.get(block.get("tool_use_id"))
                if call is None or block.get("is_error") is True:
                    continue
                tool_name, tool_input = call
                operation = tool_name.removeprefix("mcp__exomem__")
                if tool_name != f"mcp__exomem__{operation}" or operation not in WRITE_TOOLS:
                    continue
                result = _tool_payload(block.get("content"))
                if result is None or (
                    result.get("status") not in {"committed", "success", "completed", "updated"}
                    and result.get("success") is not True
                ):
                    continue
                due = result.get("due_state")
                if isinstance(due, dict) and isinstance(due.get("categories"), dict):
                    top = due.get("top")
                    if isinstance(top, list):
                        for category, count in due["categories"].items():
                            if not isinstance(count, int):
                                continue
                            refs = [
                                row["ref"]
                                for row in top
                                if isinstance(row, dict)
                                and row.get("category") == category
                                and isinstance(row.get("ref"), str)
                            ]
                            for path in sorted(
                                {
                                    str(review_index[ref]["path"])
                                    for ref in refs
                                    if ref in review_index
                                    and review_index[ref].get("category") == category
                                }
                            ):
                                matched = [
                                    ref
                                    for ref in refs
                                    if ref in review_index
                                    and review_index[ref].get("category") == category
                                    and review_index[ref].get("path") == path
                                ]
                                evidence = {
                                    ref: {
                                        "role": review_index[ref].get("role"),
                                        "evidence_refs": review_index[ref].get("evidence_refs"),
                                    }
                                    for ref in matched
                                }
                                items.append(
                                    StateItem(
                                        id=f"carrier-{turn_id}-{len(items)}",
                                        kind="container",
                                        raw={
                                            "surface": "client_carrier",
                                            "category": category,
                                            "targets": path,
                                            "after_write": "true",
                                            "count": str(count),
                                            "review_refs": json.dumps(matched),
                                            "review_evidence": json.dumps(evidence),
                                        },
                                    )
                                )
                if turn_id.endswith("consent"):
                    items.append(
                        StateItem(
                            id=f"action-{turn_id}-{len(items)}",
                            kind="container",
                            raw={
                                "surface": "client_action",
                                "authority": "consent",
                                "operation": operation,
                                "turn_id": turn_id,
                                "targets": str(tool_input.get("path") or ""),
                            },
                        )
                    )
    return tuple(items)


def project_replay_snapshot(
    vault: Path, *, phase: str, taken_at: str, runtime_surfaces: bool = False
) -> EpistemicStateSnapshot:
    """Add exact parsed public units to the existing canonical-file projection."""
    from exomem.semantic_contract import build_page_state

    snapshot = VaultProjector(vault, runtime_surfaces=runtime_surfaces).project(
        phase=phase, taken_at=taken_at
    )
    units: list[StateItem] = []
    for path in sorted((vault / "Knowledge Base/Notes").rglob("*.md")):
        if not path.is_file():
            continue
        relative = path.relative_to(vault).as_posix()
        state = build_page_state(vault, relative, path.read_text(encoding="utf-8"))
        for unit in state.document.units:
            if unit.unit_ref is None:
                continue
            targets = tuple(
                relation.target.removeprefix("[[").removesuffix("]]") for relation in unit.relations
            )
            units.append(
                StateItem(
                    id=unit.unit_ref,
                    kind="container",
                    title=unit.title,
                    text=unit.content,
                    current="yes" if state.status == "active" else "no",
                    cites=targets,
                    locator=f"{relative}#{unit.anchor or unit.fingerprint}",
                    locator_kind="file",
                    raw={
                        "unit_ref": unit.unit_ref,
                        "parent_path": relative,
                        "category": unit.category,
                        "kind": unit.kind,
                        "fingerprint": unit.fingerprint,
                        "context": unit.context or "",
                        "relations": json.dumps(
                            [{"kind": row.kind, "target": row.target} for row in unit.relations],
                            ensure_ascii=False,
                        ),
                    },
                )
            )
    return snapshot.model_copy(update={"items": (*snapshot.items, *units)})


@contextmanager
def _environment(env: Mapping[str, str]):
    previous = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(previous)


def _assertion_names(corpus: ReplayCorpus) -> tuple[str, str]:
    if corpus.family_id == "f30":
        return "role_signal_delivered_after_write", "role_state_settled_with_provenance"
    return "transient_signal_delivered_after_write", "transient_state_settled_without_dismissal"


def _served_review_index(vault: Path, taken_at: str) -> dict[str, dict[str, Any]]:
    from datetime import date

    from exomem import attention, due_state

    today = date.fromisoformat(taken_at[:10])
    served = {
        str(row.get("ref")): row
        for row in due_state.served_entries(vault, today=today)
        if row.get("category") in {"artifact_role_promotion", "transient_state_review"}
    }
    review = attention.attention(
        vault,
        categories=["artifact_role_promotion", "transient_state_review"],
        limit=0,
        state="all",
        today=today,
        record_surfacing=False,
    )
    indexed: dict[str, dict[str, Any]] = {}
    for item in review.items:
        if not item.ref or item.ref not in served or item.path != served[item.ref].get("path"):
            continue
        category = served[item.ref].get("category")
        reasons = [reason for reason in item.reasons if reason.get("category") == category]
        if len(reasons) != 1:
            continue
        meta = reasons[0].get("meta") or {}
        indexed[item.ref] = {
            "category": category,
            "path": item.path,
            "role": meta.get("role"),
            "evidence_refs": meta.get("evidence_refs"),
        }
    return indexed


def run_development(
    corpus: ReplayCorpus,
    *,
    out_dir: Path,
    taken_at: str,
    arm_ids: tuple[str, ...] = client.ARM_ORDER,
    envelope: client.AgentEnvelope | None = None,
    model: str = "sonnet",
    max_turns: int = client.DEFAULT_MAX_TURNS,
    dry_run: bool = False,
    parent_env: Mapping[str, str] | None = None,
    runner_factory=client._subprocess_runner_factory,
    prominence_writer=client._write_prominence,
) -> dict[str, Any]:
    """Attempt both native arms; pending families produce findings, never scores."""
    if unknown := set(arm_ids) - set(client.ARMS):
        raise ValueError(f"unknown arms: {sorted(unknown)}")
    fixture = fixture_payload(corpus)
    fixture_bytes = (FIXTURE_DIR / f"{corpus.corpus_id}.yaml").read_bytes()
    if yaml.safe_load(fixture_bytes) != fixture:
        raise ValueError(f"sequence-five fixture drift: {corpus.corpus_id}")
    report: dict[str, Any] = {
        "family_id": corpus.family_id,
        "corpus_id": corpus.corpus_id,
        "corpus_sha256": corpus.digest(),
        "fixture_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "model": model,
        "exomem_version": __import__("exomem").__version__,
        "amendment_sequence": 5,
        "amendment_acknowledged": corpus.family_id not in withheld_family_ids(),
        "claim_status": "development-only",
        "score": None,
        "taken_at": taken_at,
        "dry_run": dry_run,
        "arms": [],
    }
    try:
        envelope = envelope or client.discover_agent_envelope()
    except client.AgentEnvelopeNotDiscovered as error:
        report["cli_version"] = None
        reason = str(error)
        for arm_id in arm_ids:
            report["arms"].append(
                {
                    "arm": arm_id,
                    "harness_fault": True,
                    "reason": reason,
                    "turns_executed": 0,
                    "snapshots": {},
                    "score": None,
                    "assertions": {
                        name: {"outcome": "blocked", "evidence": reason}
                        for name in _assertion_names(corpus)
                    },
                }
            )
        return report
    report["cli_version"] = envelope.version
    try:
        safe_out = (
            Path(out_dir).expanduser().resolve()
            if dry_run
            else client.refuse_unsafe_out_dir(out_dir)
        )
    except client.JourneySetupError as error:
        for arm_id in arm_ids:
            report["arms"].append(
                {
                    "arm": arm_id,
                    "harness_fault": True,
                    "reason": str(error),
                    "turns_executed": 0,
                    "snapshots": {},
                    "score": None,
                    "assertions": {
                        name: {"outcome": "blocked", "evidence": str(error)}
                        for name in _assertion_names(corpus)
                    },
                }
            )
        return report
    if not dry_run and safe_out.exists():
        raise client.JourneySetupError("development output already exists; use a fresh directory")
    parent = dict(os.environ if parent_env is None else parent_env)
    floor, _removed = client.environment_floor(parent)
    for arm_id in arm_ids:
        arm = client.ARMS[arm_id]
        workdir = safe_out / arm_id
        vault = workdir / "vault"
        config = workdir / "mcp.json"
        env = client.arm_environment(floor, arm=arm, workdir=workdir, vault=vault)
        env.update(
            {
                "EXOMEM_STATE_ROOT": str(workdir / "state"),
                "XDG_STATE_HOME": str(workdir / "xdg-state"),
                "EXOMEM_WRITER_LEASE_STATE_DIR": str(workdir / "leases"),
                "KB_MCP_DISABLE_EMBEDDINGS": "1",
                "KB_MCP_DISABLE_MEDIA_EXTRACTION": "1",
                "EXOMEM_DISABLE_CLIP": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONPATH": os.pathsep.join(
                    str(client.REPO_ROOT / part) for part in ("src", "benchmarks")
                ),
            }
        )
        session_id = str(uuid.uuid4())
        block = workdir / "custom-instructions.txt" if arm.uses_custom_instructions else None
        argvs = [
            client.build_turn_argv(
                executable=envelope.executable,
                arm=arm,
                prompt=turn.text,
                mcp_config=config,
                session_id=session_id,
                model=model,
                max_turns=max_turns,
                first=index == 0,
                append_system_prompt_file=block,
                plugin_dir=client.PLUGIN_DIR if arm.uses_plugin else None,
            )
            for index, turn in enumerate(corpus.turns)
        ]
        row: dict[str, Any] = {
            "arm": arm_id,
            "prominence": arm.prominence,
            "session_id": session_id,
            "argv": argvs,
            "env_delta": client.environment_delta(parent, env),
            "turns_executed": 0,
            "harness_fault": False,
            "reason": None,
            "snapshots": {},
            "assertions": {},
            "score": None,
        }
        report["arms"].append(row)
        if dry_run:
            continue
        workdir.mkdir(parents=True)
        ctx = client.ArmContext(arm, workdir, vault, config, session_id, env, {})
        snapshots: dict[str, EpistemicStateSnapshot] = {}
        witnesses: list[StateItem] = []
        turns = {turn.turn_id: argv for turn, argv in zip(corpus.turns, argvs, strict=True)}
        try:
            with _environment(env):
                seed_inputs(corpus, vault)
            write_mcp_config(config, vault=vault, workdir=workdir, python_executable=sys.executable)
            payload = json.loads(config.read_text())
            payload["mcpServers"]["exomem"]["env"].update(
                {
                    key: env[key]
                    for key in (
                        "EXOMEM_STATE_ROOT",
                        "EXOMEM_VAULT_PATH",
                        "XDG_STATE_HOME",
                        "EXOMEM_WRITER_LEASE_STATE_DIR",
                    )
                }
            )
            config.write_text(json.dumps(payload, indent=2) + "\n")
            if block is not None:
                block.write_text(client.custom_instructions_block(arm.prominence)[0])
            prominence_writer(arm, env)
            run = runner_factory(ctx)
            for phase in fixture["phases"]:
                if not phase["phase_id"].startswith(arm_id):
                    continue
                for op in phase["ops"]:
                    if op["op"] == "snapshot":
                        with _environment(env):
                            observed = project_replay_snapshot(
                                vault,
                                phase=phase["phase_id"],
                                taken_at=taken_at,
                                runtime_surfaces=True,
                            )
                        snapshots[op["ref"]] = observed.model_copy(
                            update={"items": (*observed.items, *witnesses)}
                        )
                    elif op["op"] == "agent_turn":
                        proc = run(turns[op["ref"]])
                        (workdir / f"turn-{op['ref']}.jsonl").write_text(proc.stdout or "")
                        transcript = parse_stream_json_transcript((proc.stdout or "").splitlines())
                        reason = client.fault_reason(proc, transcript)
                        if reason is None and client.result_subtype(proc.stdout or "") != "success":
                            reason = "the client stream has no successful terminal result"
                        if reason is not None:
                            raise client.JourneySetupError(f"turn {op['ref']}: {reason}")
                        with _environment(env):
                            review_index = _served_review_index(vault, taken_at)
                        witnesses.extend(
                            carrier_items_from_transcript(
                                proc.stdout or "",
                                turn_id=op["ref"],
                                review_index=review_index,
                            )
                        )
                        row["turns_executed"] += 1
                names = [expectation["assert"] for expectation in phase["expect"]]
                seed_ref = next(op["ref"] for op in phase["ops"] if op["op"] == "snapshot")
                final_ref = next(
                    op["ref"] for op in reversed(phase["ops"]) if op["op"] == "snapshot"
                )
                assertion_ctx = AssertionContext(
                    snapshots[final_ref],
                    prior=snapshots[seed_ref],
                    subject=corpus.corpus_id,
                )
                row["assertions"].update(
                    {name: resolve(name)(assertion_ctx).model_dump() for name in names}
                )
        except (client.JourneySetupError, OSError, ValueError, subprocess.SubprocessError) as error:
            row.update(harness_fault=True, reason=str(error))
            row["assertions"] = {
                name: {"outcome": "blocked", "evidence": str(error)}
                for name in _assertion_names(corpus)
            }
        else:
            for ref, snapshot in snapshots.items():
                path = workdir / f"{ref}.json"
                path.write_text(snapshot.model_dump_json(indent=2))
                row["snapshots"][ref] = str(path)
    if not dry_run:
        safe_out.mkdir(parents=True, exist_ok=True)
        (safe_out / "development.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


__all__ = ["carrier_items_from_transcript", "project_replay_snapshot", "run_development"]
