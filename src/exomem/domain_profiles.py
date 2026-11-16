"""Per-domain profiles as session context, for the domains a turn touches.

A profile is a live page of the shipped `profile` type whose topics resolve to one registered domain. The block
follows the stance's shape: compiled from admitted units, cached on what was admitted, pinned for the
invocation and decided again at terminal egress. It differs in being keyed by domain. Each served profile has its own
generation, so a caller holds some profiles and is sent the others.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import threading
from collections import OrderedDict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from . import (
    activation,
    activation_conventions,
    context_catalogue,
    note_types,
    profile_selection,
    session_units,
    source_taxonomy,
    vocabulary_resolution,
)
from .governance import egress, principal

# A resource bound, not vault vocabulary. The character bounds are the vault's `budgets` in its conventions.
CACHE_SIZE = 128
_CACHE: OrderedDict[tuple, tuple[date | None, dict[str, Any], list[dict[str, Any]]]] = OrderedDict()
_LOCK = threading.Lock()


def enabled() -> bool:
    return os.environ.get("EXOMEM_ACTIVATION_PROFILES", "on").strip().casefold() != "off"


def item_line(label: str, item: Mapping[str, Any]) -> str:
    """One line as the hook renders it (`_packet_line`): the service counts what the hook writes."""
    marker = f" (review due {item['review_due']})" if item.get("review_due") else ""
    fields = (label, f"{item['text']}{marker}", str(item["ref"]))
    kind, body, handle = (" ".join(field.split()) for field in fields)
    return f"- profile {kind}: {body} [{handle}]" if handle else f"- profile {kind}: {body}"


def _empty(state: str = "empty", selection: str = profile_selection.NONE) -> dict[str, Any]:
    """A block with no profile. `omitted` lists the profiles that did not fit; an unavailable block cannot say.

    `selection` names the evidence read to choose profiles: `semantic`, `lexical`, or `none` when no reading ran.
    """
    return {"state": state, "profiles": [], "omitted": None if state == "unavailable" else [], "selection": selection}


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class ProfilePage:
    """One admitted catalogue page that is a domain's profile."""

    path: str
    ref: str
    domain: str


def profile_pages(root: Path, caller: Any, catalogue: Any, taxonomy: source_taxonomy.SourceTaxonomy) -> list[ProfilePage]:
    """The caller's profile pages whose topics resolve to one registered domain, in path order.

    A page that names two domains, or one the registry does not resolve, supplies nothing here: it is neither a
    domain's profile nor the global one.
    """
    if not caller.domains_admitted:
        return []
    pending = set(catalogue.pending())
    pages = []
    for entry in catalogue.entries_by_id(catalogue.profile_ids()):
        if not entry.path or entry.path in pending or not caller.sees(entry.path):
            continue
        scope = vocabulary_resolution.profile_scope(taxonomy, entry.topics)
        if scope.kind == vocabulary_resolution.SCOPE_DOMAIN:
            pages.append(ProfilePage(entry.path, entry.ref or entry.path, scope.domain))
    return sorted(pages, key=lambda page: page.path)


def _with_ancestors(taxonomy: source_taxonomy.SourceTaxonomy, touched: Sequence[str]) -> list[str]:
    """Each touched domain followed by its ancestors, nearest first, once each."""
    ordered: dict[str, None] = {}
    for key in touched:
        for domain in (key, *taxonomy.domain_ancestors(key)):
            ordered.setdefault(domain, None)
    return list(ordered)


def compile_block(
    root: Path, caller: Any, *, turn: str = "", turn_vector: Any = None,
    catalogue: Any = None, known_generations: Mapping[str, str] | None = None, today: date | None = None,
    lexical: profile_selection.LexicalBasis | None = None,
) -> dict[str, Any]:
    """The profiles for the domains the turn touches, from admitted units only.

    A domain is touched when its profile page clears a floor in `profile_selection`, which reads the turn vector
    the activation pass already holds and one keyword query, both restricted to the caller's profile pages.
    The keyword query reads the catalogue at the `lexical` checkpoint the request holds; with none it does not run.
    `selection` on the block names the evidence read, so a turn without a vector shows as `lexical`.

    A hidden page or unit changes no count, generation or expiry. Each profile fits whole lines within the vault's
    `budgets.domain_profile_max_chars`, and together they fit `budgets.profiles_max_chars`. The block has its own
    bound outside the request's `max_chars`. A profile the caller holds at its current generation is
    `unchanged`: it spends none of the total. A profile that does not fit is listed in `omitted` by domain, label
    and page ref, so the agent can read it on demand; the vault holds no limit on how many profiles or levels exist.
    """
    today = today or date.today()
    known = known_generations or {}
    own_catalogue = catalogue is None
    catalogue = catalogue or context_catalogue.ContextCatalogue(root)
    try:
        taxonomy = source_taxonomy.load_taxonomy(root)
        pages = profile_pages(root, caller, catalogue, taxonomy)
    finally:
        if own_catalogue:
            catalogue.close()
    by_domain: dict[str, list[ProfilePage]] = {}
    for page in pages:
        by_domain.setdefault(page.domain, []).append(page)
    conventions = activation_conventions.load_conventions(root).conventions
    # The shipped conventions file always sets every limit; a missing one reads as the most restrictive value, so a
    # broken install selects and serves nothing.
    each = max(0, conventions.budget("domain_profile_max_chars") or 0)
    total = max(0, conventions.budget("profiles_max_chars") or 0)
    semantic_floor = conventions.budget("profile_semantic_floor")
    lexical_floor = conventions.budget("profile_lexical_floor")
    selection = profile_selection.select(
        root, [page.path for page in pages], turn, turn_vector, restricted=caller.restricted, lexical=lexical,
        semantic_floor=float("inf") if semantic_floor is None else semantic_floor,
        lexical_floor=float("inf") if lexical_floor is None else lexical_floor,
    )
    if pages and turn.strip() and selection.evidence == profile_selection.NONE:
        # Profiles exist and neither reading could run: the turn's profiles are unknown, not absent.
        block = _empty("unavailable")
        _pin(root, caller, block, [])
        return block
    domain_of = {page.path: page.domain for page in pages}
    touched = list(dict.fromkeys(domain_of[path] for path in selection.pages))[: max(0, int(conventions.budget("profile_domains_max") or 0))]
    order = [domain for domain in _with_ancestors(taxonomy, touched) if domain in by_domain]
    block = _empty(selection=selection.evidence)
    pinned: list[dict[str, Any]] = []
    used = 0
    stopped = False
    for domain in order:
        entry, units = _profile(root, caller, taxonomy, domain, [page.path for page in by_domain[domain]], each, today)
        if entry is None:
            continue
        # A page the caller sees and whose units were admitted: the agent may read it on demand.
        unfit = {"domain": domain, "label": entry["label"], "ref": by_domain[domain][0].ref}
        if not entry["lines"]:
            # Lines were admitted and none fit the profile's own bound: it does not fit.
            block["omitted"].append(unfit)
            pinned.extend(units)
            continue
        if entry["generation"] == known.get(domain):
            block["profiles"].append({**entry, "state": "unchanged", "lines": []})
            continue
        cost = sum(len(item_line(entry["label"], line)) + 1 for line in entry["lines"])
        if stopped or used + cost > total:
            # A weaker profile never passes over a stronger one that did not fit.
            stopped = True
            block["omitted"].append(unfit)
            pinned.extend(units)
            continue
        block["profiles"].append(entry)
        pinned.extend(units)
        used += cost
    # A turn that touched a profile the budget could not hold is not an empty one: `omitted` names it.
    block["state"] = "ready" if block["profiles"] or block["omitted"] else "empty"
    _pin(root, caller, block, pinned)
    return block


def _profile(
    root: Path, caller: Any, taxonomy: source_taxonomy.SourceTaxonomy, domain: str, paths: Sequence[str],
    limit: int, today: date,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """One domain's profile entry and the units it was admitted as, or None when it has no admitted line to serve.

    An entry with no lines is one whose admitted lines all exceeded `limit`.
    """
    basis = note_types.Basis(root)

    def accepts(page: Any) -> bool:
        # The catalogue may be a fold behind the page: decide again from the page's own bytes.
        scope = vocabulary_resolution.profile_scope(taxonomy, page.frontmatter.get("topics"))
        return (
            basis.selects(activation.normalized_page_type(page.page_type), note_types.profile)
            and scope.kind == vocabulary_resolution.SCOPE_DOMAIN and scope.domain == domain
        )

    admitted, status_basis = session_units.admit(root, caller, paths, accepts)
    definition = taxonomy.domains.get(domain)
    label = definition.label if definition is not None else source_taxonomy.derive_label(domain)
    key = (str(root), caller.binding, domain, _digest([label, session_units.identity(admitted)]), limit)
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None and (cached[0] is None or today < cached[0]):
            _CACHE.move_to_end(key)
            return copy.deepcopy(cached[1]) or None, copy.deepcopy(cached[2])
    served = session_units.serve(
        admitted, status_basis, cost=lambda line: len(item_line(label, line)), limit=limit, today=today,
    )
    entry: dict[str, Any] | None = None
    if served.lines or served.omitted:
        entry = {
            "domain": domain, "label": label, "generation": "", "state": "ready", "lines": served.lines,
            "omitted": served.omitted, "expired": served.expired,
        }
        entry["generation"] = _digest(entry)
    with _LOCK:
        _CACHE[key] = (min(served.boundaries) if served.boundaries else None, copy.deepcopy(entry or {}), copy.deepcopy(served.units))
        _CACHE.move_to_end(key)
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    return entry, served.units


def _pin(root: Path, caller: Any, block: dict[str, Any], served: list[dict[str, Any]]) -> None:
    held = principal.invocation_served()
    if held is not None:
        held[("domain_profiles", str(root), caller.binding, _digest(block), block["state"])] = (
            copy.deepcopy(block), copy.deepcopy(served),
        )


def guard_block(root: Path, caller: Any, block: dict[str, Any]) -> dict[str, Any]:
    """Restore this invocation's block and decide its complete unit prose at terminal egress."""
    held = principal.invocation_served()
    pinned = None if held is None else held.get(
        ("domain_profiles", str(root), caller.binding, _digest(block), block.get("state")),
    )
    if pinned is None:
        return _empty("unavailable")
    block, served = pinned
    # A bare {ref, text} line lacks the page path a release-gated vault needs for an identified page's unit refs.
    if any(verdict != egress.UNIT_KEPT for verdict in egress.classify_units(root, copy.deepcopy(served), purpose=caller.purpose)):
        return _empty("unavailable")
    return copy.deepcopy(block)
