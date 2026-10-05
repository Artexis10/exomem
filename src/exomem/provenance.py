"""provenance: read-only scan of `<!-- key:value -->` tags in note bodies.

The taste/opinion notes carry lightweight provenance as HTML comments —
`<!-- platform:imdb -->`, `<!-- conv:2026-06-01 -->`, `<!-- add-to-imdb -->`.
They're invisible to structured query, so reconciliation ("which takes are
flagged add-to-imdb but not yet pushed?") used to mean a manual full-text scan.

This module reads those tags at query time — an on-demand walk over markdown
bodies, the same cheap pass `audit` and keyword-`find` already do (<1s for
~600 files). Crucially it adds NO new state: the tags stay in the markdown
(the single source of truth); there is no index and no sidecar to drift.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import unquote

from . import context_refs, memory_refs, semantic_units
from . import find as find_module
from . import get_page as get_page_module
from .vault import FrontmatterError, kb_root, parse_frontmatter

if TYPE_CHECKING:
    from .semantic_contract import RelationFact


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _canonical_value(value: object) -> list:
    kind = type(value)
    if kind in {type(None), bool, int, float, str}:
        return [kind.__name__, value]
    if kind is datetime:
        stamp = value.astimezone(UTC) if value.tzinfo is not None else value
        return ["datetime", stamp.isoformat()]
    if kind is date:
        return ["date", value.isoformat()]
    if kind is bytes:
        return ["bytes", value.hex()]
    if kind is dict:
        entries = [[_canonical_value(key), _canonical_value(item)] for key, item in value.items()]
        return ["dict", sorted(entries, key=lambda entry: _canonical_json(entry[0]))]
    if kind in {list, tuple, set}:
        items = [_canonical_value(item) for item in value]
        return [kind.__name__, sorted(items, key=_canonical_json) if kind is set else items]
    raise ValueError("unsupported frontmatter value")


def evidence_version(content: str) -> str:
    """Bind retained body and typed YAML metadata, excluding only backlinks."""
    try:
        frontmatter, body, _ = parse_frontmatter(content, strict=True)
        material = {key: value for key, value in frontmatter.items() if key != "ingested_into"}
        encoded = _canonical_json(["exomem-evidence-v1", _canonical_value(material), body])
    except (FrontmatterError, TypeError, ValueError, OverflowError, RecursionError) as error:
        raise ValueError("EVIDENCE_VERSION_INVALID: metadata cannot bind retained evidence") from error
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


MAX_ORIGIN_BYTES = 16 * 1024
MAX_ORIGIN_ASSESSMENT_INPUTS = 8
MAX_ORIGIN_BINDINGS = 32
_ORIGIN_HASH_RE = re.compile(r"[0-9a-f]{64}")
_ORIGIN_COMMENT_RE = re.compile(r"<!--(?P<body>.*?)(?P<end>-->|\Z)", re.DOTALL)
# A reserved opener is found directly, never through another comment's span:
# a stray `<!--` earlier on the page must not swallow the carrier after it.
_ORIGIN_OPENER_RE = re.compile(r"<!--(?=\s*exomem-origin(?:[:\s]|-->|\Z))", re.IGNORECASE)
_ORIGIN_PAYLOAD_RE = re.compile(r"^\s*exomem-origin:v1\s+(.+?)\s*$", re.DOTALL)


@dataclass(frozen=True)
class OriginError(ValueError):
    code: str
    reason: str

    def __str__(self) -> str:
        return f"{self.code}: {self.reason}"


@dataclass(frozen=True)
class OriginDocument:
    """Syntactic metadata and reserved character spans, never release authority."""

    status: str
    payload: dict | None = None
    spans: tuple[tuple[int, int], ...] = ()
    reason: str | None = None

    def without_metadata(self, content: str) -> str:
        """Remove this document's designated spans from the same source text."""
        return remove_carriers(content, self.spans)


def _merged(spans: Iterable[tuple[int, int]]) -> tuple[tuple[int, int], ...]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return tuple(merged)


def remove_carriers(content: str, spans: Iterable[tuple[int, int]]) -> str:
    """Remove carrier spans; a carrier alone on its line takes the whole line.

    The writer places metadata on its own line, so removing that line restores
    the author's text exactly, leading blank lines included, and a carrier on
    its own line inside a list item leaves no whitespace-only line behind. A
    carrier written as its own paragraph, between blank lines, also takes one
    of them, so the paragraphs around it keep a single separator. A carrier
    sharing its line with prose removes only its own characters.
    """
    for start, end in sorted(spans, reverse=True):
        line_start = max(content.rfind("\n", 0, start), content.rfind("\r", 0, start)) + 1
        after = _blank_rest(content, end)
        if not content[line_start:start].strip(" \t") and after is not None:
            start, end = line_start, after
            following = _blank_rest(content, end) if end < len(content) else None
            if following is not None and _follows_blank_line(content, start):
                end = following
        content = content[:start] + content[end:]
    return content


def _blank_rest(content: str, index: int) -> int | None:
    """The offset past this line's terminator when only blanks remain on it, else None."""
    while index < len(content) and content[index] in " \t":
        index += 1
    if content.startswith("\r\n", index):
        return index + 2
    if index == len(content) or content[index] in "\r\n":
        return min(index + 1, len(content))
    return None


def _follows_blank_line(content: str, line_start: int) -> bool:
    """Whether the line before the one starting at `line_start` is blank."""
    if not line_start:
        return False
    end = line_start - (2 if content.startswith("\r\n", line_start - 2) else 1)
    begin = max(content.rfind("\n", 0, end), content.rfind("\r", 0, end)) + 1
    return not content[begin:end].strip(" \t")


@dataclass(frozen=True)
class OriginScopeMatch:
    """An exact owner-supplied output scope, never input authority."""

    status: str
    scope: dict | None = None
    unit: semantic_units.SemanticUnit | None = None
    text: str | None = None
    span: tuple[int, int] | None = None


def _origin_invalid(reason: str) -> OriginError:
    return OriginError("ORIGIN_METADATA_INVALID", reason)


def _origin_text(value: object) -> str:
    if type(value) is not str or not value.strip():
        raise _origin_invalid("expected a nonempty string")
    return value


def _origin_hash(value: object) -> str:
    text = _origin_text(value)
    if _ORIGIN_HASH_RE.fullmatch(text) is None:
        raise _origin_invalid("expected a canonical fingerprint")
    return text


def _origin_fragment(value: object) -> str:
    text = _origin_text(value)
    prefix, marker, fragment = text.partition("#")
    if (
        prefix
        or not marker
        or not fragment
        or "#" in fragment
        or semantic_units._anchored_unit_ref("", unquote(fragment)) != text
    ):
        raise _origin_invalid("expected a local exact unit fragment")
    return text


def _origin_reference(value: object, *, allow_unit: bool = True) -> str:
    text = _origin_text(value)
    parent, marker, fragment = text.partition("#")
    identity = memory_refs.parse_memory_ref(parent)
    if identity is None or memory_refs.memory_ref(identity) != parent:
        raise _origin_invalid("expected a canonical memory reference")
    if marker:
        if not allow_unit:
            raise _origin_invalid("expected a page reference")
        _origin_fragment("#" + fragment)
    return text


def _origin_span(value: object) -> dict:
    if type(value) is not dict or set(value) != {"start_offset", "end_offset"}:
        raise _origin_invalid("span fields are invalid")
    start, end = value["start_offset"], value["end_offset"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end:
        raise _origin_invalid("span offsets are invalid")
    return dict(value)


def _origin_labels(value: object, inputs: Mapping, *, assessment: bool = False) -> list[str]:
    if type(value) is not list or not value:
        raise _origin_invalid("input membership is invalid")
    labels = [_origin_text(label) for label in value]
    if len(set(labels)) != len(labels) or any(label not in inputs for label in labels):
        raise _origin_invalid("input membership is duplicate or dangling")
    if assessment and len(labels) > MAX_ORIGIN_ASSESSMENT_INPUTS:
        raise _origin_invalid("assessment has too many inputs")
    return sorted(labels)


def _origin_assessment(value: object, inputs: Mapping) -> dict:
    if type(value) is not dict or set(value) != {"inputs", "basis", "by", "reason"}:
        raise _origin_invalid("assessment fields are invalid")
    if value["basis"] != "agent_assessment":
        raise _origin_invalid("assessment basis is unsupported")
    return {
        "inputs": _origin_labels(value["inputs"], inputs, assessment=True),
        "basis": "agent_assessment",
        "by": _origin_text(value["by"]),
        "reason": _origin_text(value["reason"]),
    }


def _origin_scope(scope: object, *, authoring: bool) -> dict:
    variants = {
        "unit": ({"kind", "unit_ref"}, {"span"}, "fingerprint"),
        "relation": ({"kind", "relation", "direction", "peer"}, set(), "occurrence_fingerprint"),
        "field": ({"kind", "field"}, set(), "fingerprint"),
        "record_field": ({"kind", "collection_id", "item_key", "field"}, set(), "fingerprint"),
    }
    if type(scope) is not dict or _origin_text(scope.get("kind")) not in variants:
        raise _origin_invalid("scope kind is unsupported")
    kind = scope["kind"]
    required, optional, fingerprint = variants[kind]
    if not authoring:
        required = required | {fingerprint}
    if not required <= set(scope) or set(scope) - required - optional - {fingerprint}:
        raise _origin_invalid("scope fields are invalid")
    target = {key: _origin_text(scope[key]) for key in required - {fingerprint}}
    if fingerprint in scope:
        target[fingerprint] = _origin_hash(scope[fingerprint])
    if kind == "unit":
        _origin_fragment(scope["unit_ref"])
        if "span" in scope:
            target["span"] = _origin_span(scope["span"])
    elif kind == "relation":
        if scope["direction"] not in {"outbound", "inbound"}:
            raise _origin_invalid("relation direction is invalid")
        _origin_reference(scope["peer"], allow_unit=False)
    elif kind == "record_field":
        if memory_refs.normalize_id(scope["collection_id"]) != scope["collection_id"]:
            raise _origin_invalid("collection identity is not canonical")
    return target


def _origin_payload(value: object, *, authoring: bool) -> dict:
    if type(value) is not dict or set(value) != {"inputs", "assessments", "bindings"}:
        raise _origin_invalid("envelope fields are invalid")
    raw_inputs, assessments, bindings = value["inputs"], value["assessments"], value["bindings"]
    if type(raw_inputs) is not dict or type(assessments) is not list or type(bindings) is not list:
        raise _origin_invalid("envelope collections are invalid")
    if len(bindings) > MAX_ORIGIN_BINDINGS:
        raise _origin_invalid("too many output bindings")
    inputs = {}
    for label, raw in raw_inputs.items():
        _origin_text(label)
        if type(raw) is not dict or set(raw) - {"reference", "version", "unit_fingerprint", "span"}:
            raise _origin_invalid("retained input fields are invalid")
        if not {"reference", "version"} <= set(raw):
            raise _origin_invalid("retained input binding is missing")
        item = {
            "reference": _origin_reference(raw["reference"]),
            "version": _origin_hash(raw["version"]),
        }
        unit = "#" in item["reference"]
        if unit != ("unit_fingerprint" in raw):
            raise _origin_invalid("unit input needs its exact fingerprint")
        if unit:
            item["unit_fingerprint"] = _origin_hash(raw["unit_fingerprint"])
        if "span" in raw:
            item["span"] = _origin_span(raw["span"])
        inputs[label] = item
    normalized = {
        "inputs": inputs,
        "assessments": [_origin_assessment(item, inputs) for item in assessments],
        "bindings": [],
    }
    for binding in bindings:
        if type(binding) is not dict or set(binding) != {"inputs", "scope"}:
            raise _origin_invalid("output binding fields are invalid")
        target = _origin_scope(binding["scope"], authoring=authoring)
        normalized["bindings"].append(
            {
                "inputs": _origin_labels(binding["inputs"], inputs),
                "scope": target,
            }
        )
    return normalized


def _scope_fingerprint(kind: str, value: object) -> str:
    try:
        encoded = _canonical_json([f"exomem-origin-{kind}-v1", _canonical_value(value)])
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, OverflowError, RecursionError) as error:
        raise _origin_invalid("scope value has no supported typed encoding") from error


def match_origin_scope(
    scope: object,
    *,
    document: semantic_units.SemanticUnitDocument | None = None,
    fields: Mapping[str, object] | None = None,
    record_identity: tuple[str, str] | None = None,
    owner_ref: str | None = None,
    relations: Iterable[tuple[RelationFact, str | None, str | None]] = (),
    authoring: bool = False,
) -> OriginScopeMatch:
    """Match one bounded scope against current owner-supplied values, without I/O."""
    try:
        target = _origin_scope(scope, authoring=authoring)
        kind = target["kind"]
        fingerprint = "occurrence_fingerprint" if kind == "relation" else "fingerprint"
        unit = None
        selected_text = None
        selected_span = None
        if kind == "unit":
            if document is None or document.parent_ref is None:
                return OriginScopeMatch("unavailable")
            parent = document.parent_ref
            if parent.startswith("exomem://vault/"):
                # File Records already use the parser's path label, not a memory
                # UUID. Only their owning adapter may supply that exact label.
                path = unquote(parent.removeprefix("exomem://vault/"))
                if (
                    record_identity is None
                    or owner_ref != parent
                    or not path
                    or context_refs.vault_ref(path) != parent
                ):
                    return OriginScopeMatch("unavailable")
            else:
                parent = _origin_reference(parent, allow_unit=False)
            if owner_ref is not None and parent != owner_ref:
                return OriginScopeMatch("unavailable")
            resolution = document.resolve_unit(
                parent + target["unit_ref"], expected_fingerprint=target.get(fingerprint)
            )
            if resolution.status != "found":
                return OriginScopeMatch(resolution.status)
            unit = resolution.unit
            current = _origin_hash(unit.fingerprint)
            start, end = 0, len(unit.span.text)
            if "span" in target:
                start, end = target["span"]["start_offset"], target["span"]["end_offset"]
                if end > len(unit.span.text):
                    raise _origin_invalid("scope span exceeds its exact unit text")
            selected_text = unit.span.text[start:end]
            selected_span = (unit.span.start_offset + start, unit.span.start_offset + end)
        elif kind in {"field", "record_field"}:
            if fields is None:
                return OriginScopeMatch("unavailable")
            if kind == "record_field":
                if record_identity is None:
                    return OriginScopeMatch("unavailable")
                if record_identity != (target["collection_id"], target["item_key"]):
                    return OriginScopeMatch("missing")
            name = target["field"]
            if name not in fields:
                return OriginScopeMatch("missing")
            value = [name, fields[name]]
            if kind == "record_field":
                value = [*record_identity, *value]
            current = _scope_fingerprint(kind, value)
        else:
            if owner_ref is None:
                return OriginScopeMatch("unavailable")
            owner = _origin_reference(owner_ref, allow_unit=False)
            desired = (
                (owner, target["peer"])
                if target["direction"] == "outbound"
                else (target["peer"], owner)
            )
            candidates = set()
            missing = "missing"
            for fact, source, destination in relations:
                if fact.canonical_relation != target["relation"]:
                    continue
                if fact.target_status == "ambiguous":
                    missing = "ambiguous"
                    continue
                if (
                    not fact.authored
                    or fact.registry_status not in {"core", "extension", "alias"}
                    or fact.target_status != "resolved"
                    or source is None
                    or destination is None
                ):
                    if missing != "ambiguous":
                        missing = "unavailable"
                    continue
                endpoints = (
                    _origin_reference(source, allow_unit=False),
                    _origin_reference(destination, allow_unit=False),
                )
                if endpoints == desired:
                    candidates.add(
                        _scope_fingerprint(
                            kind, [_origin_text(fact.identity), fact.canonical_relation, *endpoints]
                        )
                    )
            if not candidates:
                return OriginScopeMatch(missing)
            expected = target.get(fingerprint)
            if expected is not None:
                if expected not in candidates:
                    return OriginScopeMatch("stale")
                current = expected
            elif len(candidates) != 1:
                return OriginScopeMatch("ambiguous")
            else:
                current = next(iter(candidates))
        if fingerprint in target and target[fingerprint] != current:
            return OriginScopeMatch("stale")
        target[fingerprint] = current
        return OriginScopeMatch("found", target, unit, selected_text, selected_span)
    except OriginError:
        if authoring:
            raise
        return OriginScopeMatch("unavailable")


def encode_origin(payload: object, *, authoring: bool = False) -> str:
    """Encode bounded syntactic provenance; authoring may omit a target fingerprint."""
    try:
        normalized = _origin_payload(payload, authoring=authoring)
        encoded = json.dumps(
            normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        encoded = encoded.replace("<", "\\u003c").replace(">", "\\u003e")
        block = "<!-- exomem-origin:v1 " + encoded + " -->"
        if len(block.encode("utf-8")) > MAX_ORIGIN_BYTES:
            raise OriginError("ORIGIN_METADATA_TOO_LARGE", "origin metadata exceeds its byte cap")
        return block
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        if isinstance(error, OriginError):
            raise
        raise _origin_invalid("origin metadata is not supported JSON") from error


def _origin_object(pairs: list[tuple[str, object]]) -> dict:
    value = {}
    for key, item in pairs:
        if key in value:
            raise _origin_invalid("JSON fields are duplicated")
        value[key] = item
    return value


def parse_origin(
    content: str, *, managed: bool, authoring: bool = False, strict: bool = False
) -> OriginDocument:
    """Read designated comments only; raw captures opt out with ``managed=False``."""
    if not managed:
        return OriginDocument("absent")
    if "<!--" not in content or "exomem-origin" not in content.lower():
        return OriginDocument("absent")
    from .markdown_regions import code_spans, outside

    blocks = []
    code = code_spans(content)
    for opener in _ORIGIN_OPENER_RE.finditer(content):
        if outside(content, opener.start(), code):
            # Every opener outside code or an escape is a carrier, even one
            # inside an earlier comment; each runs to its own first `-->`.
            blocks.append(_ORIGIN_COMMENT_RE.match(content, opener.start()))
    spans = _merged(match.span() for match in blocks)
    if not blocks:
        return OriginDocument("absent")
    try:
        if len(blocks) != 1:
            raise _origin_invalid("multiple designated origin blocks")
        block = blocks[0]
        match = _ORIGIN_PAYLOAD_RE.fullmatch(block.group("body"))
        if not block.group("end") or match is None:
            raise _origin_invalid("origin block is malformed or unsupported")
        if len(block.group().encode("utf-8")) > MAX_ORIGIN_BYTES:
            raise OriginError("ORIGIN_METADATA_TOO_LARGE", "origin metadata exceeds its byte cap")
        payload = json.loads(match.group(1), object_pairs_hook=_origin_object)
        payload = _origin_payload(payload, authoring=authoring)
        encode_origin(payload, authoring=authoring)
        return OriginDocument("valid", payload=payload, spans=spans)
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        refusal = (
            error
            if isinstance(error, OriginError)
            else _origin_invalid("origin metadata is not valid JSON")
        )
        if strict:
            if refusal is error:
                raise
            raise refusal from error
        return OriginDocument("unassessed", spans=spans, reason=refusal.reason)


def parse_owned_origin(content: str, *, owner_path: str) -> OriginDocument:
    """Identify managed attribution using its real owner, not a claimed page type."""
    if "<!--" not in content or "exomem-origin" not in content.lower():
        return OriginDocument("absent")
    from . import source_closure

    return parse_origin(content, managed=not source_closure._eligible_path(owner_path))  # noqa: SLF001


def origin_prose(content: str, *, owner_path: str) -> str:
    """Keep structured managed attribution out of prose, preserving raw captures."""
    return parse_owned_origin(content, owner_path=owner_path).without_metadata(content)


# A key:value token inside a comment. Value runs to the next whitespace.
_TAG_RE = re.compile(r"([A-Za-z][\w-]*)\s*:\s*([^\s]+)")


@dataclass
class ProvenanceFinding:
    path: str            # vault-relative, with .md
    line_number: int     # 1-based, body-relative (frontmatter excluded)
    row_text: str        # the full source line
    tags: dict[str, str]  # merged key:value across all comments on the line

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "line_number": self.line_number,
            "row_text": self.row_text,
            "tags": self.tags,
        }


def _resolve_filter(
    tag: str | None, key: str | None, value: str | None
) -> tuple[str | None, str | None]:
    """Fold `tag` ("key" or "key:value") into (key, value); explicit args win.

    Returns lowercased (key, value) for case-insensitive comparison, or Nones.
    """
    if tag:
        if ":" in tag:
            t_key, t_val = tag.split(":", 1)
        else:
            t_key, t_val = tag, None
        key = key or t_key
        if value is None:
            value = t_val
    return (key.lower() if key else None, value.lower() if value else None)


def _scan_body(
    rel_path: str, body: str, key_f: str | None, value_f: str | None
) -> list[ProvenanceFinding]:
    if "<!--" not in body:
        return []
    from .markdown_regions import scan_markdown

    regions = scan_markdown(body)
    comments_by_line: dict[int, list[str]] = {}
    for start, end in regions.comment_spans:
        comment = body[start:end]
        if "\n" in comment or "\r" in comment or not comment.endswith("-->"):
            continue
        lineno = body.count("\n", 0, start) + 1
        comments_by_line.setdefault(lineno, []).append(comment)
    findings: list[ProvenanceFinding] = []
    for lineno, line in enumerate(body.split("\n"), start=1):
        tags: dict[str, str] = {}
        for comment in comments_by_line.get(lineno, ()):
            for tm in _TAG_RE.finditer(comment.removeprefix("<!--").removesuffix("-->")):
                tags[tm.group(1)] = tm.group(2)  # last-wins on duplicate keys
        if not tags:
            continue
        if key_f is not None:
            matched = next((k for k in tags if k.lower() == key_f), None)
            if matched is None:
                continue
            if value_f is not None and tags[matched].lower() != value_f:
                continue
        findings.append(
            ProvenanceFinding(
                path=rel_path, line_number=lineno, row_text=line, tags=tags
            )
        )
    return findings


def scan_provenance(
    vault_root: Path,
    *,
    tag: str | None = None,
    key: str | None = None,
    value: str | None = None,
    path: str | None = None,
) -> list[ProvenanceFinding]:
    """Scan note bodies for provenance tags. Read-only; no index/sidecar.

    Filter by `tag` ("key" or "key:value" shorthand), or explicit `key`/`value`.
    Restrict to one file with `path` (else the whole Knowledge Base is walked).
    Line numbers are body-relative (provenance never lives in frontmatter).
    """
    key_f, value_f = _resolve_filter(tag, key, value)

    if path is not None:
        res = get_page_module.get_page(vault_root, path=path)
        return _scan_body(res.path, res.body, key_f, value_f)

    findings: list[ProvenanceFinding] = []
    for p in find_module._walk_md(kb_root(vault_root)):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        page = find_module._parse_page(p, mtime, vault_root)
        if page is None:
            continue
        findings.extend(_scan_body(page.rel_path, page.body, key_f, value_f))
    findings.sort(key=lambda f: (f.path, f.line_number))
    return findings


def origin_keys(
    sources_by_path: Mapping[str, Iterable[str]],
    *,
    fallback: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """One independent-origin key per page. Overlapping Sources collapse.

    Pages that declare an overlapping Source belong to one derivative
    component (a union-find over shared Sources), so a single Source fanned
    out into many notes counts once and copies add nothing. A page with no
    declared Source is its own origin: `fallback[path]` when given (a session
    key, say), else `page:<path>`.
    """
    paths = list(sources_by_path)
    parent = list(range(len(paths)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    sources = [frozenset(sources_by_path[path]) for path in paths]
    first_by_source: dict[str, int] = {}
    for index, declared in enumerate(sources):
        for source in sorted(declared):
            union(index, first_by_source.setdefault(source, index))
    component: dict[int, set[str]] = {}
    for index, declared in enumerate(sources):
        if declared:
            component.setdefault(find(index), set()).update(declared)
    out: dict[str, str] = {}
    for index, path in enumerate(paths):
        if sources[index]:
            encoded = json.dumps(
                sorted(component[find(index)]),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            out[path] = "source:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        else:
            out[path] = (fallback or {}).get(path) or f"page:{path}"
    return out
