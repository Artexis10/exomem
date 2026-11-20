"""The governed pointer write: the only way a page in an append-only tree changes after capture.

A caller declares what it changes: registry pointer fields (`page_fields`), the links that follow
pending moves, or the page's lineage. The write parses the page before and after, pairs its
frontmatter entries and its wikilinks, and admits a difference only inside a declared span:

- the value of a field that the `page-fields` registry declares with `role: pointer`;
- a wikilink target whose old and new forms name the same `exomem_id`;
- one lineage entry that a move appends after every byte of the page's `moved_from` list.

Every other byte must be identical, or the write refuses with `APPEND_ONLY`. The parse decides
what changed: the frontmatter through the YAML composer, the links through the wikilink grammar
(`vault.iter_body_wikilinks`), never a pattern over the page.

Each committed write leaves one create-only, owner-only record of its reason, its before and
after hashes and each span's location and bytes. The record is written pending before the commit
and published after it, so a write that never commits leaves no record (`recording`). An undoable
move also leaves an owner-only map from its operation id to the records of the pages it
repointed; its lineage entry names only the operation, so the page never says how many there
were. A revert applies only while the page holds the recorded after hash, and its result must
hash to the recorded before hash.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import os
import uuid
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import page_fields, page_lineage, private_state, vault
from .kbdir import managed_form, managed_relative

#: The owner-only store under the vault's state directory that holds the records.
STORE = "pointer-writes"
#: The owner-only store that maps an undoable move's operation id to its records, by page.
OPERATIONS = "pointer-write-operations"
_SCHEMA_VERSION = 1
#: The two span kinds a record holds; the record format fixes them.
FIELD_SPAN = "field"
LINK_SPAN = "link"
#: The refusal of a write to a page the call does not name, without the preview that listed it.
PREVIEW_REQUIRED = "POINTER_PREVIEW_REQUIRED"


class PointerWriteError(ValueError):
    """A change the pointer write does not admit, or a revert that no longer applies."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Declared:
    """What a call says it changes on one page.

    `fields` are frontmatter fields; only registry pointer fields are admitted. `moves` maps each
    pending move's old path to its new path: a link may change from a form that names the old
    page to a form that names the new one. `lineage` admits one entry appended to the page's
    `moved_from` list, after its earlier bytes; never a removal or a `reclassified_from` change.
    """

    fields: Collection[str] = frozenset()
    moves: Mapping[str, str] = field(default_factory=dict)
    lineage: bool = False


@dataclass(frozen=True)
class Span:
    kind: str
    #: The field name, or the old link target.
    key: str
    before_at: int
    after_at: int
    before: str
    after: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "key": self.key,
            "before_at": self.before_at,
            "after_at": self.after_at,
            "before": self.before,
            "after": self.after,
        }


@dataclass(frozen=True)
class PointerWrite:
    """A proven change to one page, ready to commit and record."""

    path: str
    reason: str
    before: str
    after: str
    spans: tuple[Span, ...]

    @property
    def before_hash(self) -> str:
        return vault.content_hash(self.before)

    @property
    def after_hash(self) -> str:
        return vault.content_hash(self.after)

    def reported(self) -> dict[str, str]:
        """The page as a receipt names it: its display title first, then its path."""
        front, body, _block = vault.parse_frontmatter(self.after)
        return {"title": vault.resolve_display_title(front, body, self.path), "path": self.path}


# --------------------------------------------------------------------------- proof


def _refuse(rel: str, what: str) -> PointerWriteError:
    return PointerWriteError(
        "APPEND_ONLY",
        f"{rel} is append-only, and the write would change {what}; only declared pointer "
        "spans may change",
    )


def _blocks(text: str) -> tuple[int, int, int] | None:
    """The offsets of a page's frontmatter block (start, end with its last line break) and body."""
    match = vault._FM_PATTERN.match(text)
    if match is None:
        return None
    end = match.end(1) + len(vault.frontmatter_line_break(text, match))
    return match.start(1), end, match.start(2)


def _cut_end(yaml_text: str, end: int) -> int:
    """`end` extended over the line break that directly follows a scalar entry, if any."""
    if end and yaml_text[end - 1] == "\n":
        return end
    for terminator in ("\r\n", "\n"):
        if yaml_text.startswith(terminator, end):
            return end + len(terminator)
    return end


def _without(yaml_text: str, spans: Mapping[str, tuple[int, int]], keys: Collection[str]) -> str:
    """`yaml_text` with the entries of `keys` cut out, each with its own line break."""
    kept = yaml_text
    cuts = sorted(
        ((spans[key][0], _cut_end(yaml_text, spans[key][1])) for key in keys if key in spans),
        reverse=True,
    )
    for start, end in cuts:
        kept = kept[:start] + kept[end:]
    return kept


def _link_spans(
    rel: str,
    before: str,
    after: str,
    offsets: tuple[int, int],
    same_page: Callable[[str, str], bool],
) -> list[Span]:
    """The link-target spans by which `after` differs from `before`, or a refusal.

    The two texts must hold the same links in the same order with identical bytes between and
    around their targets; a changed target must name the same page in both forms.
    """
    from .provenance import in_carrier

    old_links = list(vault.iter_body_wikilinks(before))
    new_links = list(vault.iter_body_wikilinks(after))
    if len(old_links) != len(new_links):
        raise _refuse(rel, "which links the page holds")
    # An origin carrier is recorded data, which the move's link rewrite never changes.
    in_old_carrier = in_carrier(before)
    spans: list[Span] = []
    old_at = new_at = 0
    for old, new in zip(old_links, new_links, strict=True):
        old_start, old_end = old.span(1)
        new_start, new_end = new.span(1)
        if before[old_at:old_start] != after[new_at:new_start]:
            raise _refuse(rel, "text outside a link target")
        old_target, new_target = old.group(1), new.group(1)
        if old_target != new_target:
            if in_old_carrier(old.start(), old.end()):
                raise _refuse(rel, "a link inside an origin carrier")
            old_path, old_mark, old_anchor = old_target.partition("#")
            new_path, new_mark, new_anchor = new_target.partition("#")
            if (old_mark, old_anchor) != (new_mark, new_anchor) or not same_page(
                old_path.strip(), new_path.strip()
            ):
                raise PointerWriteError(
                    "APPEND_ONLY",
                    f"{rel} is append-only, and a link it holds would name another page; a "
                    "repointed link must name the page it named",
                )
            spans.append(
                Span(
                    LINK_SPAN, old_path.strip(), offsets[0] + old_start, offsets[1] + new_start,
                    old_target, new_target,
                )
            )
        old_at, new_at = old_end, new_end
    if before[old_at:] != after[new_at:]:
        raise _refuse(rel, "text outside a link target")
    return spans


def identity_at(vault_root: Path, rel: str) -> str | None:
    """The `exomem_id` of the page the vault serves at `rel`, or None."""
    from . import memory_refs

    raw = vault.served_page_bytes(vault_root, rel)
    if raw is None:
        return None
    front, _body, block = vault.parse_frontmatter(raw.decode("utf-8", errors="replace"))
    return memory_refs.normalize_id(front.get(memory_refs.ID_FIELD)) if block is not None else None


def page_matcher(
    vault_root: Path, moves: Mapping[str, str], *, settled: bool = False
) -> Callable[[str, str], bool]:
    """Whether an old and a new link target name one page with an `exomem_id`, across `moves`.

    A target resolves where the vault serves it now. A target that names a pending move's new
    path, which holds no page yet, stands for the page that moves there. With `settled` the
    moves have committed: a target that names a move's old path stands for the page now at its
    new path, as a curation `repoint-links` step reads the pages its plan's moves placed.
    """
    root = Path(vault_root)
    arriving: dict[str, str] = {}
    for old_rel, new_rel in moves.items():
        named, held = (old_rel, new_rel) if settled else (new_rel, old_rel)
        no_ext = named.removesuffix(".md")
        for form in (no_ext, managed_relative(managed_form(no_ext))):
            arriving[form] = held

    held_resolver: list[vault.WikilinkResolver] = []

    def resolver() -> vault.WikilinkResolver:
        # One resolver per matcher: a resolver per link reads every page of the vault again.
        if not held_resolver:
            from .find import shared_resolver

            held_resolver.append(shared_resolver(root))
        return held_resolver[0]

    def page_of(target: str) -> str | None:
        key = target.removesuffix(".md")
        if key in arriving:
            return identity_at(root, arriving[key])
        canonical, warning = vault.normalize_wikilink(key, root, resolver_factory=resolver)
        if warning is not None:
            if "/" not in key:
                # A bare name that no live page holds, of a page that one of the moves renames.
                for form, held in arriving.items():
                    if form.rsplit("/", 1)[-1] == key:
                        return identity_at(root, held)
            return None
        resolved = canonical.split("#", 1)[0]
        return identity_at(root, resolved if resolved.endswith(".md") else resolved + ".md")

    def same_page(old_target: str, new_target: str) -> bool:
        old = page_of(old_target)
        return old is not None and old == page_of(new_target)

    return same_page


def _comments(rel: str, yaml_text: str) -> list[str]:
    """A frontmatter's comments in order, or a refusal of one that uses a YAML anchor or alias.

    A comment is the text between the parser's tokens, so this counts one inside an admitted
    span too, which the span comparison cannot see. An alias makes one field's parsed value
    depend on another field's bytes, so an admitted span could change an undeclared field.
    """
    try:
        tokens = list(vault.yaml.scan(yaml_text))
    except vault.yaml.YAMLError as error:
        raise PointerWriteError("INVALID_FRONTMATTER", "frontmatter is invalid") from error
    if any(isinstance(token, (vault.yaml.AnchorToken, vault.yaml.AliasToken)) for token in tokens):
        raise PointerWriteError(
            "APPEND_ONLY",
            f"{rel} is append-only, and its frontmatter uses YAML anchors or aliases, so no "
            "write can prove that only its declared fields change",
        )
    comments: list[str] = []
    at = 0
    for token in tokens:
        gap = yaml_text[at : token.start_mark.index]
        comments.extend(line.strip() for line in gap.splitlines() if line.strip())
        at = max(at, token.end_mark.index)
    return comments


def _require_lineage_append(
    rel: str, old_yaml: str, new_yaml: str, old_entry: str | None, new_entry: str | None
) -> None:
    """Refuse a lineage change other than one entry appended after the list's earlier bytes."""
    old_value = (vault.yaml.safe_load(old_yaml) or {}).get(page_lineage.LINEAGE_FIELD)
    new_value = (vault.yaml.safe_load(new_yaml) or {}).get(page_lineage.LINEAGE_FIELD)
    earlier = old_value or []
    appended = (
        isinstance(earlier, list)
        and isinstance(new_value, list)
        and new_value[:-1] == earlier
        and len(new_value) == len(earlier) + 1
        and page_lineage.entry_path(new_value[-1]) is not None
        and isinstance(new_value[-1], Mapping)
    )
    # A non-empty list keeps its bytes, comments included; the entry follows them. Cost: a flow
    # list (`[{path: ...}]`) has no byte-preserving append, so a move of such a page refuses.
    kept = not earlier or (
        old_entry is not None and new_entry is not None and new_entry.startswith(old_entry)
    )
    if not (appended and kept):
        raise _refuse(rel, f"its `{page_lineage.LINEAGE_FIELD}` field other than by one appended entry")


def prove(
    vault_root: Path,
    rel: str,
    before: str,
    after: str,
    declared: Declared,
    *,
    same_page: Callable[[str, str], bool] | None = None,
) -> tuple[Span, ...]:
    """The declared spans by which `after` differs from `before`, or `APPEND_ONLY`.

    `same_page` answers whether two link targets name one page; by default it reads the vault
    across `declared.moves` (`page_matcher`).
    """
    from .record_formats import FrontmatterSpliceError, frontmatter_entry_spans

    matcher = same_page or page_matcher(vault_root, declared.moves)
    old_blocks, new_blocks = _blocks(before), _blocks(after)
    if (old_blocks is None) != (new_blocks is None):
        raise _refuse(rel, "its frontmatter block")
    if old_blocks is None or new_blocks is None:
        return tuple(_link_spans(rel, before, after, (0, 0), matcher))
    old_start, old_end, old_body = old_blocks
    new_start, new_end, new_body = new_blocks
    if before[:old_start] != after[:new_start] or before[old_end:old_body] != after[new_end:new_body]:
        raise _refuse(rel, "its frontmatter fences")
    old_yaml, new_yaml = before[old_start:old_end], after[new_start:new_end]
    if _comments(rel, old_yaml) != _comments(rel, new_yaml):
        raise _refuse(rel, "a comment in its frontmatter")
    try:
        old_spans, new_spans = frontmatter_entry_spans(old_yaml), frontmatter_entry_spans(new_yaml)
    except FrontmatterSpliceError as error:
        raise PointerWriteError(error.code, error.reason) from error
    pointers = page_fields.pointer_fields(vault_root)
    admitted = {name for name in declared.fields if name in pointers}

    def entry(yaml_text: str, spans: Mapping[str, tuple[int, int]], key: str) -> str | None:
        span = spans.get(key)
        return None if span is None else yaml_text[span[0] : span[1]]

    def cut(yaml_text: str, spans: Mapping[str, tuple[int, int]], key: str, offset: int) -> tuple[int, str]:
        """An entry's location and bytes with its own line break; (-1, "") when it is absent."""
        if key not in spans:
            return -1, ""
        start, end = spans[key]
        return offset + start, yaml_text[start : _cut_end(yaml_text, end)]

    spans: list[Span] = []
    handled: list[str] = []
    for key in dict.fromkeys([*old_spans, *new_spans]):
        old_entry, new_entry = entry(old_yaml, old_spans, key), entry(new_yaml, new_spans, key)
        if old_entry == new_entry:
            continue
        handled.append(key)
        if key == page_lineage.LINEAGE_FIELD and declared.lineage:
            _require_lineage_append(rel, old_yaml, new_yaml, old_entry, new_entry)
            old_at, old_bytes = cut(old_yaml, old_spans, key, old_start)
            new_at, new_bytes = cut(new_yaml, new_spans, key, new_start)
            spans.append(Span(FIELD_SPAN, key, old_at, new_at, old_bytes, new_bytes))
        elif key in admitted:
            old_at, old_bytes = cut(old_yaml, old_spans, key, old_start)
            new_at, new_bytes = cut(new_yaml, new_spans, key, new_start)
            spans.append(Span(FIELD_SPAN, key, old_at, new_at, old_bytes, new_bytes))
        elif old_entry is not None and new_entry is not None:
            spans.extend(
                _link_spans(
                    rel, old_entry, new_entry,
                    (old_start + old_spans[key][0], new_start + new_spans[key][0]),
                    matcher,
                )
            )
        else:
            raise _refuse(rel, f"its `{key}` field")
    if _without(old_yaml, old_spans, handled) != _without(new_yaml, new_spans, handled):
        raise _refuse(rel, "its frontmatter outside the declared fields")
    spans.extend(
        _link_spans(rel, before[old_body:], after[new_body:], (old_body, new_body), matcher)
    )
    return tuple(spans)


def plan(
    vault_root: Path,
    rel: str,
    before: str,
    after: str,
    declared: Declared,
    *,
    reason: str,
    same_page: Callable[[str, str], bool] | None = None,
) -> PointerWrite:
    """Prove `after` against `before` and return the write to commit and record."""
    spans = prove(vault_root, rel, before, after, declared, same_page=same_page)
    return PointerWrite(rel, reason, before, after, spans)


# --------------------------------------------------------------------------- approval


def preview_digest(moves: Mapping[str, str], writes: Iterable[PointerWrite]) -> str:
    """The digest that binds a preview to the writes it listed on pages its call does not name.

    It covers each move and each write's page with its before and after hash, so a commit whose
    writes differ from the listed ones, or whose pages changed since, has another digest.
    """
    payload = {
        "moves": sorted(moves.items()),
        "writes": sorted((write.path, write.before_hash, write.after_hash) for write in writes),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()


def require_preview(
    moves: Mapping[str, str],
    writes: Collection[PointerWrite],
    digest: str | None,
    *,
    preview_call: str,
) -> None:
    """Refuse writes to pages the call does not name unless `digest` is the preview's digest.

    The call that names a page approves its own writes, so a caller passes only the others.
    """
    if not writes or digest == preview_digest(moves, writes):
        return
    stale = " The digest passed no longer matches them." if digest else ""
    raise PointerWriteError(
        PREVIEW_REQUIRED,
        f"this call repoints {len(writes)} append-only page(s) that it does not name.{stale} "
        f"Preview it with {preview_call}, review the listed outcomes and pass the preview's "
        "`preview_digest`.",
    )


# --------------------------------------------------------------------------- records


#: The stem suffix of a record written before its commit and not yet published.
_PENDING = ".pending"


def new_record_id() -> str:
    return uuid.uuid4().hex


#: An operation id has the shape of a record id; both are fresh random hex.
new_operation_id = new_record_id


def _is_id(value: object) -> bool:
    return isinstance(value, str) and bool(value) and all(c in "0123456789abcdef" for c in value)


@contextlib.contextmanager
def recording(
    vault_root: Path,
    writes: Sequence[tuple[PointerWrite, str]],
    *,
    operation: str | None = None,
) -> Iterator[list[str]]:
    """Record each write, under its id, around the commit that makes it; yield the ids.

    Each record is flushed pending before the block runs, so a crash after the commit keeps it,
    and published when the block returns. When the block raises, a record is published only
    while its page holds the write's after hash: a write that never committed leaves no record.
    With `operation`, the map from that operation id to each write's page and record is written
    first, and removed again when no record survives a failed block.
    """
    store = private_state.store(vault_root, STORE)
    operations = private_state.store(vault_root, OPERATIONS) if operation and writes else None
    if operations is not None:
        private_state.write_record(
            operations,
            str(operation),
            {
                "schema_version": _SCHEMA_VERSION,
                "operation": operation,
                "records": {write.path: record_id for write, record_id in writes},
            },
            temp_suffix=".tmp",
            durable=True,
            create_only=True,
        )
        private_state.sync_directory(operations)
    for write, record_id in writes:
        private_state.write_record(
            store,
            record_id + _PENDING,
            {
                "schema_version": _SCHEMA_VERSION,
                "id": record_id,
                "page": write.path,
                "reason": write.reason,
                "at": dt.datetime.now(dt.UTC).isoformat(),
                "before_hash": write.before_hash,
                "after_hash": write.after_hash,
                "spans": [span.as_dict() for span in write.spans],
            },
            temp_suffix=".tmp",
            durable=True,
            create_only=True,
        )
    if writes:
        private_state.sync_directory(store)
    try:
        yield [record_id for _write, record_id in writes]
    except BaseException:
        published = False
        for write, record_id in writes:
            if _holds(vault_root, write.path, write.after_hash):
                _publish(store, record_id)
                published = True
            else:
                private_state.remove(store / f"{record_id}{_PENDING}.json")
        if writes:
            private_state.sync_directory(store)
        if operations is not None and not published:
            private_state.remove(operations / f"{operation}.json")
            private_state.sync_directory(operations)
        raise
    for _write, record_id in writes:
        _publish(store, record_id)
    if writes:
        private_state.sync_directory(store)


def _holds(vault_root: Path, rel: str, content_hash: Any) -> bool:
    """Whether the page at `rel`, as the vault serves it, hashes to `content_hash`."""
    raw = vault.served_page_bytes(vault_root, rel)
    if raw is None:
        return False
    return vault.content_hash(raw.decode("utf-8", errors="replace")) == content_hash


def _publish(store: Path, record_id: str) -> None:
    """Give a pending record its final name; the folder entry is the caller's to flush."""
    pending = store / f"{record_id}{_PENDING}.json"
    try:
        os.link(pending, store / f"{record_id}.json")
    except FileExistsError:
        pass
    private_state.remove(pending)


def load_record(vault_root: Path, record_id: str) -> dict[str, Any]:
    """The published record `record_id`.

    A pending record, which a stop between a commit and its publication leaves, is published
    here when its page holds its after hash; otherwise its write never committed.
    """
    if not _is_id(record_id):
        raise PointerWriteError("POINTER_RECORD_UNKNOWN", "no pointer-write record has that id")
    store = private_state.store(vault_root, STORE)
    path = store / f"{record_id}.json"
    pending = store / f"{record_id}{_PENDING}.json"
    if not path.exists() and pending.exists():
        loaded = _read_record(pending)
        if loaded is not None and _holds(
            vault_root, str(loaded.get("page")), loaded.get("after_hash")
        ):
            _publish(store, record_id)
            private_state.sync_directory(store)
    loaded = _read_record(path)
    if loaded is None:
        raise PointerWriteError("POINTER_RECORD_UNKNOWN", "no pointer-write record has that id")
    if loaded.get("schema_version") != _SCHEMA_VERSION:
        raise PointerWriteError("POINTER_RECORD_UNKNOWN", "the pointer-write record is unreadable")
    return loaded


def operation_records(vault_root: Path, operation: str | None) -> dict[str, str]:
    """The record id of each append-only page that operation `operation` changed, by page.

    A move records its referrers' repoints and the moved page's own lineage write. An
    operation that changed no append-only page has no map, and so none. A caller loads only the
    records of the pages it may restore (`load_record`).
    """
    if operation is None:
        return {}
    if not _is_id(operation):
        raise PointerWriteError("POINTER_RECORD_UNKNOWN", "no move has that operation id")
    path = private_state.store(vault_root, OPERATIONS) / f"{operation}.json"
    if not path.exists():
        return {}
    loaded = _read_record(path)
    records = loaded.get("records") if loaded is not None else None
    if (
        loaded is None
        or loaded.get("schema_version") != _SCHEMA_VERSION
        or not isinstance(records, dict)
    ):
        raise PointerWriteError(
            "POINTER_RECORD_UNKNOWN", "the pointer-write records of that move are unreadable"
        )
    return {str(page): str(record_id) for page, record_id in records.items()}


def _read_record(path: Path) -> dict[str, Any] | None:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded if isinstance(loaded, dict) else None


def reverted_text(loaded: Mapping[str, Any], current: str) -> str:
    """The page's recorded before bytes, rebuilt from `current`, which must hold the after hash."""
    if vault.content_hash(current) != loaded.get("after_hash"):
        raise PointerWriteError(
            "POINTER_REVERT_STALE",
            f"{loaded.get('page')} changed after the pointer write, so it cannot be reverted",
        )
    text = current
    spans = sorted(loaded.get("spans") or (), key=lambda span: span["after_at"], reverse=True)
    for span in spans:
        at, new = span["after_at"], span["after"]
        if at < 0:
            raise PointerWriteError(
                "POINTER_REVERT_UNSUPPORTED",
                "the write removed a field, and its record holds no place to restore it",
            )
        if text[at : at + len(new)] != new:
            raise PointerWriteError("POINTER_REVERT_STALE", "the record does not match the page")
        text = text[:at] + span["before"] + text[at + len(new) :]
    if vault.content_hash(text) != loaded.get("before_hash"):
        raise PointerWriteError(
            "POINTER_REVERT_STALE", "the record does not rebuild the page's before bytes"
        )
    return text


def revert_write(
    loaded: Mapping[str, Any], record_id: str, current: str, *, path: str | None = None
) -> PointerWrite:
    """The write that returns a recorded page from `current` to its before bytes, to commit and record."""
    return PointerWrite(
        path or str(loaded.get("page")),
        f"revert of pointer write {record_id}",
        current,
        reverted_text(loaded, current),
        tuple(
            Span(
                span["kind"], span["key"], span["after_at"], span["before_at"],
                span["after"], span["before"],
            )
            for span in loaded.get("spans") or ()
        ),
    )

