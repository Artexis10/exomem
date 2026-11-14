"""Dense evidence for caller-declared passages, bound to current Source bytes."""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from . import (
    embeddings,
    memory_refs,
    note_types,
    recall_space,
    runtime_resources,
    structured_filters,
)
from .find_types import ParsedPage
from .source_coverage import CAPTURE_HEADING

# This heading and fence are the fixed declaration format, never a vocabulary of key meanings.
_KEY_BLOCK_START = "## Passage keys\n\n```json\n"
_KEY_BLOCK_END = "\n```\n\n"


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


@dataclass(frozen=True)
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
        title = page.title.strip()
        for ordinal, (key, text) in enumerate(zip(keys, texts, strict=True), 1):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
            cuts = [0]
            if embeddings._needs_character_cap(text):
                cursor = 0
                for piece in embeddings._split_by_characters(text)[:-1]:
                    cursor = text.index(piece, cursor) + len(piece)
                    cuts.append(cursor)
            else:
                # Whitespace tokens define the fixed chunk cap; no prose meaning is inferred.
                cuts.extend(
                    word.start()
                    for number, word in enumerate(re.finditer(r"\S+", text))
                    if number and number % embeddings.MAX_WORDS_PER_CHUNK == 0
                )
            cuts.append(len(text))
            for start, end in zip(cuts[:-1], cuts[1:], strict=True):
                piece = text[start:end]
                indexed = f"{title}\n\n{piece}" if title else piece
                chunks.append(
                    PassageChunk(key, ordinal, digest, offset + start, end - start, indexed)
                )
            offset += len(text) + 2
    result = tuple(chunks)
    page.__dict__["_declared_passage_chunks"] = result
    return result


def validate_ranking_options(options: Mapping[str, Any]) -> None:
    """Refuse explicit rankers that dense-only passage recall cannot apply."""
    # These are closed search protocol fields and their ranking-neutral values, not vocabulary.
    neutral = {
        "mode": "vector",
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
                "passages use dense similarity only"
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
            filters=request["filters"],
            relations=request["relations"],
            relation_of=request["relation_of"],
            relation_direction=request["relation_direction"],
            **shortcuts,
        )


_DECLARED_SOURCES: OrderedDict[Path, tuple[tuple, tuple, frozenset[str]]] = OrderedDict()
_DECLARED_SOURCES_LOCK = threading.Lock()


def declared_sources(vault_root: Path) -> frozenset[str]:
    """Project already-parsed metadata once per published semantic corpus state."""
    from . import semantic_contract

    corpus, census = semantic_contract._build_corpus_context_with_census(vault_root)
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
            for path, page in corpus.pages.items()
            if "declared_passages" in page.frontmatter
            and types.selects(page.page_type, note_types.captured_source)
        )
        if census is not None:
            _DECLARED_SOURCES[root] = (census, dependency, paths)
            # Match the corpus cache's retention bound without retaining page bodies here.
            while len(_DECLARED_SOURCES) > semantic_contract._CORPUS_CONTEXT_CACHE_MAX_VAULTS:
                _DECLARED_SOURCES.popitem(last=False)
        return paths


def find(vault_root: Path, request: Mapping[str, Any]) -> dict:
    """Resolve eligibility first, select chunks once, then verify their byte binding."""
    from . import find as find_module

    limit = request["limit"]
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("INVALID_PASSAGE_LIMIT: set limit to an integer from 1 to 200")
    scope = request["scope"]
    if scope not in ("kb", "kb-only", "vault"):
        raise ValueError("INVALID_PASSAGE_SCOPE: set scope to kb, kb-only, or vault")
    query = request["query"]
    if not query or not query.strip():
        raise ValueError("INVALID_PASSAGE_QUERY: supply a nonempty query to rank passages")
    shortcuts = structured_filters.FilterShortcuts(
        **{
            field.name: request[field.name]
            for field in fields(structured_filters.FilterShortcuts)
            if request.get(field.name) is not None
        }
    )
    plan = structured_filters.compile_filter(request["filters"], shortcuts=shortcuts)
    managed = find_module._managed_reader()
    pending = find_module._request_pending_coverage(vault_root, managed=managed, timings=None)
    snapshot = find_module.FreshnessSnapshot(vault_root, pending=pending)
    walk_scope = "vault" if scope == "vault" else "kb"
    eligible = (
        find_module._resolve_eligible_filter_paths(
            vault_root,
            scope=walk_scope,
            plan=plan,
            snapshot=snapshot,
            pending=pending,
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
    )
    if relations is not None:
        eligible = set(relations) if eligible is None else eligible & relations
    allowed = set(declared_sources(vault_root) & snapshot.recall_paths(walk_scope))
    if eligible is not None:
        allowed.intersection_update(eligible)
    if admit is not None:
        allowed = {path for path in allowed if admit(path)}
    index = embeddings.get_embedding_index(vault_root)
    identity = index.identity
    with runtime_resources.model_execution(wait=False), recall_space.encoding_for(index):
        query_vector = embeddings.embed_texts([query], is_query=True)[0]
    recall_space.require_same_space(index, identity, query_vector)
    selected = index.select_chunks(query_vector, limit, allowed_paths=allowed, encoded_for=identity)
    pages = {
        path: find_module._resolve_page(vault_root, path, pending)
        for path in dict.fromkeys(path for path, _number, _stored, _score in selected)
    }
    matches = index.matching_chunks(
        [
            (
                path,
                number,
                stored,
                chunks[number].text if chunks is not None and 0 <= number < len(chunks) else None,
            )
            for path, number, stored, _score in selected
            for chunks in (declared_chunks(pages[path]) if pages[path] is not None else None,)
        ]
    )
    hits = []
    for path, number, _stored, score in selected:
        page = pages[path]
        chunks = declared_chunks(page) if page is not None else None
        if page is None or (path, number) not in matches or chunks is None or number >= len(chunks):
            continue
        chunk = chunks[number]
        hits.append(
            {
                "ref": memory_refs.memory_ref(page.frontmatter["exomem_id"]),
                "path": path,
                "source_identity": page.frontmatter.get("source_identity"),
                "passage_key": chunk.key,
                "passage_ordinal": chunk.ordinal,
                "passage_hash": chunk.passage_hash,
                "span": {"start": chunk.start, "length": chunk.length},
                "score": score,
            }
        )
    space = identity or recall_space.current_identity(len(query_vector))
    withheld = len(selected) - len(hits)
    summary = f"Found {len(hits)} passage matches."
    if withheld:
        summary += f" Withheld {withheld} chunks that do not match their current Source."
    return {
        "user_summary": summary,
        "hits": hits,
        "recall_space": asdict(space),
        "withheld_chunks": withheld,
    }
