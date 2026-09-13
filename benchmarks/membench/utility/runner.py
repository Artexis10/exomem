"""Fresh-actor, paired-arm runner for the epistemic-utility action instrument.

Extends `membench`; not a separate runner framework. Reuses the existing
native-agent worker (:mod:`lme.native_agent`), native cell
(:mod:`lme.native_cell`), metered backend and its ``BudgetLedger``
(:mod:`lme.metered`, :mod:`protocol.budget`) rather than inventing parallel
machinery.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from lme.native_agent import AgentLimits, RunEnvelope
from membench.utility.action_world import ActionWorld, grade_snapshot
from membench.utility.actor import MEMORY_TOOLS, UtilityBroker, run_phase
from membench.utility.schema import (
    PHASES,
    VARIANTS,
    ActionOutcome,
    AttemptStatus,
    UsageRecord,
)
from membench.utility.scenarios import actor_view, generate_episode
from membench.utility.scoring import aggregate_usage, score_variant
from protocol.budget import BudgetLedger

#: One registered operational family; comparative execution must be gated on
#: its release before it can back a score or claim (see design.md §Protocol).
UTILITY_FAMILY_ID = "f32"

#: Bumped whenever a manifest field's meaning changes.
MANIFEST_VERSION = 1
#: Bumped whenever generated episodes change, so cached runs stop comparing.
SCENARIO_GENERATOR_VERSION = "utility_action_episode.v2"

#: 8 model calls/phase * 3 phases * 2 arms sharing one pair's model budget.
CALLS_PER_PAIR = 48
MAX_INPUT_TOKENS = 48_000
MAX_OUTPUT_TOKENS = 2_000
#: Pinned endpoint price ceiling (see design.md §Cost and execution schedule).
INPUT_RATE_CEILING_PER_MILLION = 0.15
OUTPUT_RATE_CEILING_PER_MILLION = 0.50
DEFAULT_CAP_USD = 2.0
MAX_CAP_USD = 2.0
DEFAULT_PHASE_SECONDS = 180.0
MAX_PHASE_SECONDS = 180.0
PROFILES: tuple[str, ...] = ("semantic", "fixture")
#: Markdown links inside the shipped skill: the documented reference set.
_DOCUMENTED_LINK = re.compile(r"\]\((?!https?:|#)([^)\s]+)\)")
_CREDENTIAL_ENVS = ("OPENAI_API_KEY", "OPENROUTER_API_KEY")
_EPSILON = 1e-9


class UtilityRunnerError(ValueError):
    """Refused before any actor or spend: bad config, cap, or gate."""


class UtilityReportError(ValueError):
    """A saved run cannot support the claim being read out of it."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _next_seq(ledger: BudgetLedger) -> int:
    return len(ledger._entries())


def pair_reservation_usd(*, input_rate: float, output_rate: float) -> float:
    """The conservative per-pair model reservation at a given price.

    Raises if the endpoint is priced above the pinned ceiling: a more
    expensive endpoint must fail preflight, never silently raise the cap.
    """

    if input_rate > INPUT_RATE_CEILING_PER_MILLION or output_rate > OUTPUT_RATE_CEILING_PER_MILLION:
        raise UtilityRunnerError(
            f"endpoint price ({input_rate}/{output_rate} per million) exceeds the pinned "
            f"reservation ceiling ({INPUT_RATE_CEILING_PER_MILLION}/{OUTPUT_RATE_CEILING_PER_MILLION})"
        )
    return CALLS_PER_PAIR * (MAX_INPUT_TOKENS * input_rate + MAX_OUTPUT_TOKENS * output_rate) / 1_000_000


def _agent_limits(phase_seconds: float) -> AgentLimits:
    return AgentLimits(
        phase_model_calls=8,
        phase_tool_calls=64,
        run_model_calls=24,
        run_tool_calls=192,
        max_output_tokens=MAX_OUTPUT_TOKENS,
        max_context_tokens=MAX_INPUT_TOKENS,
        phase_seconds=phase_seconds,
        run_seconds=3 * phase_seconds,
    )


def _arm_order(seed: int, variant_index: int) -> tuple[str, str]:
    return ("control", "memory") if (seed + variant_index) % 2 == 0 else ("memory", "control")


def _load_guidance(product_root: Path) -> dict[str, str]:
    """Frozen shipped skill/reference text, read once per run.

    Only the memory arm's :class:`~membench.utility.actor.UtilityBroker`
    ever exposes this through ``available``/``tool``/messages; loading it
    unconditionally here is not itself an actor-observable difference.
    """

    schema_dir = Path(product_root) / "src" / "exomem" / "_scaffold" / "_Schema"
    skill_path = schema_dir / "SKILL.md"
    if not skill_path.is_file():
        raise UtilityRunnerError(f"product scaffold SKILL.md is missing: {skill_path}")
    skill = skill_path.read_text(encoding="utf-8")
    guidance = {"SKILL.md": skill}
    # Every reference the shipped skill actually documents, so the arm reads
    # what the product ships rather than whatever happens to match "*.md".
    for relative in sorted({match.group(1) for match in _DOCUMENTED_LINK.finditer(skill)}):
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise UtilityRunnerError(f"scaffold documents a path outside itself: {relative}")
        path = schema_dir / relative
        if not path.is_file():
            raise UtilityRunnerError(f"documented scaffold reference is missing: {relative}")
        guidance[relative] = path.read_text(encoding="utf-8")
    # References also link to one another using inline-code paths. Freeze
    # the shipped reference directory, including those indirect references.
    for path in sorted((schema_dir / "references").glob("*.md")):
        if path.is_symlink():
            raise UtilityRunnerError("scaffold references must be regular local files")
        guidance[path.relative_to(schema_dir).as_posix()] = path.read_text(encoding="utf-8")
    return guidance


def _child_totals(ledger: BudgetLedger) -> tuple[float, float, bool]:
    """``(committed, held, stopped)`` for one pair's own child ledger.

    ``committed`` sums actual charges already recorded; ``held`` is whatever
    remains reserved but not yet released (0 once every call settles
    normally, positive after an uncertain/STOP failure).
    """

    entries = ledger._entries()
    committed = sum(entry.units for entry in entries if entry.kind == "commit")
    # The ledger's running total is reservations minus releases, so a settled
    # call's charge is still inside it until its remainder is released. What
    # is *held* is only the part no commit has accounted for yet; adding the
    # two without this subtraction charges every settled call twice.
    held = max(0.0, ledger._running_total() - committed)
    if held < _EPSILON:
        held = 0.0
    stopped = ledger.stop_path.exists()
    return committed, held, stopped


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(value, sort_keys=True, allow_nan=False) + "\n")


def _outcome_to_dict(outcome: ActionOutcome) -> dict[str, Any]:
    return {
        "episode_id": outcome.episode_id,
        "variant": outcome.variant,
        "arm": outcome.arm,
        "status": outcome.status.value,
        "success": outcome.success,
        "destructive_effects": list(outcome.destructive_effects),
        "failure_reason": outcome.failure_reason,
    }


def _usage_from_phase_result(
    episode_id: str, variant: str, arm: str, phase: str, result: dict[str, Any]
) -> UsageRecord:
    """One phase's observed usage. Absent measurements stay ``None``."""

    return UsageRecord(
        episode_id=episode_id,
        variant=variant,
        arm=arm,
        phase=phase,
        input_tokens=None if result.get("unknown_usage_calls") else result.get("input_tokens"),
        output_tokens=None if result.get("unknown_usage_calls") else result.get("output_tokens"),
        turns=result.get("model_calls"),
        wall_time_s=result.get("elapsed_seconds"),
        cost_usd=None if result.get("unknown_usage_calls") else result.get("cost_usd"),
        # Reported by the provider when it reports them at all; reasoning is
        # already inside output_tokens and is never added to it again.
        cache_read_tokens=result.get("cache_read_tokens"),
        cache_write_tokens=result.get("cache_write_tokens"),
        reasoning_tokens=result.get("reasoning_tokens"),
        model_time_s=result.get("model_seconds"),
        tool_time_s=result.get("tool_seconds"),
        peak_context_tokens=result.get("peak_context_tokens"),
        cumulative_context_tokens=result.get("cumulative_context_tokens"),
    )


class _NullCell:
    """Placeholder for the control arm: never called, no product access."""

    schemas: dict[str, Any] = {}

    async def call(self, name: str, arguments: dict) -> dict:  # pragma: no cover - defensive
        raise RuntimeError("control arm must never call a product cell")

    def snapshot(self) -> dict:
        return {"stored_bytes": 0, "files": {}}


def _default_cell_factory(
    root: Path, *, product_root: Path, python: Path, profile: str,
    model_cache: Path | None, clip_model_cache: Path | None,
):
    from lme.native_cell import NativeCell

    return NativeCell(
        root, python=python, product_root=product_root, profile=profile,
        model_cache=model_cache, clip_model_cache=clip_model_cache,
    )


def _default_identity_provider(product_root: Path):
    from protocol.contracts import derive_preregistration_identity

    return derive_preregistration_identity(product_root)


def _default_family_gate(identity: Any, family_ids: list[str]) -> None:
    from protocol.contracts import require_amended_families_released

    require_amended_families_released(identity, family_ids)


def _default_identity_validator(identity: Any, product_root: Path) -> None:
    from protocol.contracts import PreregistrationIdentity, validate_preregistration_identity

    if not isinstance(identity, PreregistrationIdentity):
        raise UtilityReportError("manifest pre-registration identity is malformed")
    validate_preregistration_identity(identity, repo_root=product_root)


def _default_report_family_gate(identity: Any, family_ids: list[str]) -> None:
    _default_family_gate(identity, family_ids)


def _default_fixture_gate(family_id: str, product_root: Path) -> None:
    from epistemic.amendments import require_family_released

    require_family_released(family_id, repo_root=product_root)


# --------------------------------------------------------------------------
# Preflight: everything checkable is checked before an actor or a dollar
# --------------------------------------------------------------------------


def _positive_finite(value: object, *, maximum: float) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(value)
        and 0 < value <= maximum
    )


def _validate_request(
    *, output: Path, seed: object, paid: object, approval_token: object, cap_usd: object,
    phase_seconds: object, profile: object, variants: object, product_root: Path,
    python: Path, tokenizer_path: Path,
) -> None:
    if output.exists():
        raise UtilityRunnerError(f"output already exists: {output}")
    if paid is not True:
        raise UtilityRunnerError("paid execution must be explicitly requested (paid=True)")
    if not isinstance(approval_token, str) or not approval_token.strip():
        raise UtilityRunnerError("approval_token must be nonblank for paid execution")
    marker = approval_token.strip()
    if marker.startswith(("sk-", "sk_")) or any(
        marker == os.environ.get(name, "\0") for name in _CREDENTIAL_ENVS
    ):
        raise UtilityRunnerError("approval_token is an operator marker, never an API key")
    if not _positive_finite(cap_usd, maximum=MAX_CAP_USD):
        raise UtilityRunnerError(f"cap_usd must be a finite number in (0, {MAX_CAP_USD}]")
    if not _positive_finite(phase_seconds, maximum=MAX_PHASE_SECONDS):
        raise UtilityRunnerError(
            f"phase_seconds must be a finite number in (0, {MAX_PHASE_SECONDS}]"
        )
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise UtilityRunnerError("seed must be a plain integer")
    if profile not in PROFILES:
        raise UtilityRunnerError(f"profile must be one of {PROFILES}")
    if (
        not isinstance(variants, (tuple, list))
        or not variants
        or any(v not in VARIANTS for v in variants)
        or len(set(variants)) != len(variants)
    ):
        raise UtilityRunnerError(f"variants must be a unique non-empty subset of {VARIANTS}")
    for label, path in (
        ("product_root", product_root), ("python", python), ("tokenizer_path", tokenizer_path)
    ):
        if not Path(path).exists():
            raise UtilityRunnerError(f"{label} does not exist: {path}")


# --------------------------------------------------------------------------
# Evaluator-only manifest
# --------------------------------------------------------------------------


def _digest(salt: str, *parts: str) -> str:
    payload = "\x00".join((salt, *parts)).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _file_digests(root: Path) -> dict[str, str]:
    digests: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise UtilityReportError("artifact tree contains a symlink")
        if path.is_file():
            with path.open("rb") as stream:
                digests[str(path.relative_to(root))] = hashlib.file_digest(stream, "sha256").hexdigest()
    return digests


def _product_revision(product_root: Path) -> dict[str, Any]:
    """Product commit and tree cleanliness through the protocol's Git reader."""

    from protocol.contracts import _run_git

    try:
        commit = _run_git(Path(product_root), "rev-parse", "HEAD").stdout.decode().strip()
        status = _run_git(Path(product_root), "status", "--porcelain").stdout.decode()
    except Exception:  # noqa: BLE001 - a non-repository product root is a fact
        return {"commit": None, "tree_clean": None}
    return {"commit": commit or None, "tree_clean": not status.strip()}


def _episode_hashes(seed: int, variants: tuple[str, ...], salt: str) -> tuple[dict, dict]:
    phase_hashes, oracle_hashes = {}, {}
    for variant in variants:
        episode = generate_episode(seed=seed, variant=variant)
        phase_hashes[variant] = _digest(salt, *(view.narrative for view in episode.phases))
        oracle_hashes[variant] = _digest(salt, repr(episode.oracle))
    return phase_hashes, oracle_hashes


def _build_manifest(
    *, seed: int, variants: tuple[str, ...], product_root: Path, guidance: dict[str, str],
    profile: str, cap_usd: float, phase_seconds: float, reservation: float,
    endpoint_profile: Any, identity: Any, limits: AgentLimits, salt: str,
    synthetic_reasons: list[str], tokenizer_path: Path,
) -> dict[str, Any]:
    """The frozen, evaluator-only description of what this run will be.

    Never handed to an actor: it contains the seed, the target hashes and the
    salt that makes those hashes unrecoverable without it.
    """

    phase_hashes, oracle_hashes = _episode_hashes(seed, variants, salt)
    schema_dir = Path(product_root) / "src" / "exomem" / "_scaffold" / "_Schema"
    identity_payload = (
        identity.model_dump(mode="json") if hasattr(identity, "model_dump") else str(identity)
    )
    return {
        "manifest_version": MANIFEST_VERSION,
        "hash_salt": salt,
        "synthetic": bool(synthetic_reasons),
        "synthetic_reasons": sorted(synthetic_reasons),
        "scenario": {
            "generator_version": SCENARIO_GENERATOR_VERSION,
            "seed": seed,
            "variants": list(variants),
            "phase_hashes": phase_hashes,
            "oracle_hashes": oracle_hashes,
        },
        "product": {
            "root": str(Path(product_root).resolve()),
            **_product_revision(Path(product_root)),
            "scaffold_hashes": _file_digests(schema_dir) if schema_dir.is_dir() else {},
            "guidance_hashes": {
                name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                for name, text in sorted(guidance.items())
            },
            "cell_profile": profile,
            "semantic_shipped_default": profile == "semantic",
        },
        "actor": {
            "client": "membench native API actor; not a Claude Code or Codex workflow",
            "tool_renderer": "MCP JSON envelope; exact duplicate text removed; raw envelope retained in trace",
            "model": endpoint_profile.model,
            "wire_model": endpoint_profile.wire_model("openrouter"),
            "provider": endpoint_profile.provider_name,
            "provider_slug": endpoint_profile.provider_slug,
            "effort": endpoint_profile.reasoning_effort,
            "fallback": False,
            "judge": None,
            "tokenizer": {
                "name": endpoint_profile.tokenizer,
                "sha256": endpoint_profile.tokenizer_sha256,
                "source": endpoint_profile.tokenizer_source,
                "path": str(tokenizer_path),
            },
            "pricing": {
                "input_per_million_usd": endpoint_profile.input_rate,
                "output_per_million_usd": endpoint_profile.output_rate,
                "cached_input_per_million_usd": endpoint_profile.cached_rate,
                "cache_write_per_million_usd": endpoint_profile.cache_write_rate,
                "source": endpoint_profile.pricing_source,
                "verified_on": endpoint_profile.pricing_verified_on,
                "ceiling_per_million_usd": {
                    "input": INPUT_RATE_CEILING_PER_MILLION,
                    "output": OUTPUT_RATE_CEILING_PER_MILLION,
                },
            },
            "parameters": {
                "max_output_tokens": MAX_OUTPUT_TOKENS,
                "max_input_tokens": MAX_INPUT_TOKENS,
                "samples": 1,
                "sampling": "provider default",
            },
        },
        "tool_schemas": {
            "world": ActionWorld(
                generate_episode(seed=seed, variant=variants[0]), arm="evaluator"
            ).tool_schemas(),
            "memory_tool_names": sorted(MEMORY_TOOLS),
            "harness_tool_names": ["discover_tools", "read_file"],
        },
        "limits": {
            **asdict(limits),
            "cap_usd": float(cap_usd),
            "pair_reservation_usd": reservation,
            "phase_seconds": float(phase_seconds),
            "calls_per_pair": CALLS_PER_PAIR,
        },
        "clock": {
            "started_at": _now(),
            "timezone": "UTC",
            "source": "system clock, hermetic phase deadlines",
        },
        "policy": {
            "memory_attentiveness": "product_default_as_shipped",
            "context_policy": "product_default_as_shipped",
            "ordinary_persistence": "workspace_notes_shared_by_both_arms",
            "arm_order": {
                variant: list(_arm_order(seed, index)) for index, variant in enumerate(variants)
            },
            "retry_policy": "none",
            "experiment_mode": "whole_system_utility",
        },
        "protocol": {
            "family_id": UTILITY_FAMILY_ID,
            "identity": identity_payload,
            "release_gate": "require_amended_families_released before any claim",
        },
    }


# --------------------------------------------------------------------------
# One pair
# --------------------------------------------------------------------------


@dataclass
class _ArmRun:
    outcome: ActionOutcome
    usage: list[UsageRecord] = field(default_factory=list)


@dataclass
class _PairResult:
    variant: str
    outcomes: dict[str, ActionOutcome]
    usage: list[UsageRecord]
    halted: bool


def _missing(episode, variant: str, arm: str, reason: str) -> ActionOutcome:
    return ActionOutcome(
        episode_id=episode.episode_id, variant=variant, arm=arm,
        status=AttemptStatus.MISSING, success=None, failure_reason=reason,
    )


def _invalid(episode, variant: str, arm: str, reason: str, damage=()) -> ActionOutcome:
    return ActionOutcome(
        episode_id=episode.episode_id, variant=variant, arm=arm,
        status=AttemptStatus.INVALID, success=None, failure_reason=reason,
        destructive_effects=tuple(damage),
    )


def _grade_execution(episode, arm: str, snapshot: dict, execution: dict,
                     phases: list[dict]) -> ActionOutcome:
    outcome = grade_snapshot(snapshot, episode.oracle, episode.episode_id, episode.variant, arm)
    infrastructure = execution.get("infrastructure")
    if infrastructure:
        return _invalid(episode, episode.variant, arm, infrastructure, outcome.destructive_effects)
    failures = [p for p in phases if p.get("outcome_class") != "completed"]
    if failures:
        first = failures[0]
        if first.get("outcome_class") in {"declared_exhaustion", "actor_output"}:
            return replace(outcome, success=False, failure_reason=first["outcome_class"])
        return _invalid(episode, episode.variant, arm, "unclassified_phase_failure", outcome.destructive_effects)
    if len(phases) != len(PHASES):
        return _invalid(episode, episode.variant, arm, "incomplete_phase_evidence", outcome.destructive_effects)
    return outcome


def _registered_assertions(episode, snapshot: dict) -> dict[str, str]:
    """Run the registered epistemic diagnostics against the same observed state.

    These diagnose artifacts; an otherwise correct artifact cannot override
    the episode's separate execution-budget failure.
    """
    from epistemic.assertions import AssertionContext, utility_action_state_valid, utility_no_prohibited_effects
    from epistemic.snapshot import EpistemicStateSnapshot

    state = EpistemicStateSnapshot(
        provider="utility-action-world", variant=episode.variant, phase="action",
        taken_at=_now(), items=(), relations=(), declarations=(),
        projector={"name": "utility", "version": "1", "author": "membench",
                   "endpoints_used": (), "loc": 0})
    context = AssertionContext(snapshot=state, utility_world_snapshot=snapshot, utility_oracle=episode.oracle)
    return {check.__name__: check(context).outcome
            for check in (utility_action_state_valid, utility_no_prohibited_effects)}


def _session_dir(output: Path, salt: str, episode_id: str, arm: str, phase: str) -> Path:
    """An opaque worker directory: its path names no variant, arm or phase."""

    sessions = output / "sessions"
    sessions.mkdir(mode=0o700, parents=True, exist_ok=True)
    return sessions / _digest(salt, episode_id, arm, phase)[:16]


async def _run_arm(
    *, arm: str, episode, variant: str, arm_dir: Path, output: Path, salt: str,
    product_root: Path, python: Path, profile: str, phase_seconds: float,
    model_cache: Path | None, clip_model_cache: Path | None, guidance: dict[str, str],
    backend: Any, cell_factory: Callable[..., Any],
) -> tuple[_ArmRun, bool]:
    """Run one arm's three fresh sessions; never raise into its partner."""

    arm_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    world = ActionWorld(episode, arm=arm)
    usage: list[UsageRecord] = []
    cell_cm = None
    cell: Any = _NullCell()
    infrastructure: str | None = None
    phase_results: list[dict] = []
    halted = False
    try:
        if arm == "memory":
            cell_cm = cell_factory(
                arm_dir / "cell", product_root=product_root, python=python, profile=profile,
                model_cache=model_cache, clip_model_cache=clip_model_cache,
            )
            cell = await cell_cm.__aenter__()
            readiness = await cell.readiness() if hasattr(cell, "readiness") else None
            _write_json(arm_dir / "cell.json", {
                "profile": profile,
                "semantic_shipped_default": profile == "semantic",
                "readiness": readiness,
                "runtime_receipt": getattr(cell, "runtime_receipt", None),
                "memory_snapshot": cell.snapshot(),
                "tool_schemas": cell.schemas,
            })
            if profile == "semantic" and (readiness or {}).get("status") != "ready":
                # The shipped semantic treatment was requested but the cell
                # cannot serve it: that is a broken instrument, not a loss.
                infrastructure = "cell_not_ready"
        if infrastructure is None:
            envelope = RunEnvelope(_agent_limits(phase_seconds))
            broker = UtilityBroker(
                world=world, arm=arm, cell=cell, backend=backend, envelope=envelope,
                guidance=guidance,
            )
            for phase_index, phase_name in enumerate(PHASES):
                world.advance_phase(phase_index)
                view = actor_view(episode, phase_index)
                result = await run_phase(
                    broker, view=view,
                    out=_session_dir(output, salt, episode.episode_id, arm, phase_name),
                )
                _write_json(arm_dir / f"{phase_name}-result.json", result)
                phase_results.append(result)
                _write_json(arm_dir / f"{phase_name}-world.json", world.snapshot())
                if arm == "memory":
                    _write_json(arm_dir / f"{phase_name}-memory.json", cell.snapshot())
                usage.append(
                    _usage_from_phase_result(episode.episode_id, variant, arm, phase_name, result)
                )
                if result.get("outcome_class") == "infrastructure":
                    infrastructure = str(result.get("reason") or "infrastructure_fault")
                    break
                if result.get("outcome_class") in {"declared_exhaustion", "actor_output"}:
                    break
                if backend.ledger.stop_path.exists():
                    halted = True
                    break
    except Exception as exc:  # noqa: BLE001 - the partner arm must still run
        infrastructure = type(exc).__name__
    finally:
        if cell_cm is not None:
            try:
                await cell_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - teardown never hides a result
                pass
        _write_json(arm_dir / "final-world.json", world.snapshot())
    execution = {"infrastructure": infrastructure, "phase_count": len(phase_results)}
    _write_json(arm_dir / "execution.json", execution)
    _write_json(arm_dir / "assertions.json", _registered_assertions(episode, world.snapshot()))
    outcome = _grade_execution(episode, arm, world.snapshot(), execution, phase_results)
    return _ArmRun(outcome, usage), halted or backend.ledger.stop_path.exists()


async def _run_pair(
    *, variant: str, variant_index: int, seed: int, pair_dir: Path, output: Path, salt: str,
    product_root: Path, python: Path, profile: str, phase_seconds: float,
    model_cache: Path | None, clip_model_cache: Path | None,
    guidance: dict[str, str], backend: Any, cell_factory: Callable[..., Any],
) -> _PairResult:
    episode = generate_episode(seed=seed, variant=variant)
    order = _arm_order(seed, variant_index)
    outcomes: dict[str, ActionOutcome] = {}
    usage: list[UsageRecord] = []
    halted = False
    for arm in order:
        if halted:
            # Never attempted: recorded as missing coverage, never as a loss.
            outcomes[arm] = _missing(episode, variant, arm, "halted_before_attempt")
            continue
        arm_run, halted = await _run_arm(
            arm=arm, episode=episode, variant=variant, arm_dir=pair_dir / arm, output=output,
            salt=salt, product_root=product_root, python=python, profile=profile,
            phase_seconds=phase_seconds, model_cache=model_cache,
            clip_model_cache=clip_model_cache, guidance=guidance, backend=backend,
            cell_factory=cell_factory,
        )
        outcomes[arm] = arm_run.outcome
        usage.extend(arm_run.usage)
    return _PairResult(variant=variant, outcomes=outcomes, usage=usage, halted=halted)


def _settle_pair(
    parent_ledger: BudgetLedger, backend: Any, *, reservation: float, variant: str
) -> tuple[float, float, bool]:
    """Move one child's real liability onto the parent, exactly once."""

    committed, held, stopped = _child_totals(backend.ledger)
    parent_ledger.commit(
        ts=_now(), seq=_next_seq(parent_ledger), actor="utility-runner",
        op=f"pair-{variant}-settle", units=committed,
    )
    release_amount = reservation - committed - held
    if release_amount > _EPSILON:
        parent_ledger.release(
            ts=_now(), seq=_next_seq(parent_ledger), actor="utility-runner",
            op=f"pair-{variant}-settle", units=release_amount,
        )
    if committed + held > reservation + _EPSILON:
        # Known plus held liability escaped the envelope this pair reserved.
        parent_ledger.stop_path.write_text(
            f"pair {variant} liability {committed + held} exceeds its reservation {reservation}\n",
            encoding="utf-8",
        )
        stopped = True
    return committed, held, stopped


async def run_utility(
    output: Path, seed: int, *,
    product_root: Path, python: Path, tokenizer_path: Path,
    model_cache: Path | None = None, clip_model_cache: Path | None = None,
    profile: str = "semantic", cap_usd: float = DEFAULT_CAP_USD, paid: bool = False,
    approval_token: str = "", phase_seconds: float = DEFAULT_PHASE_SECONDS,
    variants: tuple[str, ...] = VARIANTS,
    identity_provider: Callable[[Path], Any] | None = None,
    family_gate: Callable[[Any, list[str]], None] | None = None,
    cell_factory: Callable[..., Any] | None = None,
    backend_factory: Callable[[Path, float, str], Any] | None = None,
) -> dict:
    """Run the paired control/memory smoke across ``variants`` for one seed.

    Refuses before any actor is created or any dollar reserved if ``paid`` is
    not explicitly set, any setting is outside its frozen envelope, ``output``
    already exists, the protocol amendment family is not released, or the
    whole three-pair reservation does not fit under ``cap_usd``.
    """

    output = Path(output)
    _validate_request(
        output=output, seed=seed, paid=paid, approval_token=approval_token, cap_usd=cap_usd,
        phase_seconds=phase_seconds, profile=profile, variants=variants,
        product_root=product_root, python=python, tokenizer_path=tokenizer_path,
    )
    variants = tuple(variants)

    synthetic_reasons: list[str] = []
    if identity_provider is not None:
        synthetic_reasons.append("injected_identity_provider")
    if family_gate is not None:
        synthetic_reasons.append("injected_family_gate")
    if cell_factory is not None:
        synthetic_reasons.append("injected_cell_factory")
    if backend_factory is not None:
        synthetic_reasons.append("injected_backend_factory")
    if profile != "semantic":
        synthetic_reasons.append("lexical_fixture_profile_not_the_shipped_default")
    if set(variants) != set(VARIANTS):
        synthetic_reasons.append("variant_subset_injection")

    identity_provider = identity_provider or _default_identity_provider
    family_gate = family_gate or _default_family_gate
    cell_factory = cell_factory or _default_cell_factory

    identity = identity_provider(product_root)
    family_gate(identity, [UTILITY_FAMILY_ID])
    if not synthetic_reasons:
        revision = _product_revision(product_root)
        if not revision["commit"] or revision["tree_clean"] is not True:
            raise UtilityRunnerError("paid utility execution requires a clean committed product tree")

    from lme.metered_profiles import GLM_MODEL, model_profile

    endpoint_profile = model_profile(GLM_MODEL, "openrouter")
    reservation = pair_reservation_usd(
        input_rate=endpoint_profile.input_rate, output_rate=endpoint_profile.output_rate
    )
    # The protocol schedules all three pairs; a subset is a test injection and
    # still has to fit the schedule the cap was approved for.
    total_reservation = reservation * len(VARIANTS)
    if total_reservation > cap_usd:
        raise UtilityRunnerError(
            f"all {len(VARIANTS)} pair reservations (${total_reservation:.4f}) do not fit "
            f"under cap_usd=${cap_usd}"
        )

    if backend_factory is None:
        def backend_factory(root: Path, pair_cap: float, token: str):
            from lme.metered import MeteredOpenAIBackend

            return MeteredOpenAIBackend(
                root, cap_usd=pair_cap, approval_token=token, model=GLM_MODEL,
                transport="openrouter", tokenizer_path=tokenizer_path,
                input_token_limit=MAX_INPUT_TOKENS,
                # A declared task-budget loss is scored, not a financial stop.
                stop_on_actor_failure=False,
            )

    output.mkdir(mode=0o700, parents=True)
    guidance = _load_guidance(product_root)
    salt = secrets.token_hex(16)
    manifest = _build_manifest(
        seed=seed, variants=variants, product_root=Path(product_root), guidance=guidance,
        profile=profile, cap_usd=cap_usd, phase_seconds=phase_seconds, reservation=reservation,
        endpoint_profile=endpoint_profile, identity=identity, limits=_agent_limits(phase_seconds),
        salt=salt, synthetic_reasons=synthetic_reasons, tokenizer_path=Path(tokenizer_path),
    )
    _write_json(output / "manifest.json", manifest)

    parent_ledger = BudgetLedger(output / "ledger", caps={"usd": float(cap_usd)})
    parent_ledger.approve(
        ts=_now(), seq=_next_seq(parent_ledger), actor="utility-runner", op="pilot-approval"
    )

    pair_records: dict[str, list[tuple[ActionOutcome, ActionOutcome]]] = {}
    usage_records: list[UsageRecord] = []
    attempted_variants: list[str] = []
    spend = {"committed_usd": 0.0, "held_usd": 0.0}
    halted = False
    started = time.monotonic()

    try:
        for variant_index, variant in enumerate(variants):
            if halted:
                break
            parent_ledger.reserve(
                ts=_now(), seq=_next_seq(parent_ledger), actor="utility-runner",
                op=f"pair-{variant}", units=reservation,
            )
            pair_dir = output / "pairs" / variant
            try:
                backend = backend_factory(pair_dir / "backend", reservation, approval_token)
            except Exception as exc:
                # Constructors perform no model calls. Keep the unattempted
                # pair visible and retire its unused escrow before stopping.
                parent_ledger.release(ts=_now(), seq=_next_seq(parent_ledger),
                                      actor="utility-runner", op=f"pair-{variant}-construction", units=reservation)
                _write_json(output / "preflight-failure.json", {"variant": variant, "error_type": type(exc).__name__})
                halted = True
                break
            attempted_variants.append(variant)
            pair_result: _PairResult | None = None
            try:
                pair_result = await _run_pair(
                    variant=variant, variant_index=variant_index, seed=seed, pair_dir=pair_dir,
                    output=output, salt=salt, product_root=product_root, python=python,
                    profile=profile, phase_seconds=phase_seconds, model_cache=model_cache,
                    clip_model_cache=clip_model_cache, guidance=guidance, backend=backend,
                    cell_factory=cell_factory,
                )
            finally:
                # Settle whatever this pair really spent, even if it failed.
                committed, held, stopped = _settle_pair(
                    parent_ledger, backend, reservation=reservation, variant=variant
                )
                spend["committed_usd"] += committed
                spend["held_usd"] += held
                _write_json(pair_dir / "spend.json", {
                    "committed_usd": committed, "held_usd": held,
                    "reservation_usd": reservation, "stopped": stopped,
                })
                if pair_result is not None:
                    usage_records.extend(pair_result.usage)
                    episode = generate_episode(seed=seed, variant=variant)
                    outcomes = {
                        arm: pair_result.outcomes.get(arm)
                        or _missing(episode, variant, arm, "never_attempted")
                        for arm in ("control", "memory")
                    }
                    _write_json(pair_dir / "outcomes.json", {
                        arm: _outcome_to_dict(outcome) for arm, outcome in outcomes.items()
                    })
                    # An attempted arm is kept even when its partner is missing.
                    pair_records[variant] = [(outcomes["control"], outcomes["memory"])]
                    halted = halted or pair_result.halted
                halted = halted or stopped
    finally:
        report = _build_report(
            seed=seed, variants=variants, attempted_variants=attempted_variants, halted=halted,
            cap_usd=float(cap_usd), reservation=reservation, pair_records=pair_records,
            usage_records=usage_records, spend=spend, manifest=manifest,
            wall_seconds=time.monotonic() - started,
        )
        _write_json(output / "report.json", report)
        _write_digests(output)
    return report


def _build_report(
    *, seed: int, variants: tuple[str, ...], attempted_variants: list[str], halted: bool,
    cap_usd: float, reservation: float,
    pair_records: dict[str, list[tuple[ActionOutcome, ActionOutcome]]],
    usage_records: list[UsageRecord], spend: dict[str, float], manifest: dict[str, Any],
    wall_seconds: float,
) -> dict[str, Any]:
    scores = {
        variant: score_variant(variant, pair_records.get(variant, []), scheduled=1)
        for variant in variants
    }
    phase_usage = {
        phase: _usage_to_dict(
            aggregate_usage([record for record in usage_records if record.phase == phase])
        )
        for phase in PHASES
    }
    measured = [
        (record.model_time_s or 0.0, record.tool_time_s or 0.0, record.wall_time_s or 0.0)
        for record in usage_records
    ]
    model_seconds = sum(item[0] for item in measured)
    tool_seconds = sum(item[1] for item in measured)
    phase_wall = sum(item[2] for item in measured)
    return {
        "seed": seed,
        "variants": list(variants),
        "attempted_variants": attempted_variants,
        "halted": halted,
        "synthetic": manifest["synthetic"],
        "synthetic_reasons": manifest["synthetic_reasons"],
        "manifest_version": manifest["manifest_version"],
        "cap_usd": cap_usd,
        "pair_reservation_usd": reservation,
        "spend": {
            "committed_usd": spend["committed_usd"],
            "held_usd": spend["held_usd"],
            "cap_usd": cap_usd,
            "pair_reservation_usd": reservation,
        },
        "scores": {
            variant: {
                "pair": _pair_outcome_to_dict(pair_outcome),
                "arms": {
                    arm: _arm_outcome_to_dict(arm_outcome)
                    for arm, arm_outcome in arm_outcomes.items()
                },
            }
            for variant, (pair_outcome, arm_outcomes) in scores.items()
        },
        "usage": _usage_to_dict(aggregate_usage(usage_records)),
        "usage_by_phase": phase_usage,
        "usage_by_variant_arm_phase": {
            variant: {
                arm: {
                    phase: _usage_to_dict(aggregate_usage([
                        r for r in usage_records if (r.variant, r.arm, r.phase) == (variant, arm, phase)
                    ])) for phase in PHASES
                } for arm in ("control", "memory")
            } for variant in variants
        },
        "overhead": {
            # Reported apart from one aggregate: model and tool time are the
            # work, the rest is harness startup and runtime overhead.
            "phase_orchestration_seconds": sum(
                record.wall_time_s - (record.model_time_s or 0.0) - (record.tool_time_s or 0.0)
                for record in usage_records
                if record.wall_time_s is not None
            ) if usage_records else None,
            "outside_phase_seconds": wall_seconds - phase_wall,
            "model_seconds": model_seconds,
            "tool_seconds": tool_seconds,
            "wall_seconds": wall_seconds,
        },
    }


def _write_digests(output: Path) -> None:
    """Digest every artifact, so later tampering has to be self-consistent."""

    digests = {
        name: value for name, value in _file_digests(output).items() if name != "digests.json"
    }
    _write_json(output / "digests.json", {"version": 1, "files": digests})


# --------------------------------------------------------------------------
# Comparative reader
# --------------------------------------------------------------------------


def load_report(
    path: Path | str, product_root: Path | str, *, allow_synthetic: bool = False,
    identity_validator: Callable[[Any, Path], None] | None = None,
    family_gate: Callable[[Any, list[str]], None] | None = None,
    fixture_gate: Callable[[str, Path], None] | None = None,
) -> dict[str, Any]:
    """Read a saved run for comparison, refusing anything it cannot support.

    The protocol gates run first, then artifact digests, then an independent
    regrade from the observed world snapshots and a freshly generated private
    oracle. Saved success flags and score rows are never trusted: they are
    compared against that regrade and a disagreement refuses the run.
    """

    run_dir, product_root = Path(path), Path(product_root)
    identity_validator = identity_validator or _default_identity_validator
    family_gate = family_gate or _default_report_family_gate
    fixture_gate = fixture_gate or _default_fixture_gate

    manifest = _read_json(run_dir / "manifest.json", "manifest")
    report = _read_json(run_dir / "report.json", "report")

    identity = _identity_from_manifest(manifest)
    identity_validator(identity, product_root)
    family_gate(identity, [UTILITY_FAMILY_ID])
    fixture_gate(UTILITY_FAMILY_ID, product_root)

    if manifest.get("synthetic") and not allow_synthetic:
        raise UtilityReportError(
            "run artifacts are labelled synthetic: "
            f"{', '.join(manifest.get('synthetic_reasons', []))}"
        )
    _verify_digests(run_dir)
    _verify_compatibility(manifest, product_root)
    recomputed = _recompute(run_dir, manifest)
    _compare_saved_scores(report, recomputed)
    return {
        "manifest": manifest,
        "report": report,
        "recomputed": recomputed,
        "synthetic": bool(manifest.get("synthetic")),
    }


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UtilityReportError(f"{label} is unreadable: {path}") from exc
    if not isinstance(value, dict):
        raise UtilityReportError(f"{label} is not an object: {path}")
    return value


def _identity_from_manifest(manifest: dict[str, Any]) -> Any:
    payload = (manifest.get("protocol") or {}).get("identity")
    if isinstance(payload, dict):
        from protocol.contracts import PreregistrationIdentity

        try:
            return PreregistrationIdentity.model_validate_json(json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - typed refusal for the reader
            raise UtilityReportError("manifest pre-registration identity is malformed") from exc
    return payload


def _verify_digests(run_dir: Path) -> None:
    recorded = _read_json(run_dir / "digests.json", "digest index").get("files")
    if not isinstance(recorded, dict) or not recorded:
        raise UtilityReportError("run has no artifact digest index")
    actual = {
        name: value for name, value in _file_digests(run_dir).items() if name != "digests.json"
    }
    for name, digest in recorded.items():
        if actual.get(name) != digest:
            raise UtilityReportError(f"artifact digest does not match its recorded value: {name}")
    for name in actual:
        if name not in recorded:
            raise UtilityReportError(f"run contains an artifact outside its digest index: {name}")


def _verify_compatibility(manifest: dict[str, Any], product_root: Path) -> None:
    if manifest.get("manifest_version") != MANIFEST_VERSION:
        raise UtilityReportError("run manifest version is incompatible with this reader")
    scenario = manifest.get("scenario") or {}
    if scenario.get("generator_version") != SCENARIO_GENERATOR_VERSION:
        raise UtilityReportError("run was generated by an incompatible scenario version")
    recorded = manifest.get("product") or {}
    current = _product_revision(product_root)
    if recorded.get("commit") != current["commit"]:
        raise UtilityReportError("product revision drifted from the manifest")
    if recorded.get("tree_clean") is False:
        raise UtilityReportError("run was produced against a dirty product tree")
    if not manifest.get("synthetic") and (not current["commit"] or current["tree_clean"] is not True):
        raise UtilityReportError("comparison requires the clean recorded product revision")
    phase_hashes, oracle_hashes = _episode_hashes(scenario["seed"], tuple(scenario["variants"]), manifest["hash_salt"])
    if phase_hashes != scenario.get("phase_hashes") or oracle_hashes != scenario.get("oracle_hashes"):
        raise UtilityReportError("scenario evidence drifted from the manifest")
    guidance = _load_guidance(product_root)
    hashes = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in guidance.items()}
    if hashes != recorded.get("guidance_hashes"):
        raise UtilityReportError("shipped guidance drifted from the manifest")


def _recompute(run_dir: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    """Regrade observed snapshots against a freshly generated private oracle."""

    scenario = manifest["scenario"]
    seed, variants = scenario["seed"], tuple(scenario["variants"])
    pair_records: dict[str, list[tuple[ActionOutcome, ActionOutcome]]] = {}
    for variant in variants:
        pair_dir = run_dir / "pairs" / variant
        if not pair_dir.is_dir():
            continue
        episode = generate_episode(seed=seed, variant=variant)
        saved = _read_json(pair_dir / "outcomes.json", f"{variant} outcomes")
        outcomes: dict[str, ActionOutcome] = {}
        for arm in ("control", "memory"):
            record = saved.get(arm) or {}
            world_path = pair_dir / arm / "final-world.json"
            if (pair_dir / arm).is_dir():
                execution = _read_json(pair_dir / arm / "execution.json", "execution")
                phases = [_read_json(p, "phase result") for phase in PHASES
                          if (p := pair_dir / arm / f"{phase}-result.json").is_file()]
                if execution.get("phase_count") != len(phases):
                    raise UtilityReportError("phase evidence is incomplete")
                outcomes[arm] = _grade_execution(
                    episode, arm, _read_json(world_path, f"{variant}/{arm} world"), execution, phases)
                assertions = _registered_assertions(episode, _read_json(world_path, "world"))
                if assertions != _read_json(pair_dir / arm / "assertions.json", "assertions"):
                    raise UtilityReportError("registered assertions disagree with observed evidence")
            else:
                outcomes[arm] = _missing(episode, variant, arm, record.get("failure_reason"))
            if _outcome_to_dict(outcomes[arm]) != record:
                raise UtilityReportError(f"{variant}/{arm} saved outcome disagrees with recomputed evidence")
        pair_records[variant] = [(outcomes["control"], outcomes["memory"])]
    scores = {
        variant: score_variant(variant, pair_records.get(variant, []), scheduled=1)
        for variant in variants
    }
    return {
        "scores": {
            variant: {
                "pair": _pair_outcome_to_dict(pair_outcome),
                "arms": {
                    arm: _arm_outcome_to_dict(arm_outcome)
                    for arm, arm_outcome in arm_outcomes.items()
                },
            }
            for variant, (pair_outcome, arm_outcomes) in scores.items()
        }
    }


def _compare_saved_scores(report: dict[str, Any], recomputed: dict[str, Any]) -> None:
    saved = report.get("scores")
    if saved != recomputed["scores"]:
        raise UtilityReportError(
            "saved score rows disagree with the outcomes recomputed from observed evidence"
        )


def _pair_outcome_to_dict(pair_outcome) -> dict[str, Any]:
    return {
        "variant": pair_outcome.variant,
        "scheduled": pair_outcome.scheduled,
        "attempted": pair_outcome.attempted,
        "valid": pair_outcome.valid,
        "invalid": pair_outcome.invalid,
        "missing": pair_outcome.missing,
        "wins": pair_outcome.wins,
        "losses": pair_outcome.losses,
        "both_pass": pair_outcome.both_pass,
        "both_fail": pair_outcome.both_fail,
        "utility_lift": pair_outcome.utility_lift,
        "harm_numerator": pair_outcome.harm_numerator,
        "harm_denominator": pair_outcome.harm_denominator,
        "conditional_harm": pair_outcome.conditional_harm,
        "unconditional_losses": pair_outcome.unconditional_losses,
    }


def _arm_outcome_to_dict(arm_outcome) -> dict[str, Any]:
    return {
        "variant": arm_outcome.variant,
        "arm": arm_outcome.arm,
        "attempted": arm_outcome.attempted,
        "valid": arm_outcome.valid,
        "invalid": arm_outcome.invalid,
        "missing": arm_outcome.missing,
        "destructive_effect_counts": dict(arm_outcome.destructive_effect_counts),
    }


def _usage_to_dict(totals: dict[str, Any]) -> dict[str, Any]:
    return {
        field_name: {
            "total": total.total,
            "known_count": total.known_count,
            "unknown_count": total.unknown_count,
        }
        for field_name, total in totals.items()
    }
