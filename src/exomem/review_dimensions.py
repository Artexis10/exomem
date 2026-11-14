"""Request-local dimension truth for explicit subjects supplied by review producers."""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import (
    epistemic_dimensions,
    lexstore,
    relation_registry,
    request_budget,
    semantic_labels,
    semantic_language_registry,
    structured_filters,
)
from .cli_ops import OpError
from .governance import egress
from .vault_store import read as store_read
from .vocabulary.registry import RegistryError


@dataclass(frozen=True, slots=True)
class ReviewTarget:
    path: str | None
    unit_ref: str | None = None
    available: bool = True


@dataclass(frozen=True, slots=True)
class Classification:
    """Filter truth is independent of optional annotation completeness."""

    status: str
    matches: bool | None = None
    dimensions: tuple[str, ...] = ()
    dependency: str | None = None

    def fields(self) -> dict[str, Any]:
        value: dict[str, Any] = {"dimension_status": self.status}
        if self.status == "known":
            value["dimensions"] = list(self.dimensions)
        elif self.dependency is not None:
            value["dimension_diagnostics"] = {"dependency": self.dependency}
        return value


def _require_dimension_fields(node: structured_filters.FilterNode | None) -> None:
    if isinstance(node, structured_filters.Predicate):
        # This review surface owns one closed filter field; retrieval owns the other fields.
        if node.field.name != "unit.dimension":
            raise structured_filters.FilterError(
                "UNSUPPORTED_FILTER_FIELD", node.field.path,
                "review filters support unit.dimension and Boolean composition only",
                expected="unit.dimension", remediation="Remove the other filter fields.",
            )
    elif isinstance(node, structured_filters.Not):
        _require_dimension_fields(node.child)
    elif node is not None:
        for child in node.children:
            _require_dimension_fields(child)


class ReviewDimensions:
    def __init__(self, root: Path, filters: Mapping[str, Any] | None = None):
        self.root = Path(root)
        self.language = None

        def resolve(value: str) -> str:
            if self.language is None:
                self.language = semantic_language_registry.load_registry(self.root)
            key = self.language.resolve_label(semantic_labels.DIMENSIONS, value)
            if key is None:
                raise structured_filters.FilterError(
                    "INVALID_FILTER_VALUE", "$.unit.dimension", f"unknown dimension {value!r}",
                    expected="a dimension key or label", remediation="Use a registered dimension.",
                )
            return key

        self.plan = structured_filters.compile_filter(filters, resolve_dimension=resolve)
        _require_dimension_fields(self.plan.root)

    @property
    def filtered(self) -> bool:
        return self.plan.root is not None

    def keep(self, result: Classification | None) -> bool:
        return result is not None and (not self.filtered or result.matches is True)

    def classify(self, targets: Sequence[ReviewTarget]) -> list[Classification | None]:
        results: list[Classification | None] = [
            Classification("unknown", dependency="target_address") if not target.available
            else Classification("not_applicable", False) if target.path is None
            else Classification("unknown", dependency="selected_structure")
            for target in targets
        ]
        if not any(target.path is not None and target.available for target in targets):
            return results
        # Optional annotations never refuse a queue; only a consumed unknown dependency prevents a filter match.
        with store_read.request_snapshot(self.root):
            keep = egress.page_release_filter(self.root)
            for start in range(0, len(targets), store_read.KEYS_PER_QUERY):
                batch = targets[start:start + store_read.KEYS_PER_QUERY]
                with contextlib.ExitStack() as scope:
                    borrowed = store_read.lend(self.root) if lexstore.store_rows_serve(self.root) else None
                    if borrowed is not None:
                        scope.callback(borrowed.close)
                        snapshot = store_read.snapshot_for_connection(borrowed)
                        if snapshot is not None:
                            scope.enter_context(snapshot.prefetch(
                                target.path for target in batch if target.path and target.available
                            ))
                    paths = set()
                    for offset, target in enumerate(batch, start):
                        if target.path is None or not target.available:
                            continue
                        if keep is not None and not keep(target.path):
                            results[offset] = None
                        else:
                            paths.add(target.path)
                    if not paths:
                        continue
                    budget = request_budget.current()
                    if budget is not None and budget.remaining() <= 0:
                        for offset, target in enumerate(batch, start):
                            if target.path in paths and target.available:
                                results[offset] = Classification("unknown", dependency="deadline")
                        continue
                    try:
                        metadata = lexstore.eligibility_metadata_result(
                            self.root, paths, scope="vault", freshness=None, include_structure=True,
                        )
                        if not metadata.readiness.complete:
                            continue
                        parents = {
                            path: row.selected_parent for path, row in (metadata.value or {}).items()
                            if path in paths and row.selected_parent is not None
                            and row.selected_parent.definitions is not None and row.selected_parent.structure.complete
                        }
                        prepared = {}
                        for offset, target in enumerate(batch, start):
                            parent = parents.get(target.path)
                            if results[offset] is None or parent is None or not target.available:
                                continue
                            units = parent.structure.units
                            if target.unit_ref is not None:
                                resolved = parent.structure.resolve_unit(target.unit_ref)
                                if resolved.status != "found" or resolved.index is None:
                                    results[offset] = Classification("unknown", dependency="target_address")
                                    continue
                                units = (units[resolved.index],)
                            views = tuple({
                                "kind": unit.kind, "instance": parent.instance_id, "form": unit.form,
                                **structured_filters.membership_facts(unit, occurrence=unit.occurrence_key),
                            } for unit in units)
                            if not views:
                                results[offset] = Classification("known", False)
                                continue
                            prepared[offset] = ({"path": target.path}, views)
                        if not prepared:
                            continue
                        language = self.language or semantic_language_registry.load_registry(self.root)
                        self.language = language
                        relations = relation_registry.load_registry(self.root)
                        filtered = epistemic_dimensions.membership(
                            language, relations, only=structured_filters.named_dimensions(self.plan),
                        )
                        full = epistemic_dimensions.membership(language, relations)
                        needed_paths = {page["path"] for page, _views in prepared.values()}
                        # Required families come first; optional annotation work cannot demote exact filter truth.
                        required = self._edges(filtered, needed_paths, keep) if self.filtered and filtered.needs_edges else (
                            epistemic_dimensions.DimensionEdges("available")
                        )
                        filtered_source = epistemic_dimensions.UnitDimensions.from_edges(filtered, required.edges)
                        for offset, (page, views) in prepared.items():
                            matches = None
                            if self.filtered and required.status == "available":
                                matches = structured_filters.page_matches(
                                    self.plan.with_dimensions(filtered_source), page=page, units=views,
                                )
                            results[offset] = Classification("unknown", matches, dependency="dimension_edges")
                        if required.status != "available":
                            continue
                        remaining = replace(full, by_family={
                            family: members for family, members in full.by_family.items()
                            if not self.filtered or family not in filtered.by_family
                        })
                        additional = self._edges(remaining, needed_paths, keep)
                        if additional.status != "available":
                            continue
                        full_source = epistemic_dimensions.UnitDimensions.from_edges(full, required.edges + additional.edges)
                        for offset, (page, views) in prepared.items():
                            dimensions = {key for unit in views for key in full_source(
                                page, unit, own_kind=not structured_filters.foreign_entry(
                                    "unit.kind", unit["kind"], unit, self.plan.unit_instance,
                                ),
                            )}
                            results[offset] = Classification("known", results[offset].matches, tuple(sorted(dimensions)))
                    except (OpError, RegistryError, sqlite3.Error, TimeoutError, request_budget.ReadBudgetExhausted):
                        continue
        return results

    def select(self, items: Sequence[Mapping[str, Any]], targets: Sequence[ReviewTarget], *, unsearched=0):
        results = self.classify(targets)
        return (
            [dict(item, **result.fields()) for item, result in zip(items, results, strict=True) if self.keep(result)],
            self.coverage(results, unsearched=unsearched),
        )

    def _edges(self, lookups, paths, keep):
        check = request_budget.current_checkpoint()
        budget = request_budget.current()
        if check is None and budget is not None:
            check = request_budget.ReadCheckpoint(budget)
        try:
            return epistemic_dimensions.read_edges(self.root, lookups, pages=paths, admission=keep, check=check)
        except (OpError, RegistryError, sqlite3.Error, TimeoutError, request_budget.ReadBudgetExhausted):
            return epistemic_dimensions.DimensionEdges("unavailable")

    def coverage(self, results: Sequence[Classification | None], *, unsearched: int | None = 0) -> dict[str, Any]:
        admitted = [result for result in results if result is not None]
        unresolved = sum(result.matches is None for result in admitted) if self.filtered else 0
        annotation_unknown = sum(result.status == "unknown" for result in admitted)
        return {
            "unit": "review_items",
            "window": len(admitted), "unsearched": unsearched,
            "filter_matches": sum(result.matches is True for result in admitted) if self.filtered else None,
            "filter_unresolved": unresolved if self.filtered else None,
            "annotation_known": sum(result.status == "known" for result in admitted),
            "annotation_unknown": annotation_unknown,
            "not_applicable": sum(result.status == "not_applicable" for result in admitted),
            "filter_complete": unsearched == 0 and unresolved == 0 if self.filtered else None,
            "annotation_complete": unsearched == 0 and annotation_unknown == 0,
            "complete": unsearched == 0 and unresolved == 0 and annotation_unknown == 0,
        }
