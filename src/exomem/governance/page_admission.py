"""Caller-independent selector facts, derived with canonical page bytes (store design D13).

The table holds no policy membership or caller verdict. Every reader evaluates the current policy
through membership's own kernel, at the snapshot that holds the page's hash. Missing or older facts
leave the scalar release chain in charge.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, fields
from pathlib import Path

from .. import document_containers, find_corpus, vault
from ..find_types import ParsedPage
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


def _subject(raw: str, path: str) -> membership.MarkdownSubject:
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {field.name for field in fields(membership.MarkdownSubject)}:
        raise ValueError("incomplete selector facts")
    subject = membership.MarkdownSubject(**{
        key: tuple(item) if isinstance(item, list) else item for key, item in value.items()
    })
    if subject.path != path or not isinstance(subject.path, str):
        raise ValueError("selector facts name another page")
    for field in fields(subject):
        item = getattr(subject, field.name)
        if field.name == "path":
            continue
        if field.type == "bool":
            if type(item) is not bool:
                raise ValueError("invalid selector fact flag")
        elif not isinstance(item, tuple) or not all(isinstance(part, str) for part in item):
            raise ValueError("invalid selector fact values")
    return subject


def scopes_many(root: Path, paths: list[str], policy: Policy) -> dict[str, frozenset[str]]:
    """Resolve proven ordinary pages; every omitted path still needs the scalar chain.

    Facts, membership and misses live only in the request's pinned snapshot. The keyed join proves
    the facts describe that snapshot's exact bytes, including when a concurrent commit is newer.
    """
    from ..vault_store import ownership, read

    memo = read.request_memo(root)
    if memo is None:
        return {}
    facts = memo.setdefault((__name__, "facts"), {})
    wanted = [path for path in dict.fromkeys(paths) if path not in facts]
    if wanted:
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
                        if version != FACTS_VERSION or not simple or ownership.classify(path) is not ownership.Ownership.PAGE:
                            continue
                        try:
                            facts[path] = (sha256, _subject(raw, path), size)
                        except (TypeError, ValueError):
                            pass  # Malformed derived state cannot supply a visibility proof.
    scopes = memo.setdefault((__name__, "scopes", policy.fingerprint), {})
    result = {}
    for path in paths:
        fact = facts.get(path)
        if fact is None:
            continue
        sha256, subject, _size = fact
        key = (path, sha256)
        if key not in scopes:
            try:
                scopes[key] = membership.evaluate_subject(root, subject, policy)
            except membership.MembershipUnresolved:
                scopes[key] = None
        if scopes[key] is not None:
            result[path] = scopes[key]
    return result


def evidence(root: Path, path: str) -> tuple[str, int]:
    """The hash and size that the current batch proved, for the unchanged disclosure receipt."""
    from ..vault_store import read

    sha256, _subject, size = read.request_memo(root)[(__name__, "facts")][path]
    return sha256, size
