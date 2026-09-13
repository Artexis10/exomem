"""Derived role support: full dependency descriptors, bounded write deltas.

Reconcile owns corpus discovery. Deltas use the written parsed page and this
index; readers validate generations and apply their audience before counting.
"""

from __future__ import annotations

import itertools
import logging
import re
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import yaml

from . import artifact_role_review as sensor

log = logging.getLogger(__name__)
MAX_EDGES = sensor.MAX_UNITS * sensor.MAX_EVIDENCE
_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")


def _generation(root: Path, path: str) -> list[int] | None:
    try:
        info = (root / path).stat()
        return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    except OSError:
        return None


def _state(root: Path, parsed: Any) -> Any:
    from . import semantic_contract

    return semantic_contract.build_page_state(
        root,
        parsed.rel_path,
        "---\n" + yaml.safe_dump(parsed.frontmatter) + "---\n" + parsed.body,
    )


def _aliases(path: str, title: str, identity: str) -> tuple[str, ...]:
    from .kbdir import kb_prefix

    return tuple(
        dict.fromkeys(
            (
                path.removesuffix(".md"),
                path.removeprefix(kb_prefix()).removesuffix(".md"),
                Path(path).stem,
                title.casefold(),
                identity,
            )
        )
    )


def _raw_links(unit: Any) -> list[str]:
    return list(
        dict.fromkeys(
            [relation.target for relation in unit.relations] + _WIKILINK.findall(unit.content)
        )
    )


def describe(root: Path, state: Any) -> dict[str, Any]:
    units = state.document.units
    complete = len(units) <= sensor.MAX_UNITS and all(
        len(u.content) + len(u.context or "") <= sensor.MAX_UNIT_CHARS for u in units
    )
    rows = []
    edges = 0
    if complete:
        for unit in units:
            raw = _raw_links(unit)
            edges += len(raw)
            if edges > MAX_EDGES:
                complete = False
                break
            resolution = state.document.resolve_unit(
                sensor.unit_identity(unit), expected_fingerprint=unit.fingerprint
            )
            rows.append(
                {
                    "ref": sensor.unit_identity(unit),
                    "fingerprint": unit.fingerprint,
                    "valid": resolution.status == "found",
                    "category": unit.category,
                    "kind": unit.kind,
                    "raw_links": raw,
                    "unit": {
                        key: getattr(unit, key)
                        for key in (
                            "content",
                            "context",
                            "category",
                            "kind",
                            "title",
                            "verdict",
                            "anchor",
                            "unit_ref",
                            "fingerprint",
                            "occurrence",
                        )
                    },
                    "supersedes": [r.target for r in unit.relations if r.kind == "supersedes"],
                }
            )
    return {
        "path": state.path,
        "identity": state.identity,
        "parent_ref": state.document.parent_ref,
        "type": state.page_type,
        "eligible": state.eligible_compiled,
        "status": state.status,
        "n": state.frontmatter.get("n"),
        "generation": _generation(root, state.path),
        "aliases": list(
            _aliases(state.path, state.title, state.document.parent_ref or state.identity)
        ),
        "units": rows if complete else [],
        "complete": complete,
    }


def _checkpoint(index: Mapping[str, Any]) -> None:
    from . import request_budget

    deadline = index.get("_deadline")
    if deadline is not None and (
        time.monotonic() >= deadline or not request_budget.can_afford(0.05)
    ):
        raise TimeoutError("artifact role advisory budget exhausted")


def _target(index: Mapping[str, Any], raw: str, source: str) -> tuple[str, str | None] | None:
    _checkpoint(index)
    clean = raw.strip().removeprefix("[[").removesuffix("]]").split("|", 1)[0]
    name, _, anchor = clean.partition("#")
    if not name:
        paths = [source]
    else:
        name = name.removesuffix(".md").strip("/")
        paths = index["aliases"].get(name, [])
        if not paths:
            paths = index["aliases"].get(name.casefold(), [])
    authorize = index.get("_authorize")
    if authorize is not None:
        visible = []
        for position, path in enumerate(paths):
            _checkpoint(index)
            if index.get("_deadline") is not None and position >= MAX_EDGES:
                raise TimeoutError("artifact role alias coverage capped")
            if authorize(path):
                visible.append(path)
                if len(visible) > 1:
                    break
        paths = visible
    if len(paths) != 1:
        return None
    path = paths[0]
    page = index["pages"].get(path)
    if not page:
        return None
    if not anchor:
        return path, None
    matches = [
        u
        for u in page["units"]
        if u["ref"].rpartition("#")[2] == anchor or u["unit"]["anchor"] == anchor.removeprefix("^")
    ]
    if len(matches) != 1 or not matches[0]["valid"]:
        return None
    return path, matches[0]["ref"]


def _resolved(index: Mapping[str, Any], page: Mapping[str, Any]) -> tuple[dict, dict, set, set]:
    links, sources, superseded, dependencies = {}, {}, set(), set()
    for unit in page["units"]:
        exact, documents = [], []
        for raw in unit["raw_links"]:
            target = _target(index, raw, page["path"])
            if target is None:
                continue
            path, ref = target
            dependencies.add(path)
            if ref:
                exact.append(ref)
            if index["pages"][path]["type"] == "source":
                documents.append(path)
        links[unit["ref"]] = tuple(sorted(set(exact)))
        sources[unit["ref"]] = tuple(sorted(set(documents)))
        for raw in unit["supersedes"]:
            target = _target(index, raw, page["path"])
            if (
                target
                and target[1]
                and unit["valid"]
                and unit["unit"]["verdict"] not in {"retracted", "false", "failed"}
            ):
                superseded.add(target[1])
    return links, sources, superseded, dependencies


def _view(page: Mapping[str, Any]) -> Any:
    return SimpleNamespace(
        identity=page["identity"],
        path=page["path"],
        eligible_compiled=page["eligible"],
        page_type=page["type"],
        status=page["status"],
        frontmatter={"n": page["n"]},
        document=SimpleNamespace(units=tuple(SimpleNamespace(**u["unit"]) for u in page["units"])),
    )


def _candidate_row(candidate: Any) -> dict[str, Any]:
    return {
        "family": candidate.family,
        "role": candidate.role,
        "partition": candidate.partition,
        "signal_version": candidate.signal_version,
        "material_version": candidate.material_version,
        "reason": candidate.reason,
        "evidence_refs": list(candidate.evidence_refs),
        "sources": list(candidate.sources),
        "represented": sensor.represented_text(candidate.units[0].content),
        "fingerprints": {sensor.unit_identity(u): u.fingerprint for u in candidate.units},
    }


def _covers(
    index: Mapping[str, Any],
    origin: str,
    candidate: Mapping[str, Any],
    destination: Mapping[str, Any],
) -> dict[str, Any] | None:
    if (
        not destination["complete"]
        or not destination["eligible"]
        or destination["path"] == origin
        or destination["type"] == "experiment"
        or destination["status"] not in {None, "", "active"}
    ):
        return None
    if candidate["role"] == "research_synthesis" and destination["type"] != "research-note":
        return None
    links, sources, _, _ = _resolved(index, destination)
    for unit in destination["units"]:
        view = SimpleNamespace(**unit["unit"])
        proper_role = (
            sensor.procedure(view)
            if candidate["role"] == "reusable_method"
            else sensor.synthesis(view)
        )
        if (
            not proper_role
            or sensor.represented_text(unit["unit"]["content"]) != candidate["represented"]
        ):
            continue
        if not set(candidate["evidence_refs"]) <= set(links[unit["ref"]]):
            continue
        # Unit resolution above is against the current document identity; compare
        # the evidence generation too, so a stale retained support cannot cover.
        origin_units = {u["ref"]: u for u in index["pages"][origin]["units"] if u["valid"]}
        if any(
            ref not in origin_units or origin_units[ref]["fingerprint"] != fp
            for ref, fp in candidate["fingerprints"].items()
        ):
            continue
        return {"path": destination["path"], "sources": list(sources[unit["ref"]])}
    return None


def _origin(index: dict[str, Any], path: str, *, bounded: bool = False) -> dict[str, Any]:
    page = index["pages"].get(path)
    unknown = {
        "epoch": index["support_epoch"],
        "coverage": dict.fromkeys(sensor.FAMILIES, "unknown"),
        "candidates": [],
    }
    if page is None:
        return {
            "epoch": index["support_epoch"],
            "coverage": dict.fromkeys(sensor.FAMILIES, "complete"),
            "candidates": [],
        }
    if not page["complete"]:
        return {**unknown, "coverage": dict.fromkeys(sensor.FAMILIES, "capped")}
    links, sources, superseded, _ = _resolved(index, page)
    destinations = index["reverse"].get(path, {})
    if bounded and len(destinations) > MAX_EDGES:
        return unknown
    for dest in destinations:
        _checkpoint(index)
        if index.get("_authorize") is not None and not index["_authorize"](dest):
            continue
        destination = index["pages"].get(dest)
        if (
            destination
            and destination["complete"]
            and destination["eligible"]
            and destination["status"] in {None, "", "active", "ongoing"}
        ):
            _, _, external_superseded, _ = _resolved(index, destination)
            superseded.update(external_superseded)
    result = sensor.detect(
        _view(page),
        source_documents=sources,
        exact_links=links,
        superseded_refs=frozenset(superseded),
        candidate_limit=None,
    )
    _checkpoint(index)
    rows = []
    for candidate in result.candidates:
        row = _candidate_row(candidate)
        row["supports"] = []
        if candidate.family == sensor.FAMILIES[0]:
            for dest in destinations:
                _checkpoint(index)
                if index.get("_authorize") is not None and not index["_authorize"](dest):
                    continue
                destination = index["pages"].get(dest)
                support = _covers(index, path, row, destination) if destination else None
                if support:
                    row["supports"].append(support)
        rows.append(row)
    return {"epoch": index["support_epoch"], "coverage": dict(result.coverage), "candidates": rows}


def _register(index: dict[str, Any], page: Mapping[str, Any]) -> None:
    for name in page["aliases"]:
        paths = index["aliases"].get(name, [])
        if page["path"] not in paths:
            paths.append(page["path"])
            index["aliases"][name] = paths


def _edges(index: dict[str, Any], path: str) -> None:
    page = index["pages"][path]
    _, _, _, dependencies = _resolved(index, page)
    # Keep potential endpoints too. A private alias may be ambiguous globally
    # while the same authored target has one owner for a narrower audience.
    for unit in page["units"]:
        for raw in unit["raw_links"]:
            name = (
                raw.strip()
                .removeprefix("[[")
                .removesuffix("]]")
                .split("|", 1)[0]
                .split("#", 1)[0]
                .removesuffix(".md")
                .strip("/")
            )
            candidates = index["aliases"].get(name, []) or index["aliases"].get(name.casefold(), [])
            for position, dependency in enumerate(candidates):
                _checkpoint(index)
                if index.get("_deadline") is not None and (
                    position >= MAX_EDGES or len(dependencies) >= MAX_EDGES
                ):
                    raise TimeoutError("artifact role dependency extraction capped")
                dependencies.add(dependency)
    for dependency in dependencies:
        index["reverse"].setdefault(dependency, {})[path] = True
    index["dependencies"][path] = sorted(dependencies)


def build(root: Path, parsed_pages: list[Any]) -> dict[str, Any]:
    index: dict[str, Any] = {
        "support_epoch": 0,
        "pages": {},
        "origins": {},
        "aliases": {},
        "reverse": {},
        "dependencies": {},
    }
    for parsed in parsed_pages:
        state = _state(root, parsed)
        page = describe(root, state)
        index["pages"][state.path] = page
        _register(index, page)
    for path in index["pages"]:
        _edges(index, path)
    for path, page in index["pages"].items():
        if page["type"] == "experiment":
            try:
                index["origins"][path] = _origin(index, path)
            except Exception:  # noqa: BLE001 - derived advice never changes authored state
                log.debug("artifact review detector failed", exc_info=True)
                index["origins"][path] = {
                    "epoch": 0,
                    "coverage": dict.fromkeys(sensor.FAMILIES, "unknown"),
                    "candidates": [],
                }
    return index


def delta(
    root: Path, index: dict[str, Any], path: str, parsed: Any, *, families: set[str] | None = None
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """At most eight dependent origins; overflow invalidation never visits the tail."""
    previous = index["pages"].get(path)
    dependents = index["reverse"].get(path, {})
    # The reverse index includes destinations and source consumers. Read only the
    # bounded prefix; an epoch handles every unvisited row without enumerating it.
    affected = list(itertools.islice(dependents, sensor.MAX_DEPENDENT_ORIGINS))
    overflow = (
        dependents.has_more(sensor.MAX_DEPENDENT_ORIGINS)
        if isinstance(dependents, _Dependents)
        else len(dependents) > sensor.MAX_DEPENDENT_ORIGINS
    )
    if previous:
        old_dependencies = index["dependencies"].pop(path, [])
        affected.extend(old_dependencies[: sensor.MAX_DEPENDENT_ORIGINS])
        overflow |= len(old_dependencies) > sensor.MAX_DEPENDENT_ORIGINS
        if len(old_dependencies) > MAX_EDGES:
            overflow = True
        for dependency in old_dependencies[:MAX_EDGES]:
            index["reverse"].get(dependency, {}).pop(path, None)
        for name in previous["aliases"]:
            paths = index["aliases"].get(name, [])
            if path in paths:
                paths.remove(path)
            if not paths:
                index["aliases"].pop(name, None)
            else:
                index["aliases"][name] = paths
    if parsed is None:
        index["pages"].pop(path, None)
        page = None
    else:
        page = describe(root, _state(root, parsed))
        _checkpoint(index)
        index["pages"][path] = page
        _register(index, page)
        _edges(index, path)
        # A new destination names origins through its outgoing dependency edges.
        dependencies = index["dependencies"].get(path, [])
        affected.extend(dependencies[: sensor.MAX_DEPENDENT_ORIGINS])
        overflow |= len(dependencies) > sensor.MAX_DEPENDENT_ORIGINS or not page["complete"]
    # New/ambiguous alias ownership can change links not in the resolved index.
    # Mark the untouched world unknown; do not scan to discover those links here.
    overflow |= previous is None or page is None or previous["aliases"] != page["aliases"]
    touched = list(dict.fromkeys([path, *affected]))
    overflow |= len(touched) > sensor.MAX_DEPENDENT_ORIGINS + 1
    touched = touched[: sensor.MAX_DEPENDENT_ORIGINS + 1]
    if overflow:
        index["support_epoch"] += 1
    for origin in touched:
        if (
            origin in index["origins"]
            or (index["pages"].get(origin) or {}).get("type") == "experiment"
        ):
            try:
                row = _origin(index, origin, bounded=True)
                if families is not None:
                    row["candidates"] = [c for c in row["candidates"] if c["family"] in families]
                    for family in set(sensor.FAMILIES) - families:
                        row["coverage"][family] = "unknown"
                index["origins"][origin] = row
            except Exception:  # noqa: BLE001 - never invalidate a committed write
                index["origins"][origin] = {
                    "epoch": index["support_epoch"],
                    "coverage": dict.fromkeys(sensor.FAMILIES, "unknown"),
                    "candidates": [],
                }
    return index, tuple(touched)


def compose(
    root: Path,
    index: Mapping[str, Any],
    path: str,
    authorize: Callable[[str], bool],
    *,
    validate: bool = True,
) -> tuple[list[Any], dict[str, str]]:
    from .audit import AuditFinding

    coverage = dict.fromkeys(sensor.FAMILIES, "complete")
    page = index.get("pages", {}).get(path)
    row = index.get("origins", {}).get(path)
    if not row or not page or not authorize(path):
        return [], coverage
    if row["epoch"] != index["support_epoch"]:
        return [], dict.fromkeys(sensor.FAMILIES, "unknown")
    coverage.update(row["coverage"])
    audience_index = {**index, "_authorize": authorize}
    recomposed = _origin(audience_index, path, bounded=True)
    for family in sensor.FAMILIES:
        if coverage[family] != "unknown":
            coverage[family] = recomposed["coverage"][family]
    row = recomposed

    def current(dependency: str) -> bool:
        desc = index["pages"].get(dependency)
        return bool(desc and (not validate or _generation(root, dependency) == desc["generation"]))

    if not current(path):
        return [], dict.fromkeys(sensor.FAMILIES, "unknown")
    # Negative support is evidence too: a previously unrelated linked page can
    # become a covering destination without a governed delta.
    for dependency in index["reverse"].get(path, {}):
        _checkpoint(index)
        if authorize(dependency) and not current(dependency):
            return [], dict.fromkeys(sensor.FAMILIES, "unknown")
    findings = []
    counts = dict.fromkeys(sensor.FAMILIES, 0)
    for candidate in row["candidates"]:
        family = candidate["family"]
        if coverage[family] == "unknown":
            continue
        sources = tuple(p for p in candidate["sources"] if authorize(p))
        if candidate["role"] == "research_synthesis" and len(sources) < 2:
            continue
        supports = [support for support in candidate["supports"] if authorize(support["path"])]
        if any(not current(p) for p in [*sources, *(s["path"] for s in supports)]):
            coverage[family] = "unknown"
            continue
        if any(
            candidate["role"] != "research_synthesis"
            or set(sources) <= {p for p in support["sources"] if authorize(p)}
            for support in supports
        ):
            continue
        counts[family] += 1
        if counts[family] > sensor.MAX_CANDIDATES:
            coverage[family] = "capped"
            continue
        findings.append(
            AuditFinding(
                category=family,
                severity="info",
                path=path,
                detail="Review a reusable artifact home."
                if family == sensor.FAMILIES[0]
                else "Review current pending wording alongside the observed result.",
                proposed_fix=(
                    "Read the evidence units and use governed edits under existing authority."
                ),
                component={"family": family, "origin_ref": page["parent_ref"]},
                meta={
                    "role": candidate["role"],
                    "reason": candidate["reason"],
                    "evidence_refs": candidate["evidence_refs"],
                    "signal_version": sensor._hash((candidate["material_version"], sources)),
                    "review_partition": candidate["partition"],
                    "due_since": "0001-01-01",
                    "action_hints": ["review_destination", "extract_and_link"]
                    if family == sensor.FAMILIES[0]
                    else ["review_current_wording"],
                },
            )
        )
    _checkpoint(index)
    return [f for f in findings if coverage[f.category] != "unknown"], coverage


def inspect(
    root: Path, index: Mapping[str, Any], authorize: Callable[[str], bool], *, validate: bool = True
) -> tuple[list[Any], dict[str, str]]:
    findings = []
    coverage = dict.fromkeys(sensor.FAMILIES, "complete")
    order = {"complete": 0, "capped": 1, "unknown": 2}
    for path in index.get("origins", {}):
        _checkpoint(index)
        rows, states = compose(root, index, path, authorize, validate=validate)
        findings.extend(rows)
        for family, status in states.items():
            if order[status] > order[coverage[family]]:
                coverage[family] = status
    return findings, coverage


# The support index is keyed separately from the compact due-state JSON. Rewriting
# a page must not serialize every other page's unit/dependency descriptors.
STORE_VERSION = 2
MAX_SERVED_ORIGINS = 64


def store_path(root: Path) -> Path:
    from . import state_paths

    return state_paths.vault_state_dir(root) / ".artifact-role-state.sqlite"


def persist(root: Path, index: Mapping[str, Any]) -> dict[str, Any]:
    import json
    import sqlite3

    from . import state_paths

    state_paths.ensure_vault_state_dir(root)
    with sqlite3.connect(store_path(root), timeout=0.1) as connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS rows (section TEXT, key TEXT, payload TEXT,
                PRIMARY KEY(section,key));
            CREATE TABLE IF NOT EXISTS edges (target TEXT, source TEXT,
                PRIMARY KEY(target,source));
            CREATE TABLE IF NOT EXISTS aliases (name TEXT, path TEXT, PRIMARY KEY(name,path));
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value INTEGER);
        """)
        connection.execute("DELETE FROM rows")
        connection.execute("DELETE FROM edges")
        connection.execute("DELETE FROM aliases")
        for section in ("pages", "origins", "dependencies"):
            connection.executemany(
                "INSERT INTO rows VALUES (?,?,?)",
                (
                    (section, key, json.dumps(value, separators=(",", ":")))
                    for key, value in index[section].items()
                ),
            )
        connection.executemany(
            "INSERT INTO aliases VALUES (?,?)",
            ((name, path) for name, paths in index["aliases"].items() for path in paths),
        )
        connection.executemany(
            "INSERT INTO edges VALUES (?,?)",
            (
                (target, source)
                for target, sources in index["reverse"].items()
                for source in sources
            ),
        )
        connection.executemany(
            "INSERT OR REPLACE INTO meta VALUES (?,?)",
            (
                ("version", STORE_VERSION),
                ("support_epoch", index["support_epoch"]),
            ),
        )
    return {"version": STORE_VERSION, "has_origins": bool(index["origins"])}


class _Rows(Mapping):
    def __init__(self, connection: Any, section: str):
        self.connection = connection
        self.section = section
        self.cache: dict[str, Any] = {}

    def __getitem__(self, key: str) -> Any:
        import json

        if key not in self.cache:
            row = self.connection.execute(
                "SELECT payload FROM rows WHERE section=? AND key=?", (self.section, key)
            ).fetchone()
            if row is None:
                raise KeyError(key)
            self.cache[key] = json.loads(row[0])
        return self.cache[key]

    def __setitem__(self, key: str, value: Any) -> None:
        import json

        self.cache[key] = value
        self.connection.execute(
            "INSERT OR REPLACE INTO rows VALUES (?,?,?)",
            (self.section, key, json.dumps(value, separators=(",", ":"))),
        )

    def pop(self, key: str, default: Any = None) -> Any:
        value = self.get(key, default)
        self.cache.pop(key, None)
        self.connection.execute("DELETE FROM rows WHERE section=? AND key=?", (self.section, key))
        return value

    def __iter__(self):
        return (
            row[0]
            for row in self.connection.execute(
                "SELECT key FROM rows WHERE section=? ORDER BY key", (self.section,)
            )
        )

    def __len__(self) -> int:
        return len(
            self.connection.execute(
                "SELECT key FROM rows WHERE section=? LIMIT ?", (self.section, MAX_EDGES + 1)
            ).fetchall()
        )


class _Dependents(Mapping):
    def __init__(self, connection: Any, target: str):
        self.connection, self.target = connection, target

    def __getitem__(self, source: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM edges WHERE target=? AND source=?", (self.target, source)
        ).fetchone()
        if row is None:
            raise KeyError(source)
        return True

    def __setitem__(self, source: str, value: Any) -> None:
        self.connection.execute("INSERT OR IGNORE INTO edges VALUES (?,?)", (self.target, source))

    def pop(self, source: str, default: Any = None) -> None:
        self.connection.execute(
            "DELETE FROM edges WHERE target=? AND source=?", (self.target, source)
        )

    def __iter__(self):
        return (
            row[0]
            for row in self.connection.execute(
                "SELECT source FROM edges WHERE target=? ORDER BY source", (self.target,)
            )
        )

    def has_more(self, limit: int) -> bool:
        return (
            self.connection.execute(
                "SELECT 1 FROM edges WHERE target=? LIMIT 1 OFFSET ?", (self.target, limit)
            ).fetchone()
            is not None
        )

    def __len__(self) -> int:
        return len(
            self.connection.execute(
                "SELECT source FROM edges WHERE target=? LIMIT ?", (self.target, MAX_EDGES + 1)
            ).fetchall()
        )


class _Reverse:
    def __init__(self, connection: Any):
        self.connection = connection

    def get(self, target: str, default: Any = None) -> _Dependents:
        return _Dependents(self.connection, target)

    setdefault = get


def _stored_index(connection: Any) -> dict[str, Any]:
    version = connection.execute("SELECT value FROM meta WHERE key='version'").fetchone()
    if not version or version[0] != STORE_VERSION:
        raise ValueError("artifact role projection version mismatch")
    epoch = connection.execute("SELECT value FROM meta WHERE key='support_epoch'").fetchone()
    return {
        "support_epoch": epoch[0],
        **{section: _Rows(connection, section) for section in ("pages", "origins", "dependencies")},
        "reverse": _Reverse(connection),
        "aliases": _Aliases(connection),
    }


def apply_delta(root: Path, path: str, parsed: Any, *, families: set[str]) -> None:
    import sqlite3

    if not store_path(root).is_file():
        return
    with sqlite3.connect(store_path(root), timeout=0.05) as connection:
        index = _stored_index(connection)
        index["_deadline"] = time.monotonic() + 0.2
        try:
            delta(root, index, path, parsed, families=families)
        except Exception:  # noqa: BLE001 - invalidate all old support without visiting it
            index["support_epoch"] += 1
            log.debug("keyed artifact role delta unavailable", exc_info=True)
        connection.execute(
            "UPDATE meta SET value=? WHERE key='support_epoch'", (index["support_epoch"],)
        )


def served(root: Path, authorize: Callable[[str], bool]) -> tuple[list[Any], dict[str, str]]:
    import sqlite3

    unknown = dict.fromkeys(sensor.FAMILIES, "unknown")
    if not store_path(root).is_file():
        return [], unknown
    try:
        with sqlite3.connect(
            f"file:{store_path(root)}?mode=ro", uri=True, timeout=0.05
        ) as connection:
            index = _stored_index(connection)
            index["_deadline"] = time.monotonic() + 0.2
            paths = []
            for position, path in enumerate(itertools.islice(index["origins"], MAX_EDGES + 1)):
                _checkpoint(index)
                if position == MAX_EDGES:
                    return [], dict.fromkeys(sensor.FAMILIES, "capped")
                if not authorize(path):
                    continue
                paths.append(path)
                if len(paths) > MAX_SERVED_ORIGINS:
                    return [], dict.fromkeys(sensor.FAMILIES, "capped")
            index["origins"] = {path: index["origins"][path] for path in paths}
            return inspect(root, index, authorize)
    except Exception:  # noqa: BLE001 - compact carriers never expose invalid advice
        log.debug("keyed artifact role projection unavailable", exc_info=True)
        return [], unknown


class _AliasPaths:
    """Incremental alias membership, never a corpus-sized JSON value."""

    def __init__(self, connection: Any, name: str):
        self.connection, self.name = connection, name

    def __iter__(self):
        return (
            row[0]
            for row in self.connection.execute(
                "SELECT path FROM aliases WHERE name=? ORDER BY path", (self.name,)
            )
        )

    def __len__(self):
        return len(
            self.connection.execute(
                "SELECT 1 FROM aliases WHERE name=? LIMIT 2", (self.name,)
            ).fetchall()
        )

    def __contains__(self, path: str):
        return (
            self.connection.execute(
                "SELECT 1 FROM aliases WHERE name=? AND path=?", (self.name, path)
            ).fetchone()
            is not None
        )

    def __getitem__(self, index: int):
        row = self.connection.execute(
            "SELECT path FROM aliases WHERE name=? ORDER BY path LIMIT 1 OFFSET ?",
            (self.name, index),
        ).fetchone()
        if row is None:
            raise IndexError(index)
        return row[0]

    def append(self, path: str):
        self.connection.execute("INSERT OR IGNORE INTO aliases VALUES (?,?)", (self.name, path))

    def remove(self, path: str):
        self.connection.execute("DELETE FROM aliases WHERE name=? AND path=?", (self.name, path))


class _Aliases:
    def __init__(self, connection: Any):
        self.connection = connection

    def get(self, name: str, default: Any = None):
        return _AliasPaths(self.connection, name)

    def __setitem__(self, name: str, paths: Any):
        if isinstance(paths, _AliasPaths):
            return
        self.connection.execute("DELETE FROM aliases WHERE name=?", (name,))
        self.connection.executemany(
            "INSERT INTO aliases VALUES (?,?)", ((name, path) for path in paths)
        )

    def pop(self, name: str, default: Any = None):
        self.connection.execute("DELETE FROM aliases WHERE name=?", (name,))
