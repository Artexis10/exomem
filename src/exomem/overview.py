"""overview: bounded, read-only vault-structure report.

Answers "what does this vault look like?" in one cheap pass so agents never
bulk-read a vault for a structural question, and so the setup wizard can scan a
vault BEFORE `init` lays down `Knowledge Base/` (this module takes a raw path
and never goes through `vault.resolve_vault`, which refuses un-initialized
roots).

Measurement only: one `os.walk`, `stat()` for everything, capped content reads
for markdown link/frontmatter stats. Output is bounded by construction — depth
and breadth caps on the tree, capped lists with exact totals alongside, a
per-file read cap — so the report stays token-bounded on arbitrarily large
vaults. Skips are reported, never silent.

The skip-set deliberately differs from `vault.VAULT_SCAN_SKIP_DIRS`: `_Schema`
is NOT skipped here (a structure report should show the schema tree), while
dot-directories are skipped unless `include_hidden=True`.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import access
from .governance.policy import GOVERNANCE_DIRNAME, is_governance_path
from .kbdir import kb_dirname

DEFAULT_MAX_DEPTH = 3
BREADTH_CAP = 12           # tree entries per parent folder
JUNK_LIST_CAP = 20         # listed junk paths per category (counts stay exact)
TOP_FILES_CAP = 5          # largest / oldest-unmodified entries
PATTERN_CAP = 3            # dominant name patterns per folder
CONTENT_READ_CAP = 512 * 1024  # bytes; larger md files are counted, not read

#: `_Governance/` is the policy tree, not vault content, and it is skipped for
#: EVERY audience including the owner — the same reasoning that keeps it out of
#: `find` (`test_governance_dir_never_surfaces_in_find`). It holds YAML rather
#: than markdown, so before this it sailed past the release walk filter and
#: enumerated the policy filenames themselves — `rules/['r.yaml']`,
#: `scopes/['patterns.yaml']` — telling a restricted audience how the vault is
#: governed and how many rules constrain it. Excluded outright rather than
#: decided, because a decision is the wrong shape: the answer never depends on
#: who is asking.
_SKIP_ALWAYS = frozenset({".git", "node_modules", GOVERNANCE_DIRNAME})
_SKIP_DEFAULT = frozenset({"_trash", "_attachments", ".trash"})

_WIKILINK = re.compile(r"\[\[[^\]]+\]\]")
_MD_LINK = re.compile(r"(?<!\[)\[[^\]\[]*\]\([^)]+\)")
_CONFLICT_NAME = re.compile(r"^(?P<base>.+) \d+(?P<ext>\.[^.]+)$")
_CONFLICT_TOKENS = ("conflicted copy", "sync-conflict")

SCOPE_NOTE = (
    "exomem writes only under 'Knowledge Base/'. Everything else in the vault "
    "is read-only input — never modified, still searchable via find "
    "scope=\"vault\"."
)


class OverviewError(Exception):
    """Structured failure: `code` is machine-readable, `reason` human-readable."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass
class _DirStat:
    path: str
    depth: int
    files_direct: int = 0
    md_direct: int = 0
    bin_direct: int = 0
    fm_yes: int = 0
    fm_total: int = 0
    wikilinks: int = 0
    md_links: int = 0
    names: list[str] = field(default_factory=list)
    children: list[str] = field(default_factory=list)
    # recursive accumulators, filled deepest-first after the walk
    files_rec: int = 0
    md_rec: int = 0
    bin_rec: int = 0
    fm_yes_rec: int = 0
    fm_total_rec: int = 0
    wl_rec: int = 0
    ml_rec: int = 0


def _resolve_subtree(root: Path, path: str) -> tuple[Path, str]:
    rel = (path or "").replace("\\", "/").strip("/")
    if rel:
        if Path(rel).is_absolute() or rel.startswith(".."):
            raise OverviewError("INVALID_PATH", f"path escapes the vault: {path!r}")
        scan = root / rel
        try:
            inside = scan.resolve().is_relative_to(root.resolve())
        except OSError as e:  # unresolvable component
            raise OverviewError("INVALID_PATH", str(e)) from e
        if not inside:
            raise OverviewError("INVALID_PATH", f"path escapes the vault: {path!r}")
    else:
        scan = root
    # A scan rooted AT (or inside) the policy tree walks it: pruning by child
    # dirname never fires, because a directory is not a child of itself. Same
    # NOT_FOUND shape the withheld-subtree probe uses, and ordered before
    # `exists()` so a present-but-excluded tree cannot answer differently from
    # an absent one.
    if rel and is_governance_path(rel):
        raise OverviewError("NOT_FOUND", f"no such vault path: {rel}")
    # …and again on the RESOLVED root, because a symlink launders the name:
    # `Notes/gov-link -> _Governance` carries no `_Governance` component in
    # `rel`, so the check above never fires, and `os.walk` follows the scan
    # root it is handed regardless of `followlinks=False` — that flag governs
    # descent, not the starting point.
    try:
        resolved = scan.resolve()
        if is_governance_path(str(resolved.relative_to(root.resolve()))):
            raise OverviewError("NOT_FOUND", f"no such vault path: {rel}")
    except (OSError, ValueError):
        # Unresolvable or outside the root — the checks below own that case.
        pass
    # Scoped-probe refusal: an excluded scan root (dir OR file) reads as
    # missing — byte-identical to the NOT_FOUND below — before `scan.exists()`
    # can let an excluded-but-present subtree succeed with an empty-but-valid
    # report, or `scan.is_dir()` can leak an excluded file via NOT_A_DIR.
    if rel and access.refuse_if_excluded(root, rel):
        raise OverviewError("NOT_FOUND", f"no such vault path: {rel}")
    if not scan.exists():
        raise OverviewError("NOT_FOUND", f"no such vault path: {rel or '.'}")
    if not scan.is_dir():
        raise OverviewError("NOT_A_DIR", f"not a directory: {rel}")
    return scan, rel


def overview(
    root: Path | str,
    path: str = "",
    max_depth: int = DEFAULT_MAX_DEPTH,
    include_hidden: bool = False,
    samples: int = 5,
) -> dict:
    """Structure report for `root` (or the subtree `path` under it). Read-only.

    Works on vaults with no initialized `Knowledge Base/` — the report simply
    carries `kb: {present: false}`.
    """
    root = Path(root)
    if not root.is_dir():
        raise OverviewError("NOT_FOUND", f"no such directory: {root}")
    scan, rel = _resolve_subtree(root, path)

    # Release-plane pruning, at the walk (design D1 / finding N1c). Imported
    # lazily so this module keeps no import-time dependency on governance, and
    # `None` on an ungoverned vault means the loop below is untouched.
    #
    # It has to happen HERE rather than at the dispatcher because every number
    # this function returns — per-folder counts, frontmatter coverage, totals,
    # `largest`, `oldest_unmodified`, junk lists — is a reduction over the
    # walk. Filtering the rendered rows afterwards leaves the counts standing,
    # and a count is a sharper oracle than the row it replaced.
    from .governance import egress as _egress

    release_keep = _egress.release_walk_filter(root)

    dirstats: dict[str, _DirStat] = {}
    skipped_dirs: set[str] = set()
    oversized = 0
    zero_byte: list[str] = []
    conflicts: list[str] = []
    sizes: list[tuple[int, str]] = []
    mtimes: list[tuple[float, str]] = []
    total_bytes = 0
    max_depth_seen = 0

    for dirpath, dirnames, filenames in os.walk(scan):
        dirnames.sort()
        filenames.sort()

        rp = os.path.relpath(dirpath, scan)
        rp = "" if rp == "." else rp.replace(os.sep, "/")
        # vault-relative (from `root`, not `scan`) form for access-tier checks —
        # `_access.yaml` entries are keyed from the vault root, not the scanned
        # subtree.
        root_rp = f"{rel}/{rp}" if rel and rp else (rel or rp)

        kept: list[str] = []
        for d in dirnames:
            if d == GOVERNANCE_DIRNAME:
                # Pruned SILENTLY — not even a `skipped.dirs` entry. Naming it
                # there just relocates the oracle: "this vault is governed" is
                # exactly the fact the entry would carry, and it is the first
                # thing a restricted audience wants to know.
                continue
            if d in _SKIP_ALWAYS or (
                not include_hidden and (d.startswith(".") or d in _SKIP_DEFAULT)
            ):
                skipped_dirs.add(d)
                continue
            # Excluded subtrees are pruned silently — no skipped-dir entry, no
            # "hidden N" marker (either would itself leak the subtree's
            # existence/size). They simply never entered the walk.
            child_rel = f"{root_rp}/{d}" if root_rp else d
            if access.access_tier(root, child_rel) == access.TIER_EXCLUDED:
                continue
            kept.append(d)
        dirnames[:] = kept

        depth = 0 if not rp else rp.count("/") + 1
        max_depth_seen = max(max_depth_seen, depth)
        st = dirstats.setdefault(rp, _DirStat(path=rp, depth=depth))
        st.children = [f"{rp}/{d}" if rp else d for d in dirnames]

        name_set = set(filenames)
        for fn in filenames:
            if not include_hidden and fn.startswith("."):
                continue
            child_rel = f"{root_rp}/{fn}" if root_rp else fn
            # Excluded-tier files never enter the walk (access.py contract) …
            if access.access_tier(root, child_rel) == access.TIER_EXCLUDED:
                continue
            # … and neither do withheld pages, so they cannot reach a count, a
            # sample, a size ranking or a junk list. Pruned silently: a
            # "hidden N" marker would itself be the oracle.
            if release_keep is not None and not release_keep(child_rel):
                continue
            fpath = Path(dirpath) / fn
            try:
                fst = fpath.stat()
            except OSError:
                continue
            frel = f"{rp}/{fn}" if rp else fn
            st.files_direct += 1
            st.names.append(fn)
            total_bytes += fst.st_size
            sizes.append((fst.st_size, frel))
            mtimes.append((fst.st_mtime, frel))
            if fst.st_size == 0:
                zero_byte.append(frel)
            m = _CONFLICT_NAME.match(fn)
            low = fn.lower()
            if (m and (m["base"] + m["ext"]) in name_set) or any(
                t in low for t in _CONFLICT_TOKENS
            ):
                conflicts.append(frel)
            if low.endswith(".md"):
                st.md_direct += 1
                if fst.st_size > CONTENT_READ_CAP:
                    oversized += 1
                    continue
                try:
                    text = fpath.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                st.fm_total += 1
                if text.lstrip("﻿").startswith("---"):
                    st.fm_yes += 1
                st.wikilinks += len(_WIKILINK.findall(text))
                st.md_links += len(_MD_LINK.findall(text))
            else:
                st.bin_direct += 1

    # roll direct stats up into recursive accumulators, deepest-first
    for rp in sorted(dirstats, key=lambda r: dirstats[r].depth, reverse=True):
        st = dirstats[rp]
        st.files_rec += st.files_direct
        st.md_rec += st.md_direct
        st.bin_rec += st.bin_direct
        st.fm_yes_rec += st.fm_yes
        st.fm_total_rec += st.fm_total
        st.wl_rec += st.wikilinks
        st.ml_rec += st.md_links
        if rp:
            parent = rp.rsplit("/", 1)[0] if "/" in rp else ""
            pst = dirstats[parent]
            pst.files_rec += st.files_rec
            pst.md_rec += st.md_rec
            pst.bin_rec += st.bin_rec
            pst.fm_yes_rec += st.fm_yes_rec
            pst.fm_total_rec += st.fm_total_rec
            pst.wl_rec += st.wl_rec
            pst.ml_rec += st.ml_rec

    # select shown folders: depth cap rolls deeper folders into ancestors,
    # breadth cap keeps the busiest children with an explicit omitted count
    shown: dict[str, int] = {}

    def _select(rp: str) -> None:
        st = dirstats[rp]
        if st.depth >= max_depth:
            shown[rp] = len(st.children)
            return
        ranked = sorted(st.children, key=lambda c: (-dirstats[c].files_rec, c))
        if release_keep is not None:
            # A folder whose every page was pruned must not appear as an
            # empty node: "this folder exists and holds nothing you may see"
            # is the same disclosure the counts were making. It reads as a
            # folder that is not there.
            ranked = [c for c in ranked if dirstats[c].files_rec > 0]
        keep = ranked[:BREADTH_CAP]
        shown[rp] = len(ranked) - len(keep)
        for child in sorted(keep):
            _select(child)

    _select("")

    # Scoped-probe refusal, the same shape PR #321 used for excluded subtrees:
    # asking specifically about a subtree that governance empties reads as
    # NOT_FOUND — byte-identical to a path that does not exist — rather than
    # returning a valid report full of zeroes, which would confirm both that
    # the folder exists and that it is being withheld.
    if release_keep is not None and rel and dirstats[""].files_rec == 0:
        raise OverviewError("NOT_FOUND", f"no such vault path: {rel}")

    tree: list[dict] = []
    for rp in sorted(shown):
        st = dirstats[rp]
        buckets = Counter(re.sub(r"\d", "N", n) for n in st.names)
        patterns = [
            {"pattern": p, "count": c}
            for p, c in sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
            if c >= 2
        ][:PATTERN_CAP]
        fm_pct = (
            round(100 * st.fm_yes_rec / st.fm_total_rec, 1) if st.fm_total_rec else None
        )
        tree.append(
            {
                "path": rp,
                "depth": st.depth,
                "files_direct": st.files_direct,
                "files_recursive": st.files_rec,
                "markdown": st.md_rec,
                "binary": st.bin_rec,
                "frontmatter_pct": fm_pct,
                "wikilinks": st.wl_rec,
                "md_links": st.ml_rec,
                "name_patterns": patterns,
                "sample_names": st.names[: max(samples, 0)],
                "children_omitted": shown[rp],
            }
        )

    kb: dict = {"present": False}
    if (root / kb_dirname()).is_dir():
        kb = {"present": True, "path": kb_dirname()}
        if not rel:
            kb_stat = dirstats.get(kb_dirname())
            kb["files"] = kb_stat.files_rec if kb_stat else 0

    root_stat = dirstats[""]
    return {
        "scope_note": SCOPE_NOTE,
        "root": rel,
        "totals": {
            "files": root_stat.files_rec,
            "dirs": len(dirstats) - 1,
            "markdown": root_stat.md_rec,
            "binary": root_stat.bin_rec,
            "bytes": total_bytes,
            "max_depth_seen": max_depth_seen,
        },
        "kb": kb,
        "tree": tree,
        "junk": {
            "zero_byte": sorted(zero_byte)[:JUNK_LIST_CAP],
            "sync_conflicts": sorted(conflicts)[:JUNK_LIST_CAP],
            "counts": {
                "zero_byte": len(zero_byte),
                "sync_conflicts": len(conflicts),
            },
        },
        "largest": [
            {"path": p, "bytes": s}
            for s, p in sorted(sizes, key=lambda t: (-t[0], t[1]))[:TOP_FILES_CAP]
        ],
        "oldest_unmodified": [
            {
                "path": p,
                "modified": datetime.fromtimestamp(m, tz=UTC).date().isoformat(),
            }
            for m, p in sorted(mtimes, key=lambda t: (t[0], t[1]))[:TOP_FILES_CAP]
        ],
        "skipped": {
            "dirs": sorted(skipped_dirs),
            "oversized_files": oversized,
        },
        "warnings": [],
    }
