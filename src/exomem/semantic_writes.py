"""Private writer-neutral semantic creation preflight and commit routing."""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from . import (
    activation_manifest,
    freshness,
    memory_schema,
    relation_registry,
    relation_review,
    semantic_authoring,
    semantic_contract,
    semantic_index,
    semantic_language_registry,
    vault,
)
from . import (
    find as find_module,
)
from .kbdir import kb_prefix

_TOKEN_VERSION = 1
_MAX_TOKEN_BYTES = 12 * 1024
_COMPILED_TYPES = frozenset(
    {
        "research-note",
        "insight",
        "failure",
        "pattern",
        "experiment",
        "production-log",
    }
)
_EXISTING_OPERATIONS = frozenset({"edit", "observe", "tier2_overwrite", "tier2_append"})
_FEEDBACK_FINDING_LIMIT = 32
_FEEDBACK_RELATION_FACT_LIMIT = 16
_FEEDBACK_ITEM_LIMIT = 32
_FEEDBACK_COUNT_LIMIT = 32
_FEEDBACK_STRING_BYTE_LIMIT = 256
_FEEDBACK_NESTED_ITEM_LIMIT = 8
_FEEDBACK_BYTE_BUDGET = 120 * 1024
_RECOVERY_REVIEW_LIMIT = 256
_RECOVERY_FEEDBACK_ENTRY_LIMIT = 32
_POSTHOC_FINDING_LIMIT = 256
_POSTHOC_PATH_LIMIT = 64
_POSTHOC_SUMMARY_LIMIT = 64
_POSTHOC_BYTE_BUDGET = 120 * 1024
_MOVE_WIKILINK_PATTERN = re.compile(r"\[\[([^\]\|\n]+?)(\|[^\]\n]*)?\]\]")


def rewrite_wikilinks_for_move(text: str, old_rel: str, new_rel: str) -> tuple[str, int]:
    """Pure canonical path-only rewrite shared by move staging and review carry."""
    old_no_ext = old_rel.removesuffix(".md")
    new_no_ext = new_rel.removesuffix(".md")
    prefix = kb_prefix()
    old_full = old_no_ext if old_no_ext.startswith(prefix) else prefix + old_no_ext
    new_full = new_no_ext if new_no_ext.startswith(prefix) else prefix + new_no_ext
    old_stripped = old_full.removeprefix(prefix)
    new_stripped = new_full.removeprefix(prefix)
    old_basename = old_no_ext.rsplit("/", 1)[-1]
    new_basename = new_no_ext.rsplit("/", 1)[-1]
    changed = 0

    def replace(match: re.Match[str]) -> str:
        nonlocal changed
        target = match.group(1).strip()
        alias = match.group(2) or ""
        target_path, marker, anchor = target.partition("#")
        anchor_suffix = f"#{anchor}" if marker else ""
        target_path = target_path.rstrip()
        target_no_ext = target_path.removesuffix(".md")
        if target_no_ext in {old_full, old_stripped}:
            changed += 1
            replacement = new_full if target_path.startswith(prefix) else new_stripped
            return f"[[{replacement}{anchor_suffix}{alias}]]"
        if "/" not in target_no_ext and target_no_ext == old_basename:
            changed += 1
            return f"[[{new_basename}{anchor_suffix}{alias}]]"
        return match.group(0)

    return _MOVE_WIKILINK_PATTERN.sub(replace, text), changed


def _bounded_feedback_text(value: str, truncation: dict[str, int]) -> str:
    raw = value.encode("utf-8", errors="replace")
    if len(raw) <= _FEEDBACK_STRING_BYTE_LIMIT:
        return raw.decode("utf-8")
    ellipsis = "…"
    prefix = raw[: _FEEDBACK_STRING_BYTE_LIMIT - len(ellipsis.encode("utf-8"))]
    text = prefix.decode("utf-8", errors="ignore")
    retained = len(text.encode("utf-8"))
    truncation["strings_truncated"] += 1
    truncation["string_bytes_omitted"] += len(raw) - retained
    return text + ellipsis


def _bounded_feedback_value(value: Any, truncation: dict[str, int]) -> Any:
    if isinstance(value, str):
        return _bounded_feedback_text(value, truncation)
    if isinstance(value, (list, tuple)):
        retained = value[:_FEEDBACK_NESTED_ITEM_LIMIT]
        truncation["nested_items_omitted"] += len(value) - len(retained)
        return [_bounded_feedback_value(item, truncation) for item in retained]
    if isinstance(value, dict):
        bounded: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = _bounded_feedback_text(str(raw_key), truncation)
            if key in bounded:
                suffix = f"#{len(bounded)}"
                key = _bounded_feedback_text(
                    key + suffix,
                    truncation,
                )
            bounded[key] = _bounded_feedback_value(item, truncation)
        return bounded
    return value


def _feedback_serialized_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True).encode("utf-8"))


def _fit_feedback_byte_budget(value: dict[str, Any]) -> dict[str, Any]:
    """Deterministically omit retained items until canonical JSON is bounded."""
    relation = value.get("relation_disposition")
    top_omitted = value["omitted_counts"]
    candidates: list[tuple[dict[str, Any], str, dict[str, int]]] = [
        (value, "warnings", top_omitted),
        (value, "errors", top_omitted),
        (value, "blocking_findings", top_omitted),
        (value, "findings", top_omitted),
        (value, "actions", top_omitted),
    ]
    if isinstance(relation, dict):
        relation_omitted = relation["omitted_counts"]
        candidates.extend(
            (
                (relation, "rejected_facts", relation_omitted),
                (relation, "qualifying_facts", relation_omitted),
                (relation, "qualifying_directions", relation_omitted),
                (relation, "actions", relation_omitted),
            )
        )

    while _feedback_serialized_size(value) >= _FEEDBACK_BYTE_BUDGET:
        removed_total = 0
        for container, key, omitted in candidates:
            items = container[key]
            if not items:
                continue
            keep = len(items) // 2
            removed = len(items) - keep
            del items[keep:]
            omitted[key] = omitted.get(key, 0) + removed
            removed_total += removed
        if removed_total == 0:
            break
        value["truncation"]["budget_items_omitted"] += removed_total
    value["omitted_counts"] = dict(sorted(top_omitted.items()))
    if isinstance(relation, dict):
        relation["omitted_counts"] = dict(sorted(relation["omitted_counts"].items()))
    return value


_ERROR_FINDING_LIMIT = semantic_contract.ERROR_FINDING_LIMIT

# Single renderer, shared with relation_review via semantic_contract. Keeping a
# second copy here would let the two blocking paths drift apart, which is the
# whole failure this reporting was added to prevent.
_blocking_finding_text = semantic_contract.blocking_finding_text
_blocking_reason = semantic_contract.blocking_reason


def _blocking_reason_for_evaluations(
    evaluations: Sequence[Any],
    prefix: str = "semantic contract has blocking findings",
) -> str:
    """Same as `_blocking_reason` for multi-page operations (move, recovery).

    `should_block` on these preflights is an `any()` across evaluations, so the
    blocking findings are spread over several pages. Naming the page each
    finding came from is what makes a multi-page rejection actionable.
    """
    findings = tuple(
        finding for item in evaluations for finding in item.contract_result.blocking_findings
    )
    if not findings:
        return prefix
    shown = findings[:_ERROR_FINDING_LIMIT]
    rendered = "; ".join(_blocking_finding_text(item) for item in shown)
    omitted = len(findings) - len(shown)
    if omitted:
        rendered += f"; +{omitted} more (validate_only returns the full set)"
    return f"{prefix}: {rendered}"


def _bounded_semantic_feedback(
    result: semantic_contract.SemanticContractResult,
) -> dict[str, Any]:
    """Project a full internal result into deterministic bounded writer feedback."""
    omitted: dict[str, int] = {}
    truncation = {
        "strings_truncated": 0,
        "string_bytes_omitted": 0,
        "nested_items_omitted": 0,
    }

    def findings(
        name: str, values: tuple[semantic_contract.ContractFinding, ...]
    ) -> list[dict[str, Any]]:
        if len(values) > _FEEDBACK_FINDING_LIMIT:
            omitted[name] = len(values) - _FEEDBACK_FINDING_LIMIT
        return [
            _bounded_feedback_value(item.as_dict(), truncation)
            for item in values[:_FEEDBACK_FINDING_LIMIT]
        ]

    def items(name: str, values: tuple[str, ...]) -> list[str]:
        if len(values) > _FEEDBACK_ITEM_LIMIT:
            omitted[name] = len(values) - _FEEDBACK_ITEM_LIMIT
        return [_bounded_feedback_text(item, truncation) for item in values[:_FEEDBACK_ITEM_LIMIT]]

    kind_counts = result.kind_counts[:_FEEDBACK_COUNT_LIMIT]
    category_counts = result.category_counts[:_FEEDBACK_COUNT_LIMIT]
    if len(result.kind_counts) > _FEEDBACK_COUNT_LIMIT:
        omitted["kind_counts"] = len(result.kind_counts) - _FEEDBACK_COUNT_LIMIT
    if len(result.category_counts) > _FEEDBACK_COUNT_LIMIT:
        omitted["category_counts"] = len(result.category_counts) - _FEEDBACK_COUNT_LIMIT

    relation_value: dict[str, Any] | None = None
    disposition = result.relation_disposition
    if disposition is not None:
        relation_omitted: dict[str, int] = {}

        def relation_items(name: str, values: tuple[str, ...]) -> list[str]:
            if len(values) > _FEEDBACK_ITEM_LIMIT:
                relation_omitted[name] = len(values) - _FEEDBACK_ITEM_LIMIT
            return [
                _bounded_feedback_text(item, truncation) for item in values[:_FEEDBACK_ITEM_LIMIT]
            ]

        if len(disposition.qualifying_facts) > _FEEDBACK_RELATION_FACT_LIMIT:
            relation_omitted["qualifying_facts"] = (
                len(disposition.qualifying_facts) - _FEEDBACK_RELATION_FACT_LIMIT
            )
        if len(disposition.rejected_facts) > _FEEDBACK_RELATION_FACT_LIMIT:
            relation_omitted["rejected_facts"] = (
                len(disposition.rejected_facts) - _FEEDBACK_RELATION_FACT_LIMIT
            )
        relation_value = {
            "kind": disposition.kind,
            "satisfied": disposition.satisfied,
            "current": disposition.current,
            "qualifying_directions": relation_items(
                "qualifying_directions", disposition.qualifying_directions
            ),
            "qualifying_facts": [
                _bounded_feedback_value(fact.as_dict(), truncation)
                for fact in disposition.qualifying_facts[:_FEEDBACK_RELATION_FACT_LIMIT]
            ],
            "rejected_facts": [
                _bounded_feedback_value(item.as_dict(), truncation)
                for item in disposition.rejected_facts[:_FEEDBACK_RELATION_FACT_LIMIT]
            ],
            "actions": relation_items("actions", disposition.actions),
            "omitted_counts": dict(sorted(relation_omitted.items())),
        }

    value = {
        "mode": _bounded_feedback_text(result.mode, truncation),
        "operation": _bounded_feedback_text(result.operation, truncation),
        "findings": findings("findings", result.findings),
        "errors": findings("errors", result.errors),
        "warnings": findings("warnings", result.warnings),
        "blocking_findings": findings("blocking_findings", result.blocking_findings),
        "should_block": result.should_block,
        "semantic_unit_count": result.semantic_unit_count,
        "compact_unit_count": result.compact_unit_count,
        "rich_unit_count": result.rich_unit_count,
        "kind_counts": _bounded_feedback_value(dict(kind_counts), truncation),
        "category_counts": _bounded_feedback_value(dict(category_counts), truncation),
        # Already bounded to at most eight deterministic entries in the shared
        # leaf; adapters carry it verbatim and never recompute it.
        "category_feedback": [entry.as_dict() for entry in result.category_feedback],
        "category_feedback_omitted": result.category_feedback_omitted,
        "relation_disposition": relation_value,
        "actions": items("actions", result.actions),
        "omitted_counts": dict(sorted(omitted.items())),
        "truncation": {
            "byte_budget": _FEEDBACK_BYTE_BUDGET,
            **truncation,
            "budget_items_omitted": 0,
        },
    }
    return _fit_feedback_byte_budget(value)


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


@dataclass(slots=True)
class SemanticWriteError(ValueError):
    code: str
    reason: str
    validation_findings: tuple[semantic_contract.ContractFinding, ...] = ()

    def __post_init__(self) -> None:
        ValueError.__init__(self, f"{self.code}: {self.reason}")

    def as_semantic_validation_error(self) -> dict[str, Any] | None:
        """Project only canonical semantic-authoring refusals for public facades."""
        canonical = semantic_authoring.AUTHORING_CONTRACT.findings
        authored = tuple(
            finding for finding in self.validation_findings if finding.code in canonical
        )
        if not authored:
            return None
        by_code = {finding.code: finding for finding in authored}
        primary = by_code.get("missing_semantic_unit", authored[0])
        definition = canonical[primary.code]
        if primary.code == "missing_semantic_unit":
            compact = definition["compact_remediation"]
            rich = definition["rich_remediation"]
            remediation = f"{compact} {rich}"
        else:
            compact = None
            rich = None
            remediation = definition["remediation"]
        payload: dict[str, Any] = {
            "code": primary.code,
            "message": primary.detail or definition["when"],
            "remediation": remediation,
            "findings": [finding.as_dict() for finding in self.validation_findings],
            "validation_state": "rejected",
            "mutated": False,
        }
        if compact is not None:
            payload["compact_remediation"] = compact
        if rich is not None:
            payload["rich_remediation"] = rich
        return payload


@dataclass(frozen=True, slots=True)
class PosthocPageEvaluation:
    """One governed Markdown page evaluated against a shared current corpus."""

    path: str
    contract_result: semantic_contract.SemanticContractResult
    grandfathered: bool
    activation: Literal["current", "prospective"]


def _posthoc_finding_sort_key(item: dict[str, Any]) -> tuple[Any, ...]:
    code = str(item.get("code") or "")
    resolved_rule = tuple(str(value) for value in item.get("resolved_rule") or ())
    rule = resolved_rule[2] if len(resolved_rule) >= 3 else ""
    namespace = resolved_rule[0] if resolved_rule else ""
    legacy_backlog = bool(
        code == "RELATION_DISPOSITION_MISSING" and item.get("grandfathered") is True
    )
    current = item.get("activation") == "current"
    if (
        not legacy_backlog
        and current
        and item.get("grandfathered") is not True
        and item.get("severity") == "error"
    ):
        priority = 0
    elif rule == "syntax" or (
        namespace in {"categories", "kinds"} and (rule == "registry" or "REGISTRY" in code.upper())
    ):
        priority = 1
    elif legacy_backlog:
        priority = 3
    else:
        priority = 2
    identity = json.dumps(
        item.get("governed_element_identity") or (),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return priority, code, str(item.get("path") or ""), identity


@dataclass(frozen=True, slots=True)
class PosthocBatch:
    """Bounded non-content projection shared by watcher, audit, and reconcile."""

    operation: Literal["watcher", "audit", "reconcile"]
    activation: Literal["current", "prospective"]
    evaluations: tuple[PosthocPageEvaluation, ...]
    corpus: semantic_contract.SemanticCorpusContext | None = None

    def as_dict(self, detail: Literal["actionable", "full"] = "actionable") -> dict[str, Any]:
        if detail not in {"actionable", "full"}:
            raise SemanticWriteError(
                "SEMANTIC_POSTHOC_INVALID_DETAIL",
                "posthoc detail must be actionable or full",
            )
        full = detail == "full"
        audit_projection = self.operation == "audit"
        findings: list[dict[str, Any]] = []
        summary: dict[str, int] = {}
        bounded = {
            "strings_truncated": 0,
            "string_bytes_omitted": 0,
            "nested_items_omitted": 0,
        }
        for evaluation in self.evaluations:
            disposition = evaluation.contract_result.relation_disposition
            disposition_value = (
                {
                    "kind": disposition.kind,
                    "satisfied": disposition.satisfied,
                    "current": disposition.current,
                }
                if disposition is not None
                else None
            )
            for finding in evaluation.contract_result.findings:
                summary[finding.code] = summary.get(finding.code, 0) + 1
                item = {
                    "path": evaluation.path,
                    "code": finding.code,
                    "severity": finding.severity,
                    "governed_element_identity": list(finding.governed_element_identity),
                    "resolved_rule": list(finding.resolved_rule),
                    "relation_disposition": disposition_value,
                    "actions": list(evaluation.contract_result.actions),
                    "activation": evaluation.activation,
                    "grandfathered": evaluation.grandfathered,
                }
                findings.append(item if full else _bounded_feedback_value(item, bounded))
        total = len(findings)
        ordered_for_bounds = False
        if not full and audit_projection and total > _POSTHOC_FINDING_LIMIT:
            findings.sort(key=_posthoc_finding_sort_key)
            ordered_for_bounds = True
        if not full:
            findings = findings[:_POSTHOC_FINDING_LIMIT]
        omitted = total - len(findings)
        if full:
            evaluated_paths = [item.path for item in self.evaluations]
        else:
            evaluated_paths = [
                _bounded_feedback_text(item.path, bounded)
                for item in self.evaluations[:_POSTHOC_PATH_LIMIT]
            ]
        summary_items = sorted(summary.items())
        retained_summary = dict(
            summary_items if full or audit_projection else summary_items[:_POSTHOC_SUMMARY_LIMIT]
        )
        value: dict[str, Any] = {
            "operation": self.operation,
            "activation": self.activation,
            "evaluated_paths": evaluated_paths,
            "semantic_contract_findings": findings,
            "semantic_contract_summary": retained_summary,
            "omitted_counts": {
                "evaluated_paths": len(self.evaluations) - len(evaluated_paths),
                "semantic_contract_findings": omitted,
                "semantic_contract_summary": len(summary_items) - len(retained_summary),
            },
            "truncation": {
                "byte_budget": None if full else _POSTHOC_BYTE_BUDGET,
                "finding_limit": None if full else _POSTHOC_FINDING_LIMIT,
                "path_limit": None if full else _POSTHOC_PATH_LIMIT,
                "summary_limit": (None if full or audit_projection else _POSTHOC_SUMMARY_LIMIT),
                **bounded,
                "budget_items_omitted": 0,
            },
        }
        if audit_projection or full:
            value["truncation"].update(
                {
                    "observation_complete": True,
                    "findings_complete": omitted == 0,
                }
            )
        if full:
            return value
        if (
            audit_projection
            and _feedback_serialized_size(value) >= _POSTHOC_BYTE_BUDGET
            and not ordered_for_bounds
        ):
            findings.sort(key=_posthoc_finding_sort_key)
        variable_collections = [
            ("semantic_contract_findings", findings),
            ("evaluated_paths", evaluated_paths),
        ]
        if not audit_projection:
            variable_collections.append(("semantic_contract_summary", retained_summary))
        while _feedback_serialized_size(value) >= _POSTHOC_BYTE_BUDGET:
            removed_total = 0
            for name, collection in variable_collections:
                if not collection:
                    continue
                keep = len(collection) // 2
                if isinstance(collection, dict):
                    retained_keys = list(collection)[:keep]
                    removed = len(collection) - len(retained_keys)
                    retained_values = {key: collection[key] for key in retained_keys}
                    collection.clear()
                    collection.update(retained_values)
                else:
                    removed = len(collection) - keep
                    del collection[keep:]
                value["omitted_counts"][name] += removed
                removed_total += removed
            value["truncation"]["budget_items_omitted"] += removed_total
            if removed_total == 0:
                break
        if audit_projection:
            value["truncation"]["findings_complete"] = (
                value["omitted_counts"]["semantic_contract_findings"] == 0
            )
        return value


def _posthoc_relative_path(root: Path, path: Path | str) -> str | None:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except (OSError, ValueError):
            return None
    value = str(path).replace("\\", "/").lstrip("/")
    return value or None


def evaluate_posthoc_batch(
    vault_root: Path,
    *,
    paths: list[Path | str] | tuple[Path | str, ...] | None = None,
    operation: Literal["watcher", "audit", "reconcile"],
    corpus: semantic_contract.SemanticCorpusContext | None = None,
    language_registry: semantic_language_registry.SemanticLanguageRegistry | None = None,
    saved_contracts: tuple[memory_schema.LoadedMemoryContract, ...] | None = None,
) -> PosthocBatch:
    """Evaluate current governed Markdown once without writing or repairing it."""
    if operation not in {"watcher", "audit", "reconcile"}:
        raise SemanticWriteError(
            "SEMANTIC_POSTHOC_INVALID_OPERATION",
            "posthoc semantic operation is unsupported",
        )
    root = Path(vault_root)
    if corpus is None:
        registry = relation_registry.load_registry(root)
        language = semantic_language_registry.load_registry(root)
        resolved_saved_contracts = memory_schema.load_saved_contracts(root)
        corpus = semantic_contract.build_corpus_context(
            root,
            registry=registry,
            language_registry=language,
        )
    else:
        language = language_registry or semantic_language_registry.load_registry(root)
        resolved_saved_contracts = (
            saved_contracts
            if saved_contracts is not None
            else memory_schema.load_saved_contracts(root)
        )
    manifest = activation_manifest.load_manifest(root)
    activation_status: Literal["current", "prospective"] = (
        "current" if manifest is not None else "prospective"
    )
    if paths is None:
        selected = sorted(corpus.pages)
    else:
        selected = sorted(
            {rel for path in paths if (rel := _posthoc_relative_path(root, path)) is not None}
        )

    contracts_by_scope: dict[
        tuple[tuple[str, ...], str | None], memory_schema.ResolvedMemoryContracts
    ] = {}
    evaluations: list[PosthocPageEvaluation] = []
    for rel_path in selected:
        state = corpus.pages.get(rel_path)
        if state is None or not state.eligible_governed:
            continue
        scope = (state.projects, state.page_type)
        contracts = contracts_by_scope.get(scope)
        if contracts is None:
            contracts = memory_schema.resolve_contracts(
                resolved_saved_contracts,
                projects=state.projects,
                page_type=state.page_type,
                language_registry=language,
            )
            contracts_by_scope[scope] = contracts
        review = (
            relation_review.load_relation_review(root, state, corpus=corpus)
            if semantic_contract.requires_semantic_unit(state)
            else None
        )
        grandfathered = bool(
            manifest is not None
            and activation_manifest.is_grandfathered(
                root,
                state.path,
                source_hash=state.source_hash,
                exomem_id=(state.identity if state.identity_kind == "exomem_id" else None),
                manifest=manifest,
                census=corpus.activation_census,
            )
        )
        result = semantic_contract.evaluate(
            before=None,
            after=state,
            operation=operation,
            mode="posthoc",
            before_contracts=contracts,
            after_contracts=contracts,
            before_corpus=corpus,
            after_corpus=corpus,
            before_review=None,
            after_review=review,
            grandfathered=False,
            include_relation_disposition=semantic_contract.requires_semantic_unit(state),
        )
        evaluations.append(
            PosthocPageEvaluation(
                state.path,
                result,
                grandfathered,
                activation_status,
            )
        )
    return PosthocBatch(operation, activation_status, tuple(evaluations), corpus)


@dataclass(frozen=True, slots=True)
class DraftRegistration:
    key: str
    category: str
    folder: str

    def as_dict(self) -> dict[str, str]:
        return {"key": self.key, "category": self.category, "folder": self.folder}


@dataclass(frozen=True, slots=True)
class DraftToken:
    writer: str
    operation: str
    destination: str
    render_date: str
    registrations: tuple[DraftRegistration, ...] = ()

    def encode(self) -> str:
        value = {
            "version": _TOKEN_VERSION,
            "writer": self.writer,
            "operation": self.operation,
            "destination": self.destination,
            "render_date": self.render_date,
            "registrations": [item.as_dict() for item in self.registrations],
        }
        raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(raw) > _MAX_TOKEN_BYTES:
            raise SemanticWriteError("DRAFT_TOKEN_TOO_LARGE", "draft token exceeds its bound")
        encoded = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if len(encoded.encode("utf-8")) > relation_review.MAX_DRAFT_TOKEN_ENCODED_BYTES:
            raise SemanticWriteError("DRAFT_TOKEN_TOO_LARGE", "draft token exceeds its bound")
        return encoded

    @classmethod
    def decode(cls, token: object) -> DraftToken:
        if (
            type(token) is not str
            or not token
            or len(token.encode("utf-8")) > relation_review.MAX_DRAFT_TOKEN_ENCODED_BYTES
        ):
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token is invalid")
        try:
            padded = token + "=" * (-len(token) % 4)
            raw = base64.b64decode(padded, altchars=b"-_", validate=True)
            if len(raw) > _MAX_TOKEN_BYTES:
                raise ValueError
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
        except (
            binascii.Error,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as error:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token is invalid") from error
        if type(value) is not dict or set(value) != {
            "version",
            "writer",
            "operation",
            "destination",
            "render_date",
            "registrations",
        }:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid fields")
        if value["version"] != _TOKEN_VERSION or any(
            type(value[key]) is not str
            for key in ("writer", "operation", "destination", "render_date")
        ):
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid fields")
        try:
            render_date = dt.date.fromisoformat(value["render_date"])
        except ValueError as error:
            raise SemanticWriteError(
                "INVALID_DRAFT_TOKEN", "draft token has invalid render date"
            ) from error
        if (
            render_date.isoformat() != value["render_date"]
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value["writer"])
            or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value["operation"])
            or len(value["destination"]) > 1024
        ):
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid fields")
        registrations_raw = value["registrations"]
        if type(registrations_raw) is not list or len(registrations_raw) > 64:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token has invalid registrations")
        registrations: list[DraftRegistration] = []
        for item in registrations_raw:
            if (
                type(item) is not dict
                or set(item) != {"key", "category", "folder"}
                or any(type(item[key]) is not str for key in ("key", "category", "folder"))
            ):
                raise SemanticWriteError(
                    "INVALID_DRAFT_TOKEN", "draft token has invalid registrations"
                )
            registrations.append(DraftRegistration(item["key"], item["category"], item["folder"]))
        decoded = cls(
            value["writer"],
            value["operation"],
            value["destination"],
            value["render_date"],
            tuple(registrations),
        )
        if decoded.encode() != token:
            raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token is not canonical")
        return decoded


@dataclass(frozen=True, slots=True)
class CreationPreflight:
    applicability: Literal["full", "structural", "not_semantic"]
    destination: str
    source: str
    draft_id: str | None
    draft_token: str
    mutated: Literal[False]
    contract_result: semantic_contract.SemanticContractResult | None
    creation_validation: relation_review.CreationDraftValidation | None
    semantic_state: semantic_contract.SemanticPageState

    @property
    def draft_hash(self) -> str | None:
        return self.creation_validation.draft_hash if self.creation_validation is not None else None

    @property
    def relation_candidates(self) -> tuple[relation_review.RelationCandidate, ...]:
        return (
            self.creation_validation.relation_candidates
            if self.creation_validation is not None
            else ()
        )

    def as_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "applicability": self.applicability,
            "destination": self.destination,
            "draft_id": self.draft_id,
            "draft_hash": self.draft_hash,
            "draft_token": self.draft_token,
            "mutated": self.mutated,
            "contract_result": (
                self.contract_result.as_dict() if self.contract_result is not None else None
            ),
        }
        if self.creation_validation is not None:
            value.update(self.creation_validation.as_dict())
            value["draft_token"] = self.draft_token
            value["applicability"] = self.applicability
        return value


@dataclass(frozen=True, slots=True)
class CreationCommit:
    applicability: Literal["full", "structural", "not_semantic"]
    mutated: Literal[True]
    written_paths: tuple[str, ...]
    contract_result: semantic_contract.SemanticContractResult | None
    creation_commit: relation_review.CreationDraftCommit | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "applicability": self.applicability,
            "mutated": self.mutated,
            "written_paths": list(self.written_paths),
            "contract_result": (
                self.contract_result.as_dict() if self.contract_result is not None else None
            ),
            "creation": (
                self.creation_commit.as_dict() if self.creation_commit is not None else None
            ),
        }


@dataclass(frozen=True, slots=True)
class ExistingPreflight:
    """Detached before/after semantic decision for one existing Markdown write."""

    applicability: Literal["full", "structural", "not_semantic"]
    operation: Literal["edit", "observe", "tier2_overwrite", "tier2_append"]
    path: str
    before_source: str
    after_source: str
    before: semantic_contract.SemanticPageState
    after: semantic_contract.SemanticPageState
    grandfathered: bool
    transition_token: str
    transition_hash: str
    mutated: Literal[False]
    contract_result: semantic_contract.SemanticContractResult
    before_corpus: semantic_contract.SemanticCorpusContext
    after_corpus: semantic_contract.SemanticCorpusContext
    before_contracts: memory_schema.ResolvedMemoryContracts
    after_contracts: memory_schema.ResolvedMemoryContracts
    before_review: semantic_contract.RelationReviewState | None
    after_review: semantic_contract.RelationReviewState | None
    requested_decision: relation_review.LifecycleDecision | None
    activation_census: activation_manifest.ActivationCensus
    prospective_manifest: activation_manifest.ActivationManifest
    manifest_install_required: bool
    resolver_freshness: tuple[int, int, str] | None
    primary_guard: vault.PathGuard
    committed_replay: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "applicability": self.applicability,
            "operation": self.operation,
            "path": self.path,
            "grandfathered": self.grandfathered,
            "transition_token": self.transition_token,
            "transition_hash": self.transition_hash,
            "mutated": self.mutated,
            "contract_result": _bounded_semantic_feedback(self.contract_result),
        }


@dataclass(frozen=True, slots=True)
class ExistingCommit:
    applicability: Literal["full", "structural", "not_semantic"]
    operation: Literal["edit", "observe", "tier2_overwrite", "tier2_append"]
    path: str
    mutated: bool
    written_paths: tuple[str, ...]
    contract_result: semantic_contract.SemanticContractResult
    index_report: Any | None
    lifecycle_state: str | None
    transition_token: str

    def as_dict(self) -> dict[str, Any]:
        value = {
            "applicability": self.applicability,
            "operation": self.operation,
            "path": self.path,
            "mutated": self.mutated,
            "written_paths": list(self.written_paths),
            "contract_result": _bounded_semantic_feedback(self.contract_result),
            "lifecycle_state": self.lifecycle_state,
            "transition_token": self.transition_token,
        }
        if self.index_report is not None:
            value["index"] = self.index_report.as_dict()
        return value


@dataclass(frozen=True, slots=True)
class MovePageEvaluation:
    """One page evaluated against the shared before/final move corpus."""

    before: semantic_contract.SemanticPageState
    after: semantic_contract.SemanticPageState
    before_contracts: memory_schema.ResolvedMemoryContracts
    after_contracts: memory_schema.ResolvedMemoryContracts
    before_review: semantic_contract.RelationReviewState | None
    after_review: semantic_contract.RelationReviewState | None
    grandfathered: bool
    transition_token: str
    contract_result: semantic_contract.SemanticContractResult
    requested_decision: relation_review.LifecycleDecision | None = None
    carried_from: relation_review.LifecycleDecision | None = None


@dataclass(frozen=True, slots=True)
class MovePreflight:
    """Exact, mutation-free semantic preflight for one filesystem move batch."""

    old_path: str
    new_path: str
    source: str
    moved_source: str
    rewrites: tuple[vault.PlannedWrite, ...]
    evaluations: tuple[MovePageEvaluation, ...]
    before_corpus: semantic_contract.SemanticCorpusContext
    after_corpus: semantic_contract.SemanticCorpusContext
    activation_census: activation_manifest.ActivationCensus
    prospective_manifest: activation_manifest.ActivationManifest
    manifest_install_required: bool
    source_guard: vault.PathGuard
    destination_guard: vault.PathGuard
    mutated: Literal[False] = False

    @property
    def should_block(self) -> bool:
        return any(item.contract_result.should_block for item in self.evaluations)

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": "move",
            "old_path": self.old_path,
            "new_path": self.new_path,
            "affected_paths": [item.after.path for item in self.evaluations],
            "mutated": self.mutated,
            "contract_results": {
                item.after.identity: _bounded_semantic_feedback(item.contract_result)
                for item in self.evaluations
            },
        }


@dataclass(frozen=True, slots=True)
class MoveCommit:
    """Committed move semantics; filesystem/index details stay with the adapter."""

    preflight: MovePreflight
    lifecycle_states: tuple[tuple[str, str], ...]
    mutated: Literal[True] = True

    def as_dict(self) -> dict[str, Any]:
        value = self.preflight.as_dict()
        value["mutated"] = self.mutated
        value["lifecycle_states"] = dict(self.lifecycle_states)
        return value


@dataclass(frozen=True, slots=True)
class RecoveryEntry:
    trash_path: str
    original_path: str
    restore_path: str
    source: str
    source_guard: vault.PathGuard
    destination_guard: vault.PathGuard
    sidecar_guard: vault.PathGuard | None
    sidecar_source: str | None = None


@dataclass(frozen=True, slots=True)
class RecoveryPageEvaluation:
    entry: RecoveryEntry
    before: semantic_contract.SemanticPageState
    after: semantic_contract.SemanticPageState
    before_contracts: memory_schema.ResolvedMemoryContracts
    after_contracts: memory_schema.ResolvedMemoryContracts
    before_review: semantic_contract.RelationReviewState | None
    after_review: semantic_contract.RelationReviewState | None
    grandfathered: bool
    transition_token: str
    contract_result: semantic_contract.SemanticContractResult
    requested_decision: relation_review.LifecycleDecision | None = None


def _recovery_result_reviewable(
    result: semantic_contract.SemanticContractResult,
) -> bool:
    return (
        result.relation_disposition is not None
        and result.relation_disposition.kind == "missing"
        and {finding.code for finding in result.blocking_findings}
        == {"RELATION_DISPOSITION_MISSING"}
    )


@dataclass(frozen=True, slots=True)
class RecoveryPreflight:
    entries: tuple[RecoveryEntry, ...]
    evaluations: tuple[RecoveryPageEvaluation, ...]
    before_corpus: semantic_contract.SemanticCorpusContext
    prior_corpus: semantic_contract.SemanticCorpusContext
    after_corpus: semantic_contract.SemanticCorpusContext
    destination_root_guard: vault.PathGuard
    trash_census_guards: tuple[vault.DirectoryCensusGuard, ...] = ()
    recovery_sidecar_guard: vault.PathGuard | None = None
    mutated: Literal[False] = False

    @property
    def should_block(self) -> bool:
        return any(item.contract_result.should_block for item in self.evaluations)

    def as_dict(self) -> dict[str, Any]:
        return _recovery_feedback(self)


@dataclass(frozen=True, slots=True)
class RecoveryCommit:
    preflight: RecoveryPreflight
    lifecycle_states: tuple[tuple[str, str], ...]
    mutated: Literal[True] = True

    def as_dict(self) -> dict[str, Any]:
        return _recovery_feedback(
            self.preflight,
            mutated=self.mutated,
            lifecycle_states=self.lifecycle_states,
        )


def _recovery_feedback(
    preflight: RecoveryPreflight,
    *,
    mutated: bool = False,
    lifecycle_states: tuple[tuple[str, str], ...] = (),
) -> dict[str, Any]:
    review_requests = [
        {
            "page_identity": item.after.identity,
            "transition_token": item.transition_token,
            "transition_hash": hashlib.sha256(item.transition_token.encode("utf-8")).hexdigest(),
        }
        for item in preflight.evaluations
        if item.after.eligible_compiled
        and item.after.identity_kind == "exomem_id"
        and item.after_review is None
        and item.requested_decision is None
        and _recovery_result_reviewable(item.contract_result)
        and preflight.after_corpus.identity_census.paths_by_identity.get(item.after.identity)
        == (item.after.path,)
    ]
    retained_entries = preflight.entries[:_RECOVERY_FEEDBACK_ENTRY_LIMIT]
    retained_evaluations = preflight.evaluations[:_RECOVERY_FEEDBACK_ENTRY_LIMIT]
    retained_states = lifecycle_states[:_RECOVERY_FEEDBACK_ENTRY_LIMIT]
    omitted = {
        "restored_paths": len(preflight.entries) - len(retained_entries),
        "contract_results": len(preflight.evaluations) - len(retained_evaluations),
        "lifecycle_states": len(lifecycle_states) - len(retained_states),
    }
    value: dict[str, Any] = {
        "operation": "recover",
        "mutated": mutated,
        "restored_paths": [item.restore_path for item in retained_entries],
        "contract_results": {
            item.after.identity: _bounded_semantic_feedback(item.contract_result)
            for item in retained_evaluations
        },
        "relation_review_requests": review_requests,
        "lifecycle_states": dict(retained_states),
        "omitted_counts": omitted,
        "truncation": {
            "byte_budget": _FEEDBACK_BYTE_BUDGET,
            "budget_items_omitted": 0,
        },
    }
    while _feedback_serialized_size(value) >= _FEEDBACK_BYTE_BUDGET:
        removed = 0
        for key in ("contract_results", "lifecycle_states"):
            container = value[key]
            if not container:
                continue
            retained = list(container)[: len(container) // 2]
            removed_now = len(container) - len(retained)
            value[key] = {item: container[item] for item in retained}
            value["omitted_counts"][key] += removed_now
            removed += removed_now
        paths = value["restored_paths"]
        if paths:
            keep = len(paths) // 2
            removed_now = len(paths) - keep
            del paths[keep:]
            value["omitted_counts"]["restored_paths"] += removed_now
            removed += removed_now
        value["truncation"]["budget_items_omitted"] += removed
        if removed == 0:
            raise SemanticWriteError(
                "SEMANTIC_FEEDBACK_LIMIT",
                "recovery review requests exceed the bounded feedback budget",
            )
    return value


MoveMutation = Callable[
    [
        tuple[vault.PlannedWrite, ...],
        tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...],
        vault.PathGuard,
    ],
    None,
]

RecoveryMutation = Callable[[], None]


def _existing_transition_token(
    *, operation: str, path: str, before_hash: str, after_hash: str
) -> str:
    payload = {
        "version": 1,
        "transition_id": str(uuid.uuid4()),
        "operation": operation,
        "path": path,
        "before_hash": before_hash,
        "after_hash": after_hash,
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _recovery_token_path(trash_path: str, restore_path: str) -> str:
    return json.dumps(
        [trash_path, restore_path],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _decode_existing_transition_token(token: str) -> dict[str, Any]:
    if (
        type(token) is not str
        or not token
        or len(token.encode("utf-8")) > relation_review.MAX_DRAFT_TOKEN_ENCODED_BYTES
    ):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_json_object)
    except (TypeError, ValueError, UnicodeError, binascii.Error) as error:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        ) from error
    if type(value) is not dict or set(value) != {
        "version",
        "transition_id",
        "operation",
        "path",
        "before_hash",
        "after_hash",
    }:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    if value["version"] != 1 or any(
        type(value[key]) is not str
        for key in ("transition_id", "operation", "path", "before_hash", "after_hash")
    ):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    try:
        transition_id = str(uuid.UUID(value["transition_id"]))
    except (AttributeError, TypeError, ValueError) as error:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        ) from error
    if transition_id != value["transition_id"]:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_TOKEN", "transition token is invalid"
        )
    return value


def _existing_transition_id(token: str) -> str:
    return str(_decode_existing_transition_token(token)["transition_id"])


def _existing_applicability(
    before: semantic_contract.SemanticPageState,
    after: semantic_contract.SemanticPageState,
) -> Literal["full", "structural", "not_semantic"]:
    if semantic_contract.requires_semantic_unit(after):
        return "full"
    if (
        before.page_type in _COMPILED_TYPES
        or after.page_type in _COMPILED_TYPES
        or before.page_type == "entity"
        or after.page_type == "entity"
    ):
        return "structural"
    # This coordinator is entered only for governed Markdown. Untyped/arbitrary
    # Markdown still receives the structural/safety contract; non-Markdown
    # writers preserve their legacy path and never enter this seam.
    return "structural"


def preflight_existing(
    vault_root: Path,
    *,
    path: str,
    after_source: str,
    operation: Literal["edit", "observe", "tier2_overwrite", "tier2_append"],
    expected_before_hash: str | None = None,
    transition_token: str | None = None,
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
) -> ExistingPreflight:
    """Evaluate an existing-page transition without mutating any shared state."""
    if operation not in _EXISTING_OPERATIONS:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_INVALID_OPERATION",
            "existing semantic write operation is unsupported",
        )
    if relation_disposition not in {None, "reviewed_none"}:
        raise SemanticWriteError("INVALID_RELATION_REVIEW", "relation disposition is invalid")
    root = Path(vault_root)
    before_source, primary_guard = vault.read_guarded_text(root, root / path)
    raw_before_hash = vault.content_hash(before_source)
    # The parser/corpus and public content-hash contract normalize platform
    # newlines. Keep the raw-byte PathGuard, but evaluate the same logical text
    # on Windows so CRLF alone never looks like concurrent semantic drift.
    before_source = before_source.replace("\r\n", "\n").replace("\r", "\n")
    before_hash = vault.content_hash(before_source)
    if expected_before_hash is not None and expected_before_hash not in {
        raw_before_hash,
        before_hash,
    }:
        raise SemanticWriteError("STALE_SEMANTIC_WRITE", "page changed before semantic preflight")

    registry = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    loaded_contracts = memory_schema.load_saved_contracts(root)
    resolver_freshness_before = freshness.triple(root, "vault")
    before_corpus = semantic_contract.build_corpus_context(
        root, registry=registry, language_registry=language
    )
    resolver_freshness_after = freshness.triple(root, "vault")
    resolver_freshness = (
        resolver_freshness_before if resolver_freshness_before == resolver_freshness_after else None
    )
    before = semantic_contract.build_page_state(
        root,
        path,
        before_source,
        relation_registry=registry,
        language_registry=language,
    )
    if before_corpus.pages.get(path) != before:
        # The guarded read can observe a canonical or external replacement in
        # the short window before its freshness event is published. Repair the
        # exact evaluated page from those authoritative bytes so a lagging
        # process cache never turns a valid write into a corpus-state refusal.
        before_corpus = before_corpus.with_candidate(before)
    after = semantic_contract.build_page_state(
        root,
        path,
        after_source,
        relation_registry=registry,
        language_registry=language,
    )
    after_corpus = before_corpus.with_candidate(after)
    before_contracts = memory_schema.resolve_contracts(
        loaded_contracts,
        projects=before.projects,
        page_type=before.page_type,
        language_registry=language,
    )
    after_contracts = memory_schema.resolve_contracts(
        loaded_contracts,
        projects=after.projects,
        page_type=after.page_type,
        language_registry=language,
    )
    applicability = _existing_applicability(before, after)
    before_review: semantic_contract.RelationReviewState | None = None
    after_review: semantic_contract.RelationReviewState | None = None
    if applicability == "full":
        before_review = relation_review.load_relation_review(root, before, corpus=before_corpus)
        after_review = relation_review.load_relation_review(root, after, corpus=after_corpus)

    token = transition_token or _existing_transition_token(
        operation=operation,
        path=path,
        before_hash=before.source_hash,
        after_hash=after.source_hash,
    )
    token_value = _decode_existing_transition_token(token)
    committed_replay = bool(
        transition_token is not None
        and token_value["operation"] == operation
        and token_value["path"] == path
        and token_value["after_hash"] == before.source_hash
        and token_value["after_hash"] == after.source_hash
    )
    if (
        token_value["operation"] != operation
        or token_value["path"] != path
        or token_value["after_hash"] != after.source_hash
        or (token_value["before_hash"] != before.source_hash and not committed_replay)
    ):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "transition token does not match the exact before and after state",
        )
    transition_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    requested_decision: relation_review.LifecycleDecision | None = None
    if relation_disposition == "reviewed_none":
        if applicability != "full":
            raise SemanticWriteError(
                "INVALID_RELATION_REVIEW",
                "reviewed-none applies only to an active compiled result",
            )
        if relation_review_hash != transition_hash:
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_REVIEW_MISMATCH",
                "review hash does not match the validated transition",
            )
        if (
            after.identity_kind != "exomem_id"
            or after.review_fingerprint is None
            or after_corpus.identity_census.paths_by_identity.get(after.identity) != (after.path,)
        ):
            raise SemanticWriteError(
                "RELATION_REVIEW_STABLE_ID_REQUIRED",
                "lifecycle reviewed-none requires one unique stable page identity",
            )
        try:
            requested_decision = relation_review.build_lifecycle_decision(
                page_identity=after.identity,
                after_fingerprint=after.review_fingerprint,
                reason=relation_review_reason or "",
            )
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        after_review = semantic_contract.RelationReviewState(
            "reviewed_none",
            requested_decision.page_identity,
            requested_decision.after_fingerprint,
            reason=requested_decision.reason,
            reference=requested_decision.reference,
        )

    manifest = activation_manifest.load_manifest(root)
    boundary = activation_manifest.plan_activation_boundary(
        before_corpus.activation_census, manifest=manifest
    )
    grandfathered = activation_manifest.is_grandfathered(
        root,
        path,
        source_hash=before.source_hash,
        exomem_id=before.identity if before.identity_kind == "exomem_id" else None,
        manifest=boundary.manifest,
        census=before_corpus.activation_census,
    )
    result = semantic_contract.evaluate(
        before=before,
        after=after,
        operation=operation,
        mode="precommit",
        before_contracts=before_contracts,
        after_contracts=after_contracts,
        before_corpus=before_corpus,
        after_corpus=after_corpus,
        before_review=before_review,
        after_review=after_review,
        grandfathered=grandfathered and before.eligible_compiled,
        include_relation_disposition=applicability == "full",
        language_registry=language,
    )
    return ExistingPreflight(
        applicability,
        operation,
        path,
        before_source,
        after_source,
        before,
        after,
        grandfathered,
        token,
        transition_hash,
        False,
        result,
        before_corpus,
        after_corpus,
        before_contracts,
        after_contracts,
        before_review,
        after_review,
        requested_decision,
        before_corpus.activation_census,
        boundary.manifest,
        boundary.install_required,
        resolver_freshness,
        primary_guard,
        committed_replay,
    )


def _reevaluate_existing(
    preflight: ExistingPreflight,
    *,
    manifest: activation_manifest.ActivationManifest,
) -> tuple[semantic_contract.SemanticContractResult, bool]:
    grandfathered = activation_manifest.is_grandfathered(
        preflight.before_corpus.vault_root,
        preflight.path,
        source_hash=preflight.before.source_hash,
        exomem_id=(
            preflight.before.identity if preflight.before.identity_kind == "exomem_id" else None
        ),
        manifest=manifest,
        census=preflight.activation_census,
    )
    result = semantic_contract.evaluate(
        before=preflight.before,
        after=preflight.after,
        operation=preflight.operation,
        mode="precommit",
        before_contracts=preflight.before_contracts,
        after_contracts=preflight.after_contracts,
        before_corpus=preflight.before_corpus,
        after_corpus=preflight.after_corpus,
        before_review=preflight.before_review,
        after_review=preflight.after_review,
        grandfathered=grandfathered and preflight.before.eligible_compiled,
        include_relation_disposition=preflight.applicability == "full",
        language_registry=semantic_language_registry.load_registry(
            preflight.before_corpus.vault_root
        ),
    )
    return result, grandfathered


def _commit_existing_locked(
    root: Path,
    *,
    preflight: ExistingPreflight,
    auxiliaries: tuple[vault.PlannedWrite, ...],
    result: semantic_contract.SemanticContractResult,
) -> ExistingCommit:
    """Plan lifecycle state and commit while the semantic namespace is held."""
    if preflight.committed_replay:
        if preflight.applicability != "full":
            return ExistingCommit(
                preflight.applicability,
                preflight.operation,
                preflight.path,
                False,
                (),
                result,
                None,
                "committed_replay",
                preflight.transition_token,
            )
        if (
            preflight.after.identity_kind != "exomem_id"
            or preflight.after.review_fingerprint is None
        ):
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "committed replay requires the exact stable resulting identity",
            )
        try:
            prepared = relation_review.load_lifecycle_prepared(root, preflight.after.identity)
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        token_value = _decode_existing_transition_token(preflight.transition_token)
        requested_reference = (
            preflight.requested_decision.reference
            if preflight.requested_decision is not None
            else None
        )
        exact = bool(
            prepared is not None
            and prepared.transition_id == token_value["transition_id"]
            and prepared.operation == preflight.operation
            and prepared.page_identity == preflight.after.identity
            and prepared.before_path == preflight.path
            and prepared.before_source_hash == token_value["before_hash"]
            and prepared.after_path == preflight.path
            and prepared.after_source_hash == preflight.after.source_hash
            and prepared.after_fingerprint == preflight.after.review_fingerprint
            and prepared.decision_reference == requested_reference
            and prepared.transition_token_hash
            == hashlib.sha256(preflight.transition_token.encode("utf-8")).hexdigest()
            and prepared.auxiliary_hash
            == relation_review.lifecycle_auxiliary_hash(auxiliaries, root)
        )
        if not exact:
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "committed replay does not match the prepared transition",
            )
        return ExistingCommit(
            preflight.applicability,
            preflight.operation,
            preflight.path,
            False,
            (),
            result,
            None,
            "committed_replay",
            preflight.transition_token,
        )

    lifecycle_writes: tuple[vault.PlannedWrite, ...] = ()
    required_guards: tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...] = ()
    lifecycle_state: str | None = None
    stable_active = (
        preflight.applicability == "full"
        and preflight.after.identity_kind == "exomem_id"
        and preflight.after.review_fingerprint is not None
        and preflight.after_corpus.identity_census.paths_by_identity.get(preflight.after.identity)
        == (preflight.path,)
    )
    if stable_active:
        try:
            prepared = relation_review.build_lifecycle_prepared_transition(
                transition_id=_existing_transition_id(preflight.transition_token),
                operation=preflight.operation,
                page_identity=preflight.after.identity,
                before_path=preflight.before.path,
                before_source_hash=preflight.before.source_hash,
                after_path=preflight.after.path,
                after_source_hash=preflight.after.source_hash,
                after_fingerprint=preflight.after.review_fingerprint,
                decision=preflight.requested_decision,
                transition_token=preflight.transition_token,
                auxiliary_hash=relation_review.lifecycle_auxiliary_hash(auxiliaries, root),
            )
            current = relation_review.LifecyclePrimaryBinding(
                preflight.before.path,
                preflight.before.source_hash,
                preflight.before.review_fingerprint,
            )
            lifecycle = relation_review.plan_lifecycle_transition(
                root,
                decision=preflight.requested_decision,
                prepared=prepared,
                current=current,
            )
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        lifecycle_state = lifecycle.state
        if lifecycle.state == "committed_replay":
            return ExistingCommit(
                preflight.applicability,
                preflight.operation,
                preflight.path,
                False,
                (),
                result,
                None,
                lifecycle.state,
                preflight.transition_token,
            )
        lifecycle_writes = lifecycle.writes
        required_guards = lifecycle.required_guards

    writes = [*lifecycle_writes, *auxiliaries]
    writes.append(
        vault.PlannedWrite(
            root / preflight.path,
            preflight.after_source,
            guard=preflight.primary_guard,
        )
    )
    reports: list[Any] = []
    written = vault.batch_atomic_write(
        writes,
        vault_root=root,
        required_guards=required_guards,
        index_reports=reports,
        semantic_states={preflight.path: semantic_index.from_semantic_page_state(preflight.after)},
    )
    report = reports[0] if reports else None
    return ExistingCommit(
        preflight.applicability,
        preflight.operation,
        preflight.path,
        True,
        tuple(path.relative_to(root).as_posix() for path in written),
        result,
        report,
        lifecycle_state,
        preflight.transition_token,
    )


def commit_existing(
    vault_root: Path,
    *,
    preflight: ExistingPreflight,
    auxiliary_writes: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite] = (),
) -> ExistingCommit:
    """Commit one preflighted existing-page transition, primary Markdown last."""
    root = Path(vault_root)
    if preflight.contract_result.should_block:
        raise SemanticWriteError(
            "SEMANTIC_CONTRACT_BLOCKED",
            _blocking_reason(preflight.contract_result),
            preflight.contract_result.blocking_findings,
        )

    result = preflight.contract_result
    auxiliaries = tuple(auxiliary_writes)
    if preflight.manifest_install_required:
        winner = activation_manifest.ensure_manifest(root, census=preflight.activation_census)
        result, _ = _reevaluate_existing(preflight, manifest=winner)
        if result.should_block:
            raise SemanticWriteError(
                "SEMANTIC_CONTRACT_BLOCKED",
                _blocking_reason(
                    result,
                    "semantic contract blocked against the activation boundary winner",
                ),
                result.blocking_findings,
            )

    if preflight.resolver_freshness is not None:
        try:
            find_module.prime_resolver_from_entries(
                root,
                preflight.before_corpus.resolver_entries,
                expected_freshness=preflight.resolver_freshness,
            )
        except Exception:  # noqa: BLE001 — rebuildable graph cache never blocks commit
            pass

    try:
        with vault.vault_creation_lock(root, "semantic-creation"):
            return _commit_existing_locked(
                root,
                preflight=preflight,
                auxiliaries=auxiliaries,
                result=result,
            )
    except vault.VaultLockTimeout as error:
        raise SemanticWriteError(
            "SEMANTIC_CREATION_LOCK_TIMEOUT",
            "timed out acquiring semantic creation lock",
        ) from error
    except vault.VaultLockError as error:
        raise SemanticWriteError(error.code, error.reason) from error


def _move_state_map(
    root: Path,
    *,
    before_corpus: semantic_contract.SemanticCorpusContext,
    old_path: str,
    new_path: str,
    source: str,
    moved_source: str,
    rewrites: tuple[vault.PlannedWrite, ...],
    registry: relation_registry.RelationRegistry,
    language: semantic_language_registry.SemanticLanguageRegistry,
) -> tuple[
    semantic_contract.SemanticCorpusContext,
    dict[str, semantic_contract.SemanticPageState],
]:
    states = dict(before_corpus.pages)
    states.pop(old_path, None)
    changed: dict[str, semantic_contract.SemanticPageState] = {}
    moved = semantic_contract.build_page_state(
        root,
        new_path,
        moved_source,
        relation_registry=registry,
        language_registry=language,
    )
    states[new_path] = moved
    changed[new_path] = moved
    for write in rewrites:
        try:
            rel = write.path.relative_to(root).as_posix()
        except ValueError as error:
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "move rewrite is outside the vault",
            ) from error
        rewritten = semantic_contract.build_page_state(
            root,
            rel,
            write.content,
            relation_registry=registry,
            language_registry=language,
        )
        states[rel] = rewritten
        changed[rel] = rewritten

    entries = tuple(
        entry
        for entry in before_corpus.identity_census.entries
        if entry.path not in {old_path, *changed}
    )
    identity_census = semantic_contract.StableIdentityCensus(entries)
    for state in changed.values():
        identity_census = identity_census.with_page(state)
    return (
        semantic_contract.SemanticCorpusContext.from_states(
            root,
            states.values(),
            registry=registry,
            identity_census=identity_census,
        ),
        changed,
    )


def _move_dependency_signature(
    corpus: semantic_contract.SemanticCorpusContext,
    path: str,
) -> tuple[Any, ...]:
    state = corpus.pages[path]

    def fact_signature(fact: semantic_contract.RelationFact) -> tuple[Any, ...]:
        qualification = semantic_contract.qualify_relation(
            fact, registry=corpus.registry, corpus=corpus
        )
        return (
            fact.identity,
            fact.logical_source_path,
            fact.logical_target_path,
            fact.resolved_target_path,
            fact.target_status,
            fact.canonical_relation,
            fact.registry_status,
            fact.target_page_type,
            qualification.qualifies,
            qualification.reasons,
        )

    return (
        state.identity_kind,
        state.identity,
        state.status,
        state.page_type,
        state.projects,
        tuple(fact_signature(fact) for fact in corpus.outbound.get(path, ())),
        tuple(fact_signature(fact) for fact in corpus.inbound.get(path, ())),
    )


def _review_carry_signature(
    corpus: semantic_contract.SemanticCorpusContext,
    path: str,
) -> tuple[Any, ...]:
    state = corpus.pages[path]

    def target_identity(fact: semantic_contract.RelationFact) -> tuple[str, str] | None:
        resolved = (fact.resolved_target_path or "").split("#", 1)[0]
        target = corpus.pages.get(resolved)
        if target is None:
            return None
        return target.identity_kind, target.identity

    def facts(direction: str) -> tuple[tuple[Any, ...], ...]:
        values = (
            corpus.outbound.get(path, ())
            if direction == "outbound"
            else corpus.inbound.get(path, ())
        )
        result: list[tuple[Any, ...]] = []
        for fact in values:
            qualification = semantic_contract.qualify_relation(
                fact, registry=corpus.registry, corpus=corpus
            )
            result.append(
                (
                    direction,
                    fact.canonical_relation,
                    fact.family,
                    fact.registry_status,
                    fact.origin,
                    fact.authored_line,
                    fact.authored_anchor,
                    fact.source_kind,
                    fact.target_page_type,
                    fact.target_status,
                    target_identity(fact),
                    qualification.qualifies,
                    qualification.reasons,
                )
            )
        return tuple(sorted(result, key=repr))

    return (
        state.identity_kind,
        state.identity,
        state.page_type,
        state.projects,
        state.status,
        state.title,
        tuple((unit.kind, unit.category) for unit in state.document.units),
        facts("outbound"),
        facts("inbound"),
    )


def _move_evaluation_pairs(
    *,
    old_path: str,
    new_path: str,
    changed: dict[str, semantic_contract.SemanticPageState],
    before_corpus: semantic_contract.SemanticCorpusContext,
    after_corpus: semantic_contract.SemanticCorpusContext,
) -> tuple[tuple[str, str], ...]:
    """Return the deterministic fixed-point closure affected by a move."""
    pairs: dict[str, str] = {new_path: old_path}
    for path in changed:
        if path != new_path:
            pairs[path] = path

    # Corpus signatures already include authored resolution, identity/status,
    # inbound/outbound qualifying sets, and registry disposition inputs. Iterate
    # to a fixed point so later dependency dimensions can extend this without a
    # one-hop assumption; the hard bound is the finite final corpus.
    for _ in range(len(after_corpus.pages) + 1):
        added = False
        for path in sorted(after_corpus.eligible_compiled_paths):
            if path in pairs or path not in before_corpus.pages:
                continue
            if _move_dependency_signature(before_corpus, path) != _move_dependency_signature(
                after_corpus, path
            ):
                pairs[path] = path
                added = True
        if not added:
            break
    return tuple((pairs[path], path) for path in sorted(pairs))


def preflight_move(
    vault_root: Path,
    *,
    old_path: str,
    new_path: str,
    source: str,
    moved_source: str,
    source_guard: vault.PathGuard,
    destination_guard: vault.PathGuard,
    rewrites: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite] = (),
) -> MovePreflight:
    """Evaluate a move and all exact inbound rewrites against one final corpus."""
    root = Path(vault_root)
    rewrite_tuple = tuple(rewrites)
    if (
        source_guard.target != old_path
        or source_guard.leaf_policy != "content"
        or source_guard.expected_content_hash != vault.content_hash(source)
        or destination_guard.target != new_path
        or destination_guard.leaf_policy != "absent"
    ):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "move guards do not bind the exact source and destination",
        )
    expected_moved_source, _ = rewrite_wikilinks_for_move(source, old_path, new_path)
    if moved_source not in {source, expected_moved_source}:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "moved source is not an exact path-only rewrite",
        )
    for write in rewrite_tuple:
        if write.guard is None or write.guard.leaf_policy != "content":
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "move rewrite is not bound to exact source bytes",
            )

    registry = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    loaded_contracts = memory_schema.load_saved_contracts(root)
    before_corpus = semantic_contract.build_corpus_context(
        root, registry=registry, language_registry=language
    )
    before_moved = before_corpus.pages.get(old_path)
    normalized_source = source.replace("\r\n", "\n").replace("\r", "\n")
    if before_moved is None or before_moved.source_hash not in {
        vault.content_hash(source),
        vault.content_hash(normalized_source),
    }:
        raise SemanticWriteError(
            "STALE_SEMANTIC_WRITE", "move source changed during semantic preflight"
        )
    pure_path_rewrites: set[str] = set()
    for write in rewrite_tuple:
        rel = write.path.relative_to(root).as_posix()
        before_rewrite = before_corpus.pages.get(rel)
        try:
            before_source = write.path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SemanticWriteError(
                "STALE_SEMANTIC_WRITE",
                "inbound page changed during semantic move preflight",
            ) from error
        normalized_before_source = before_source.replace("\r\n", "\n").replace("\r", "\n")
        if (
            before_rewrite is None
            or before_rewrite.source_hash
            not in {
                vault.content_hash(before_source),
                vault.content_hash(normalized_before_source),
            }
            or write.guard.expected_content_hash != vault.content_hash(before_source)
        ):
            raise SemanticWriteError(
                "STALE_SEMANTIC_WRITE",
                "inbound page changed during semantic move preflight",
            )
        expected_source, changed_count = rewrite_wikilinks_for_move(
            before_source, old_path, new_path
        )
        if changed_count and expected_source == write.content:
            pure_path_rewrites.add(rel)

    after_corpus, changed = _move_state_map(
        root,
        before_corpus=before_corpus,
        old_path=old_path,
        new_path=new_path,
        source=source,
        moved_source=moved_source,
        rewrites=rewrite_tuple,
        registry=registry,
        language=language,
    )
    manifest = activation_manifest.load_manifest(root)
    boundary = activation_manifest.plan_activation_boundary(
        before_corpus.activation_census, manifest=manifest
    )
    evaluations: list[MovePageEvaluation] = []
    pairs = _move_evaluation_pairs(
        old_path=old_path,
        new_path=new_path,
        changed=changed,
        before_corpus=before_corpus,
        after_corpus=after_corpus,
    )
    for before_path, after_path in pairs:
        before = before_corpus.pages[before_path]
        after = after_corpus.pages[after_path]
        before_contracts = memory_schema.resolve_contracts(
            loaded_contracts,
            projects=before.projects,
            page_type=before.page_type,
            language_registry=language,
        )
        after_contracts = memory_schema.resolve_contracts(
            loaded_contracts,
            projects=after.projects,
            page_type=after.page_type,
            language_registry=language,
        )
        applicability = _existing_applicability(before, after)
        before_review = None
        after_review = None
        requested_decision: relation_review.LifecycleDecision | None = None
        carried_from: relation_review.LifecycleDecision | None = None
        if applicability == "full":
            try:
                before_review = relation_review.load_relation_review(
                    root, before, corpus=before_corpus
                )
                after_review = relation_review.load_relation_review(
                    root, after, corpus=after_corpus
                )
            except relation_review.RelationReviewError as error:
                raise SemanticWriteError(error.code, error.reason) from error
            can_carry = bool(
                before_path == after_path
                and after_path in pure_path_rewrites
                and before_review is not None
                and before_review.kind == "reviewed_none"
                and before.identity_kind == "exomem_id"
                and before.review_fingerprint is not None
                and after.identity_kind == "exomem_id"
                and after.identity == before.identity
                and after.review_fingerprint is not None
                and _review_carry_signature(before_corpus, before_path)
                == _review_carry_signature(after_corpus, after_path)
            )
            if can_carry and (
                after_review is None
                or not semantic_contract.is_relation_review_current(
                    after_review, after, after_corpus
                )
            ):
                try:
                    carried_from = relation_review.load_lifecycle_decision(
                        root, before.identity, before.review_fingerprint
                    )
                    if carried_from is None or carried_from.reference != before_review.reference:
                        carried_from = None
                    else:
                        requested_decision = relation_review.build_lifecycle_decision(
                            page_identity=after.identity,
                            after_fingerprint=after.review_fingerprint,
                            reason=carried_from.reason,
                        )
                        after_review = semantic_contract.RelationReviewState(
                            "reviewed_none",
                            requested_decision.page_identity,
                            requested_decision.after_fingerprint,
                            reason=requested_decision.reason,
                            reference=requested_decision.reference,
                        )
                except relation_review.RelationReviewError as error:
                    raise SemanticWriteError(error.code, error.reason) from error
        grandfathered = bool(
            before.identity_kind == "exomem_id"
            and before.eligible_compiled
            and activation_manifest.is_grandfathered(
                root,
                before.path,
                source_hash=before.source_hash,
                exomem_id=before.identity,
                manifest=boundary.manifest,
                census=before_corpus.activation_census,
            )
        )
        result = semantic_contract.evaluate(
            before=before,
            after=after,
            operation="move",
            mode="precommit",
            before_contracts=before_contracts,
            after_contracts=after_contracts,
            before_corpus=before_corpus,
            after_corpus=after_corpus,
            before_review=before_review,
            after_review=after_review,
            grandfathered=grandfathered,
            include_relation_disposition=applicability == "full",
            language_registry=language,
        )
        token = _existing_transition_token(
            operation="move",
            path=before.path,
            before_hash=before.source_hash,
            after_hash=after.source_hash,
        )
        evaluations.append(
            MovePageEvaluation(
                before,
                after,
                before_contracts,
                after_contracts,
                before_review,
                after_review,
                grandfathered,
                token,
                result,
                requested_decision,
                carried_from,
            )
        )
    return MovePreflight(
        old_path,
        new_path,
        source,
        moved_source,
        rewrite_tuple,
        tuple(evaluations),
        before_corpus,
        after_corpus,
        before_corpus.activation_census,
        boundary.manifest,
        boundary.install_required,
        source_guard,
        destination_guard,
    )


def _plan_move_lifecycle(
    root: Path,
    preflight: MovePreflight,
) -> tuple[
    tuple[vault.PlannedWrite, ...],
    tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...],
    tuple[tuple[str, str], ...],
]:
    writes: list[vault.PlannedWrite] = []
    guards: list[vault.PathGuard | vault.DirectoryCensusGuard] = []
    states: list[tuple[str, str]] = []
    auxiliaries = list(preflight.rewrites)
    if preflight.moved_source != preflight.source:
        auxiliaries.append(vault.PlannedWrite(root / preflight.new_path, preflight.moved_source))
    auxiliary_hash = relation_review.lifecycle_auxiliary_hash(auxiliaries, root)
    for item in preflight.evaluations:
        if (
            item.before.path == item.after.path
            and item.before.source_hash == item.after.source_hash
            and item.before.review_fingerprint == item.after.review_fingerprint
        ):
            continue
        stable_active = bool(
            item.after.eligible_compiled
            and item.after.identity_kind == "exomem_id"
            and item.after.review_fingerprint is not None
            and preflight.after_corpus.identity_census.paths_by_identity.get(item.after.identity)
            == (item.after.path,)
        )
        if not stable_active:
            continue
        try:
            prepared = relation_review.build_lifecycle_prepared_transition(
                transition_id=_existing_transition_id(item.transition_token),
                operation="move",
                page_identity=item.after.identity,
                before_path=item.before.path,
                before_source_hash=item.before.source_hash,
                after_path=item.after.path,
                after_source_hash=item.after.source_hash,
                after_fingerprint=item.after.review_fingerprint,
                decision=item.requested_decision,
                transition_token=item.transition_token,
                auxiliary_hash=auxiliary_hash,
                carried_from=item.carried_from,
            )
            plan = relation_review.plan_lifecycle_transition(
                root,
                decision=item.requested_decision,
                prepared=prepared,
                current=relation_review.LifecyclePrimaryBinding(
                    item.before.path,
                    item.before.source_hash,
                    item.before.review_fingerprint,
                ),
            )
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        writes.extend(plan.writes)
        guards.extend(plan.required_guards)
        states.append((item.after.identity, plan.state))
    return tuple(writes), tuple(guards), tuple(states)


def commit_move(
    vault_root: Path,
    *,
    preflight: MovePreflight,
    mutate: MoveMutation,
) -> MoveCommit:
    """Commit a preflighted move while holding the shared semantic namespace."""
    root = Path(vault_root)
    if preflight.should_block:
        raise SemanticWriteError(
            "SEMANTIC_CONTRACT_BLOCKED",
            _blocking_reason_for_evaluations(preflight.evaluations),
            tuple(
                finding
                for item in preflight.evaluations
                for finding in item.contract_result.blocking_findings
            ),
        )
    if preflight.manifest_install_required:
        winner = activation_manifest.ensure_manifest(root, census=preflight.activation_census)
        if winner != preflight.prospective_manifest:
            raise SemanticWriteError(
                "SEMANTIC_CONTRACT_BLOCKED",
                "semantic move must be retried against the activation boundary winner",
            )
    try:
        with vault.vault_creation_lock(root, "semantic-creation"):
            preflight.source_guard.recheck(root)
            destination_guard = preflight.destination_guard.prepare_and_bind_parents(root)
            preflight.source_guard.recheck(root)
            lifecycle_writes, required_guards, lifecycle_states = _plan_move_lifecycle(
                root, preflight
            )
            mutate(lifecycle_writes, required_guards, destination_guard)
    except vault.VaultLockTimeout as error:
        raise SemanticWriteError(
            "SEMANTIC_CREATION_LOCK_TIMEOUT",
            "timed out acquiring semantic creation lock",
        ) from error
    except vault.VaultLockError as error:
        raise SemanticWriteError(error.code, error.reason) from error
    return MoveCommit(preflight, lifecycle_states)


def _corpus_with_recovery_states(
    root: Path,
    base: semantic_contract.SemanticCorpusContext,
    states: list[semantic_contract.SemanticPageState],
    *,
    registry: relation_registry.RelationRegistry,
    removed_paths: frozenset[str] = frozenset(),
) -> semantic_contract.SemanticCorpusContext:
    pages = dict(base.pages)
    identity_census = semantic_contract.StableIdentityCensus(
        tuple(entry for entry in base.identity_census.entries if entry.path not in removed_paths)
    )
    for state in states:
        pages[state.path] = state
        identity_census = identity_census.with_page(state)
    return semantic_contract.SemanticCorpusContext.from_states(
        root,
        pages.values(),
        registry=registry,
        identity_census=identity_census,
    )


def preflight_recovery(
    vault_root: Path,
    *,
    entries: tuple[RecoveryEntry, ...] | list[RecoveryEntry],
    destination_root_guard: vault.PathGuard | None = None,
    trash_census_guards: tuple[vault.DirectoryCensusGuard, ...] = (),
    recovery_sidecar_guard: vault.PathGuard | None = None,
    relation_reviews: Mapping[str, Mapping[str, str]] | None = None,
) -> RecoveryPreflight:
    """Evaluate exact trashed Markdown bytes at all final restore paths."""
    root = Path(vault_root)
    review_mapping = relation_reviews or {}
    if not isinstance(review_mapping, Mapping) or len(review_mapping) > _RECOVERY_REVIEW_LIMIT:
        raise SemanticWriteError(
            "INVALID_RELATION_REVIEW",
            "recovery relation reviews must be a bounded identity mapping",
        )
    consumed_reviews: set[str] = set()
    bound_entries = tuple(entries)
    if not bound_entries:
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "recovery semantic preflight requires at least one Markdown entry",
        )
    if len({item.restore_path for item in bound_entries}) != len(bound_entries):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "recovery contains duplicate restore paths",
        )
    root_destination = destination_root_guard or bound_entries[0].destination_guard
    if root_destination.leaf_policy != "absent":
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_MISMATCH",
            "recovery root destination guard must bind absence",
        )
    root_prefix = f"{root_destination.target.rstrip('/')}/"
    for item in bound_entries:
        if (
            item.source_guard.target != item.trash_path
            or item.source_guard.leaf_policy != "content"
            or item.source_guard.expected_content_hash != vault.content_hash(item.source)
            or item.destination_guard.target != item.restore_path
            or item.destination_guard.leaf_policy != "absent"
            or (item.sidecar_guard is not None and item.sidecar_guard.leaf_policy != "content")
            or (item.sidecar_guard is None) != (item.sidecar_source is None)
            or (
                item.restore_path != root_destination.target
                and not item.restore_path.startswith(root_prefix)
            )
        ):
            raise SemanticWriteError(
                "LIFECYCLE_TRANSITION_MISMATCH",
                "recovery guards do not bind exact trash and destination state",
            )

    registry = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    loaded_contracts = memory_schema.load_saved_contracts(root)
    before_corpus = semantic_contract.build_corpus_context(
        root, registry=registry, language_registry=language
    )
    prior_states: list[semantic_contract.SemanticPageState] = []
    after_states: list[semantic_contract.SemanticPageState] = []
    for item in bound_entries:
        prior_states.append(
            semantic_contract.build_page_state(
                root,
                item.original_path,
                item.source,
                relation_registry=registry,
                language_registry=language,
            )
        )
        after_states.append(
            semantic_contract.build_page_state(
                root,
                item.restore_path,
                item.source,
                relation_registry=registry,
                language_registry=language,
            )
        )
    prior_corpus = _corpus_with_recovery_states(
        root,
        before_corpus,
        prior_states,
        registry=registry,
        removed_paths=frozenset(item.trash_path for item in bound_entries),
    )
    after_corpus = _corpus_with_recovery_states(
        root,
        before_corpus,
        after_states,
        registry=registry,
        removed_paths=frozenset(item.trash_path for item in bound_entries),
    )
    manifest = activation_manifest.load_manifest(root)
    evaluations: list[RecoveryPageEvaluation] = []
    for entry, before, after in zip(bound_entries, prior_states, after_states, strict=True):
        before_contracts = memory_schema.resolve_contracts(
            loaded_contracts,
            projects=before.projects,
            page_type=before.page_type,
            language_registry=language,
        )
        after_contracts = memory_schema.resolve_contracts(
            loaded_contracts,
            projects=after.projects,
            page_type=after.page_type,
            language_registry=language,
        )
        applicability = _existing_applicability(before, after)
        before_review = None
        after_review = None
        if applicability == "full":
            try:
                before_review = relation_review.load_relation_review(
                    root, before, corpus=prior_corpus
                )
                after_review = relation_review.load_relation_review(
                    root, after, corpus=after_corpus
                )
            except relation_review.RelationReviewError as error:
                raise SemanticWriteError(error.code, error.reason) from error
        requested_decision: relation_review.LifecycleDecision | None = None
        requested_review = review_mapping.get(after.identity)
        supplied_token = (
            requested_review.get("transition_token")
            if isinstance(requested_review, Mapping)
            else None
        )
        token = supplied_token or _existing_transition_token(
            operation="recover",
            path=_recovery_token_path(entry.trash_path, entry.restore_path),
            before_hash=before.source_hash,
            after_hash=after.source_hash,
        )
        grandfathered = bool(
            after.identity_kind == "exomem_id"
            and after.eligible_compiled
            and activation_manifest.is_grandfathered(
                root,
                after.path,
                source_hash=after.source_hash,
                exomem_id=after.identity,
                manifest=manifest,
                census=after_corpus.activation_census,
            )
        )
        baseline_result = semantic_contract.evaluate(
            before=before,
            after=after,
            operation="recover",
            mode="precommit",
            before_contracts=before_contracts,
            after_contracts=after_contracts,
            before_corpus=prior_corpus,
            after_corpus=after_corpus,
            before_review=before_review,
            after_review=after_review,
            grandfathered=grandfathered,
            include_relation_disposition=applicability == "full",
            language_registry=language,
        )
        if requested_review is not None:
            expected_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
            token_value = _decode_existing_transition_token(token)
            if (
                applicability != "full"
                or after_review is not None
                or not _recovery_result_reviewable(baseline_result)
                or after.identity_kind != "exomem_id"
                or after.review_fingerprint is None
                or after_corpus.identity_census.paths_by_identity.get(after.identity)
                != (after.path,)
                or not isinstance(requested_review, Mapping)
                or set(requested_review) != {"transition_token", "transition_hash", "reason"}
                or token_value["operation"] != "recover"
                or token_value["path"] != _recovery_token_path(entry.trash_path, entry.restore_path)
                or token_value["before_hash"] != before.source_hash
                or token_value["after_hash"] != after.source_hash
                or requested_review.get("transition_hash") != expected_hash
            ):
                raise SemanticWriteError(
                    "LIFECYCLE_TRANSITION_REVIEW_MISMATCH",
                    "recovery review mapping does not match the validated transition",
                )
            try:
                requested_decision = relation_review.build_lifecycle_decision(
                    page_identity=after.identity,
                    after_fingerprint=after.review_fingerprint,
                    reason=requested_review.get("reason", ""),
                )
            except relation_review.RelationReviewError as error:
                raise SemanticWriteError(error.code, error.reason) from error
            after_review = semantic_contract.RelationReviewState(
                "reviewed_none",
                requested_decision.page_identity,
                requested_decision.after_fingerprint,
                reason=requested_decision.reason,
                reference=requested_decision.reference,
            )
            consumed_reviews.add(after.identity)
        result = baseline_result
        if requested_decision is not None:
            result = semantic_contract.evaluate(
                before=before,
                after=after,
                operation="recover",
                mode="precommit",
                before_contracts=before_contracts,
                after_contracts=after_contracts,
                before_corpus=prior_corpus,
                after_corpus=after_corpus,
                before_review=before_review,
                after_review=after_review,
                grandfathered=grandfathered,
                include_relation_disposition=True,
                language_registry=language,
            )
        evaluations.append(
            RecoveryPageEvaluation(
                entry,
                before,
                after,
                before_contracts,
                after_contracts,
                before_review,
                after_review,
                grandfathered,
                token,
                result,
                requested_decision,
            )
        )
    if consumed_reviews != set(review_mapping):
        raise SemanticWriteError(
            "LIFECYCLE_TRANSITION_REVIEW_MISMATCH",
            "recovery review mapping contains an unvalidated page identity",
        )
    reviewable_count = sum(
        item.requested_decision is None and _recovery_result_reviewable(item.contract_result)
        for item in evaluations
    )
    if reviewable_count > _RECOVERY_REVIEW_LIMIT:
        raise SemanticWriteError(
            "RELATION_REVIEW_HISTORY_LIMIT",
            "recovery requires too many reviewed-none decisions for one batch",
        )
    preflight = RecoveryPreflight(
        bound_entries,
        tuple(evaluations),
        before_corpus,
        prior_corpus,
        after_corpus,
        root_destination,
        tuple(trash_census_guards),
        recovery_sidecar_guard,
    )
    preflight.as_dict()
    return preflight


def _plan_recovery_lifecycle(
    root: Path, preflight: RecoveryPreflight
) -> tuple[
    tuple[vault.PlannedWrite, ...],
    tuple[vault.PathGuard | vault.DirectoryCensusGuard, ...],
    tuple[tuple[str, str], ...],
]:
    writes: list[vault.PlannedWrite] = []
    guards: list[vault.PathGuard | vault.DirectoryCensusGuard] = []
    states: list[tuple[str, str]] = []
    auxiliary_hash = relation_review.lifecycle_auxiliary_hash((), root)
    for item in preflight.evaluations:
        stable_active = bool(
            item.after.eligible_compiled
            and item.after.identity_kind == "exomem_id"
            and item.after.review_fingerprint is not None
            and preflight.after_corpus.identity_census.paths_by_identity.get(item.after.identity)
            == (item.after.path,)
        )
        if not stable_active:
            continue
        try:
            prepared = relation_review.build_lifecycle_prepared_transition(
                transition_id=_existing_transition_id(item.transition_token),
                operation="recover",
                page_identity=item.after.identity,
                before_path=item.entry.trash_path,
                before_source_hash=item.before.source_hash,
                after_path=item.after.path,
                after_source_hash=item.after.source_hash,
                after_fingerprint=item.after.review_fingerprint,
                decision=item.requested_decision,
                transition_token=item.transition_token,
                auxiliary_hash=auxiliary_hash,
            )
            trash_proof = None
            if item.entry.sidecar_guard is not None:
                assert item.entry.sidecar_source is not None
                live_owner_paths = tuple(
                    path
                    for path in preflight.before_corpus.identity_census.paths_by_identity.get(
                        item.after.identity, ()
                    )
                    if not path.startswith(f"{kb_prefix()}_trash/")
                )
                trash_proof = relation_review.LifecycleTrashProof(
                    page_identity=item.after.identity,
                    original_path=item.entry.original_path,
                    trash_path=item.entry.trash_path,
                    source_hash=item.before.source_hash,
                    review_fingerprint=item.before.review_fingerprint,
                    source_guard=item.entry.source_guard,
                    sidecar_source=item.entry.sidecar_source,
                    sidecar_guard=item.entry.sidecar_guard,
                    live_owner_paths=live_owner_paths,
                )
            plan = relation_review.plan_lifecycle_transition(
                root,
                decision=item.requested_decision,
                prepared=prepared,
                current=relation_review.LifecyclePrimaryBinding(
                    item.entry.trash_path,
                    item.before.source_hash,
                    item.before.review_fingerprint,
                ),
                trashed_committed=trash_proof,
            )
        except relation_review.RelationReviewError as error:
            raise SemanticWriteError(error.code, error.reason) from error
        writes.extend(plan.writes)
        guards.extend(plan.required_guards)
        states.append((item.after.identity, plan.state))
    return tuple(writes), tuple(guards), tuple(states)


def commit_recovery(
    vault_root: Path,
    *,
    preflight: RecoveryPreflight,
    mutate: RecoveryMutation,
) -> RecoveryCommit:
    """Prepare lifecycle auxiliaries, then perform one exact restore mutation."""
    root = Path(vault_root)
    try:
        with vault.vault_creation_lock(root, "semantic-creation"):
            for census_guard in preflight.trash_census_guards:
                census_guard.recheck(root)
            if preflight.recovery_sidecar_guard is not None:
                preflight.recovery_sidecar_guard.recheck(root)
            for item in preflight.entries:
                item.source_guard.recheck(root)
                if item.sidecar_guard is not None:
                    item.sidecar_guard.recheck(root)
                item.destination_guard.recheck(root)
            lifecycle_writes, required_guards, lifecycle_states = _plan_recovery_lifecycle(
                root, preflight
            )
            if preflight.should_block:
                raise SemanticWriteError(
                    "SEMANTIC_CONTRACT_BLOCKED",
                    _blocking_reason_for_evaluations(preflight.evaluations),
                    tuple(
                        finding
                        for item in preflight.evaluations
                        for finding in item.contract_result.blocking_findings
                    ),
                )
            destination_root_guard = preflight.destination_root_guard.prepare_and_bind_parents(root)
            destination_root_guard.recheck(root)
            destination_guards = tuple(
                vault.PathGuard.capture(root, item.restore_path, leaf_policy="absent")
                for item in preflight.entries
            )
            destination_root_guard.recheck(root)
            for item, destination_guard in zip(preflight.entries, destination_guards, strict=True):
                item.source_guard.recheck(root)
                destination_guard.recheck(root)
                if item.sidecar_guard is not None:
                    item.sidecar_guard.recheck(root)
            for census_guard in preflight.trash_census_guards:
                census_guard.recheck(root)
            if preflight.recovery_sidecar_guard is not None:
                preflight.recovery_sidecar_guard.recheck(root)
            if lifecycle_writes:
                vault.batch_atomic_write(
                    lifecycle_writes,
                    vault_root=root,
                    required_guards=(*required_guards, destination_root_guard),
                )
            destination_root_guard.recheck(root)
            for destination_guard in destination_guards:
                destination_guard.recheck(root)
            mutate()
    except vault.VaultLockTimeout as error:
        raise SemanticWriteError(
            "SEMANTIC_CREATION_LOCK_TIMEOUT",
            "timed out acquiring semantic creation lock",
        ) from error
    except vault.VaultLockError as error:
        raise SemanticWriteError(error.code, error.reason) from error
    return RecoveryCommit(preflight, lifecycle_states)


def _evaluate_structural(
    root: Path,
    *,
    destination: str,
    source: str,
    operation: str,
) -> tuple[
    semantic_contract.SemanticContractResult,
    semantic_contract.SemanticPageState,
]:
    registry = relation_registry.load_registry(root)
    language = semantic_language_registry.load_registry(root)
    contracts = memory_schema.load_saved_contracts(root)
    before = semantic_contract.build_corpus_context(
        root, registry=registry, language_registry=language
    )
    candidate = semantic_contract.build_page_state(
        root,
        destination,
        source,
        relation_registry=registry,
        language_registry=language,
    )
    resolved = memory_schema.resolve_contracts(
        contracts,
        projects=candidate.projects,
        page_type=candidate.page_type,
        language_registry=language,
    )
    return (
        semantic_contract.evaluate(
            before=None,
            after=candidate,
            operation=operation,
            mode="precommit",
            before_contracts=resolved,
            after_contracts=resolved,
            before_corpus=before,
            after_corpus=before.with_candidate(candidate),
            include_relation_disposition=semantic_contract.requires_semantic_unit(candidate),
            language_registry=language,
        ),
        candidate,
    )


def preflight_creation(
    vault_root: Path,
    *,
    path: str,
    source: str,
    operation: str,
    writer: str,
    draft_id: str | None,
    draft_token: str,
    registrations: tuple[DraftRegistration, ...] = (),
    relation_disposition: str | None = None,
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> CreationPreflight:
    root = Path(vault_root)
    if relation_disposition not in {None, "reviewed_none"}:
        raise SemanticWriteError("INVALID_RELATION_REVIEW", "relation disposition is invalid")
    token = DraftToken.decode(draft_token)
    if (
        token.writer != writer
        or token.operation != operation
        or token.destination != path
        or token.registrations != registrations
    ):
        raise SemanticWriteError("INVALID_DRAFT_TOKEN", "draft token does not match this creation")
    try:
        vault.parse_frontmatter(source, strict=True)
    except vault.FrontmatterError as error:
        raise SemanticWriteError(error.code, "draft frontmatter is invalid") from error
    result, state = _evaluate_structural(root, destination=path, source=source, operation=operation)
    if semantic_contract.requires_semantic_unit(state):
        if draft_id is None:
            raise SemanticWriteError("DRAFT_IDENTITY_MISMATCH", "active draft requires identity")
        validation = relation_review.revalidate_prepared_creation_draft(
            root,
            path=path,
            source=source,
            draft_id=draft_id,
            operation=operation,
            draft_token=draft_token,
            requested_disposition=relation_disposition,
            predecessor_path=predecessor_path,
            predecessor_content_hash=predecessor_content_hash,
        )
        return CreationPreflight(
            "full",
            path,
            source,
            draft_id,
            draft_token,
            False,
            validation.contract_result,
            validation,
            state,
        )
    applicability: Literal["structural", "not_semantic"] = (
        "structural"
        if state.page_type is not None or semantic_contract.compiled_intent(state)
        else "not_semantic"
    )
    return CreationPreflight(
        applicability, path, source, draft_id, draft_token, False, result, None, state
    )


def commit_creation(
    vault_root: Path,
    *,
    preflight: CreationPreflight,
    auxiliary_writes: tuple[vault.PlannedWrite, ...] | list[vault.PlannedWrite] = (),
    relation_disposition: str | None = None,
    relation_review_hash: str | None = None,
    relation_review_reason: str | None = None,
    operation: str,
    predecessor_path: str | None = None,
    predecessor_content_hash: str | None = None,
) -> CreationCommit:
    root = Path(vault_root)
    non_review_blockers = tuple(
        finding
        for finding in preflight.contract_result.blocking_findings
        if finding.resolved_rule != ("relations", "*", "disposition")
    )
    if non_review_blockers or (
        preflight.applicability != "full" and preflight.contract_result.should_block
    ):
        raise SemanticWriteError(
            "SEMANTIC_CONTRACT_BLOCKED",
            _blocking_reason(preflight.contract_result),
            preflight.contract_result.blocking_findings,
        )
    if preflight.applicability == "full":
        assert preflight.draft_id is not None
        committed = relation_review.commit_creation_draft(
            root,
            path=preflight.destination,
            source=preflight.source,
            draft_id=preflight.draft_id,
            operation=operation,
            relation_disposition=relation_disposition,
            relation_review_hash=relation_review_hash,
            relation_review_reason=relation_review_reason,
            auxiliary_writes=auxiliary_writes,
            draft_token=preflight.draft_token,
            predecessor_path=predecessor_path,
            predecessor_content_hash=predecessor_content_hash,
            semantic_state=semantic_index.from_semantic_page_state(preflight.semantic_state),
        )
        return CreationCommit(
            "full", True, committed.written_paths, committed.contract_result, committed
        )
    writes = [*auxiliary_writes]
    writes.append(
        vault.PlannedWrite(
            root / preflight.destination,
            preflight.source,
            create_only=True,
            guard=vault.PathGuard.capture(root, preflight.destination, leaf_policy="absent"),
        )
    )
    token = semantic_index.set_parent_states(
        {preflight.destination: semantic_index.from_semantic_page_state(preflight.semantic_state)}
    )
    try:
        written = vault.batch_atomic_write(writes, vault_root=root)
    finally:
        semantic_index.reset_parent_states(token)
    paths = tuple(path.relative_to(root).as_posix() for path in written)
    return CreationCommit(
        preflight.applicability,
        True,
        paths,
        preflight.contract_result,
        None,
    )
