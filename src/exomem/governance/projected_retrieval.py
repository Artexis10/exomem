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
    clip_frame_timestamp_ms: int | None = None
    #: The part of the variant's text that matched, when a further part did; None for the
    #: variant's own fields.
    part: projections.ProjectionPart | None = None

    def __post_init__(self) -> None:
        if self.clip_frame_timestamp_ms is not None and (
            type(self.clip_frame_timestamp_ms) is not int
            or not 0 <= self.clip_frame_timestamp_ms <= 4_294_967_295
        ):
            raise projections.ProjectionCanonicalizationError(
                "projected CLIP hit timestamp must be a bounded integer millisecond value"
            )
        object.__setattr__(self, "search_fields", MappingProxyType(dict(self.search_fields)))


@dataclass(frozen=True, slots=True)
class _SelectedDocument:
    variant: projections.ProjectionVariant
    #: How often each BM25 token occurs, counted once at index build: a request scores a long
    #: part without counting its tokens again.
    term_counts: Mapping[str, int]
    #: The number of BM25 tokens in the document.
    length: int
    keyword_tokens: frozenset[str]
    #: `text` case-folded once, for the keyword lane's substring test.
    folded: str
    #: The further part this document is, or None for the variant's own fields.
    part: projections.ProjectionPart | None = None

    @property
    def key(self) -> tuple[str, int]:
        return (self.variant.projection_variant_id, 1 if self.part is None else self.part.number)


def _document(
    variant: projections.ProjectionVariant,
    text: str,
    part: projections.ProjectionPart | None = None,
) -> _SelectedDocument:
    folded = text.casefold()
    tokens = bm25.tokenize(text)
    return _SelectedDocument(
        variant,
        Counter(tokens),
        len(tokens),
        frozenset(folded.split()),
        text if folded == text else folded,
        part,
    )


def matched_passage(
    variant: projections.ProjectionVariant,
    part: projections.ProjectionPart | None,
    query: str,
    *,
    semantic: bool = False,
) -> dict[str, object]:
    """`{"matched_part", "passage"}` for a hit on `variant`, from the catalog's text alone.

    A hit on a variant that holds parts names the part that matched: `part`, or part 1 when
    the variant's own fields matched. `passage.offset` counts the Source's whole stored text,
    so it opens a `read_memory` window at the match. The hit names its part without a passage
    when the part holds no literal match of the query, or when no stored position maps into
    it because a release strip changed its text. A hit on any other variant has neither. A hit
    on the full-text variant has neither when its own fields hold no literal match of the query
    and the vector lane did not rank it (a title match or a graph hop), as in direct search. A
    `semantic` hit matched in the text with no stored chunk to anchor, so it names part 1 alone.
    """
    if part is not None:
        found = None if part.start is None else _passage(part, query)
        return found or {"matched_part": part.number}
    if not variant.parts:
        return {}
    head = variant.head
    if head is None:
        return {"matched_part": 1}  # no single place holds part 1, so the match cannot be anchored
    found = _passage_in(variant.search_fields["body"][head.begin : head.end], 1, 0, head.carrier_spans, query)
    return found or ({"matched_part": 1} if semantic else {})


def _passage(part: projections.ProjectionPart, query: str) -> dict[str, object] | None:
    return _passage_in(part.text, part.number, part.start or 0, part.carrier_spans, query)


def _passage_in(
    text: str, number: int, base: int, cuts: tuple[tuple[int, int], ...], query: str
) -> dict[str, object] | None:
    from .. import find_parts

    return find_parts.passage_in(
        text,
        number=number,
        base=base,
        chunk=None,
        title=None,
        # As `find` normalizes a query before it anchors a passage on it.
        query_norm=query.lower().strip(),
        cuts=cuts,
    )


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
class ProjectionClipSample:
    """One canonical pixel/keyframe vector within a projected media row."""

    frame_timestamp_ms: int | None
    vector: tuple[float, ...]

    def __post_init__(self) -> None:
        if self.frame_timestamp_ms is not None and (
            type(self.frame_timestamp_ms) is not int
            or not 0 <= self.frame_timestamp_ms <= 4_294_967_295
        ):
            raise projections.ProjectionCanonicalizationError(
                "CLIP frame timestamp must be a bounded integer millisecond value"
            )
        vector = _vector(self.vector, "CLIP projection vector")
        if math.sqrt(sum(value * value for value in vector)) == 0:
            raise projections.ProjectionCanonicalizationError(
                "CLIP projection vector must have non-zero magnitude"
            )
        object.__setattr__(self, "vector", vector)


MAX_PROJECTED_CLIP_SAMPLES_PER_VARIANT = 40


@dataclass(frozen=True, slots=True, init=False)
class ProjectionClipMeasurement:
    """One principal-free image or multi-keyframe video measurement row."""

    measurement_key: projections.MeasurementKey
    samples: tuple[ProjectionClipSample, ...]

    def __init__(
        self,
        measurement_key: projections.MeasurementKey,
        vector: tuple[float, ...] | None = None,
        *,
        samples: tuple[ProjectionClipSample, ...] | None = None,
    ) -> None:
        if (vector is None) == (samples is None):
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement requires either one image vector or canonical samples"
            )
        if vector is not None:
            normalized = (
                ProjectionClipSample(frame_timestamp_ms=None, vector=vector),
            )
        else:
            if samples is None:
                raise projections.ProjectionCanonicalizationError(
                    "CLIP measurement samples are unavailable"
                )
            normalized = samples
        object.__setattr__(self, "measurement_key", measurement_key)
        object.__setattr__(self, "samples", normalized)
        self.__post_init__()

    def __post_init__(self) -> None:
        if not isinstance(self.measurement_key, projections.MeasurementKey):
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement key is invalid"
            )
        if self.measurement_key.lane != "clip":
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement key has an invalid lane"
            )
        if not isinstance(self.samples, tuple) or not self.samples:
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement samples must be a non-empty immutable tuple"
            )
        if len(self.samples) > MAX_PROJECTED_CLIP_SAMPLES_PER_VARIANT:
            raise projections.ProjectionCapacityExceeded(
                "projected CLIP samples exceed the fixed per-variant capacity"
            )
        if any(not isinstance(sample, ProjectionClipSample) for sample in self.samples):
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement sample has an invalid type"
            )
        if len({len(sample.vector) for sample in self.samples}) != 1:
            raise projections.ProjectionCanonicalizationError(
                "CLIP measurement samples have inconsistent dimensions"
            )
        timestamps = tuple(sample.frame_timestamp_ms for sample in self.samples)
        if any(timestamp is None for timestamp in timestamps):
            if timestamps != (None,):
                raise projections.ProjectionCanonicalizationError(
                    "CLIP image measurement must be one untimestamped sample"
                )
        elif tuple(sorted(timestamps)) != timestamps or len(set(timestamps)) != len(
            timestamps
        ):
            raise projections.ProjectionCanonicalizationError(
                "CLIP samples must use strict canonical timestamp order"
            )

    @property
    def vector(self) -> tuple[float, ...]:
        """Compatibility view for a legacy singleton image measurement."""

        if len(self.samples) != 1:
            raise projections.ProjectionCanonicalizationError(
                "multi-sample CLIP measurements do not have one vector"
            )
        return self.samples[0].vector


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
    documents = 0
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
        documents += 1 + projections.part_count(item.variants)
        projections.require_supported_capacity(catalog_items=documents)
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
        namespace: (
            projection_store.VerifiedProjectionNamespace
            | projection_store.PreparedProjectionNamespace
        ),
    ) -> None:
        if not isinstance(
            namespace,
            (
                projection_store.VerifiedProjectionNamespace,
                projection_store.PreparedProjectionNamespace,
            ),
        ):
            raise ProjectedRetrievalUnavailable(
                "projected retrieval requires a verified namespace"
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


def projection_text(variant: projections.ProjectionVariant) -> str:
    """Return the canonical text measured for one immutable projection row."""

    if not isinstance(variant, projections.ProjectionVariant):
        raise projections.ProjectionCanonicalizationError(
            "projection text requires one immutable variant"
        )
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
    *,
    clip_frame_timestamp_ms: int | None = None,
    part: projections.ProjectionPart | None = None,
) -> ProjectedLexicalHit:
    # The snippet stays the head of the Source, whichever part matched.
    compact = " ".join(projection_text(variant).split())
    return ProjectedLexicalHit(
        item_identity=variant.item_identity,
        projection_variant_id=variant.projection_variant_id,
        decision_level=variant.decision_level,
        score=score,
        search_fields=variant.search_fields,
        snippet=compact[:_SNIPPET_CHARS].rstrip(),
        clip_frame_timestamp_ms=clip_frame_timestamp_ms,
        part=part,
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
        # Each variant's own fields, then each further part it holds: a part is a document of
        # its variant, reached only when a request selects that variant.
        documents: dict[str, tuple[_SelectedDocument, ...]] = {}
        postings: dict[str, set[tuple[str, int]]] = {}
        for item in self._items.values():
            for variant in item.variants:
                held = (
                    _document(variant, projection_text(variant)),
                    *(_document(variant, part.text, part) for part in variant.parts),
                )
                documents[variant.projection_variant_id] = held
                for document in held:
                    for token in document.term_counts:
                        postings.setdefault(token, set()).add(document.key)
        self._documents = MappingProxyType(documents)
        self._postings = MappingProxyType(
            {token: frozenset(keys) for token, keys in postings.items()}
        )

    def _selected_documents(
        self,
        authorization: AuthorizationProjectionMap,
    ) -> tuple[_SelectedDocument, ...]:
        return tuple(
            document
            for variant in _selected_variants(
                self.namespace_key,
                self._items,
                authorization,
            )
            for document in self._documents[variant.projection_variant_id]
        )

    def _candidate_documents(
        self,
        documents: tuple[_SelectedDocument, ...],
        query_tokens: tuple[str, ...],
    ) -> tuple[_SelectedDocument, ...]:
        selected_keys = frozenset(document.key for document in documents)
        unique_query_tokens = frozenset(query_tokens)
        if not unique_query_tokens:
            return ()
        candidate_keys: set[tuple[str, int]] = set()
        for token in unique_query_tokens:
            candidate_keys.update(
                selected_keys.intersection(
                    self._postings.get(token, frozenset())
                )
            )
        return tuple(
            document
            for document in documents
            if document.key in candidate_keys
        )

    @staticmethod
    def _hit(document: _SelectedDocument, score: float) -> ProjectedLexicalHit:
        return _projected_hit(document.variant, score, part=document.part)

    def search_bm25(
        self,
        authorization: AuthorizationProjectionMap,
        query: str,
        *,
        k: int,
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Rank only the selected projected corpus using deterministic BM25Okapi."""

        limit = _result_limit(k)
        query_tokens = (
            tuple(bm25.tokenize(query, query=True)) if isinstance(query, str) else ()
        )
        documents = self._selected_documents(authorization)
        if not query_tokens or not documents:
            return ()

        candidates = self._candidate_documents(documents, query_tokens)
        if not candidates:
            return ()

        selected_keys = frozenset(document.key for document in documents)
        frequencies = {
            token: len(self._postings.get(token, frozenset()) & selected_keys)
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
            sum(document.length for document in documents) / len(documents),
            1.0,
        )
        scores: list[tuple[_SelectedDocument, float]] = []
        for document in candidates:
            score = 0.0
            length_normalization = 1 - _BM25_B + _BM25_B * (
                document.length / average_length
            )
            for token in query_tokens:
                frequency = document.term_counts.get(token, 0)
                score += inverse_frequencies[token] * (
                    frequency * (_BM25_K1 + 1)
                ) / (frequency + _BM25_K1 * length_normalization)
            if score > 0:
                scores.append((document, score))
        # One result per item before the cap: its best-scoring document, the lowest part on a tie.
        best: dict[str, tuple[_SelectedDocument, float]] = {}
        for document, score in scores:
            prior = best.get(document.variant.item_identity)
            if prior is None or (score, -document.key[1]) > (prior[1], -prior[0].key[1]):
                best[document.variant.item_identity] = (document, score)
        ranked = sorted(
            best.values(),
            key=lambda item: (
                -item[1],
                _sort_key(item[0].variant.item_identity),
                item[0].variant.projection_variant_id,
            ),
        )
        return tuple(self._hit(document, score) for document, score in ranked[:limit])

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
        exact_query_tokens = frozenset(query_tokens)
        # One result per item before the cap: the lowest part that matches, its own fields first.
        first_match: dict[str, _SelectedDocument] = {}
        for document in documents:
            identity = document.variant.item_identity
            if identity in first_match:
                continue
            if exact_query_tokens.issubset(document.keyword_tokens) or all(
                token in document.folded for token in query_tokens
            ):
                first_match[identity] = document
        matches = list(first_match.values())
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
        clip_variants = {
            variant.projection_variant_id: variant
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
            if key.projection_variant_id not in clip_variants:
                raise projections.ProjectionCanonicalizationError(
                    "CLIP measurement variant is not an applicable L6 pixel catalog projection"
                )
            if key.projection_variant_id in by_variant:
                raise projections.ProjectionCanonicalizationError(
                    "projected CLIP index contains a duplicate variant measurement"
                )
            variant = clip_variants[key.projection_variant_id]
            timestamps = tuple(
                sample.frame_timestamp_ms for sample in measurement.samples
            )
            if variant.search_fields.get("media_type") == "image" and timestamps != (
                None,
            ):
                raise projections.ProjectionCanonicalizationError(
                    "projected image CLIP measurement must be one untimestamped sample"
                )
            if variant.search_fields.get("media_type") == "video" and any(
                timestamp is None for timestamp in timestamps
            ):
                raise projections.ProjectionCanonicalizationError(
                    "projected video CLIP measurements require frame timestamps"
                )
            row_dimension = len(measurement.samples[0].vector)
            if dimension is None:
                dimension = row_dimension
            elif row_dimension != dimension:
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

        scored: list[tuple[projections.ProjectionVariant, float, int | None]] = []
        for variant in selected:
            measurement = self._measurements.get(variant.projection_variant_id)
            if measurement is None:
                raise ProjectedLaneUnavailable(
                    "selected projection CLIP measurement is unavailable"
                )
            sample_scores: list[tuple[float, int | None]] = []
            for sample in measurement.samples:
                measurement_magnitude = math.sqrt(
                    sum(value * value for value in sample.vector)
                )
                score = sum(
                    left * right
                    for left, right in zip(query, sample.vector, strict=True)
                ) / (query_magnitude * measurement_magnitude)
                sample_scores.append((score, sample.frame_timestamp_ms))
            score, timestamp_ms = min(
                sample_scores,
                key=lambda item: (
                    -item[0],
                    -1 if item[1] is None else item[1],
                ),
            )
            scored.append((variant, score, timestamp_ms))
        scored.sort(
            key=lambda item: (
                -item[1],
                _sort_key(item[0].item_identity),
                item[0].projection_variant_id,
            )
        )
        return tuple(
            _projected_hit(
                variant,
                score,
                clip_frame_timestamp_ms=timestamp_ms,
            )
            for variant, score, timestamp_ms in scored[:limit]
        )


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
        matched_parts: Mapping[str, int] = MappingProxyType({}),
    ) -> tuple[ProjectedLexicalHit, ...]:
        """Batch-score every projected candidate before applying the final cap.

        `matched_parts` names, by item, the further part of its selected variant that a lexical
        lane matched; that candidate is scored on the passage of the match in that part, not on
        the head of the Source, which may not hold the query at all.
        """

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
        parts: dict[str, projections.ProjectionPart] = {}
        for identity, number in matched_parts.items():
            held = selected_by_identity[identity].parts if identity in selected_by_identity else ()
            if type(number) is not int or not 2 <= number <= len(held) + 1:
                raise ProjectedRetrievalUnavailable(
                    "rerank matched part is outside the selected projected variant"
                )
            parts[identity] = held[number - 2]
        passages = [
            projection_text(variant)
            if variant.item_identity not in parts
            else _part_rerank_text(parts[variant.item_identity], query)
            for variant in variants
        ]
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
            _projected_hit(variant, score, part=parts.get(variant.item_identity))
            for _ordinal, variant, score in ranked[:limit]
        )


def _part_rerank_text(part: projections.ProjectionPart, query: str) -> str:
    """The text a reranker reads for a part that matched: the passage of the match in it."""
    found = _passage(part, query)
    passage = found["passage"] if found is not None else None
    return str(passage["text"]) if isinstance(passage, dict) else part.text


__all__ = [
    "AuthorizationProjectionMap",
    "MAX_PROJECTED_CLIP_SAMPLES_PER_VARIANT",
    "ProjectedClipIndex",
    "ProjectedLaneUnavailable",
    "ProjectedLexicalHit",
    "ProjectedLexicalIndex",
    "ProjectedReranker",
    "ProjectedRetrievalUnavailable",
    "ProjectedVectorIndex",
    "ProjectionClipSample",
    "ProjectionClipMeasurement",
    "ProjectionCatalog",
    "ProjectionVectorMeasurement",
    "ProjectionSelection",
    "clip_variant_applicable",
    "projection_text",
]
