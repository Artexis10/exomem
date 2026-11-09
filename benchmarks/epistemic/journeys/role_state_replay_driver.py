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

from ..amendments import withheld_family_ids
from ..assertions import AssertionContext
from ..projectors.exomem_vault import VaultProjector
from ..registry import resolve
from ..snapshot import EpistemicStateSnapshot, StateItem
from . import f27_replay as client
from .role_state_replay import ReplayCorpus, fixture_payload, seed_inputs

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures/sequence5"

#: What a replay turn cannot witness any more. The assertions that read a client carrier after a write
#: need a `due_state` block on the write reply, and only the bootstrap payload serves one now.
CARRIER_UNSUPPORTED = (
    "the post-write client carrier cannot be measured: a write reply no longer carries `due_state`, "
    "which only the bootstrap payload serves, so no client stream shows a carrier after a write"
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
        snapshots: dict[str, EpistemicStateSnapshot] = {}
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
                        snapshots[op["ref"]] = observed
                    elif op["op"] == "agent_turn":
                        # Refuse before the paid turn: no stream could witness the carrier.
                        raise client.JourneySetupError(f"turn {op['ref']}: {CARRIER_UNSUPPORTED}")
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


__all__ = ["project_replay_snapshot", "run_development"]
