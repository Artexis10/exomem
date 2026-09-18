"""The activation index: a disposable anchor sidecar over governed structure.

What it is for. `activate_context` accepts a raw turn and has to decide, in
bounded work, which durable anchors that turn is about. Ordinary recall cannot
answer that: it ranks pages by resemblance to a query, and a turn is not a
query. So the compiler needs a small, structural catalogue of the things a turn
can be *about* — entities and their aliases, hubs, the `Products/` and
`Systems/` pages that name resources, active Planning items, Records collection
manifests and their `claims`, and the project keys — with the alias and lexical
terms that let a turn reach them.

What it is NOT. It is never authoritative and never enters an ordinary recall
lane: every row is derived from the vault and the vault alone, and deleting the
file costs nothing but a rebuild. Nothing here is generated: a signature is the
page's own title, lede and headline section names, or a manifest's own field
names and claims. The constitution forbids a server-side generative model, and
there is none in this file.

Conventions mirrored from `embedding_index.py`, deliberately and for the same
reasons: a `meta(key, value)` write-generation token bumped inside the write
transaction (WAL commits do not move the file's mtime, so mtime-keyed
invalidation both spuriously misses and goes stale), a copy-on-write in-process
row cache keyed on that token, and a scoped wipe when the schema version on
disk is not the one this binary writes.
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import threading
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import find_corpus, sidecar_store
from .kbdir import kb_dirname
from .state_paths import vault_state_dir

log = logging.getLogger(__name__)

#: Schema version of the sidecar this binary writes. A mismatch wipes.
#: v2 added `page_names`: an index without it cannot tell a dangling wikilink
#: from an unresolved one, so it must rebuild rather than answer from silence.
#: v3 keyed it on `(name, path)`: one row per name dropped every page but one
#: whose stem normalised the same way, leaving the loser undecided.
#: v4 added frontmatter `aliases` to the indexed spellings: `[[Dossier]]` is a
#: working vault link, so a name map without it left an alias-spelled reference
#: unresolvable and therefore undecidable.
SCHEMA_VERSION = 4
SIDECAR_NAME = ".working-set.sqlite"
DISABLE_ENV = "EXOMEM_DISABLE_WORKING_SET"
_TRUE = frozenset({"1", "true", "yes", "on"})

#: Anchor kinds, in resolution-priority order. `entity` is the referents stage's
#: kind; the other five are what this change generalises it to.
ANCHOR_KINDS: tuple[str, ...] = (
    "entity",
    "resource",
    "hub",
    "collection",
    "plan",
    "project",
)

#: Caps. The index is a catalogue of *anchors*, not of notes: a vault with more
#: hubs than this has a structure problem the index should not paper over, and
#: the bound is what keeps a cold build inside an unmanaged request budget.
MAX_ANCHORS = 2000
MAX_PLAN_ITEMS = 200
MAX_LINKS_PER_ANCHOR = 40
SIGNATURE_MAX_CHARS = 600
LEDE_MAX_CHARS = 240

_NAVIGATION_BASENAMES = frozenset({"index.md", "log.md", "readme.md"})
#: Knowledge-base folders holding immutable raw material rather than anchors.
_RAW_MATERIAL_FOLDERS = frozenset({"Sources", "Evidence"})
_SKIP_DIR_NAMES = frozenset({"_trash", "_attachments", "_Staging", "Templates"})
_WIKILINK = re.compile(r"\[\[([^\]|\n]+)(?:\|[^\]\n]*)?\]\]")
_HEADING = re.compile(r"^#{2,3}\s+(.+?)\s*$", re.MULTILINE)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9'’\-]*")

#: Section/tag names that structurally imply a semantic-unit category. The map
#: is the shipped core category vocabulary plus its plural section spellings; it
#: is a lookup, not an inference, so the same page always yields the same set.
_CATEGORY_BY_LABEL: Mapping[str, str] = {
    "decision": "decision",
    "decisions": "decision",
    "fact": "fact",
    "facts": "fact",
    "finding": "finding",
    "findings": "finding",
    "insight": "insight",
    "insights": "insight",
    "constraint": "constraint",
    "constraints": "constraint",
    "requirement": "requirement",
    "requirements": "requirement",
    "assumption": "assumption",
    "assumptions": "assumption",
    "risk": "risk",
    "risks": "risk",
    "problem": "problem",
    "problems": "problem",
    "question": "question",
    "questions": "question",
    "open question": "question",
    "open questions": "question",
    "action": "action",
    "actions": "action",
    "next steps": "action",
    "technique": "technique",
    "techniques": "technique",
    "method": "technique",
    "methods": "technique",
    "preference": "preference",
    "preferences": "preference",
    "code": "code",
    "design": "design",
    "designs": "design",
    "config": "config",
    "configs": "config",
    "configuration": "config",
    "current state": "fact",
    "status": "fact",
    "summary": "fact",
}


def disabled() -> bool:
    """True when the kill switch forbids opening or creating the sidecar."""
    return os.environ.get(DISABLE_ENV, "").strip().casefold() in _TRUE


def sidecar_path(vault_root: Path) -> Path:
    """Where one vault's activation sidecar lives — machine-local, never in the vault."""
    return vault_state_dir(Path(vault_root)) / SIDECAR_NAME


def normalize(value: object) -> str:
    """NFKC + casefold, the one normalisation anchors and turns share."""
    return unicodedata.normalize("NFKC", str(value)).strip().casefold()


def tokens_of(text: str) -> tuple[str, ...]:
    """NFKC-casefolded word tokens in reading order, repetitions kept.

    Order and repetition matter to anything that slides a window over a turn:
    dropping the second `initiative` of "Alpha Initiative and Beta Initiative"
    destroys the phrase "beta initiative" entirely. Callers that want a term SET
    use `terms_of`.
    """
    return tuple(match.group(0) for match in _TOKEN.finditer(normalize(text)))


def terms_of(text: str) -> tuple[str, ...]:
    """Deterministic lexical terms: NFKC-casefolded word tokens, deduplicated."""
    return tuple(dict.fromkeys(tokens_of(text)))


class WorkingSetIndexUnavailable(RuntimeError):
    """The sidecar could not be opened or queried.

    Distinct from an empty answer on purpose: a consumer that cannot tell "I
    looked and found nothing" from "I could not look" will eventually treat the
    second as the first.
    """


@dataclass(frozen=True, slots=True)
class AnchorRow:
    """One catalogue row. Categorical and structural throughout — no scores."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    signature: str
    aliases: tuple[str, ...]
    terms: tuple[str, ...]
    categories: tuple[str, ...]
    links: tuple[tuple[str, str, str], ...]
    #: The subset of `neighbourhood` whose pages are themselves anchors here.
    #: Exposed from the index because only the index knows the anchor set, and
    #: the resolver needs it to tell a shared SENSE from a shared page: a
    #: boilerplate note two hubs both link — reached by an alias or otherwise —
    #: makes them neither complementary nor related.
    anchor_neighbourhood: frozenset[str] = frozenset()

    @property
    def neighbourhood(self) -> frozenset[str]:
        """Paths this anchor is typed-linked to, in either direction."""
        return frozenset(target for target, _relation, _direction in self.links)


@dataclass(frozen=True, slots=True)
class _Candidate:
    """An anchor as the walkers produce it, before it reaches sqlite."""

    anchor_id: str
    path: str
    ref: str | None
    title: str
    kind: str
    lifecycle: str
    signature: str
    aliases: tuple[str, ...]
    terms: tuple[str, ...]
    categories: tuple[str, ...]
    source_signature: str


# --------------------------------------------------------------------------- #
# Structural extraction (no model, ever)
# --------------------------------------------------------------------------- #


def lede(body: str) -> str:
    """The first authored paragraph that is not a heading, capped.

    Structural extraction: the page's own first sentences, never a summary the
    server produced. Shared with the entity lane, which quotes it verbatim.
    """
    for block in body.split("\n\n"):
        text = " ".join(line.strip() for line in block.strip().splitlines() if line.strip())
        if not text or text.startswith("#") or text.startswith("---"):
            continue
        if text.startswith("- ") or text.startswith("* "):
            text = text.lstrip("-* ").strip()
        if not text:
            continue
        return text[:LEDE_MAX_CHARS]
    return ""


def _sections(body: str) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for match in _HEADING.finditer(body):
        seen.setdefault(match.group(1).strip(), None)
    return tuple(seen)


def _signature(title: str, body: str, *, extra: Iterable[str] = ()) -> str:
    parts = [title.strip(), lede(body), *_sections(body), *extra]
    return "\n".join(part for part in parts if part)[:SIGNATURE_MAX_CHARS]


def _categories(sections: Iterable[str], tags: Iterable[str]) -> tuple[str, ...]:
    found: dict[str, None] = {}
    for label in (*sections, *tags):
        category = _CATEGORY_BY_LABEL.get(normalize(label))
        if category is not None:
            found.setdefault(category, None)
    return tuple(sorted(found))


def _strings(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, (list, tuple, set)):
        out: list[str] = []
        for item in value:
            out.extend(_strings(item))
        return tuple(out)
    text = str(value).strip()
    return (text,) if text else ()


def _page_ref(frontmatter: Mapping[str, Any]) -> str | None:
    from . import memory_refs

    exomem_id = str(frontmatter.get("exomem_id") or "").strip()
    return memory_refs.memory_ref(exomem_id) if exomem_id else None


# --------------------------------------------------------------------------- #
# Source walkers
# --------------------------------------------------------------------------- #


def _walk_kb(vault_root: Path):
    root = Path(vault_root) / kb_dirname()
    if not root.is_dir():
        return
    stack = [root]
    while stack:
        directory = stack.pop()
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for entry in entries:
            name = entry.name
            if name.startswith(".") or name in _SKIP_DIR_NAMES:
                continue
            if entry.is_dir():
                stack.append(entry)
            elif entry.suffix.lower() == ".md":
                yield entry


def _source_signature(path: Path) -> str:
    try:
        stat = path.stat()
    except OSError:
        return ""
    return f"{int(stat.st_mtime_ns)}:{int(stat.st_size)}"


def _page_candidates(
    vault_root: Path,
) -> tuple[list[_Candidate], dict[str, tuple[str, ...]], dict[str, list[str]]]:
    """Walk the knowledge base once: anchor pages, wikilink edges, and a name map.

    The name map is one name to MANY paths: `normalize()` folds case and Unicode
    form, so distinct pages can share a spelling, and collapsing them would hide
    every page but one from both edge resolution and the egress guard.
    """
    from . import recall_policy

    candidates: list[_Candidate] = []
    outbound: dict[str, tuple[str, ...]] = {}
    names: dict[str, list[str]] = {}
    kb = kb_dirname()
    for path in _walk_kb(vault_root):
        rel = path.relative_to(vault_root).as_posix()
        if recall_policy.is_structured_only_path(vault_root, rel):
            continue
        page = find_corpus.CACHE.get(path, Path(vault_root))
        if page is None:
            continue
        frontmatter = page.frontmatter if isinstance(page.frontmatter, dict) else {}
        title = str(frontmatter.get("title") or page.title or path.stem).strip()
        # Every spelling this vault resolves for the page. Frontmatter `aliases`
        # belong here for the same reason the title does: `[[Dossier]]` is a
        # working link, so an alias IS identity, and a map without it leaves an
        # alias-spelled reference unresolvable — which the egress guard reads as
        # "names nothing" and serves. It is also the resolver's strongest evidence
        # kind (`exact_alias`), so treating it as weaker here was incoherent.
        for spelling in (
            rel.removesuffix(".md"),
            rel.removesuffix(".md").removeprefix(f"{kb}/"),
            path.stem,
            title,
            *_strings(frontmatter.get("aliases")),
        ):
            key = normalize(spelling)
            if not key:
                continue
            bucket = names.setdefault(key, [])
            if rel not in bucket:
                bucket.append(rel)
        links = tuple(
            dict.fromkeys(
                normalize(match)
                for match in _WIKILINK.findall(page.body)
                + list(_strings(frontmatter.get("relations")))
                + list(_strings(frontmatter.get("links")))
                if normalize(match)
            )
        )
        if links:
            outbound[rel] = links
        kind = _page_anchor_kind(rel, frontmatter, kb=kb)
        if kind is None or path.name.casefold() in _NAVIGATION_BASENAMES:
            continue
        sections = _sections(page.body)
        tags = _strings(frontmatter.get("tags"))
        aliases = tuple(
            dict.fromkeys(normalize(alias) for alias in _strings(frontmatter.get("aliases")))
        )
        signature = _signature(title, page.body)
        candidates.append(
            _Candidate(
                anchor_id=rel,
                path=rel,
                ref=_page_ref(frontmatter),
                title=title,
                kind=kind,
                lifecycle=normalize(frontmatter.get("status") or "active") or "active",
                signature=signature,
                aliases=aliases,
                terms=terms_of(" ".join((title, *aliases, *sections, *tags))),
                categories=_categories(sections, tags),
                source_signature=_source_signature(path),
            )
        )
    return candidates, outbound, names


def _page_anchor_kind(rel: str, frontmatter: Mapping[str, Any], *, kb: str) -> str | None:
    """Which anchor kind a walked page is, or None when it is not an anchor.

    Deliberately narrow. A note is not an anchor: it is what a lane retrieves
    once an anchor resolves, and admitting every note would turn the catalogue
    into a second copy of the corpus.
    """
    inside = rel.removeprefix(f"{kb}/")
    head = inside.split("/", 1)[0]
    if head == "Entities":
        return "entity" if normalize(frontmatter.get("type")) == "entity" else None
    if head in {"Products", "Systems"}:
        return "resource"
    if head in _RAW_MATERIAL_FOLDERS:
        # `Sources/` and `Evidence/` are immutable raw material. A captured
        # article or a preserved receipt that happens to carry `tags: [hub]` is
        # evidence ABOUT the world, not a durable anchor of the user's own
        # structure, and admitting it would let raw material name itself as the
        # subject of a turn.
        return None
    if "hub" in {normalize(tag) for tag in _strings(frontmatter.get("tags"))}:
        return "hub"
    return None


def _collection_candidates(vault_root: Path) -> tuple[list[_Candidate], list[_Candidate]]:
    """Records manifests (+ their claims) and active Planning items."""
    from . import structured_collections

    records: list[_Candidate] = []
    plans: list[_Candidate] = []
    try:
        manifests = structured_collections.discover_collections(Path(vault_root))
    except Exception:  # noqa: BLE001 - an unreadable manifest costs its anchor, not the build
        log.debug("activation index: collection discovery failed", exc_info=True)
        return records, plans
    for manifest in manifests:
        rel = str(getattr(manifest, "path", "") or "")
        if not rel:
            continue
        absolute = Path(vault_root) / rel
        fields = tuple(getattr(getattr(manifest, "schema", None), "fields", {}) or ())
        claims = tuple(
            dict.fromkeys(
                normalize(value)
                for values in (getattr(manifest, "claims", None) or {}).values()
                for value in _strings(values)
            )
        )
        title = str(getattr(manifest, "title", "") or absolute.parent.name).strip()
        profile = str(getattr(manifest, "semantic_profile", "") or "")
        if profile == "records":
            records.append(
                _Candidate(
                    anchor_id=rel,
                    path=rel,
                    ref=None,
                    title=title,
                    kind="collection",
                    lifecycle=normalize(getattr(manifest, "lifecycle", "active") or "active"),
                    signature=_signature(title, "", extra=(*fields, *claims)),
                    aliases=(),
                    terms=terms_of(" ".join((title, *fields, *claims))),
                    categories=("fact",),
                    source_signature=_source_signature(absolute),
                )
            )
        elif profile == "planning":
            plans.extend(_planning_candidates(vault_root, manifest, rel))
    return records, plans


def _planning_candidates(vault_root: Path, manifest: Any, rel: str) -> list[_Candidate]:
    from . import planning

    try:
        result = planning.query(
            Path(vault_root),
            manifest,
            lifecycle="active",
            limit=MAX_PLAN_ITEMS,
        )
    except Exception:  # noqa: BLE001 - a planning lane that cannot read costs its anchors only
        log.debug("activation index: planning query failed for %s", rel, exc_info=True)
        return []
    rows = result.get("rows") if isinstance(result, Mapping) else None
    if not isinstance(rows, list):
        return []
    signature = _source_signature(Path(vault_root) / rel)
    out: list[_Candidate] = []
    for row in rows:
        values = row.get("values") if isinstance(row, Mapping) else None
        if not isinstance(values, Mapping):
            values = row if isinstance(row, Mapping) else {}
        title = str(values.get("title") or "").strip()
        if not title:
            continue
        item_path = str(row.get("path") or "") if isinstance(row, Mapping) else ""
        kind_field = str(values.get("kind") or "").strip()
        tags = _strings(values.get("tags"))
        out.append(
            _Candidate(
                anchor_id=f"plan:{rel}#{normalize(title)}",
                path=item_path or rel,
                ref=None,
                title=title,
                kind="plan",
                lifecycle=normalize(values.get("lifecycle") or "active") or "active",
                signature=_signature(title, "", extra=(kind_field, *tags)),
                aliases=(),
                terms=terms_of(" ".join((title, kind_field, *tags))),
                categories=("action",),
                source_signature=f"{signature}:{normalize(title)}",
            )
        )
    return out


def _project_candidates(vault_root: Path) -> list[_Candidate]:
    from . import project_keys

    try:
        registry = project_keys.load_project_registry(Path(vault_root))
    except Exception:  # noqa: BLE001 - a missing registry costs project anchors only
        log.debug("activation index: project registry unavailable", exc_info=True)
        return []
    out: list[_Candidate] = []
    for key in registry.keys:
        folder = registry.folder_for(key) or key
        category = registry.category_for(key)
        out.append(
            _Candidate(
                anchor_id=f"project:{key}",
                path="",
                ref=f"project:{key}",
                title=str(folder).strip(),
                kind="project",
                lifecycle="active",
                signature=_signature(str(folder), "", extra=(key, category)),
                aliases=(normalize(key),),
                terms=terms_of(" ".join((str(folder), key, category))),
                categories=(),
                source_signature=f"{key}:{folder}:{category}",
            )
        )
    return out


def _resolve_links(
    outbound: Mapping[str, tuple[str, ...]],
    names: Mapping[str, Sequence[str]],
    anchor_paths: Mapping[str, str],
) -> dict[str, list[tuple[str, str, str]]]:
    """Turn wikilink names into edges between the pages the index knows.

    Both directions are recorded. An anchor's neighbourhood is what corroborates
    it and what bounds its lanes, and a hub that is linked TO but links nowhere
    would otherwise have no neighbourhood at all.
    """
    edges: dict[str, list[tuple[str, str, str]]] = {}

    def _add(anchor_id: str, target: str, direction: str) -> None:
        bucket = edges.setdefault(anchor_id, [])
        row = (target, "wikilink", direction)
        if row not in bucket and len(bucket) < MAX_LINKS_PER_ANCHOR:
            bucket.append(row)

    for source_rel, targets in outbound.items():
        source_anchor = anchor_paths.get(source_rel)
        for name in targets:
            for target_rel in names.get(name) or ():
                if target_rel == source_rel:
                    continue
                if source_anchor is not None:
                    _add(source_anchor, target_rel, "outbound")
                target_anchor = anchor_paths.get(target_rel)
                if target_anchor is not None:
                    _add(target_anchor, source_rel, "inbound")
    return edges


# --------------------------------------------------------------------------- #
# The sidecar
# --------------------------------------------------------------------------- #

_CACHE_LOCK = threading.Lock()
_ROW_CACHE: dict[Path, tuple[tuple[int, int, int], tuple[AnchorRow, ...]]] = {}


class WorkingSetIndex:
    """One vault's activation sidecar. Read-mostly; every write bumps generation."""

    def __init__(self, vault_root: Path) -> None:
        self.vault_root = Path(vault_root)
        self._path: Path | None = None
        self._conn: sqlite3.Connection | None = None

    # -- lifecycle ---------------------------------------------------------- #

    @property
    def path(self) -> Path:
        if self._path is None:
            self._path = sidecar_path(self.vault_root)
        return self._path

    def available(self) -> bool:
        """False under the kill switch. Nothing is created or opened."""
        return not disabled()

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def reset(self) -> None:
        """Drop the sidecar and every cached row — the disposability guarantee."""
        self.close()
        with _CACHE_LOCK:
            _ROW_CACHE.pop(self.path, None)
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            log.warning("activation index could not be removed: %s", self.path, exc_info=True)

    def _connect(self) -> sqlite3.Connection | None:
        if disabled():
            return None
        if self._conn is not None:
            return self._conn
        try:
            sidecar_store.ensure_sidecar_parent(self.path)
            conn = sqlite3.connect(self.path, timeout=5.0)
        except (OSError, sqlite3.Error):
            log.warning("activation index unavailable at %s", self.path, exc_info=True)
            return None
        sidecar_store.apply_sidecar_pragmas(conn)
        try:
            self._ensure_schema(conn)
        except sqlite3.Error:
            log.warning("activation index schema could not be prepared", exc_info=True)
            conn.close()
            return None
        self._conn = conn
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS anchors (
                anchor_id TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                ref TEXT,
                title TEXT NOT NULL,
                kind TEXT NOT NULL,
                lifecycle TEXT NOT NULL,
                signature TEXT NOT NULL,
                source_signature TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_aliases "
            "(anchor_id TEXT NOT NULL, alias TEXT NOT NULL, PRIMARY KEY (anchor_id, alias))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_categories "
            "(anchor_id TEXT NOT NULL, category TEXT NOT NULL, PRIMARY KEY (anchor_id, category))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_links ("
            "anchor_id TEXT NOT NULL, other_path TEXT NOT NULL, "
            "relation_type TEXT NOT NULL, direction TEXT NOT NULL, "
            "PRIMARY KEY (anchor_id, other_path, relation_type, direction))"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_vectors "
            "(anchor_id TEXT PRIMARY KEY, vector BLOB NOT NULL)"
        )
        try:
            conn.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS anchor_terms "
                "USING fts5(terms, anchor_id UNINDEXED, tokenize='unicode61')"
            )
        except sqlite3.Error:
            # FTS5 absent: `lexical_overlap` falls back to the stored term rows.
            log.info("activation index: FTS5 unavailable; lexical overlap uses stored terms")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS anchor_term_rows "
            "(anchor_id TEXT NOT NULL, term TEXT NOT NULL, PRIMARY KEY (anchor_id, term))"
        )
        # `meta` is integer-valued by the shared sidecar contract, and the
        # freshness key the index was built at is text. A second, text-valued
        # table keeps the shared token table exactly as every other sidecar
        # writes it rather than widening its type for one consumer.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS index_meta "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS anchor_term_rows_term ON anchor_term_rows(term)")
        # Every KB page's wikilink-resolvable names -> its vault-relative path.
        # `_page_candidates` already computes this map to resolve anchor links; it
        # is persisted rather than discarded so the egress guard can resolve a
        # stem found in authored prose WITHOUT a corpus walk and without needing a
        # warm semantic snapshot. It covers the vault's page set, not only anchors.
        # Keyed on the PAIR, not the name: `normalize()` folds case and Unicode
        # form, so `Widget.md` and `widget.md` share a key. A name resolves to
        # EVERY path that bears it, and the guard decides all of them — otherwise
        # the page that lost the row is never decided and a wikilink to the shared
        # stem is served against it.
        conn.execute(
            "CREATE TABLE IF NOT EXISTS page_names "
            "(name TEXT NOT NULL, path TEXT NOT NULL, PRIMARY KEY (name, path))"
        )
        conn.execute("CREATE INDEX IF NOT EXISTS page_names_name ON page_names(name)")
        sidecar_store.ensure_meta_table(conn, "anchors", self.path.name)
        stored = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        if stored is None:
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
                (SCHEMA_VERSION,),
            )
            conn.commit()
        elif int(stored[0]) != SCHEMA_VERSION:
            self._wipe(conn)

    def _wipe(self, conn: sqlite3.Connection) -> None:
        """Scoped wipe: the derived rows go, the generation token keeps counting.

        Resetting the token instead would let a consumer that cached rows from
        the old schema believe its cache is current.
        """
        for table in (
            "anchors",
            "anchor_aliases",
            "anchor_categories",
            "anchor_links",
            "anchor_vectors",
            "anchor_term_rows",
            "page_names",
            "index_meta",
        ):
            conn.execute(f"DELETE FROM {table}")
        try:
            conn.execute("DELETE FROM anchor_terms")
        except sqlite3.Error:
            pass
        sidecar_store.bump_meta(conn, "generation")
        conn.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES ('schema_version', ?)",
            (SCHEMA_VERSION,),
        )
        conn.commit()
        with _CACHE_LOCK:
            _ROW_CACHE.pop(self.path, None)

    # -- reads -------------------------------------------------------------- #

    def generation(self) -> int:
        conn = self._connect()
        if conn is None:
            return 0
        return sidecar_store.read_meta_token(conn)[1]

    def token(self) -> tuple[int, int, int]:
        conn = self._connect()
        if conn is None:
            return (0, 0, 0)
        return sidecar_store.read_meta_token(conn)

    def freshness_stamp(self) -> str:
        """The recall freshness key this catalogue was last built at, or `""`.

        The staleness signal. An index whose stamp is behind the request's key was
        built from an older vault state, and the operation must refresh it or say
        so — never serve a packet whose generation block implies otherwise.
        """
        conn = self._connect()
        if conn is None:
            return ""
        try:
            row = conn.execute(
                "SELECT value FROM index_meta WHERE key = 'freshness_key'"
            ).fetchone()
        except sqlite3.Error:
            return ""
        return str(row[0]) if row else ""

    def resolve_names(self, names: Iterable[str]) -> dict[str, tuple[str, ...]]:
        """Map wikilink names to EVERY vault path that bears them.

        The same resolution `_resolve_links` performs at build time, exposed for
        the egress guard: a bare stem out of authored prose is not a
        vault-relative path, and handing one to a release decision returns "no
        decision", which is indistinguishable from "withheld". An indexed lookup
        answers it with no filesystem work.

        Two outcomes that must not be conflated:

        * a name absent from the map resolves to nothing and is simply not in the
          result — it names no page, so there is nothing to decide; and
        * a query that cannot run RAISES. A resolver that answered "no names
          resolved" when it merely failed would remove the filter precisely when
          removing it is least safe, which is the fail-open class the guard exists
          to prevent. The caller decides what a failure means; this method will
          not decide it by returning a plausible-looking empty answer.
        """
        wanted = [normalize(name) for name in names]
        wanted = [name for name in dict.fromkeys(wanted) if name]
        if not wanted:
            return {}
        conn = self._connect()
        if conn is None:
            raise WorkingSetIndexUnavailable(
                "the activation index could not be opened for name resolution"
            )
        out: dict[str, list[str]] = {}
        for start in range(0, len(wanted), 400):
            chunk = wanted[start : start + 400]
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT name, path FROM page_names WHERE name IN ({placeholders}) "
                "ORDER BY name, path",
                chunk,
            ).fetchall()
            for name, path in rows:
                out.setdefault(str(name), []).append(str(path))
        return {name: tuple(paths) for name, paths in out.items()}

    def anchors(self) -> tuple[AnchorRow, ...]:
        """Every anchor row, copy-on-write cached on the sidecar's write token."""
        conn = self._connect()
        if conn is None:
            return ()
        token = sidecar_store.read_meta_token(conn)
        with _CACHE_LOCK:
            cached = _ROW_CACHE.get(self.path)
            if cached is not None and cached[0] == token:
                return cached[1]
        rows = self._read_rows(conn)
        with _CACHE_LOCK:
            _ROW_CACHE[self.path] = (token, rows)
        return rows

    def _read_rows(self, conn: sqlite3.Connection) -> tuple[AnchorRow, ...]:
        aliases: dict[str, list[str]] = {}
        for anchor_id, alias in conn.execute(
            "SELECT anchor_id, alias FROM anchor_aliases ORDER BY anchor_id, alias"
        ):
            aliases.setdefault(anchor_id, []).append(alias)
        categories: dict[str, list[str]] = {}
        for anchor_id, category in conn.execute(
            "SELECT anchor_id, category FROM anchor_categories ORDER BY anchor_id, category"
        ):
            categories.setdefault(anchor_id, []).append(category)
        terms: dict[str, list[str]] = {}
        for anchor_id, term in conn.execute(
            "SELECT anchor_id, term FROM anchor_term_rows ORDER BY anchor_id, term"
        ):
            terms.setdefault(anchor_id, []).append(term)
        links: dict[str, list[tuple[str, str, str]]] = {}
        for anchor_id, other, relation, direction in conn.execute(
            "SELECT anchor_id, other_path, relation_type, direction FROM anchor_links "
            "ORDER BY anchor_id, other_path, relation_type, direction"
        ):
            links.setdefault(anchor_id, []).append((other, relation, direction))
        anchor_paths = frozenset(
            str(row[0]) for row in conn.execute("SELECT path FROM anchors")
        )
        out: list[AnchorRow] = []
        for anchor_id, path, ref, title, kind, lifecycle, signature in conn.execute(
            "SELECT anchor_id, path, ref, title, kind, lifecycle, signature FROM anchors "
            "ORDER BY anchor_id"
        ):
            out.append(
                AnchorRow(
                    anchor_id=anchor_id,
                    path=path,
                    ref=ref,
                    title=title,
                    kind=kind,
                    lifecycle=lifecycle,
                    signature=signature,
                    aliases=tuple(aliases.get(anchor_id, ())),
                    terms=tuple(terms.get(anchor_id, ())),
                    categories=tuple(categories.get(anchor_id, ())),
                    links=tuple(links.get(anchor_id, ())),
                    anchor_neighbourhood=frozenset(
                        other for other, _relation, _direction in links.get(anchor_id, ())
                    )
                    & anchor_paths,
                )
            )
        return tuple(out)

    def vectors(self) -> dict[str, Any]:
        """Signature embeddings, or `{}` when the backend produced none."""
        conn = self._connect()
        if conn is None:
            return {}
        rows = conn.execute("SELECT anchor_id, vector FROM anchor_vectors").fetchall()
        if not rows:
            return {}
        import numpy as np

        return {
            anchor_id: np.frombuffer(blob, dtype=np.float32)
            for anchor_id, blob in rows
        }

    # -- writes ------------------------------------------------------------- #

    def rebuild(self, *, freshness_stamp: str | None = None) -> dict[str, Any]:
        """Derive every anchor from the vault and replace the catalogue."""
        return self._write(full=True, freshness_stamp=freshness_stamp)

    def update(self, *, freshness_stamp: str | None = None) -> dict[str, Any]:
        """Bring the catalogue to the current vault state, bumping only on change."""
        return self._write(full=False, freshness_stamp=freshness_stamp)

    def _write(self, *, full: bool, freshness_stamp: str | None = None) -> dict[str, Any]:
        if disabled():
            return {"anchors": 0, "generation": 0, "disabled": True}
        conn = self._connect()
        if conn is None:
            return {"anchors": 0, "generation": 0, "unavailable": True}
        candidates, edges, page_names = self._collect()
        existing = {
            anchor_id: signature
            for anchor_id, signature in conn.execute(
                "SELECT anchor_id, source_signature FROM anchors"
            )
        }
        wanted = {candidate.anchor_id: candidate for candidate in candidates}
        changed = full or set(existing) != set(wanted)
        if not changed:
            changed = any(
                existing[anchor_id] != candidate.source_signature
                for anchor_id, candidate in wanted.items()
            )
        if not changed:
            changed = self._links_differ(conn, edges)
        if not changed:
            stored_names = conn.execute("SELECT COUNT(*) FROM page_names").fetchone()
            wanted_names = sum(len(paths) for paths in page_names.values())
            changed = int(stored_names[0] if stored_names else 0) != wanted_names
        if not changed:
            # The vault did not move, so the rows and the generation must not
            # either; only the stamp advances, so the next request stops asking.
            if freshness_stamp is not None:
                self._stamp(conn, freshness_stamp)
            return {
                "anchors": len(existing),
                "generation": sidecar_store.read_meta_token(conn)[1],
                "unchanged": True,
            }
        vectors = _signature_vectors(candidates)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in (
                "anchors",
                "anchor_aliases",
                "anchor_categories",
                "anchor_links",
                "anchor_vectors",
                "anchor_term_rows",
                "page_names",
            ):
                conn.execute(f"DELETE FROM {table}")
            self._delete_fts(conn)
            for candidate in candidates:
                conn.execute(
                    "INSERT INTO anchors (anchor_id, path, ref, title, kind, lifecycle, "
                    "signature, source_signature) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        candidate.anchor_id,
                        candidate.path,
                        candidate.ref,
                        candidate.title,
                        candidate.kind,
                        candidate.lifecycle,
                        candidate.signature,
                        candidate.source_signature,
                    ),
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_aliases (anchor_id, alias) VALUES (?, ?)",
                    [(candidate.anchor_id, alias) for alias in candidate.aliases],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_categories (anchor_id, category) VALUES (?, ?)",
                    [(candidate.anchor_id, category) for category in candidate.categories],
                )
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_term_rows (anchor_id, term) VALUES (?, ?)",
                    [(candidate.anchor_id, term) for term in candidate.terms],
                )
                self._insert_fts(conn, candidate)
                blob = vectors.get(candidate.anchor_id)
                if blob is not None:
                    conn.execute(
                        "INSERT OR REPLACE INTO anchor_vectors (anchor_id, vector) VALUES (?, ?)",
                        (candidate.anchor_id, blob),
                    )
            conn.executemany(
                "INSERT OR REPLACE INTO page_names (name, path) VALUES (?, ?)",
                sorted(
                    (name, path)
                    for name, paths in page_names.items()
                    for path in paths
                ),
            )
            for anchor_id, rows in edges.items():
                if anchor_id not in wanted:
                    continue
                conn.executemany(
                    "INSERT OR IGNORE INTO anchor_links "
                    "(anchor_id, other_path, relation_type, direction) VALUES (?, ?, ?, ?)",
                    [(anchor_id, *row) for row in rows],
                )
            if freshness_stamp is not None:
                conn.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) VALUES "
                    "('freshness_key', ?)",
                    (freshness_stamp,),
                )
            generation = sidecar_store.bump_meta(conn, "generation")
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            log.warning("activation index write failed", exc_info=True)
            return {"anchors": 0, "generation": 0, "unavailable": True}
        with _CACHE_LOCK:
            _ROW_CACHE.pop(self.path, None)
        return {"anchors": len(candidates), "generation": generation}

    def _stamp(self, conn: sqlite3.Connection, freshness_stamp: str) -> None:
        """Record the freshness key without touching the write generation."""
        try:
            conn.execute(
                "INSERT OR REPLACE INTO index_meta (key, value) VALUES "
                "('freshness_key', ?)",
                (freshness_stamp,),
            )
            conn.commit()
        except sqlite3.Error:
            log.debug("activation index freshness stamp could not be written", exc_info=True)

    def _links_differ(
        self, conn: sqlite3.Connection, edges: Mapping[str, list[tuple[str, str, str]]]
    ) -> bool:
        stored: set[tuple[str, str, str, str]] = {
            (anchor_id, other, relation, direction)
            for anchor_id, other, relation, direction in conn.execute(
                "SELECT anchor_id, other_path, relation_type, direction FROM anchor_links"
            )
        }
        wanted = {
            (anchor_id, *row) for anchor_id, rows in edges.items() for row in rows
        }
        return stored != wanted

    def _delete_fts(self, conn: sqlite3.Connection) -> None:
        try:
            conn.execute("DELETE FROM anchor_terms")
        except sqlite3.Error:
            pass

    def _insert_fts(self, conn: sqlite3.Connection, candidate: _Candidate) -> None:
        try:
            conn.execute(
                "INSERT INTO anchor_terms (terms, anchor_id) VALUES (?, ?)",
                (" ".join(candidate.terms), candidate.anchor_id),
            )
        except sqlite3.Error:
            pass

    def _collect(
        self,
    ) -> tuple[
        list[_Candidate], dict[str, list[tuple[str, str, str]]], dict[str, list[str]]
    ]:
        pages, outbound, names = _page_candidates(self.vault_root)
        records, plans = _collection_candidates(self.vault_root)
        projects = _project_candidates(self.vault_root)
        candidates = [*pages, *records, *plans, *projects][:MAX_ANCHORS]
        anchor_paths = {
            candidate.path: candidate.anchor_id
            for candidate in candidates
            if candidate.path
        }
        edges = _resolve_links(outbound, names, anchor_paths)
        candidates.sort(key=lambda candidate: candidate.anchor_id)
        return candidates, edges, names


def _signature_vectors(candidates: Iterable[_Candidate]) -> dict[str, bytes]:
    """Embed the structural signatures, or produce nothing at all.

    Absent is the honest answer under `EXOMEM_DISABLE_EMBEDDINGS`: a zero or
    random vector would still score, and `vector_band` must never be faked.
    """
    if os.environ.get("EXOMEM_DISABLE_EMBEDDINGS"):
        return {}
    rows = [candidate for candidate in candidates if candidate.signature]
    if not rows:
        return {}
    try:
        import numpy as np

        from . import embeddings

        matrix = embeddings.embed_texts([row.signature for row in rows], is_query=False)
    except Exception:  # noqa: BLE001 - the vector lane is optional by contract
        log.info("activation index: signature embeddings unavailable", exc_info=True)
        return {}
    out: dict[str, bytes] = {}
    for row, vector in zip(rows, matrix, strict=False):
        out[row.anchor_id] = np.asarray(vector, dtype=np.float32).tobytes()
    return out
