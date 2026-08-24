"""Request-local selection and lexical search over authorization projections.

Persistent projection rows are deliberately principal-free.  A request supplies
one exact, total selector over the immutable catalog; only the selected non-L0
variants form the lexical corpus.  Hidden rows therefore cannot affect document
frequency, ranking, snippets, or candidate caps.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from numbers import Real
from types import MappingProxyType

from .. import bm25
from . import projection_store, projections

_HEX = frozenset("0123456789abcdef")
_BM25_K1 = 1.5
_BM25_B = 0.75
_MAX_RESULTS = projections.MAX_GOVERNED_CATALOG_ITEMS
_SNIPPET_CHARS = 320


class ProjectedRetrievalUnavailable(RuntimeError):
    """The authorized projection corpus is incomplete or internally inconsistent."""


class ProjectedLaneUnavailable(ProjectedRetrievalUnavailable):
    """One selected projected measurement lane is incomplete or incompatible."""


def _bounded_text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be bounded non-empty text"
        )
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise projections.ProjectionCanonicalizationError(
            f"{name} contains an invalid Unicode scalar"
        ) from error
    return value


def _digest(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in _HEX for character in value)
    ):
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be one lowercase SHA-256 digest"
        )
    return value


def _result_limit(value: object) -> int:
    if type(value) is not int or not 1 <= value <= _MAX_RESULTS:
        raise ValueError(f"k must be an integer from 1 through {_MAX_RESULTS}")
    return value


def _sort_key(value: str) -> bytes:
    return value.encode("utf-16-be")


@dataclass(frozen=True, slots=True)
class ProjectionSelection:
    """One request-local L0 or immutable projection-variant selection."""

    item_identity: str
    content_hash: str
    projection_variant_id: str | None
    decision: object | None = None

    def __post_init__(self) -> None:
        _bounded_text(self.item_identity, "item identity", maximum=4096)
        _digest(self.content_hash, "content hash")
        if self.projection_variant_id is not None:
            _digest(self.projection_variant_id, "projection variant id")
        if self.decision is not None:
            from .decisions import Decision

            if not isinstance(self.decision, Decision):
                raise projections.ProjectionCanonicalizationError(
                    "projection selection decision is invalid"
                )


@dataclass(frozen=True, slots=True)
class AuthorizationProjectionMap:
    """An immutable request-local selector for one exact projection namespace."""

    namespace_key: projections.ProjectionNamespaceKey
    selections: tuple[ProjectionSelection, ...]
    withheld_identities: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if not isinstance(self.namespace_key, projections.ProjectionNamespaceKey):
            raise projections.ProjectionCanonicalizationError(
                "authorization map namespace key is invalid"
            )
        if not isinstance(self.selections, tuple):
            raise projections.ProjectionCanonicalizationError(
                "authorization map selections must be an immutable tuple"
            )
        by_identity: dict[str, ProjectionSelection] = {}
        for selection in self.selections:
            if not isinstance(selection, ProjectionSelection):
                raise projections.ProjectionCanonicalizationError(
                    "authorization map selection has an invalid type"
                )
            if selection.item_identity in by_identity:
                raise projections.ProjectionCanonicalizationError(
                    "authorization map contains a duplicate item identity"
                )
            by_identity[selection.item_identity] = selection
        if not isinstance(self.withheld_identities, frozenset):
            raise projections.ProjectionCanonicalizationError(
                "authorization map withheld identities must be an immutable set"
            )
        withheld = frozenset(
            _bounded_text(identity, "withheld item identity", maximum=4096)
            for identity in self.withheld_identities
        )
        if withheld.intersection(by_identity):
            raise projections.ProjectionCanonicalizationError(
                "authorization map selects and withholds the same item"
            )
        object.__setattr__(
            self,
            "selections",
            tuple(by_identity[key] for key in sorted(by_identity, key=_sort_key)),
        )
        object.__setattr__(self, "withheld_identities", withheld)


@dataclass(frozen=True, slots=True)
class ProjectedLexicalHit:
    """A retrieval result whose text and identity derive only from one variant."""

    item_identity: str
    projection_variant_id: str
    decision_level: int
    score: float
    search_fields: Mapping[str, str]
    snippet: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "search_fields", MappingProxyType(dict(self.search_fields)))


@dataclass(frozen=True, slots=True)
class _SelectedDocument:
    variant: projections.ProjectionVariant
    text: str
    tokens: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ProjectionVectorMeasurement:
    """One principal-free vector measurement beneath a projection variant row."""

    measurement_key: projections.MeasurementKey
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.measurement_key, projections.MeasurementKey):
            raise projections.ProjectionCanonicalizationError(
                "vector measurement key is invalid"
            )
        if self.measurement_key.lane != "vector":
            raise projections.ProjectionCanonicalizationError(
                "vector measurement key has an invalid lane"
            )
        vector = _vector(self.vector, "projection vector")
        if math.sqrt(sum(value * value for value in vector)) == 0:
            raise projections.ProjectionCanonicalizationError(
                "projection vector must have non-zero magnitude"
            )
        object.__setattr__(self, "vector", vector)


@dataclass(frozen=True, slots=True)
class ProjectionClipMeasurement:
    """One principal-free CLIP measurement beneath an L6 projection row."""

    measurement_key: projections.MeasurementKey
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.measurement_key, projections.MeasurementKey):
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement key is invalid"
            )
        if self.measurement_key.lane != "clip":
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement key has an invalid lane"
            )
        vector = _vector(self.vector, "CLIP projection vector")
        if math.sqrt(sum(value * value for value in vector)) == 0:
            raise projections.ProjectionCanonicalizationError(
                "CLIP projection vector must have non-zero magnitude"
            )
        object.__setattr__(self, "vector", vector)


def _vector(value: object, name: str) -> tuple[float, ...]:
    if not isinstance(value, tuple) or not 1 <= len(value) <= 4096:
        raise projections.ProjectionCanonicalizationError(
            f"{name} must be a bounded immutable vector"
        )
    normalized: list[float] = []
    for component in value:
        if isinstance(component, bool) or not isinstance(component, (int, float)):
            raise projections.ProjectionCanonicalizationError(
                f"{name} components must be finite numbers"
            )
        number = float(component)
        if not math.isfinite(number):
            raise projections.ProjectionCanonicalizationError(
                f"{name} components must be finite numbers"
            )
        normalized.append(number)
    return tuple(normalized)


def _catalog_items(
    namespace_key: projections.ProjectionNamespaceKey,
    items: Iterable[projection_store.ProjectionItemVariants],
) -> Mapping[str, projection_store.ProjectionItemVariants]:
    if not isinstance(namespace_key, projections.ProjectionNamespaceKey):
        raise projections.ProjectionCanonicalizationError(
            "projected retrieval namespace key is invalid"
        )
    by_identity: dict[str, projection_store.ProjectionItemVariants] = {}
    for item in items:
        if not isinstance(item, projection_store.ProjectionItemVariants):
            raise projections.ProjectionCanonicalizationError(
                "projected retrieval catalog item has an invalid type"
            )
        if item.item_identity in by_identity:
            raise projections.ProjectionCanonicalizationError(
                "projected retrieval catalog contains a duplicate item identity"
            )
        for variant in item.variants:
            value = json.loads(variant.value_jcs)
            if value["projector_schema_version"] != (
                namespace_key.projector_schema_version
            ):
                raise projections.ProjectionCanonicalizationError(
                    "variant projector schema does not match retrieval namespace"
                )
        by_identity[item.item_identity] = item
        projections.require_supported_capacity(catalog_items=len(by_identity))
    return MappingProxyType(by_identity)


def _selected_variants(
    namespace_key: projections.ProjectionNamespaceKey,
    items: Mapping[str, projection_store.ProjectionItemVariants],
    authorization: AuthorizationProjectionMap,
) -> tuple[projections.ProjectionVariant, ...]:
    if not isinstance(authorization, AuthorizationProjectionMap):
        raise ProjectedRetrievalUnavailable("authorization map is invalid")
    if authorization.namespace_key != namespace_key:
        raise ProjectedRetrievalUnavailable(
            "authorization map namespace does not match projected index"
        )
    selections = {
        selection.item_identity: selection for selection in authorization.selections
    }
    inline_withheld = frozenset(
        identity
        for identity, selection in selections.items()
        if selection.projection_variant_id is None
    )
    explicit_withheld = authorization.withheld_identities
    if (
        inline_withheld.intersection(explicit_withheld)
        or frozenset(selections).union(explicit_withheld) != frozenset(items)
    ):
        raise ProjectedRetrievalUnavailable(
            "authorization map does not cover the exact projection catalog"
        )

    selected: list[projections.ProjectionVariant] = []
    for identity, selection in selections.items():
        item = items.get(identity)
        if item is None:
            raise ProjectedRetrievalUnavailable(
                "authorization map does not cover the exact projection catalog"
            )
        if selection.content_hash != item.content_hash:
            raise ProjectedRetrievalUnavailable(
                "authorization selection content hash does not match catalog"
            )
        if selection.projection_variant_id is None:
            continue
        variants = {variant.projection_variant_id: variant for variant in item.variants}
        variant = variants.get(selection.projection_variant_id)
        if variant is None:
            raise ProjectedRetrievalUnavailable(
                "authorization selection names an unavailable projection variant"
            )
        if variant.item_identity != identity or variant.content_hash != item.content_hash:
            raise ProjectedRetrievalUnavailable(
                "projection variant does not match its catalog item"
            )
        selected.append(variant)
    return tuple(selected)


class ProjectionCatalog:
    """One immutable catalog snapshot shared by projected measurement lanes."""

    def __init__(
        self,
        namespace: projection_store.VerifiedProjectionNamespace,
    ) -> None:
        if not isinstance(namespace, projection_store.VerifiedProjectionNamespace):
            raise ProjectedRetrievalUnavailable(
                "projected retrieval requires a verified active namespace"
            )
        self.namespace = namespace
        self.namespace_key = namespace.namespace_key
        self.items = _catalog_items(namespace.namespace_key, namespace.items)
        variants_by_descriptor: dict[
            str,
            Mapping[bytes, projections.ProjectionVariant],
        ] = {}
        for identity, item in self.items.items():
            indexed: dict[bytes, projections.ProjectionVariant] = {}
            for variant in item.variants:
                descriptor = projections.projection_variant_descriptor(variant)
                if descriptor in indexed:
                    raise projections.ProjectionCanonicalizationError(
                        "projection item contains duplicate decision variants"
                    )
                indexed[descriptor] = variant
            variants_by_descriptor[identity] = MappingProxyType(indexed)
        self._variants_by_descriptor = MappingProxyType(variants_by_descriptor)

    def select(
        self,
        authorization: AuthorizationProjectionMap,
    ) -> tuple[projections.ProjectionVariant, ...]:
        """Resolve the exact request-local non-L0 variant set."""

        return _selected_variants(self.namespace_key, self.items, authorization)

    def variant_for_descriptor(
        self,
        item_identity: str,
        descriptor: bytes,
    ) -> projections.ProjectionVariant | None:
        """Resolve one preverified immutable row without request-time rehashing."""

        variants = self._variants_by_descriptor.get(item_identity)
        if variants is None:
            raise ProjectedRetrievalUnavailable(
                "projection catalog item is unavailable"
            )
        return variants.get(descriptor)


def _variant_text(variant: projections.ProjectionVariant) -> str:
    return " ".join(
        variant.search_fields[key]
        for key in sorted(variant.search_fields, key=_sort_key)
    )


def clip_variant_applicable(variant: projections.ProjectionVariant) -> bool:
    """Whether one committed projection row owns a CLIP pixel measurement."""

    return (
        variant.decision_level == 6
        and variant.search_fields.get("media_type") in {"image", "video"}
        and not variant.search_fields.get("parent_media")
    )


def _projected_hit(
    variant: projections.ProjectionVariant,
    score: float,
) -> ProjectedLexicalHit:
    compact = " ".join(_variant_text(variant).split())
    return ProjectedLexicalHit(
        item_identity=variant.item_identity,
        projection_variant_id=variant.projection_variant_id,
        decision_level=variant.decision_level,
        score=score,
        search_fields=variant.search_fields,
        snippet=compact[:_SNIPPET_CHARS].rstrip(),
    )


class ProjectedLexicalIndex:
    """Principal-free immutable rows searched through exact request-local maps."""

    def __init__(
        self,
        namespace: projection_store.VerifiedProjectionNamespace | ProjectionCatalog,
    ) -> None:
        catalog = (
            namespace
            if isinstance(namespace, ProjectionCatalog)
            else ProjectionCatalog(namespace)
        )
        self.catalog = catalog
        self.namespace_key = catalog.namespace_key
        self._items = catalog.items
        documents: dict[str, _SelectedDocument] = {}
        postings: dict[str, set[str]] = {}
        for item in self._items.values():
            for variant in item.variants:
                text = _variant_text(variant)
                tokens = tuple(bm25.tokenize(text))
                document = _SelectedDocument(variant, text, tokens)
                documents[variant.projection_variant_id] = document
                for token in frozenset(tokens):
                    postings.setdefault(token, set()).add(
                        variant.projection_variant_id
                    )
        self._documents = MappingProxyType(documents)
        self._postings = MappingProxyType(
            {token: frozenset(variant_ids) for token, variant_ids in postings.items()}
        )

    def _selected_documents(
        self,
        authorization: AuthorizationProjectionMap,
    ) -> tuple[_SelectedDocument, ...]:
        return tuple(
            self._documents[variant.projection_variant_id]
            for variant in _selected_variants(
                self.namespace_key,
                self._items,
                authorization,
            )
        )

    def _candidate_documents(
        self,
        documents: tuple[_SelectedDocument, ...],
        query_tokens: tuple[str, ...],
    ) -> tuple[_SelectedDocument, ...]:
        selected_ids = frozenset(
            document.variant.projection_variant_id for document in documents
        )
        posting_sets = [self._postings.get(token, frozenset()) for token in query_tokens]
        if not posting_sets:
            return ()
        candidate_ids = selected_ids.intersection(
            frozenset().union(*posting_sets)
        )
        return tuple(
            document
            for document in documents
            if document.variant.projection_variant_id in candidate_ids
        )

    @staticmethod
    def _hit(document: _SelectedDocument, score: float) -> ProjectedLexicalHit:
        return _projected_hit(document.variant, score)

    def search_bm25(
        self,
        authorization: AuthorizationProjectionMap,
        query: str,
        *,
        k: int,
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Rank only the selected projected corpus using deterministic BM25Okapi."""

        limit = _result_limit(k)
        query_tokens = tuple(bm25.tokenize(query)) if isinstance(query, str) else ()
        documents = self._selected_documents(authorization)
        if not query_tokens or not documents:
            return ()

        candidates = self._candidate_documents(documents, query_tokens)
        if not candidates:
            return ()

        selected_ids = frozenset(
            document.variant.projection_variant_id for document in documents
        )
        frequencies = {
            token: len(self._postings.get(token, frozenset()) & selected_ids)
            for token in frozenset(query_tokens)
        }
        inverse_frequencies = {
            token: math.log(
                1
                + (len(documents) - document_frequency + 0.5)
                / (document_frequency + 0.5)
            )
            for token, document_frequency in frequencies.items()
        }
        average_length = max(
            sum(len(document.tokens) for document in documents) / len(documents),
            1.0,
        )
        scores: list[tuple[_SelectedDocument, float]] = []
        for document in candidates:
            term_counts = Counter(document.tokens)
            score = 0.0
            length_normalization = 1 - _BM25_B + _BM25_B * (
                len(document.tokens) / average_length
            )
            for token in query_tokens:
                frequency = term_counts[token]
                score += inverse_frequencies[token] * (
                    frequency * (_BM25_K1 + 1)
                ) / (frequency + _BM25_K1 * length_normalization)
            if score > 0:
                scores.append((document, score))
        scores.sort(
            key=lambda item: (
                -item[1],
                _sort_key(item[0].variant.item_identity),
                item[0].variant.projection_variant_id,
            )
        )
        return tuple(self._hit(document, score) for document, score in scores[:limit])

    def search_keyword(
        self,
        authorization: AuthorizationProjectionMap,
        query: str,
        *,
        k: int,
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Apply keyword mode's strict substring gate before the result cap."""

        limit = _result_limit(k)
        documents = self._selected_documents(authorization)
        query_tokens = (
            tuple(token for token in query.casefold().split() if token)
            if isinstance(query, str)
            else ()
        )
        if not query_tokens:
            return ()
        matches: list[_SelectedDocument] = []
        for document in documents:
            folded_text = document.text.casefold()
            if all(token in folded_text for token in query_tokens):
                matches.append(document)
        matches.sort(
            key=lambda document: (
                _sort_key(document.variant.item_identity),
                document.variant.projection_variant_id,
            )
        )
        return tuple(self._hit(document, 1.0) for document in matches[:limit])


class ProjectedVectorIndex:
    """Score fixed projection embeddings after request-local authorization."""

    def __init__(
        self,
        namespace: projection_store.VerifiedProjectionNamespace,
        measurements: Iterable[ProjectionVectorMeasurement],
        *,
        extractor_version: str,
        model_version: str,
    ) -> None:
        catalog = ProjectionCatalog(namespace)
        self.namespace_key = catalog.namespace_key
        self._items = catalog.items
        self.extractor_version = _bounded_text(
            extractor_version,
            "vector extractor version",
            maximum=256,
        )
        self.model_version = _bounded_text(
            model_version,
            "vector model version",
            maximum=256,
        )
        catalog_variant_ids = {
            variant.projection_variant_id
            for item in self._items.values()
            for variant in item.variants
        }
        by_variant: dict[str, ProjectionVectorMeasurement] = {}
        dimension: int | None = None
        for measurement in measurements:
            if not isinstance(measurement, ProjectionVectorMeasurement):
                raise projections.ProjectionCanonicalizationError(
                    "projected vector measurement has an invalid type"
                )
            key = measurement.measurement_key
            if key.extractor_version != self.extractor_version:
                raise projections.ProjectionCanonicalizationError(
                    "vector measurement extractor version does not match index"
                )
            if key.model_version != self.model_version:
                raise projections.ProjectionCanonicalizationError(
                    "vector measurement model version does not match index"
                )
            if key.projection_variant_id not in catalog_variant_ids:
                raise projections.ProjectionCanonicalizationError(
                    "vector measurement variant is outside the projection catalog"
                )
            if key.projection_variant_id in by_variant:
                raise projections.ProjectionCanonicalizationError(
                    "projected vector index contains a duplicate variant measurement"
                )
            if dimension is None:
                dimension = len(measurement.vector)
            elif len(measurement.vector) != dimension:
                raise projections.ProjectionCanonicalizationError(
                    "projected vector measurements have inconsistent dimensions"
                )
            by_variant[key.projection_variant_id] = measurement
        self._measurements = MappingProxyType(by_variant)
        self._dimension = dimension

    def search_vector(
        self,
        authorization: AuthorizationProjectionMap,
        query_vector: tuple[float, ...],
        *,
        k: int,
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Score every selected projection row before applying the result cap."""

        limit = _result_limit(k)
        selected = _selected_variants(
            self.namespace_key,
            self._items,
            authorization,
        )
        if not selected:
            return ()
        if self._dimension is None:
            raise ProjectedLaneUnavailable(
                "selected projection vector measurement is unavailable"
            )
        query = _vector(query_vector, "query vector")
        if len(query) != self._dimension:
            raise ProjectedLaneUnavailable(
                "query vector dimension does not match projected measurements"
            )
        query_magnitude = math.sqrt(sum(value * value for value in query))
        if query_magnitude == 0:
            raise ProjectedLaneUnavailable("query vector has zero magnitude")

        scored: list[tuple[projections.ProjectionVariant, float]] = []
        for variant in selected:
            measurement = self._measurements.get(variant.projection_variant_id)
            if measurement is None:
                raise ProjectedLaneUnavailable(
                    "selected projection vector measurement is unavailable"
                )
            measurement_magnitude = math.sqrt(
                sum(value * value for value in measurement.vector)
            )
            score = sum(
                left * right
                for left, right in zip(query, measurement.vector, strict=True)
            ) / (query_magnitude * measurement_magnitude)
            scored.append((variant, score))
        scored.sort(
            key=lambda item: (
                -item[1],
                _sort_key(item[0].item_identity),
                item[0].projection_variant_id,
            )
        )
        return tuple(_projected_hit(variant, score) for variant, score in scored[:limit])


class ProjectedClipIndex:
    """Score pixels/keyframes only for request-selected L6 projection rows."""

    def __init__(
        self,
        namespace: projection_store.VerifiedProjectionNamespace,
        measurements: Iterable[ProjectionClipMeasurement],
        *,
        extractor_version: str,
        model_version: str,
    ) -> None:
        catalog = ProjectionCatalog(namespace)
        self.namespace_key = catalog.namespace_key
        self._items = catalog.items
        self.extractor_version = _bounded_text(
            extractor_version,
            "CLIP extractor version",
            maximum=256,
        )
        self.model_version = _bounded_text(
            model_version,
            "CLIP model version",
            maximum=256,
        )
        clip_variant_ids = {
            variant.projection_variant_id
            for item in self._items.values()
            for variant in item.variants
            if clip_variant_applicable(variant)
        }
        by_variant: dict[str, ProjectionClipMeasurement] = {}
        dimension: int | None = None
        for measurement in measurements:
            if not isinstance(measurement, ProjectionClipMeasurement):
                raise projections.ProjectionCanonicalizationError(
                    "projected CLIP measurement has an invalid type"
                )
            key = measurement.measurement_key
            if key.extractor_version != self.extractor_version:
                raise projections.ProjectionCanonicalizationError(
                    "CLIP measurement extractor version does not match index"
                )
            if key.model_version != self.model_version:
                raise projections.ProjectionCanonicalizationError(
                    "CLIP measurement model version does not match index"
                )
            if key.projection_variant_id not in clip_variant_ids:
                raise projections.ProjectionCanonicalizationError(
                    "CLIP measurement variant is not an applicable L6 pixel catalog projection"
                )
            if key.projection_variant_id in by_variant:
                raise projections.ProjectionCanonicalizationError(
                    "projected CLIP index contains a duplicate variant measurement"
                )
            if dimension is None:
                dimension = len(measurement.vector)
            elif len(measurement.vector) != dimension:
                raise projections.ProjectionCanonicalizationError(
                    "projected CLIP measurements have inconsistent dimensions"
                )
            by_variant[key.projection_variant_id] = measurement
        self._measurements = MappingProxyType(by_variant)
        self._dimension = dimension

    def search_clip(
        self,
        authorization: AuthorizationProjectionMap,
        query_vector: tuple[float, ...],
        *,
        k: int,
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Authorize L6 variants inside the CLIP lane before applying its cap."""

        limit = _result_limit(k)
        selected = tuple(
            variant
            for variant in _selected_variants(
                self.namespace_key,
                self._items,
                authorization,
            )
            if clip_variant_applicable(variant)
        )
        if not selected:
            return ()
        if self._dimension is None:
            raise ProjectedLaneUnavailable(
                "selected projection CLIP measurement is unavailable"
            )
        query = _vector(query_vector, "CLIP query vector")
        if len(query) != self._dimension:
            raise ProjectedLaneUnavailable(
                "CLIP query dimension does not match projected measurements"
            )
        query_magnitude = math.sqrt(sum(value * value for value in query))
        if query_magnitude == 0:
            raise ProjectedLaneUnavailable("CLIP query vector has zero magnitude")

        scored: list[tuple[projections.ProjectionVariant, float]] = []
        for variant in selected:
            measurement = self._measurements.get(variant.projection_variant_id)
            if measurement is None:
                raise ProjectedLaneUnavailable(
                    "selected projection CLIP measurement is unavailable"
                )
            measurement_magnitude = math.sqrt(
                sum(value * value for value in measurement.vector)
            )
            score = sum(
                left * right
                for left, right in zip(query, measurement.vector, strict=True)
            ) / (query_magnitude * measurement_magnitude)
            scored.append((variant, score))
        scored.sort(
            key=lambda item: (
                -item[1],
                _sort_key(item[0].item_identity),
                item[0].projection_variant_id,
            )
        )
        return tuple(_projected_hit(variant, score) for variant, score in scored[:limit])


class ProjectedReranker:
    """Rerank complete projected-lane candidates without reopening raw content."""

    def __init__(
        self,
        namespace: projection_store.VerifiedProjectionNamespace,
    ) -> None:
        catalog = ProjectionCatalog(namespace)
        self.namespace_key = catalog.namespace_key
        self._items = catalog.items

    def rerank(
        self,
        authorization: AuthorizationProjectionMap,
        query: str,
        candidates: tuple[str, ...],
        *,
        scorer: Callable[[str, Mapping[str, str]], float],
        k: int,
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Score every projected candidate, then apply the final result cap."""

        limit = _result_limit(k)
        if not isinstance(query, str) or len(query) > 1_048_576:
            raise ValueError("query must be bounded text")
        if not isinstance(candidates, tuple) or len(candidates) > 100_000:
            raise ProjectedRetrievalUnavailable(
                "rerank candidates must be one bounded immutable tuple"
            )
        if not callable(scorer):
            raise ProjectedLaneUnavailable("projected rerank scorer is unavailable")
        seen: set[str] = set()
        for identity in candidates:
            _bounded_text(identity, "rerank candidate identity", maximum=4096)
            if identity in seen:
                raise ProjectedRetrievalUnavailable(
                    "rerank candidate set contains a duplicate"
                )
            seen.add(identity)

        selected = _selected_variants(
            self.namespace_key,
            self._items,
            authorization,
        )
        selected_by_identity = {variant.item_identity: variant for variant in selected}
        if any(identity not in selected_by_identity for identity in candidates):
            raise ProjectedRetrievalUnavailable(
                "rerank candidate is outside the selected projected corpus"
            )

        scored: list[tuple[int, projections.ProjectionVariant, float]] = []
        for ordinal, identity in enumerate(candidates):
            variant = selected_by_identity[identity]
            try:
                raw_score = scorer(query, variant.search_fields)
            except Exception as error:
                raise ProjectedLaneUnavailable("projected rerank scorer failed") from error
            if isinstance(raw_score, bool) or not isinstance(raw_score, Real):
                raise ProjectedLaneUnavailable("projected rerank score must be finite")
            score = float(raw_score)
            if not math.isfinite(score):
                raise ProjectedLaneUnavailable("projected rerank score must be finite")
            scored.append((ordinal, variant, score))
        scored.sort(key=lambda item: (-item[2], item[0]))
        return tuple(
            _projected_hit(variant, score)
            for _ordinal, variant, score in scored[:limit]
        )

    def rerank_batch(
        self,
        authorization: AuthorizationProjectionMap,
        query: str,
        candidates: tuple[str, ...],
        *,
        scorer: Callable[[str, list[str]], object],
        k: int,
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Batch-score every projected candidate before applying the final cap."""

        limit = _result_limit(k)
        if not isinstance(query, str) or len(query) > 1_048_576:
            raise ValueError("query must be bounded text")
        if (
            not isinstance(candidates, tuple)
            or len(candidates) > projections.MAX_GOVERNED_CATALOG_ITEMS
        ):
            raise ProjectedRetrievalUnavailable(
                "rerank candidates must be one bounded immutable tuple"
            )
        if not callable(scorer):
            raise ProjectedLaneUnavailable("projected rerank scorer is unavailable")
        if len(frozenset(candidates)) != len(candidates):
            raise ProjectedRetrievalUnavailable(
                "rerank candidate set contains a duplicate"
            )
        for identity in candidates:
            _bounded_text(identity, "rerank candidate identity", maximum=4096)

        selected = _selected_variants(
            self.namespace_key,
            self._items,
            authorization,
        )
        selected_by_identity = {variant.item_identity: variant for variant in selected}
        if any(identity not in selected_by_identity for identity in candidates):
            raise ProjectedRetrievalUnavailable(
                "rerank candidate is outside the selected projected corpus"
            )
        variants = tuple(selected_by_identity[identity] for identity in candidates)
        passages = [_variant_text(variant) for variant in variants]
        try:
            raw_scores = scorer(query, passages)
            scores = tuple(raw_scores)  # type: ignore[arg-type]
        except Exception as error:
            raise ProjectedLaneUnavailable("projected rerank scorer failed") from error
        if len(scores) != len(variants):
            raise ProjectedLaneUnavailable(
                "projected rerank score count does not match candidates"
            )

        ranked: list[tuple[int, projections.ProjectionVariant, float]] = []
        for ordinal, (variant, raw_score) in enumerate(
            zip(variants, scores, strict=True)
        ):
            if isinstance(raw_score, bool) or not isinstance(raw_score, Real):
                raise ProjectedLaneUnavailable(
                    "projected rerank score must be finite"
                )
            score = float(raw_score)
            if not math.isfinite(score):
                raise ProjectedLaneUnavailable(
                    "projected rerank score must be finite"
                )
            ranked.append((ordinal, variant, score))
        ranked.sort(key=lambda item: (-item[2], item[0]))
        return tuple(
            _projected_hit(variant, score)
            for _ordinal, variant, score in ranked[:limit]
        )


__all__ = [
    "AuthorizationProjectionMap",
    "ProjectedClipIndex",
    "ProjectedLaneUnavailable",
    "ProjectedLexicalHit",
    "ProjectedLexicalIndex",
    "ProjectedReranker",
    "ProjectedRetrievalUnavailable",
    "ProjectedVectorIndex",
    "ProjectionClipMeasurement",
    "ProjectionCatalog",
    "ProjectionVectorMeasurement",
    "ProjectionSelection",
    "clip_variant_applicable",
]
