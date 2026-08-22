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
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from .. import bm25
from . import projection_store, projections

_HEX = frozenset("0123456789abcdef")
_BM25_K1 = 1.5
_BM25_B = 0.75
_MAX_RESULTS = 1_000
_SNIPPET_CHARS = 320


class ProjectedRetrievalUnavailable(RuntimeError):
    """The authorized projection corpus is incomplete or internally inconsistent."""


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

    def __post_init__(self) -> None:
        _bounded_text(self.item_identity, "item identity", maximum=4096)
        _digest(self.content_hash, "content hash")
        if self.projection_variant_id is not None:
            _digest(self.projection_variant_id, "projection variant id")


@dataclass(frozen=True, slots=True)
class AuthorizationProjectionMap:
    """An immutable request-local selector for one exact projection namespace."""

    namespace_key: projections.ProjectionNamespaceKey
    selections: tuple[ProjectionSelection, ...]

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
        object.__setattr__(
            self,
            "selections",
            tuple(by_identity[key] for key in sorted(by_identity, key=_sort_key)),
        )


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


class ProjectedLexicalIndex:
    """Principal-free immutable rows searched through exact request-local maps."""

    def __init__(
        self,
        namespace_key: projections.ProjectionNamespaceKey,
        items: Iterable[projection_store.ProjectionItemVariants],
    ) -> None:
        if not isinstance(namespace_key, projections.ProjectionNamespaceKey):
            raise projections.ProjectionCanonicalizationError(
                "projected lexical namespace key is invalid"
            )
        by_identity: dict[str, projection_store.ProjectionItemVariants] = {}
        for item in items:
            if not isinstance(item, projection_store.ProjectionItemVariants):
                raise projections.ProjectionCanonicalizationError(
                    "projected lexical catalog item has an invalid type"
                )
            if item.item_identity in by_identity:
                raise projections.ProjectionCanonicalizationError(
                    "projected lexical catalog contains a duplicate item identity"
                )
            for variant in item.variants:
                value = json.loads(variant.value_jcs)
                if value["projector_schema_version"] != (
                    namespace_key.projector_schema_version
                ):
                    raise projections.ProjectionCanonicalizationError(
                        "variant projector schema does not match lexical namespace"
                    )
            by_identity[item.item_identity] = item
        self.namespace_key = namespace_key
        self._items = MappingProxyType(by_identity)

    @staticmethod
    def _variant_text(variant: projections.ProjectionVariant) -> str:
        return " ".join(
            variant.search_fields[key]
            for key in sorted(variant.search_fields, key=_sort_key)
        )

    def _selected_documents(
        self,
        authorization: AuthorizationProjectionMap,
    ) -> tuple[_SelectedDocument, ...]:
        if not isinstance(authorization, AuthorizationProjectionMap):
            raise ProjectedRetrievalUnavailable("authorization map is invalid")
        if authorization.namespace_key != self.namespace_key:
            raise ProjectedRetrievalUnavailable(
                "authorization map namespace does not match projected index"
            )
        selections = {
            selection.item_identity: selection for selection in authorization.selections
        }
        if frozenset(selections) != frozenset(self._items):
            raise ProjectedRetrievalUnavailable(
                "authorization map does not cover the exact projection catalog"
            )

        selected: list[_SelectedDocument] = []
        for identity in sorted(self._items, key=_sort_key):
            item = self._items[identity]
            selection = selections[identity]
            if selection.content_hash != item.content_hash:
                raise ProjectedRetrievalUnavailable(
                    "authorization selection content hash does not match catalog"
                )
            if selection.projection_variant_id is None:
                continue
            variants = {
                variant.projection_variant_id: variant for variant in item.variants
            }
            variant = variants.get(selection.projection_variant_id)
            if variant is None:
                raise ProjectedRetrievalUnavailable(
                    "authorization selection names an unavailable projection variant"
                )
            if variant.item_identity != identity or variant.content_hash != item.content_hash:
                raise ProjectedRetrievalUnavailable(
                    "projection variant does not match its catalog item"
                )
            text = self._variant_text(variant)
            selected.append(_SelectedDocument(variant, text, tuple(bm25.tokenize(text))))
        return tuple(selected)

    @staticmethod
    def _hit(document: _SelectedDocument, score: float) -> ProjectedLexicalHit:
        compact = " ".join(document.text.split())
        return ProjectedLexicalHit(
            item_identity=document.variant.item_identity,
            projection_variant_id=document.variant.projection_variant_id,
            decision_level=document.variant.decision_level,
            score=score,
            search_fields=document.variant.search_fields,
            snippet=compact[:_SNIPPET_CHARS].rstrip(),
        )

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

        frequencies = Counter(
            token for document in documents for token in frozenset(document.tokens)
        )
        average_length = max(
            sum(len(document.tokens) for document in documents) / len(documents),
            1.0,
        )
        scores: list[tuple[_SelectedDocument, float]] = []
        for document in documents:
            term_counts = Counter(document.tokens)
            if any(term_counts[token] == 0 for token in query_tokens):
                continue
            score = 0.0
            for token in query_tokens:
                frequency = term_counts[token]
                document_frequency = frequencies[token]
                inverse_document_frequency = math.log(
                    1
                    + (len(documents) - document_frequency + 0.5)
                    / (document_frequency + 0.5)
                )
                length_normalization = 1 - _BM25_B + _BM25_B * (
                    len(document.tokens) / average_length
                )
                score += inverse_document_frequency * (
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
        """Match all normalized query tokens before applying the result cap."""

        limit = _result_limit(k)
        query_tokens = tuple(bm25.tokenize(query)) if isinstance(query, str) else ()
        documents = self._selected_documents(authorization)
        if not query_tokens:
            return ()
        matches = [
            document
            for document in documents
            if all(token in frozenset(document.tokens) for token in query_tokens)
        ]
        matches.sort(
            key=lambda document: (
                _sort_key(document.variant.item_identity),
                document.variant.projection_variant_id,
            )
        )
        return tuple(self._hit(document, 1.0) for document in matches[:limit])


__all__ = [
    "AuthorizationProjectionMap",
    "ProjectedLexicalHit",
    "ProjectedLexicalIndex",
    "ProjectedRetrievalUnavailable",
    "ProjectionSelection",
]
