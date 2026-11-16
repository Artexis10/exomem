"""Caller-independent selector facts, derived with canonical page bytes (store design D13).

The table holds no policy membership or caller verdict. Every reader evaluates the current policy
through membership's own kernel, at the snapshot that holds the page's hash. Missing or older facts
leave the scalar release chain in charge.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from collections import OrderedDict
from dataclasses import asdict, fields
from pathlib import Path

from .. import document_containers, find_corpus, vault
from ..find_types import ParsedPage
from ..kbdir import managed_root_rel
from . import bridges, membership, raw_protection
from .policy import Policy

# Bump when normalized subjects or the simple-page proof change; independent of policy revisions.
FACTS_VERSION = 1


def create_schema(conn: sqlite3.Connection) -> None:
    """Create governance's derived table, inside the caller's transaction."""
    conn.execute("""CREATE TABLE IF NOT EXISTS page_admission(
        path TEXT PRIMARY KEY,
        sha256 TEXT NOT NULL,
        facts_version INTEGER NOT NULL,
        simple INTEGER NOT NULL CHECK(simple IN (0, 1)),
        subject TEXT NOT NULL
    )""")


def simple_page(path: str, raw: bytes, page: ParsedPage | None) -> bool:
    """Whether one page's scopes determine its content floor and release selection."""
    from ..collection_store import ownership as collections
    from ..vault_store import ownership

    if page is None:
        return False
    try:
        frontmatter, _, _ = vault.parse_frontmatter(raw.decode("utf-8"))
    except (UnicodeDecodeError, vault.FrontmatterError):
        return False
    # Custody reads raw bytes; normalized Markdown must agree before it can prove any claim absent.
    if frontmatter != page.frontmatter:
        return False
    return (
        ownership.classify(path) is ownership.Ownership.PAGE
        and path.lower().endswith(".md")
        # These closed custody, extraction and frame fields require the scalar dependency chain.
        and "container_member" not in frontmatter
        and "part_of" not in frontmatter and "parent_media" not in frontmatter
        # Only the parsed bridge grammar proves absence; the byte probe is a positive cache guard.
        and bridges.parse_bridge_frontmatter(frontmatter) == (None, None)
        and not raw_protection.marked(path)
        and not document_containers.in_payload_tree(path) and not collections.relevant(path)
    )


def replace_row(
    conn: sqlite3.Connection, path: str, raw: bytes | None, page: ParsedPage | None = None,
) -> None:
    """Replace the page's own facts or remove them, without reading a registry or deciding a caller."""
    from ..vault_store import ownership

    conn.execute("DELETE FROM page_admission WHERE path = ?", (path,))
    if raw is None or ownership.classify(path) is not ownership.Ownership.PAGE:
        return
    subject = None if page is None else asdict(membership.markdown_subject(page))
    conn.execute(
        "INSERT INTO page_admission(path, sha256, facts_version, simple, subject) VALUES (?, ?, ?, ?, ?)",
        (path, hashlib.sha256(raw).hexdigest(), FACTS_VERSION, simple_page(path, raw, page),
         json.dumps(subject, sort_keys=True, separators=(",", ":"))),
    )


def derive_files(root: Path, conn: sqlite3.Connection) -> None:
    """Migration and verification derive the same owned files with the same subject function."""
    from ..vault_store import ownership

    for owned in ownership.iter_owned(root):
        if owned.ownership is not ownership.Ownership.PAGE:
            continue
        raw = (root / owned.rel).read_bytes()
        page = find_corpus.parse_page(
            root / owned.rel, 0, root, content=raw, resolved_relative=owned.rel,
        ) if owned.rel.lower().endswith(".md") else None
        replace_row(conn, owned.rel, raw, page)


#: The subject's fields and which of them hold a flag, read once: a request decodes one subject per page.
_SUBJECT_FIELDS = frozenset(field.name for field in fields(membership.MarkdownSubject))
_SUBJECT_FLAGS = frozenset(field.name for field in fields(membership.MarkdownSubject) if field.type == "bool")


def _subject(raw: str, path: str) -> membership.MarkdownSubject:
    value = json.loads(raw)
    if not isinstance(value, dict) or value.keys() != _SUBJECT_FIELDS:
        raise ValueError("incomplete selector facts")
    subject = membership.MarkdownSubject(**{
        key: tuple(item) if isinstance(item, list) else item for key, item in value.items()
    })
    if subject.path != path or not isinstance(subject.path, str):
        raise ValueError("selector facts name another page")
    for name in _SUBJECT_FIELDS - {"path"}:
        item = getattr(subject, name)
        if name in _SUBJECT_FLAGS:
            if type(item) is not bool:
                raise ValueError("invalid selector fact flag")
        elif not isinstance(item, tuple) or not all(isinstance(part, str) for part in item):
            raise ValueError("invalid selector fact values")
    return subject


#: Entries kept for each vault's scope sets. The bound sits above the 50,000-page case, so the load target never evicts.
_SCOPE_ENTRIES_MAX = 65_536
#: Summed subject-text bytes kept for each vault (the stored text is ASCII, so characters are bytes). It bounds the text and the page path inside it, not the whole memo: with about 245 B of table and string headers for each entry, one vault holds at most about 80 MiB.
_SCOPE_BYTES_MAX = 32 * 1024 * 1024
#: Distinct scope sets shared between entries: a vault has few, and a larger table means pages rarely agree, so sharing gains little and the table restarts.
_SCOPE_SHARED_MAX = 1_024


class _ScopeMemo:
    """One vault's scope sets, valid only under the header that every entry was evaluated with."""

    __slots__ = ("header", "entries", "size", "shared")

    def __init__(self, header: tuple) -> None:
        self.header = header
        #: Subject text to `(decoded path, scope set)`; the set is None when membership was unresolved.
        self.entries: OrderedDict[str, tuple[str, frozenset[str] | None]] = OrderedDict()
        self.size = 0
        #: One object for each distinct scope set, so equal sets across entries are stored once.
        self.shared: dict[frozenset[str], frozenset[str]] = {}


_SCOPE_MEMOS: dict[str, _ScopeMemo] = {}
_SCOPE_LOCK = threading.Lock()


def forget_scopes(vault_root: Path | None = None) -> None:
    """Hand back the scope-set memo of one vault, or of every vault; the next request fills it again."""
    with _SCOPE_LOCK:
        if vault_root is None:
            _SCOPE_MEMOS.clear()
        else:
            _SCOPE_MEMOS.pop(str(vault_root), None)


def _scope_header(root: Path, policy: Policy, memo: dict) -> tuple:
    """The facts version and every input of `membership.evaluate_subject` beyond the subject text.

    Read once for each request.
    """
    key = (__name__, "scope header", policy.fingerprint, managed_root_rel())
    kept = memo.get(key)
    if kept is None:
        kept = memo[key] = (FACTS_VERSION, *membership.subject_inputs(root, policy))
    return kept


def _remember(root: Path, header: tuple, touched: list[str], fresh: dict[str, tuple]) -> None:
    """Insert one request's new scope sets and drop the least recently used past the bounds, under one lock.

    A map under another header is replaced whole, so an entry never outlives its header.
    """
    with _SCOPE_LOCK:
        held = _SCOPE_MEMOS.get(str(root))
        if held is None or held.header != header:
            held = _SCOPE_MEMOS[str(root)] = _ScopeMemo(header)
        entries = held.entries
        for text in touched:
            if text in entries:
                entries.move_to_end(text)
        for text, (path, found) in fresh.items():
            if text not in entries:
                held.size += len(text)
            if found is not None:
                found = held.shared.setdefault(found, found)
            entries[text] = (path, found)
            entries.move_to_end(text)
        if len(held.shared) > _SCOPE_SHARED_MAX:
            held.shared.clear()
        while len(entries) > _SCOPE_ENTRIES_MAX or held.size > _SCOPE_BYTES_MAX:
            held.size -= len(entries.popitem(last=False)[0])


def _resolve(
    root: Path, policy: Policy, memo: dict, facts: dict, scopes: dict, pending: dict[str, tuple],
) -> None:
    """Fill the request's `scopes` for `pending` pages, evaluating only subjects that no earlier request did."""
    header = _scope_header(root, policy, memo)
    held = _SCOPE_MEMOS.get(str(root))
    # No lock for the reads: one lookup is atomic, and a map under another header is never read.
    known = held.entries if held is not None and held.header == header else {}
    touched, fresh = [], {}
    for path, (sha256, text, _size) in pending.items():
        entry = known.get(text)
        if entry is not None and entry[0] == path:  # a row that names another page is no hit
            scopes[(path, sha256)] = entry[1]
            touched.append(text)
            continue
        try:
            subject = _subject(text, path)
        except (TypeError, ValueError):
            facts[path] = None  # Malformed derived state cannot supply a visibility proof.
            continue
        try:
            found = membership.evaluate_subject(root, subject, policy)
        except membership.MembershipUnresolved:
            found = None  # Unresolved stays unresolved: an empty set would release the page as unscoped.
        scopes[(path, sha256)] = found
        fresh[text] = (path, found)
    if touched or fresh:
        _remember(root, header, touched, fresh)


def scopes_many(root: Path, paths: list[str], policy: Policy) -> dict[str, frozenset[str]]:
    """Resolve proven ordinary pages; every omitted path still needs the scalar chain.

    Facts and the per-page results live in the request's pinned snapshot. The keyed join proves the
    facts describe that snapshot's exact bytes, including when a concurrent commit is newer. A scope
    set depends only on the subject text and the header's inputs, so one process-wide map for each
    vault serves later requests and other caller views. It stores memberships, never a verdict.
    """
    from ..vault_store import read

    memo = read.request_memo(root)
    if memo is None:
        return {}
    facts = memo.setdefault((__name__, "facts"), {})
    wanted = [path for path in dict.fromkeys(paths) if path not in facts]
    if wanted:
        pages = read.owned_as_pages(root, wanted)
        with read.snapshot(root) as snapshot:
            conn = snapshot.connection
            if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'page_admission' AND type = 'table'").fetchone() is None:
                facts.update(dict.fromkeys(wanted))
            else:
                for start in range(0, len(wanted), read.KEYS_PER_QUERY):
                    chunk = wanted[start:start + read.KEYS_PER_QUERY]
                    facts.update(dict.fromkeys(chunk))
                    for path, sha256, version, simple, raw, size in conn.execute(
                        "SELECT f.path, f.sha256, f.facts_version, f.simple, f.subject, p.size "
                        "FROM page_admission f JOIN store_pages p ON p.path = f.path AND p.sha256 = f.sha256 "
                        "WHERE f.path IN (SELECT value FROM json_each(?))", (json.dumps(chunk),),
                    ):
                        if version == FACTS_VERSION and simple and pages[path]:
                            facts[path] = (sha256, raw, size)  # decoded only on a miss of the scope-set memo
    scopes = memo.setdefault((__name__, "scopes", policy.fingerprint), {})
    pending = {
        path: fact for path in dict.fromkeys(paths)
        if (fact := facts.get(path)) is not None and (path, fact[0]) not in scopes
    }
    if pending:
        _resolve(root, policy, memo, facts, scopes, pending)
    return {
        path: found for path in dict.fromkeys(paths)
        if (fact := facts.get(path)) is not None
        and (found := scopes.get((path, fact[0]))) is not None
    }


def evidence(root: Path, path: str) -> tuple[str, int]:
    """The hash and size that the current batch proved, for the unchanged disclosure receipt."""
    from ..vault_store import read

    sha256, _text, size = read.request_memo(root)[(__name__, "facts")][path]
    return sha256, size
