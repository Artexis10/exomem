"""Development-only f28/f29 runs through the installed client, isolated per arm."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import yaml
from membench.trackc.natural_prompt_driver import write_mcp_config
from membench.trackc.witness_join import parse_stream_json_transcript

from ..amendments import withheld_family_ids
from ..projectors.exomem_vault import VaultProjector
from . import f27_replay as client
from .collection_replay import ReplayCorpus, fixture_payload


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


def seed_inputs(corpus: ReplayCorpus, root: Path) -> None:
    """Lay historical inputs, never the observed records the replay is testing."""
    from exomem import records
    from exomem.init import init_vault

    init_vault(root)
    if corpus.family_id == "f28":
        events = corpus.expected_records()
        groups = [[row] for row in events] if corpus.expect_candidate else [events]
        for index, rows in enumerate(groups, 1):
            relative = f"Knowledge Base/Notes/Research/studio/expense-{index}.md"
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            first = rows[0]
            frontmatter = {
                "type": "insight",
                "exomem_id": str(uuid.uuid5(uuid.NAMESPACE_URL, relative)),
                "title": f"Studio expense {index}",
                "created": first["effective_on"],
                "updated": rows[-1]["effective_on"],
                "status": "active",
                "tags": [corpus.domain],
                "sources": [],
            }
            units = [
                f"- [{row['status']}] {row['identity']} licence {row['status']} on {row['effective_on']} for EUR {row['amount']} "
                f"#{corpus.domain} #{row['identity'].casefold()} #studio ^event"
                for row in rows
            ]
            if len(units) > 1:
                units = [
                    line.removesuffix("^event") + f"^event-{i}" for i, line in enumerate(units, 1)
                ]
            path.write_text(
                "---\n"
                + yaml.safe_dump(frontmatter, sort_keys=False)
                + "---\n\n## Observations\n\n"
                + "\n".join(units)
                + "\n"
            )
        return

    fields = {
        name: {"type": "string", "required": True}
        for name in ("post_key", "platform", "account", "exact_text", "text_sha256")
    }
    fields["published_on"] = {"type": "date", "required": True}
    fields["sources"] = {"type": "array", "items": {"type": "link"}, "required": True}
    manifest = {
        "type": "collection",
        "exomem_id": corpus.seeded_collection,
        "title": "Studio publications",
        "semantic_profile": "records",
        "collection_version": 1,
        "schema_version": 1,
        "lifecycle": "active",
        "claims": {"terms": ["bulletin", "studio"]},
        "storage": {"strategy": "markdown-items", "source": "Items", "format_version": 1},
        "item_schema": {"natural_key": list(corpus.natural_key), "fields": fields},
    }
    result = records.create_collection(
        root,
        "Knowledge Base/Records/Studio publications/_collection.md",
        "---\n" + yaml.safe_dump(manifest, sort_keys=False) + "---\n\nPublished studio updates.\n",
        why="seed the pre-existing observed-state contract",
    )
    if result.get("error") or result.get("status") in {"failed", "refused", "not_committed"}:
        raise client.JourneySetupError(
            f"could not seed claiming collection: {result.get('error') or result.get('status')}"
        )


def run_development(
    corpus: ReplayCorpus,
    *,
    out_dir: Path,
    taken_at: str,
    arm_ids: Sequence[str] = client.ARM_ORDER,
    envelope: client.AgentEnvelope | None = None,
    model: str = "sonnet",
    max_turns: int = client.DEFAULT_MAX_TURNS,
    dry_run: bool = False,
    parent_env: Mapping[str, str] | None = None,
    runner_factory=client._subprocess_runner_factory,
    prominence_writer=client._write_prominence,
    seed=seed_inputs,
    projector_factory=None,
) -> dict[str, Any]:
    """Record execution and snapshots without bypassing comparative-run gates."""
    out_dir = client.refuse_unsafe_out_dir(out_dir)
    envelope = envelope or client.discover_agent_envelope()
    unknown = set(arm_ids) - set(client.ARMS)
    if unknown:
        raise client.JourneySetupError(f"unknown arms: {sorted(unknown)}")
    if not dry_run and out_dir.exists():
        raise client.JourneySetupError("development output already exists; use a fresh directory")
    floor, _removed = client.environment_floor(
        dict(os.environ if parent_env is None else parent_env)
    )
    report: dict[str, Any] = {
        "family_id": corpus.family_id,
        "corpus_id": corpus.corpus_id,
        "corpus_sha256": corpus.digest(),
        "taken_at": taken_at,
        "model": model,
        "cli_version": envelope.version,
        "claim_status": "development-only",
        "amendment_sequence": 4,
        "amendment_acknowledged": corpus.family_id not in withheld_family_ids(),
        "dry_run": dry_run,
        "arms": [],
    }
    for arm_id in arm_ids:
        arm = client.ARMS[arm_id]
        workdir = out_dir / arm_id
        vault = workdir / "vault"
        config = workdir / "mcp.json"
        env = client.arm_environment(floor, arm=arm, workdir=workdir, vault=vault)
        env.update(
            {
                "EXOMEM_STATE_ROOT": str(workdir / "state"),
                "XDG_STATE_HOME": str(workdir / "xdg-state"),
                "KB_MCP_DISABLE_MEDIA_EXTRACTION": "1",
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
                prompt=turn.client_input(),
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
        # Only benchmark-owned pins are recorded; inherited credential values never are.
        pins = {
            key: env[key] for key in ("EXOMEM_STATE_ROOT", "EXOMEM_VAULT_PATH", "XDG_STATE_HOME")
        }
        row: dict[str, Any] = {
            "arm": arm_id,
            "session_id": session_id,
            "env": pins,
            "argv": argvs,
            "harness_fault": False,
            "reason": None,
            "snapshots": {},
            "turns_executed": 0,
        }
        report["arms"].append(row)
        if dry_run:
            continue
        workdir.mkdir(parents=True)
        ctx = client.ArmContext(arm, workdir, vault, config, session_id, env, {})
        try:
            with _environment(env):
                seed(corpus, vault)
            write_mcp_config(config, vault=vault, workdir=workdir, python_executable=sys.executable)
            # The server inherits these pins too; the config must not restore a shared root.
            payload = json.loads(config.read_text())
            payload["mcpServers"]["exomem"]["env"].update(pins)
            config.write_text(json.dumps(payload, indent=2) + "\n")
            if block is not None:
                block.write_text(client.custom_instructions_block(arm.prominence)[0])
            prominence_writer(arm, env)
            run = runner_factory(ctx)
            projector = (
                projector_factory(vault)
                if projector_factory
                else VaultProjector(vault, runtime_surfaces=True)
            )
            turns = {turn.turn_id: argv for turn, argv in zip(corpus.turns, argvs, strict=True)}
            for phase in fixture_payload(corpus)["phases"]:
                if not phase["phase_id"].startswith(arm_id):
                    continue
                for op in phase["ops"]:
                    if op["op"] == "snapshot":
                        with _environment(env):
                            snapshot = projector.project(phase=phase["phase_id"], taken_at=taken_at)
                        target = workdir / f"{op['ref']}.json"
                        target.write_text(snapshot.model_dump_json(indent=2))
                        row["snapshots"][op["ref"]] = str(target)
                    elif op["op"] == "agent_turn":
                        proc = run(turns[op["ref"]])
                        (workdir / f"turn-{op['ref']}.jsonl").write_text(proc.stdout or "")
                        transcript = parse_stream_json_transcript((proc.stdout or "").splitlines())
                        reason = client.fault_reason(proc, transcript)
                        if reason is None and client.result_subtype(proc.stdout or "") != "success":
                            reason = "the client stream has no successful terminal result"
                        if reason is not None:
                            raise client.JourneySetupError(f"turn {op['ref']}: {reason}")
                        row["turns_executed"] += 1
        except (client.JourneySetupError, OSError, ValueError, subprocess.SubprocessError) as error:
            row.update(harness_fault=True, reason=str(error), outcome="blocked")
        else:
            row["outcome"] = "observed"
    if not dry_run:
        (out_dir / "development.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


def main(corpus_factory, argv: Sequence[str] | None = None) -> int:
    from datetime import UTC, datetime

    parser = argparse.ArgumentParser(
        description="Run a sequence-four development journey; produces no comparative score."
    )
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--arm", choices=(*client.ARM_ORDER, "both"), default="both")
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--taken-at", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    report = run_development(
        corpus_factory(),
        out_dir=args.out,
        taken_at=args.taken_at or datetime.now(UTC).isoformat(),
        arm_ids=client.ARM_ORDER if args.arm == "both" else (args.arm,),
        model=args.model,
        dry_run=args.dry_run,
    )
    print(json.dumps(report, indent=2))
    return int(any(row["harness_fault"] for row in report["arms"]))
