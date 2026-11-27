"""Ranked evidence for caller-declared passages, bound to current Source bytes."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import logging
import re
import sys
import threading
from array import array
from collections import OrderedDict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, fields
from itertools import pairwise
from pathlib import Path
from typing import Any

from . import (
    cli_ops,
    embeddings,
    freshness,
    memory_refs,
    note_types,
    recall_space,
    runtime_resources,
    structured_filters,
)
from .find_types import ParsedPage
from .log_events import log_event
from .source_coverage import CAPTURE_HEADING

_log = logging.getLogger(__name__)

# This heading and fence are the fixed declaration format, never a vocabulary of key meanings.
_KEY_BLOCK_START = "## Passage keys\n\n```json\n"
_KEY_BLOCK_END = "\n```\n\n"
MAX_RESULTS = 200
#: `vector`, also used when the caller names no mode, ranks by dense similarity alone;
#: `hybrid` fuses lexical matching with it.
PASSAGE_MODES = ("vector", "hybrid")  # nosemgrep: ep-word-set -- the search protocol's mode tokens
#: How deep each ranking reaches before fusion: the most passages a request may ask for, so a
#: passage the other ranking places within any limit is never cut before it can vote.
FUSION_DEPTH = MAX_RESULTS


@dataclass(frozen=True)
class KeyBlock:
    start: int
    end: int
    keys: tuple[str, ...] | None


def render_key_block(keys: list[str]) -> str:
    """Keep even newlines, quotes and fences in opaque keys on one JSON line."""
    return _KEY_BLOCK_START + json.dumps(keys) + _KEY_BLOCK_END


def parse_key_block(body: str, *, decode_keys: bool = True) -> KeyBlock | None:
    """Parse only the declaration before Capture; retain its span even when JSON is invalid."""
    head, capture, _text = body.partition(CAPTURE_HEADING + "\n\n")
    start = head.find(_KEY_BLOCK_START)
    if start < 0 or (start and head[start - 1] != "\n"):
        return None
    keys = None
    if decode_keys and capture and head.endswith(_KEY_BLOCK_END):
        try:
            value = json.loads(head[start + len(_KEY_BLOCK_START) : -len(_KEY_BLOCK_END)])
        except ValueError:
            pass
        else:
            if isinstance(value, list) and all(isinstance(key, str) for key in value):
                keys = tuple(value)
    return KeyBlock(start, len(head), keys)


def has_declaration(frontmatter: Mapping[str, Any], vault_root: Path | None) -> bool:
    return "declared_passages" in frontmatter and note_types.Basis(vault_root).selects(
        frontmatter.get("type"), note_types.captured_source
    )


def without_passage_keys(body: str, frontmatter: Mapping[str, Any], vault_root: Path | None) -> str:
    """Withhold a declared Source's key block from every search field."""
    if not has_declaration(frontmatter, vault_root):
        return body
    block = parse_key_block(body, decode_keys=False)
    return body[: block.start] + body[block.end :] if block is not None else body


@dataclass(frozen=True, slots=True)
class PassageChunk:
    key: str
    ordinal: int
    passage_hash: str
    start: int
    length: int
    text: str


def declared_chunks(page: ParsedPage) -> tuple[PassageChunk, ...] | None:
    """The single declared-passage cut, cached on this immutable parsed page."""
    if not has_declaration(page.frontmatter, page.vault_root):
        return None
    if "_declared_passage_chunks" in page.__dict__:
        return page.__dict__["_declared_passage_chunks"]
    count = page.frontmatter["declared_passages"]
    block = parse_key_block(page.body)
    keys = block.keys if block is not None else None
    marker = CAPTURE_HEADING + "\n\n"
    head, found, capture = page.body.partition(marker)
    texts = capture.removesuffix("\n").split("\n\n") if found else []
    chunks: list[PassageChunk] = []
    if (
        isinstance(count, int)
        and not isinstance(count, bool)
        and keys is not None
        and count == len(keys) == len(texts)
    ):
        offset = len(head) + len(marker)
        for ordinal, (key, text) in enumerate(zip(keys, texts, strict=True), 1):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            for start, end in pairwise(_cuts(text)):
                chunks.append(
                    PassageChunk(key, ordinal, digest, offset + start, end - start, text[start:end])
                )
            offset += len(text) + 2
    result = tuple(chunks)
    page.__dict__["_declared_passage_chunks"] = result
    return result


def _cuts(text: str) -> list[int]:
    """Where one declared passage splits into encoder-sized chunks, its start and end included."""
    cuts = [0]
    if embeddings._needs_character_cap(text):
        cursor = 0
        for piece in embeddings._split_by_characters(text)[:-1]:
            cursor = text.index(piece, cursor) + len(piece)
            cuts.append(cursor)
    elif len(text.split()) > embeddings.MAX_WORDS_PER_CHUNK:
        # Whitespace tokens define the fixed chunk cap; no prose meaning is inferred.
        # str.split and \S share one definition of whitespace: under the cap, no cut.
        cuts.extend(
            word.start()
            for number, word in enumerate(re.finditer(r"\S+", text))
            if number and number % embeddings.MAX_WORDS_PER_CHUNK == 0
        )
    cuts.append(len(text))
    return cuts


def chunk_texts(passages: Iterable[str]) -> list[str]:
    """The texts the index encodes for these declared passages, in order.

    A declared passage is embedded exactly as the caller sent it; the title adds no signal.
    """
    return [text[start:end] for text in passages for start, end in pairwise(_cuts(text))]


def packed_cut(chunks: Sequence[PassageChunk]) -> bytes:
    """A Source's cut without its text: each chunk's passage ordinal, body start and length."""
    return array(
        "q", [value for chunk in chunks for value in (chunk.ordinal, chunk.start, chunk.length)]
    ).tobytes()


class _PublishedChunks(Sequence[PassageChunk]):
    """A Source's chunks read from a cut published for these exact bytes, built only when asked."""

    def __init__(self, body: str, keys: tuple[str, ...], cut: bytes) -> None:
        self._body = body
        self._keys = keys
        self._cut = array("q")
        self._cut.frombytes(cut)
        self._digests: dict[int, str] = {}

    def __len__(self) -> int:
        return len(self._cut) // 3

    def __getitem__(self, number):  # type: ignore[override]  # A chunk number, never a slice.
        if not 0 <= number < len(self):
            raise IndexError(number)
        cut = self._cut
        ordinal, start, length = cut[3 * number : 3 * number + 3]
        if ordinal not in self._digests:
            # One passage's chunks are adjacent, so its span runs from its first chunk to its last.
            first = last = number
            while first and cut[3 * (first - 1)] == ordinal:
                first -= 1
            while last + 1 < len(self) and cut[3 * (last + 1)] == ordinal:
                last += 1
            passage = self._body[cut[3 * first + 1] : cut[3 * last + 1] + cut[3 * last + 2]]
            self._digests[ordinal] = hashlib.sha256(passage.encode("utf-8")).hexdigest()
        return PassageChunk(
            self._keys[ordinal - 1], ordinal, self._digests[ordinal], start, length,
            self._body[start : start + length],
        )


def source_chunks(page: ParsedPage, entry: Any | None) -> Sequence[PassageChunk] | None:
    """`declared_chunks`, read from the capture inventory's cut when it cut these exact bytes."""
    cut = getattr(entry, "chunk_cut", None)
    if cut is not None and entry.source_hash == page.snapshot_hash and has_declaration(
        page.frontmatter, page.vault_root
    ):
        block = parse_key_block(page.body)
        if block is not None and block.keys is not None:
            return _PublishedChunks(page.body, block.keys, cut)
    return declared_chunks(page)


def validate_ranking_options(options: Mapping[str, Any]) -> None:
    """Refuse explicit rankers that passage recall cannot apply."""
    mode = options.get("mode")
    if mode is not None and mode not in PASSAGE_MODES:
        raise ValueError(
            f"INVALID_PASSAGE_OPTION: set mode to {' or '.join(PASSAGE_MODES)}, or omit it, "
            "when result_level is passage"
        )
    # These are closed search protocol fields and their ranking-neutral values, not vocabulary.
    neutral = {
        "graph": False,
        "rerank": False,
        "rerank_max_candidates": None,
        "prefer_compiled": False,
        "prefer_active": False,
        "prefer_used": False,
        "widen_outside_kb": False,
        "graph_enrich": False,
    }
    for name, value in options.items():
        if name in neutral and value is not None and value != neutral[name]:
            raise ValueError(
                f"INVALID_PASSAGE_OPTION: remove {name} when result_level is passage; "
                "passages rank by dense similarity and lexical matching only"
            )


def search_memory(vault_root: Path, request: Mapping[str, Any]) -> dict:
    """Enter the existing governed find scope without the page result pipeline."""
    from . import find as find_module
    from .governance.principal import declared_purpose_scope

    for option in ("deep", "graph_enrich", "continuation"):
        if request.get(option):
            raise ValueError(
                f"INVALID_PASSAGE_OPTION: remove {option} when result_level is passage"
            )
    shortcuts = {
        field.name: request[field.name]
        for field in fields(structured_filters.FilterShortcuts)
        if field.name in request
    }
    with declared_purpose_scope(request.get("purpose")):
        return find_module.find(
            vault_root,
            query=request["query"],
            result_level="passage",
            limit=request["limit"],
            scope=request["scope"],
            mode=request["mode"],
            filters=request["filters"],
            relations=request["relations"],
            relation_of=request["relation_of"],
            relation_direction=request["relation_direction"],
            # `find` refuses a person restriction on passages rather than drop it.
            people=request.get("people"),
            **shortcuts,
        )


_DECLARED_SOURCES: OrderedDict[Path, tuple[tuple, tuple, frozenset[str]]] = OrderedDict()
_DECLARED_SOURCES_LOCK = threading.Lock()


def declared_sources(vault_root: Path, publication) -> frozenset[str]:
    """Project already-parsed metadata once per published semantic corpus state."""
    from . import semantic_contract

    census = publication.census
    types = note_types.Basis(vault_root)
    dependency = types.dependency
    root = vault_root.resolve()
    with _DECLARED_SOURCES_LOCK:
        cached = _DECLARED_SOURCES.get(root)
        if census is not None and cached is not None and cached[:2] == (census, dependency):
            _DECLARED_SOURCES.move_to_end(root)
            return cached[2]
        paths = frozenset(
            path
            for path, page in publication.pages()
            if "declared_passages" in page.frontmatter
            and types.selects(page.page_type, note_types.captured_source)
        )
        if census is not None:
            _DECLARED_SOURCES[root] = (census, dependency, paths)
            # Match the corpus cache's retention bound without retaining page bodies here.
            while len(_DECLARED_SOURCES) > semantic_contract._CORPUS_CONTEXT_CACHE_MAX_VAULTS:
                _DECLARED_SOURCES.popitem(last=False)
        return paths


@dataclass(frozen=True, slots=True)
class _VerifiedSource:
    """What a passage hit takes from one Source page: its identity fields and selected chunks."""

    exomem_id: str | None
    #: The page's `source_identity` frontmatter as canonical JSON. A string cannot change, its size
    #: is its weight, and each hit decodes its own copy, so no reply shares what memory holds.
    identity_json: str | None
    #: Chunk number to chunk, for the selected numbers the Source's cut holds. Never changed after
    #: creation, so readers may share it.
    chunks: Mapping[int, PassageChunk]
    #: The identity itself when JSON does not hold it as parsed (a YAML set, date or non-string
    #: key). Such a Source is answered but never kept in memory.
    unencoded_identity: Any = None

    def source_identity(self) -> Any:
        """The page's `source_identity`, as a copy that only one hit holds."""
        if self.identity_json is None:
            return copy.deepcopy(self.unencoded_identity)
        return json.loads(self.identity_json)


def _identity_json(identity: Any) -> str | None:
    """`identity` as canonical JSON, or None when decoding that JSON would not give it back."""
    try:
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):  # A YAML set, date or tag, or a recursive alias.
        return None
    # JSON turns a number key into a string key, so only an exact round trip is kept.
    return encoded if json.loads(encoded) == identity else None


def _tuple_size(value: Any) -> int:
    """The bytes a proof value keeps alive: a tuple of strings, numbers and nested tuples."""
    weight = sys.getsizeof(value)
    if isinstance(value, tuple):
        weight += sum(_tuple_size(item) for item in value)
    return weight


def _weigh(key: tuple, source: _VerifiedSource, proof: tuple) -> int:
    """The bytes one memory entry keeps alive: its key's path and hash, its proof, its record and Source.

    The vault root and the scope are shared by every entry and are not counted. Each request builds
    its own proof, so every entry is charged for one in full; entries that share a proof are
    over-counted, which keeps the bound true. Every `PassageChunk` field is weighed, so a field
    added there is counted here too.
    """
    weight = sys.getsizeof(key) + sum(sys.getsizeof(part) for part in key[2:]) + _tuple_size(proof)
    weight += sys.getsizeof((None, None))  # The entry's (proof, source) record.
    weight += sys.getsizeof(source) + sys.getsizeof(source.exomem_id)
    weight += sys.getsizeof(source.identity_json) + sys.getsizeof(source.chunks)
    for number, chunk in source.chunks.items():
        weight += sys.getsizeof(number) + sys.getsizeof(chunk)
        weight += sum(sys.getsizeof(getattr(chunk, field.name)) for field in fields(chunk))
    return weight


class _VerifiedSources:
    """The selected chunks of published Source pages, so a repeated selection reads no Source.

    An entry is keyed by vault, scope, path and the hash that the publication holds for the page,
    and holds only chunks cut from bytes that hashed to it, so it serves no other bytes. It serves
    while its proof (`find._eligibility_proof`) holds: the recall checkpoint moves with every
    watched edit, and the retirement generation moves once a Source erasure has removed its rows.
    Stat data plays no part. Chunks, not parsed pages, are kept: a full corpus's pages outgrow the
    page cache's byte bound, so page-sized entries would be evicted as fast as its pages are.
    """

    def __init__(self, *, max_entries: int, max_bytes: int) -> None:
        self.max_entries = max_entries
        self.max_bytes = max_bytes
        self._entries: OrderedDict[tuple, tuple[tuple, _VerifiedSource]] = OrderedDict()
        self._held = 0
        self._lock = threading.Lock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def held_bytes(self) -> int:
        """The bytes the bound applies to: every entry's weight and the ordered map that holds them."""
        return self._held + sys.getsizeof(self._entries)

    def get(self, key: tuple, proof: tuple, numbers: set[int]) -> _VerifiedSource | None:
        """The entry for `key` when it was kept under `proof` and holds every chunk in `numbers`."""
        with self._lock:
            held = self._entries.get(key)
            if held is None or held[0] != proof:
                if held is not None:
                    self._drop(key)
                return None
            if not numbers <= held[1].chunks.keys():
                return None
            self._entries.move_to_end(key)
            return held[1]

    def keep(self, verified: Mapping[tuple, _VerifiedSource], proof: tuple) -> None:
        """Add the chunks one search read under `proof`, then evict down to both bounds."""
        with self._lock:
            for key, source in verified.items():
                held = self._entries.get(key)
                if held is not None and held[0] == proof:
                    source = _VerifiedSource(
                        source.exomem_id, source.identity_json, {**held[1].chunks, **source.chunks}
                    )
                self._drop(key)
                self._entries[key] = (proof, source)
                self._held += _weigh(key, source, proof)
            while self._entries and (
                len(self._entries) > self.max_entries or self.held_bytes() > self.max_bytes
            ):
                self._drop(next(iter(self._entries)))

    def forget(self, key: tuple) -> None:
        """Drop an entry that a read has shown no longer describes the page."""
        with self._lock:
            self._drop(key)

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._held = 0

    def _drop(self, key: tuple) -> None:
        held = self._entries.pop(key, None)
        if held is not None:
            self._held -= _weigh(key, held[1], held[0])


# An entry weighs about 1,450 bytes with its proof, and each chunk of 500 characters about 900 more,
# so 16 MiB holds 4096 Sources with about 12,000 chunks among them. `find.unload_ram_caches` releases it with the
# page cache.
_VERIFIED_SOURCES = _VerifiedSources(max_entries=4096, max_bytes=16 * 1024 * 1024)


def _verified_sources(
    vault_root: Path, scope: str, snapshot, publication, selected: Sequence[tuple]
) -> dict[str, _VerifiedSource | None]:
    """Each selected Source's selected chunks, from memory while its proof holds, else read again.

    None for a Source whose page is not admitted, cannot be read now, or no longer has the bytes the
    published corpus holds for it.
    """
    from . import find as find_module
    from . import semantic_contract

    wanted: dict[str, set[int]] = {}
    for path, number, _stored, _score in selected:
        wanted.setdefault(path, set()).add(number)
    proof = find_module._eligibility_proof(vault_root, scope, snapshot)
    root = vault_root.absolute()
    sources: dict[str, _VerifiedSource | None] = {}
    verified: dict[tuple, _VerifiedSource] = {}
    entries = None
    for path, numbers in wanted.items():
        published = publication.source_hash(path)
        key = (root, scope, path, published)
        if proof is not None and published is not None:
            held = _VERIFIED_SOURCES.get(key, proof, numbers)
            if held is not None:
                sources[path] = held
                continue
        if entries is None:
            inventory = semantic_contract.borrow_capture_inventory(vault_root)
            entries = inventory[0].entries if inventory is not None else {}
        # Passage search admits no catalogue view, so this read checks no catalogue hash.
        page = find_module._resolve_page(vault_root, path)
        # A passage answers only from published bytes, as the memory does, so other bytes are withheld.
        if page is None or published is None or page.snapshot_hash != published:
            sources[path] = None
            _VERIFIED_SOURCES.forget(key)
            continue
        chunks = source_chunks(page, entries.get(path))
        exomem_id = page.frontmatter.get("exomem_id")
        identity = page.frontmatter.get("source_identity")
        identity_json = _identity_json(identity)
        source = sources[path] = _VerifiedSource(
            exomem_id,
            identity_json,
            {
                number: chunks[number]
                for number in numbers
                if chunks is not None and 0 <= number < len(chunks)
            },
            None if identity_json is not None else identity,
        )
        # Only a string id and an identity held as JSON have an exact weight; others are not kept.
        if identity_json is not None and isinstance(exomem_id, str):
            verified[key] = source
        else:
            _VERIFIED_SOURCES.forget(key)
    # Proved before the reads and again before the insert, as for the eligible set: chunks read
    # across a corpus change or a Source erasure are answered but never kept.
    if verified and proof is not None and find_module._eligibility_proof(vault_root, scope, snapshot) == proof:
        _VERIFIED_SOURCES.keep(verified, proof)
    return sources


def _require_authority(vault_root: Path) -> None:
    from . import source_capture

    # Whole-namespace authority avoids per-candidate body admission; non-owners retain page search.
    try:
        source_capture.require_authority(vault_root)
    except source_capture.SourceCaptureError:
        raise cli_ops.OpError(
            "PASSAGE_AUTHORITY_REQUIRED",
            "Passage search requires whole-namespace content authority.",
            "Use an authorized service connection, or use ordinary page search.",
        ) from None


def _anchor_visible(vault_root: Path, path: str) -> bool:
    from .governance import lifecycle

    # Namespace authority replaces per-page admission; the anchor's bytes are not result evidence.
    _require_authority(vault_root)
    return not lifecycle.is_tombstoned(vault_root, path)


def find(vault_root: Path, request: Mapping[str, Any]) -> dict:
    """Resolve eligibility first, select chunks once, then verify their byte binding."""
    _require_authority(vault_root)
    limit = request["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_RESULTS:
        raise ValueError(f"INVALID_PASSAGE_LIMIT: set limit to an integer from 1 to {MAX_RESULTS}")
    scope = request["scope"]
    # nosemgrep: ep-word-membership -- The find API fixes these scope tokens.
    if scope not in ("kb", "kb-only", "vault"):
        raise ValueError("INVALID_PASSAGE_SCOPE: set scope to kb, kb-only, or vault")
    query = request["query"]
    if not query or not query.strip():
        raise ValueError("INVALID_PASSAGE_QUERY: supply a nonempty query to rank passages")
    validate_ranking_options({"mode": request.get("mode")})
    shortcuts = structured_filters.FilterShortcuts(
        **{
            field.name: request[field.name]
            for field in fields(structured_filters.FilterShortcuts)
            if request.get(field.name) is not None
        }
    )
    plan = structured_filters.compile_filter(request["filters"], shortcuts=shortcuts)
    # Admission refuses immediately; admitted encodes share the existing serialized execution turn.
    with runtime_resources.model_admission():
        try:
            return _find_admitted(vault_root, request, plan)
        except freshness.RecallProjectionUnavailable:
            raise _publication_unavailable() from None


def _stop_if_caller_left(phase: str) -> None:
    """End a search whose caller has gone, at the boundary before `phase`.

    FastMCP keeps a synchronous tool running after its caller times out, and only the
    serving host's cancel scope records the departure. The finished result would have no
    reader, while the work keeps an admission slot that a live caller is refused for.
    Other tools keep running on purpose: a capture's caller adopts its settled write.
    """
    from anyio import from_thread

    try:
        from_thread.check_cancelled()
    # The documented error of every anyio that mcp admits; `NoEventLoopError` exists only from 4.11.
    except RuntimeError:
        return  # A CLI or in-process call has no serving host, so no caller can leave it.
    except asyncio.CancelledError:
        log_event(_log, logging.INFO, "passage_search_abandoned", fields={"phase": phase})
        raise


class _CallerDeparture:
    """The serving host's record that the caller left, as the `StopSignal` a model-turn wait polls."""

    def is_set(self) -> bool:
        from anyio import from_thread

        try:
            from_thread.check_cancelled()
        except RuntimeError:
            return False  # No serving host, as in `_stop_if_caller_left`.
        except asyncio.CancelledError:
            return True
        return False


def _publication_unavailable() -> cli_ops.OpError:
    return cli_ops.OpError(
        "PASSAGE_INDEX_UNAVAILABLE",
        "The published passage index is unavailable.",
        "Let the service publish its corpus, or run its maintained warm-up before passage search.",
        details={"complete": False, "status": "unavailable"},
    )


def _find_admitted(vault_root: Path, request: Mapping[str, Any], plan) -> dict:
    from . import find as find_module
    from . import semantic_contract

    limit, scope, query = request["limit"], request["scope"], request["query"]
    plan = find_module._withhold_positions(vault_root, plan)
    _stop_if_caller_left("eligibility")
    publication = semantic_contract.borrow_published_corpus(vault_root)
    if publication is None:
        raise _publication_unavailable()
    snapshot = find_module.FreshnessSnapshot(vault_root, require_live_recall=True)
    walk_scope = "vault" if scope == "vault" else "kb"
    eligible = (
        find_module._reusable_eligible_filter_paths(
            vault_root, scope=walk_scope, plan=plan, snapshot=snapshot
        )
        if plan.root is not None
        else None
    )
    admit = request.get("admit_path") or find_module.caller_admission(vault_root)
    relations, _, _ = find_module._resolve_relation_filter(
        vault_root,
        relations=request["relations"],
        relation_of=request["relation_of"],
        relation_direction=request["relation_direction"],
        keep=admit,
        snapshot=snapshot,
        anchor_visible=lambda path: _anchor_visible(vault_root, path),
    )
    if relations is not None:
        eligible = set(relations) if eligible is None else eligible & relations
    allowed = set(declared_sources(vault_root, publication) & snapshot.recall_paths(walk_scope))
    if eligible is not None:
        allowed.intersection_update(eligible)
    if admit is not None:
        allowed = {path for path in allowed if admit(path)}
    _stop_if_caller_left("encode")
    index = embeddings.get_embedding_index(vault_root)
    identity = index.identity
    try:
        query_vector = embeddings.encode_query(index, query, cancel_event=_CallerDeparture())
    except runtime_resources.ModelBusyError:
        # The turn wait also ends when the caller leaves; that search stops before its encode.
        _stop_if_caller_left("encode")
        raise
    recall_space.require_same_space(index, identity, query_vector)
    _stop_if_caller_left("selection")
    fusing = request.get("mode") == "hybrid"
    # A fused search reads both rankings at one sidecar token, on one connection.
    selection = index.select_passage_chunks(
        query_vector, FUSION_DEPTH if fusing else limit,
        allowed_paths=allowed, encoded_for=identity, lexical_query=query if fusing else None,
    )
    lexical = selection.lexical
    selected = selection.hits[:limit] if lexical is None else _fused(selection.hits, lexical, limit)
    similarity = {(path, number): score for path, number, _ref, score in selection.hits}
    _stop_if_caller_left("verification")
    sources = _verified_sources(vault_root, walk_scope, snapshot, publication, selected)

    def chunk_of(path: str, number: int) -> PassageChunk | None:
        source = sources[path]
        return source.chunks.get(number) if source is not None else None

    matches = index.matching_chunks(
        [
            (path, number, stored, chunk.text if chunk is not None else None)
            for path, number, stored, _score in selected
            for chunk in (chunk_of(path, number),)
        ]
    )
    hits = []
    for path, number, _stored, score in selected:
        chunk = chunk_of(path, number)
        if chunk is None or (path, number) not in matches:
            continue
        source = sources[path]
        hits.append(
            {
                "ref": memory_refs.memory_ref(source.exomem_id),
                "path": path,
                "source_identity": source.source_identity(),
                "passage_key": chunk.key,
                "passage_ordinal": chunk.ordinal,
                "passage_hash": chunk.passage_hash,
                "span": {"start": chunk.start, "length": chunk.length},
                "score": score,
                # Unknown, not zero, for a lexical match beyond the dense ranking's depth.
                "similarity": similarity.get((path, number)),
            }
        )
    space = identity or recall_space.current_identity(len(query_vector))
    withheld = len(selected) - len(hits)
    summary = f"Found {len(hits)} passage matches."
    if withheld:
        summary += f" Withheld {withheld} chunks that do not match their current Source."
    reply = {
        "user_summary": summary,
        "hits": hits,
        "recall_space": asdict(space),
        "recall_space_id": space.identity_digest,
        # Fixed protocol tokens: how this reply was ranked, and which backend scored it.
        "ranking": "dense" if lexical is None else "fused",
        "scoring_mode": selection.scoring_mode,
        "withheld_chunks": withheld,
    }
    if fusing:
        # Fixed protocol tokens: whether the lexical stage could take part in this reply.
        lexical_state = (
            "ready" if lexical is not None
            else "failed" if index.passage_terms_failed()
            else "preparing"
        )
        # The count tells a fused reply whose lexical ranking was empty from one it changed.
        reply["diagnostics"] = {
            "lexical": lexical_state,
            "lexical_candidates": len(lexical) if lexical is not None else None,
        }
    return reply


def _fused(dense: list[tuple], lexical: list[tuple], limit: int) -> list[tuple]:
    """The top `limit` of both rankings by RRF, each scored with the dense score at its rank.

    A caller that maps or floors scores then sees the distribution of a dense-only reply,
    while the order is the fused one. Tied passages share the score of their first rank,
    and a reply holds no more passages than the dense ranking supplied.
    """
    from . import fusion, ranking_config

    stored = {(path, number): ref for path, number, ref, _score in lexical}
    stored.update({(path, number): ref for path, number, ref, _score in dense})
    ranked = fusion.reciprocal_rank_fusion_scored(
        [
            [((path, number), score) for path, number, _ref, score in hits]
            for hits in (dense, lexical)
        ],
        k=ranking_config._active_ranking().rrf_k,
    )[: min(limit, len(dense))]
    selected, value, score = [], None, None
    for rank, (key, fused) in enumerate(ranked):
        if rank == 0 or fused != value:
            value, score = fused, dense[rank][3]
        selected.append((key[0], key[1], stored[key], score))
    return selected
