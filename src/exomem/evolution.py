"""The `evolution` view — "how did my view on X change over time?"

The vault already records how a conclusion moved: `replace` links every superseded note to
its successor (`superseded_by`) and predecessor (`supersedes`), and logs the reason to
`log.md`. This module reads that doubly-linked chain and surfaces it as an ordered
**timeline**: for a topic query, find the matching notes, resolve each into its supersession
chain, and return one timeline per chain — each version with its own structural claims, its
date, and the recorded reason it was superseded.

PURE ASSEMBLY (measurement), mirroring `attention.py` / `context_pack.py`:
- versions are ordered by the supersession POINTER spine (not by date — `replace` bumps the
  old page's `updated:` to the supersession date, so dates alone mis-order);
- each version's claims are extracted STRUCTURALLY (reused from `context_pack`);
- the transition reason is the RECORDED `log.md` `why:` (via `vault.read_log_entries`),
  surfaced verbatim — the server never generates a "here's how your thinking changed"
  narrative. The brain reads consecutive versions and infers the evolution.

Nothing is mutated, no generative/reasoning model runs, and `find` ordering is untouched.
Chains of length 1 (a note never superseded) are dropped — there is no evolution to show.
Every cap that drops content is reported in `truncation`.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import context_pack, get_page
from . import find as find_module
from . import vault as vault_module
from .find import Hit, ParsedPage

_DEFAULT_MAX_CHAINS = 10
_DEFAULT_MAX_VERSIONS = 25
_OVERFETCH = 5  # find more candidates than chains, since members collapse into one chain


def _int_env(value: int | None, env: str, default: int) -> int:
    if value is not None:
        return value
    raw = os.environ.get(env)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _load_link(vault_root: Path, raw_wikilink: str) -> ParsedPage | None:
    """Resolve a `supersedes`/`superseded_by` wikilink to its parsed page (or None)."""
    target = context_pack._wikilink_target(raw_wikilink)
    if not target:
        return None
    rel = target if target.endswith(".md") else target + ".md"
    return find_module._CACHE.get(vault_root / rel, vault_root)


def _resolve_chain(vault_root: Path, start: ParsedPage) -> dict[str, ParsedPage]:
    """Walk the supersession pointers from `start` both ways into the full chain.

    Forward via `superseded_by`, backward via `supersedes`, `seen`-guarded (the chain is
    linear — `replace` refuses to supersede an already-superseded page — but guard cycles).
    """
    members: dict[str, ParsedPage] = {start.rel_path: start}
    cur: ParsedPage | None = start
    while cur and cur.superseded_by:
        nxt = _load_link(vault_root, cur.superseded_by[0])
        if nxt is None or nxt.rel_path in members:
            break
        members[nxt.rel_path] = nxt
        cur = nxt
    cur = start
    while cur and cur.supersedes:
        prev = _load_link(vault_root, cur.supersedes[0])
        if prev is None or prev.rel_path in members:
            break
        members[prev.rel_path] = prev
        cur = prev
    return members


def _order_chain(vault_root: Path, members: dict[str, ParsedPage]) -> list[ParsedPage]:
    """Order chain members oldest→newest along the pointer spine (origin → head)."""
    def _is_origin(p: ParsedPage) -> bool:
        if not p.supersedes:
            return True
        prev = _load_link(vault_root, p.supersedes[0])
        return prev is None or prev.rel_path not in members

    origin = next((p for p in members.values() if _is_origin(p)), next(iter(members.values())))
    ordered: list[ParsedPage] = []
    seen: set[str] = set()
    cur: ParsedPage | None = origin
    while cur is not None and cur.rel_path not in seen:
        ordered.append(cur)
        seen.add(cur.rel_path)
        if not cur.superseded_by:
            break
        nxt = _load_link(vault_root, cur.superseded_by[0])
        cur = nxt if (nxt is not None and nxt.rel_path in members) else None
    return ordered


def _transition_reason(vault_root: Path, new_page: ParsedPage) -> tuple[str | None, str | None]:
    """The recorded reason `new_page` superseded its predecessor — read verbatim from the
    `log.md` entry `replace` writes under the new page (returns (reason, date)).

    Match the explicit `replace` op first (what supersession records); fall back to a
    summary mention only if absent, so a later ordinary `edit` whose rationale happens to
    say "supersede" can't masquerade as the transition reason.
    """
    entries = vault_module.read_log_entries(vault_root, new_page.rel_path)
    for entry in entries:
        if entry.get("op") == "replace":
            return entry.get("summary"), entry.get("date")
    for entry in entries:
        if "supersede" in entry.get("summary", "").lower():
            return entry["summary"], entry.get("date")
    return None, None


def _build_timeline(
    vault_root: Path, ordered: list[ParsedPage], *, anchor: str, max_versions: int
) -> tuple[dict, int]:
    """Assemble one chain's timeline dict; returns (timeline, dropped_versions)."""
    dropped = 0
    shown = ordered
    if max_versions > 0 and len(ordered) > max_versions:
        dropped = len(ordered) - max_versions
        shown = ordered[-max_versions:]  # keep the most recent versions (head matters most)

    versions: list[dict] = []
    for page in shown:
        transition = None
        if page.superseded_by:
            succ = _load_link(vault_root, page.superseded_by[0])
            if succ is not None:
                reason, tdate = _transition_reason(vault_root, succ)
                transition = {"reason": reason, "date": tdate}
        versions.append({
            "path": page.rel_path,
            "title": page.title,
            "status": page.status or "active",
            "date": page.updated,
            "claims": context_pack._extract_claims(page),
            "transition": transition,
        })

    # span + n_versions describe the WHOLE chain (`ordered`), not just the shown window —
    # the truncation note carries the dropped count, so these stay chain-level facts.
    dates = [p.updated for p in ordered if p.updated]
    timeline = {
        "chain_id": ordered[-1].rel_path,   # the active head
        "topic_anchor": anchor,
        "span": {
            "from": min(dates) if dates else "",
            "to": max(dates) if dates else "",
            "n_versions": len(ordered),
        },
        "versions": versions,
    }
    return timeline, dropped


def build_timelines(
    vault_root: Path,
    hits: list[Hit],
    *,
    max_chains: int | None = None,
    max_versions: int | None = None,
) -> dict:
    """Resolve the hits' supersession chains into ordered timelines. Pure measurement.

    Dedups hits that land on the same chain (keyed by active-head path), drops chains of
    length < 2, orders each by the pointer spine, caps to `max_chains` (find-relevance
    order) and each timeline to `max_versions`, reporting every drop in `truncation`.
    """
    max_chains = _int_env(max_chains, "EXOMEM_EVOLUTION_MAX_CHAINS", _DEFAULT_MAX_CHAINS)
    max_versions = _int_env(max_versions, "EXOMEM_EVOLUTION_MAX_VERSIONS", _DEFAULT_MAX_VERSIONS)

    seen_heads: set[str] = set()
    built: list[tuple[dict, int]] = []  # (timeline, dropped_versions)
    for hit in hits:
        page = find_module._CACHE.get(vault_root / hit.path, vault_root)
        if page is None:
            continue
        members = _resolve_chain(vault_root, page)
        if len(members) < 2:
            continue  # never superseded → no evolution to show
        ordered = _order_chain(vault_root, members)
        head = ordered[-1].rel_path
        if head in seen_heads:
            continue  # another hit already surfaced this chain
        seen_heads.add(head)
        built.append(_build_timeline(
            vault_root, ordered, anchor=page.rel_path, max_versions=max_versions
        ))

    # Apply the chains cap FIRST, so per-timeline version-truncation notes below can only
    # reference chains that are actually returned (never a dropped one).
    truncation: list[str] = []
    total = len(built)
    if max_chains > 0 and total > max_chains:
        built = built[:max_chains]
        truncation.append(
            f"showing {max_chains} of {total} chains "
            f"({total - max_chains} more not shown; raise `limit`)"
        )

    timelines: list[dict] = []
    for timeline, dropped in built:
        if dropped:
            truncation.append(
                f"timeline {timeline['chain_id']} capped at {max_versions} versions "
                f"({dropped} older not shown; raise EXOMEM_EVOLUTION_MAX_VERSIONS)"
            )
        timelines.append(timeline)
    return {"timelines": timelines, "truncation": truncation}


def evolution(
    vault_root: Path,
    *,
    query: str,
    limit: int = 10,
    scope: str = "kb",
    projects: list[str] | None = None,
    tags: list[str] | None = None,
) -> dict:
    """Timelines of how the topic's conclusions changed, from supersession chains. Read-only.

    Finds the topic, resolves each hit's supersession chain, and returns one ordered
    timeline per chain. Empty `timelines` means nothing matching the topic has been
    superseded — honestly empty, not an error. Results are bounded by `find`'s candidate
    pool (≤100): a chain whose only matching members fall outside it won't surface.
    """
    # Overfetch candidates since several hits collapse into one chain; `find` clamps to
    # 100, so an "uncapped" (limit<=0) call fetches that full pool.
    overfetch = 100 if limit <= 0 else min(max(limit * _OVERFETCH, 25), 100)
    hits = find_module.find(
        vault_root,
        query=query,
        scope=scope,
        projects=projects,
        tags=tags,
        limit=overfetch,
    )
    built = build_timelines(vault_root, hits, max_chains=limit)
    return {"query": query, **built}


def evolution_for_path(
    vault_root: Path,
    *,
    path: str,
    max_versions: int | None = None,
) -> dict:
    """Return the recorded supersession chain for one known page.

    This is the path-specific counterpart to :func:`evolution`: it avoids topic-search
    ambiguity when a caller already has a canonical review target. Assembly remains
    pointer-ordered and measurement-only; a page with no supersession history returns an
    honest empty timeline.
    """
    try:
        canonical_path = get_page.get_page(vault_root, path=path).path
    except get_page.GetError as exc:
        raise ValueError(f"{exc.code}: {exc.reason}") from exc
    page = find_module._CACHE.get(vault_root / canonical_path, vault_root)
    if page is None:
        raise ValueError(f"NOT_FOUND: no readable page at {canonical_path}")
    return evolution_for_page(
        vault_root,
        page=page,
        target_path=canonical_path,
        max_versions=max_versions,
    )


def evolution_for_page(
    vault_root: Path,
    *,
    page: ParsedPage,
    target_path: str | None = None,
    max_versions: int | None = None,
) -> dict:
    """Assemble one already-parsed page's recorded supersession chain.

    Review-context assembly uses this seam to avoid reparsing the selected target for
    each response section. Callers that only have a path use :func:`evolution_for_path`.
    """
    canonical_path = target_path or page.rel_path
    members = _resolve_chain(vault_root, page)
    if len(members) < 2:
        return {"target_path": canonical_path, "timelines": [], "truncation": []}

    resolved_max = _int_env(
        max_versions,
        "EXOMEM_EVOLUTION_MAX_VERSIONS",
        _DEFAULT_MAX_VERSIONS,
    )
    ordered = _order_chain(vault_root, members)
    timeline, dropped = _build_timeline(
        vault_root,
        ordered,
        anchor=canonical_path,
        max_versions=resolved_max,
    )
    truncation = []
    if dropped:
        truncation.append(
            f"timeline {timeline['chain_id']} capped at {resolved_max} versions "
            f"({dropped} older not shown; raise max_versions)"
        )
    return {
        "target_path": canonical_path,
        "timelines": [timeline],
        "truncation": truncation,
    }
