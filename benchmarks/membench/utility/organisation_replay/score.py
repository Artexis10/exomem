"""The f34 scorer: PREREGISTRATION section 7 sequence 8, with ``protocol.yaml``.

It reads the run root (``OUT/binding-<arm>.json`` and
``OUT/<situation>/<arm>/run.json``) and the key files of the situations the run
declares, and nothing else. It makes no model call. Each arm's role comes from
its binding record, never from a label the operator types.
A residual answer goes to a blind judge as a five-field item; the scorer
never judges one, and a judge never overturns a deterministic result.

The key files carry no published schema. ``KEY_SCHEMA`` below is the closed
set of fields the scorer knows, and it refuses a key with any other field, or a
rule it does not implement, rather than ignore what it cannot read.

The run reaches a decision only when every situation it declares has both arms
finished, no more than two are struck and every residual item has a verdict.
Otherwise the report says which of those states it is in, and gives no table.
The ``score`` command exits 0 when it decided or awaits the judge, 1 for a void
or incomplete run, and 2 when it refuses an input.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import secrets
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .runfile import _result_text, check_run
from .situation import (
    HELD_OUT_PREFIX,
    SituationRefused,
    _refuse_held_out_without_custody,
    manifest_digests,
)

#: The reference relations of the protocol's end-state reading: a closed set that
#: the core relation pack fixes, so a code constant is the protocol's own list.
REFERENCE_RELATIONS = frozenset({"links_to", "mentions", "about_entity", "cites"})
#: The key schema's local reference for the referent page; its value is prose.
REFERENT_REF = "R"
#: z for a 95% interval, as protocol.yaml states it.
Z = 1.959963984540054
#: More struck situations than this void the run (section 7, Faults).
STRIKE_LIMIT = 2
#: The Unicode ``Dash=Yes`` property (PropList.txt), a closed set the standard
#: fixes; ``unicodedata`` has no property lookup, and a test pins it to ``\p{Dash}``.
DASHES = frozenset(
    map(
        chr,
        [
            *(0x2D, 0x58A, 0x5BE, 0x1400, 0x1806, *range(0x2010, 0x2016), 0x2053, 0x207B),
            *(0x208B, 0x2212, 0x2E17, 0x2E1A, 0x2E3A, 0x2E3B, 0x2E40, 0x2E5D, 0x301C),
            *(0x3030, 0x30A0, 0xFE31, 0xFE32, 0xFE58, 0xFE63, 0xFF0D, 0x10D6E, 0x10EAD),
        ],
    )
)
#: protocol.yaml's diagnostics bootstrap: resamples and seed.
BOOTSTRAP_RESAMPLES = 10_000
BOOTSTRAP_SEED = 20261030
#: The key file's closed schema: each mapping names its known fields, ``...`` marks
#: a leaf, and a one-item list marks a list of that shape. ``endpoints`` maps open
#: local references (``A``, ``B``, ``R``) to names, so its own keys are not checked.
_EDGE = {"relation": ..., "from": ..., "to": ...}
_ANSWER = {"question": ..., "key_fact": ..., "accepted_values": [...]}
KEY_SCHEMA = {
    "id": ...,
    "case_file": ...,
    "case_sha256": ...,
    "category": ...,
    "variant": ...,
    "client_shape": ...,
    "endpoints": ...,
    "source_truth": [...],
    "referent": {"match_tokens": [...], "wrong_core_types": [...], "durability": ...},
    "promoted_entry": {"registry": ..., "key": ...},
    "state_success": {
        "rule": ...,
        "note": ...,
        "pair": [...],
        "folder": ...,
        "folders": [...],
        "required_edge": _EDGE,
    },
    "supported_edges": [_EDGE],
    "false_precision": {
        "pairs": [{"between": [...], "supported_edges": [_EDGE]}],
        "wrong_core_types": [...],
        "wrong_homes": [...],
        "twin_promoted": [...],
        "lure_families_named_for_diagnosis": [...],
    },
    "diagnostic_anchor_edge": [_EDGE],
    "matched_shape": ...,
    "answer": _ANSWER,
    "judge_reference_view": _ANSWER,
}
#: The fields a key must carry for every rule.
KEY_REQUIRED = ("id", "case_sha256", "category", "variant", "client_shape", "state_success")
#: The false-precision fields each variant's protocol.yaml rule reads. Variants are
#: SCHEMA.md's closed tokens; without a field a key would score part of its false
#: precision as absent. The two referent rules read a wrong type or an unsupported
#: edge to the anchor, so they need both fields.
FALSE_PRECISION_FIELDS = {
    "specific_fits": ("false_precision.pairs",),
    "generic_or_none": ("false_precision.pairs",),
    "positive": ("false_precision.wrong_core_types", "false_precision.pairs"),
    "durability_twin": ("false_precision.twin_promoted",),
    "under_expected_home": ("false_precision.wrong_homes",),
    "outside_unrelated_homes": ("false_precision.wrong_homes",),
    "promoted_entity_type": ("false_precision.wrong_core_types", "false_precision.pairs"),
    "promoted_relation": ("false_precision.pairs",),
}


class ScoreRefused(RuntimeError):
    """An input the scorer cannot score without guessing."""


# --------------------------------------------------------------------------
# Matching (protocol.yaml normalization and answer_reading).
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFKC, casefold, every dash character read as ``-``, whitespace collapsed, trimmed."""

    folded = unicodedata.normalize("NFKC", text or "").casefold()
    dashed = "".join("-" if ch in DASHES else ch for ch in folded)
    return " ".join(dashed.split())


def bounded_match(value: str, text: str) -> bool:
    """The value occurs in the text, bounded by an end of the text or a non-alphanumeric character."""

    needle, hay = normalize(value), normalize(text)
    if not needle:
        return False
    start = hay.find(needle)
    while start != -1:
        end = start + len(needle)
        if (start == 0 or not hay[start - 1].isalnum()) and (
            end == len(hay) or not hay[end].isalnum()
        ):
            return True
        start = hay.find(needle, start + 1)
    return False


def _spaced(text: str) -> str:
    """A stem read with hyphens and underscores as spaces, then normalized."""

    return normalize((text or "").replace("-", " ").replace("_", " "))


# --------------------------------------------------------------------------
# The end state of one run, read as protocol.yaml's end_state_reading.
# --------------------------------------------------------------------------


@dataclass
class EndState:
    pages: list[dict]
    changed: set[str]
    edges: list[dict]
    relations: Mapping[str, Mapping]
    vault_types: Mapping[str, Mapping]
    kb_prefix: str
    notes_root: str
    aliases: dict[str, str] = field(default_factory=dict)

    @classmethod
    def read(cls, end_state: Mapping) -> EndState:
        from exomem import kbdir
        from exomem.note_types import NOTES_FOLDER

        relations = end_state.get("relations") or {}
        aliases = {
            alias: key for key, entry in relations.items() for alias in entry.get("aliases") or []
        }
        return cls(
            pages=list(end_state.get("pages") or []),
            changed=set(end_state.get("pages_changed") or []),
            edges=list(end_state.get("edges") or []),
            relations=relations,
            vault_types=(end_state.get("entity_types") or {}).get("vault") or {},
            kb_prefix=kbdir.kb_prefix(),
            notes_root=NOTES_FOLDER,
            aliases=aliases,
        )

    def kb_relative(self, path: str) -> str:
        return path.removeprefix(self.kb_prefix)

    def relation_lineage(self, key: str) -> list[str]:
        canonical = self.aliases.get(key, key)
        entry = self.relations.get(canonical) or {}
        return [canonical, *(entry.get("parent_chain") or [])]

    def is_generic(self, key: str) -> bool:
        return any((self.relations.get(k) or {}).get("generic") for k in self.relation_lineage(key))

    def is_reference(self, key: str) -> bool:
        return bool(REFERENCE_RELATIONS & set(self.relation_lineage(key)))

    def is_typed(self, key: str) -> bool:
        """Neither generic nor reference: the edges that can be false precision."""

        return not self.is_generic(key) and not self.is_reference(key)

    def readings(self, edge: Mapping) -> list[tuple[str, str | None, str | None]]:
        """``(relation, subject, target)`` readings of one edge.

        The edge as written; both orientations for a symmetric relation; and the
        registered inverse read the other way, which states the same fact.
        """

        relation, subject, target = edge["relation"], edge.get("subject"), edge.get("target_path")
        entry = self.relations.get(self.aliases.get(relation, relation)) or {}
        out = [(relation, subject, target)]
        if entry.get("direction") == "symmetric":
            out.append((relation, target, subject))
        if entry.get("inverse"):
            out.append((entry["inverse"], target, subject))
        return out

    def type_lineage(self, page: Mapping) -> list[str]:
        key = page.get("entity_type") or page.get("type") or ""
        return [key, *((self.vault_types.get(key) or {}).get("parent_chain") or [])]

    def entity_pages(self) -> list[dict]:
        return [page for page in self.pages if page.get("role") == "entity"]

    def notes_pages(self, paths: Iterable[str]) -> list[str]:
        root = self.notes_root + "/"
        return sorted(p for p in paths if self.kb_relative(p).startswith(root))

    def under(self, path: str, folder: str) -> bool:
        return self.kb_relative(path).startswith(folder.strip("/") + "/")

    def created(self, page: Mapping) -> bool:
        return not page.get("in_seed")


# --------------------------------------------------------------------------
# One arm in one situation.
# --------------------------------------------------------------------------


class Situation:
    """The key's items resolved against one arm's end state.

    A typed claim is from the capture session when the page that holds the edge
    was created or changed in that session (``pages_changed``), or, for an entity
    page, when the seed snapshot does not hold the page (``in_seed``). The run
    file keeps no list of seed edges, so the scorer cannot check that the seed
    did not hold an edge. SCHEMA.md seeds carry no typed edges, so the two
    readings agree; a seed edge on a page the session changed would also count.
    """

    def __init__(self, key: Mapping, state: EndState) -> None:
        self.key = key
        self.state = state
        self.tokens = list((key.get("referent") or {}).get("match_tokens") or [])

    def referent_pages(self, tokens: Sequence[str] | None = None) -> list[dict]:
        tokens = self.tokens if tokens is None else tokens
        return [
            page
            for page in self.state.entity_pages()
            if any(
                normalize(token) in normalize(page.get("title") or "")
                or _spaced(token) in _spaced(page.get("stem") or "")
                for token in tokens
            )
        ]

    def resolve(self, ref: str) -> set[str]:
        if ref == REFERENT_REF:
            return {page["path"] for page in self.referent_pages()}
        name = (self.key.get("endpoints") or {}).get(ref)
        if not name:
            raise ScoreRefused(f"{self.key['id']}: the key names no endpoint {ref!r}")
        return {
            page["path"]
            for page in self.state.entity_pages()
            if normalize(page.get("title") or "") == normalize(name)
            or _spaced(page.get("stem") or "") == _spaced(name)
        }

    def between(self, x: str, y: str, *, capture_only: bool = False) -> list[dict]:
        xs, ys = self.resolve(x), self.resolve(y)
        return [
            edge
            for edge in self.state.edges
            if (not capture_only or edge.get("holder") in self.state.changed)
            and (
                (edge.get("subject") in xs and edge.get("target_path") in ys)
                or (edge.get("subject") in ys and edge.get("target_path") in xs)
            )
        ]

    def matches(self, edge: Mapping, entry: Mapping, *, lineage_key: str | None = None) -> bool:
        """The edge states the entry: its lineage holds the relation, from and to agree."""

        wanted = lineage_key or entry["relation"]
        sources, targets = self.resolve(entry["from"]), self.resolve(entry["to"])
        return any(
            wanted in self.state.relation_lineage(relation)
            and subject in sources
            and target in targets
            for relation, subject, target in self.state.readings(edge)
        )

    def pair_false_precision(self) -> list[str]:
        found = []
        for pair in (self.key.get("false_precision") or {}).get("pairs") or []:
            x, y = pair["between"]
            supported = pair.get("supported_edges") or []
            for edge in self.between(x, y, capture_only=True):
                if self.state.is_typed(edge["relation"]) and not any(
                    self.matches(edge, entry) for entry in supported
                ):
                    found.append(f"unsupported_edge:{edge['relation']}")
        return found

    def wrong_type_false_precision(self) -> list[str]:
        wrong = set((self.key.get("false_precision") or {}).get("wrong_core_types") or [])
        return [
            f"wrong_type:{page.get('entity_type') or page.get('type')}"
            for page in self.referent_pages()
            if page["path"] in self.state.changed and wrong & set(self.state.type_lineage(page))
        ]

    def home_false_precision(self) -> list[str]:
        created = [p["path"] for p in self.state.pages if self.state.created(p)]
        return [
            f"wrong_home:{home}"
            for home in (self.key.get("false_precision") or {}).get("wrong_homes") or []
            if any(self.state.under(path, home) for path in self.state.notes_pages(created))
        ]

    def twin_false_precision(self) -> list[str]:
        tokens = (self.key.get("false_precision") or {}).get("twin_promoted") or []
        created = [page for page in self.referent_pages(tokens) if self.state.created(page)]
        return ["twin_referent_page"] if tokens and created else []

    def false_precision(self) -> list[str]:
        return (
            self.pair_false_precision()
            + self.wrong_type_false_precision()
            + self.home_false_precision()
            + self.twin_false_precision()
        )

    def named_paths(self) -> set[str]:
        refs = set(self.key.get("endpoints") or {})
        named: set[str] = set()
        for ref in refs:
            named |= self.resolve(ref)
        return named

    def anchor_edge_supported(self) -> bool | None:
        """Whether an edge states one of the key's diagnostic anchor entries; ``None`` without one."""

        entries = self.key.get("diagnostic_anchor_edge") or []
        if not entries:
            return None
        return any(
            self.matches(edge, entry)
            for entry in entries
            for edge in self.between(entry["from"], entry["to"])
        )

    def uncounted_typed_claims(self) -> int:
        named = self.named_paths()
        edges = sum(
            1
            for edge in self.state.edges
            if edge.get("holder") in self.state.changed
            and self.state.is_typed(edge["relation"])
            and not (edge.get("subject") in named and edge.get("target_path") in named)
        )
        pages = sum(
            1
            for page in self.state.entity_pages()
            if self.state.created(page) and page["path"] not in named
        )
        return edges + pages


# Each rule returns ``(state outcome, omission codes)``. The rule names are the key
# schema's closed ``state_success.rule`` tokens; the omission codes are protocol.yaml's.


def _pair_omission(s: Situation, edges: list[dict], *, reference_code: bool) -> list[str]:
    if not edges:
        return ["no_edge_between_pair"]
    if all(s.state.is_reference(e["relation"]) for e in edges):
        return ["reference_edge_only" if reference_code else "promoted_entry_not_used"]
    if not any(s.state.is_typed(e["relation"]) for e in edges):
        return ["generic_edge_only"]
    return []


def _pair_edge_supported(s: Situation) -> tuple[bool, list[str]]:
    x, y = s.key["state_success"]["pair"]
    edges = s.between(x, y)
    held = any(
        s.state.is_typed(edge["relation"])
        and any(s.matches(edge, entry) for entry in s.key.get("supported_edges") or [])
        for edge in edges
    )
    return held, [] if held else _pair_omission(s, edges, reference_code=True)


def _none_required(s: Situation) -> tuple[bool, list[str]]:
    return True, []


def _referent_fits(s: Situation) -> tuple[bool, list[str]]:
    wrong = set((s.key.get("referent") or {}).get("wrong_core_types") or [])
    pages = s.referent_pages()
    held = any(not (wrong & set(s.state.type_lineage(page))) for page in pages)
    return held, [] if pages else ["no_referent_page"]


def _placed_under_home(s: Situation) -> tuple[bool, list[str]]:
    changed = s.state.notes_pages(s.state.changed)
    folder = s.key["state_success"]["folder"]
    held = any(s.state.under(path, folder) for path in changed)
    if held:
        return True, []
    return False, ["placed_outside_expected_home" if changed else "no_notes_root_page"]


def _placed_outside_homes(s: Situation) -> tuple[bool, list[str]]:
    changed = s.state.notes_pages(s.state.changed)
    if not changed:
        return False, ["no_notes_root_page"]
    created = {p["path"] for p in s.state.pages if s.state.created(p)}
    homes = s.key["state_success"]["folders"]
    inside = any(s.state.under(path, home) for path in changed if path in created for home in homes)
    return not inside, []


def _referent_type_promoted(s: Situation) -> tuple[bool, list[str]]:
    promoted = s.key["promoted_entry"]["key"]
    pages = s.referent_pages()
    held = any(promoted in s.state.type_lineage(page) for page in pages)
    if held:
        return True, []
    return False, ["promoted_entry_not_used" if pages else "no_referent_page"]


def _pair_edge_promoted(s: Situation) -> tuple[bool, list[str]]:
    required = s.key["state_success"]["required_edge"]
    promoted = s.key["promoted_entry"]["key"]
    edges = s.between(required["from"], required["to"])
    held = any(s.matches(edge, required, lineage_key=promoted) for edge in edges)
    if held:
        return True, []
    if any(promoted in s.state.relation_lineage(edge["relation"]) for edge in edges):
        # The promoted entry is used, the other way round: no listed code says that.
        return False, []
    return False, _pair_omission(s, edges, reference_code=False) or ["promoted_entry_not_used"]


#: Each rule, and the key fields it reads beyond ``KEY_REQUIRED``, as dotted paths.
RULES = {
    "pair_edge_supported": (_pair_edge_supported, ("state_success.pair", "supported_edges")),
    "none_required": (_none_required, ()),
    "referent_entity_exists_with_fitting_type": (_referent_fits, ("referent.match_tokens",)),
    "placed_under_home": (_placed_under_home, ("state_success.folder",)),
    "placed_outside_homes": (_placed_outside_homes, ("state_success.folders",)),
    "referent_type_lineage_includes_promoted": (
        _referent_type_promoted,
        ("referent.match_tokens", "promoted_entry.key"),
    ),
    "pair_edge_lineage_includes_promoted": (
        _pair_edge_promoted,
        ("state_success.required_edge", "promoted_entry.key"),
    ),
}


def _hook_texts(run_dir: Path, session: Mapping) -> list[str]:
    """Hook-injected context: each hook response's text in the session's transcript."""

    from membench.utility.switch_eval.session import hook_push

    transcript = run_dir / session["transcript_path"]
    if not transcript.is_file():
        raise ScoreRefused(f"{transcript} is missing; received-fact cannot be read")
    texts = []
    for line in transcript.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "hook_response":
            texts.append(hook_push(event))
    return texts


def received_fact(key: Mapping, run_dir: Path, fresh: Mapping) -> bool:
    """An accepted value or a match token occurs in a fresh tool result or hook context."""

    values = list(key["answer"]["accepted_values"]) + list(
        (key.get("referent") or {}).get("match_tokens") or []
    )
    texts = [_result_text(call.get("result")) for call in fresh.get("calls") or []]
    texts += _hook_texts(run_dir, fresh)
    return any(bounded_match(value, text) for value in values for text in texts)


def score_arm(key: Mapping, run: Mapping, run_dir: Path) -> dict:
    """One arm's deterministic row for one situation."""

    rule = key["state_success"].get("rule")
    if rule not in RULES:
        raise ScoreRefused(f"{key['id']}: rule {rule!r} is not implemented")
    situation = Situation(key, EndState.read(run["end_state"]))
    state, omissions = RULES[rule][0](situation)
    false_precision = situation.false_precision()
    sessions = {s["role"]: s for s in run.get("sessions") or []}
    fresh = sessions.get("fresh") or {}
    answer = fresh.get("final_answer") or ""
    matched = any(bounded_match(v, answer) for v in key["answer"]["accepted_values"])
    residual = (
        not matched and state and run["status"] == "ok" and received_fact(key, run_dir, fresh)
    )
    if not matched and not residual:
        omissions = [*omissions, "answer_lacks_fact"]
    reasons = run["end_state"].get("operation_log", {}).get("edge_review_reasons") or {}
    return {
        "arm": run["arm"],
        "status": run["status"],
        "state_outcome": state,
        "answer_match": matched,
        # The judge decides these; until then success is unknown, never false.
        "residual": residual,
        "false_precision": bool(false_precision),
        "false_precision_claims": false_precision,
        "omissions": omissions,
        "success": None
        if residual
        else bool(state and matched and not false_precision and run["status"] == "ok"),
        "diagnostics": {
            "calls": {role: len(s.get("calls") or []) for role, s in sessions.items()},
            "guard_refusals": {role: s.get("guard_refusals") for role, s in sessions.items()},
            "refusal_codes": sorted(
                c["refusal_code"]
                for s in sessions.values()
                for c in s.get("calls") or []
                if c.get("refusal_code")
            ),
            "uncounted_typed_claims": situation.uncounted_typed_claims(),
            "anchor_edge_supported": situation.anchor_edge_supported(),
            "registry_added": [
                {"registry": row.get("registry"), "key": row.get("key")}
                for row in run["end_state"].get("registry_history") or []
            ],
            "edge_review_reasons": reasons,
            "fresh_calls_by_tool": _count(c.get("tool") for c in fresh.get("calls") or []),
        },
        "_answer": answer,
    }


def _count(names: Iterable[str | None]) -> dict[str, int]:
    out: dict[str, int] = {}
    for name in names:
        out[str(name)] = out.get(str(name), 0) + 1
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------
# Statistics (protocol.yaml statistics.interval).
# --------------------------------------------------------------------------


def wilson(x: int, n: int) -> tuple[float, float]:
    p = x / n
    spread = Z * math.sqrt(Z * Z + 4 * n * p * (1 - p))
    return (2 * n * p + Z * Z - spread) / (2 * (n + Z * Z)), (2 * n * p + Z * Z + spread) / (
        2 * (n + Z * Z)
    )


def newcombe_paired(a: int, b: int, c: int, d: int) -> dict:
    """Newcombe (1998) method 10, candidate minus baseline; a both, b candidate only, c baseline only."""

    n = a + b + c + d
    if n == 0:
        return {"n": 0, "difference": None, "lower": None, "upper": None}
    p1, p2 = (a + b) / n, (a + c) / n
    l1, u1 = wilson(a + b, n)
    l2, u2 = wilson(a + c, n)
    product = (a + b) * (c + d) * (a + c) * (b + d)
    phi = (a * d - b * c) / math.sqrt(product) if product else 0.0
    theta = p1 - p2
    delta = math.sqrt(max(0.0, (p1 - l1) ** 2 - 2 * phi * (p1 - l1) * (u2 - p2) + (u2 - p2) ** 2))
    epsilon = math.sqrt(max(0.0, (u1 - p1) ** 2 - 2 * phi * (u1 - p1) * (p2 - l2) + (p2 - l2) ** 2))
    return {"n": n, "difference": theta, "lower": theta - delta, "upper": theta + epsilon}


def _table(rows: Sequence[Mapping], measure: str) -> dict:
    cells = {"a": 0, "b": 0, "c": 0, "d": 0}
    for row in rows:
        cand, base = row["candidate"][measure], row["baseline"][measure]
        cells["a" if cand and base else "b" if cand else "c" if base else "d"] += 1
    return {**cells, **newcombe_paired(cells["a"], cells["b"], cells["c"], cells["d"])}


def tables(rows: Sequence[Mapping]) -> dict:
    """The success and false-precision tables, each with its Newcombe interval."""

    false_precision = _table(rows, "false_precision")
    return {
        "success": _table(rows, "success"),
        "false_precision": false_precision,
        # Situations with false precision in the candidate minus those in the baseline.
        "false_precision_excess": false_precision["b"] - false_precision["c"],
    }


def statistics(rows: Sequence[Mapping]) -> dict:
    """The primary tables and protocol.yaml's decision rule over them."""

    primary = tables(rows)
    lower = primary["success"]["lower"]
    gain = lower is not None and lower > 0
    guard = primary["false_precision_excess"] <= 1
    return {
        **primary,
        "decision_rule": {
            "gain_established": gain,
            "false_precision_guard": guard,
            "merge": gain and guard,
        },
    }


def bootstrap_difference(differences: Sequence[float]) -> dict:
    """The mean paired difference with protocol.yaml's 95% percentile bootstrap over situations."""

    n = len(differences)
    if n == 0:
        return {"n": 0, "difference": None, "lower": None, "upper": None}
    rng = random.Random(BOOTSTRAP_SEED)
    means = sorted(
        sum(rng.choice(differences) for _ in range(n)) / n for _ in range(BOOTSTRAP_RESAMPLES)
    )
    return {
        "n": n,
        "difference": sum(differences) / n,
        # Nearest-rank percentiles of the sorted resample means.
        "lower": means[int(0.025 * BOOTSTRAP_RESAMPLES)],
        "upper": means[int(0.975 * BOOTSTRAP_RESAMPLES) - 1],
    }


#: Diagnostics the scorer does not compute; reported as unavailable, never as zero.
DIAGNOSTICS_UNAVAILABLE = (
    "abandoned_writes",
    "resent_bytes",
    "duplicated_reasons: the readback records reason flags, not reason text",
    "twin_referent_notice",
    "promoted_key_served_before_first_write",
    "fresh_map_profile_page_pulls: reported as calls by tool name only",
)


def diagnostics(rows: Sequence[Mapping]) -> dict:
    """Per-arm counts and paired differences (candidate minus baseline), report only."""

    def paired(value) -> dict:
        pairs = [(value(r["baseline"]), value(r["candidate"])) for r in rows]
        known = [(b, c) for b, c in pairs if b is not None and c is not None]
        return {
            "baseline": sum(b for b, _ in known),
            "candidate": sum(c for _, c in known),
            "unknown": len(pairs) - len(known),
            "paired": bootstrap_difference([c - b for b, c in known]),
        }

    def calls(role: str):
        return lambda arm: arm["diagnostics"]["calls"].get(role)

    return {
        # Every code some row carries; a code no row carries has no entry.
        "omissions": {
            code: paired(lambda arm, code=code: int(code in arm["omissions"]))
            for code in sorted(
                {c for r in rows for arm in ("baseline", "candidate") for c in r[arm]["omissions"]}
            )
        },
        "uncounted_typed_claims": paired(lambda arm: arm["diagnostics"]["uncounted_typed_claims"]),
        "capture_calls": paired(calls("capture")),
        "fresh_calls": paired(calls("fresh")),
        "edge_review_reasons": {
            role: edge_review_counts([r[role]["diagnostics"]["edge_review_reasons"] for r in rows])
            for role in ("baseline", "candidate")
        },
        "unavailable": list(DIAGNOSTICS_UNAVAILABLE),
    }


def edge_review_counts(records: Sequence[Mapping]) -> dict:
    """One arm's generic edges with and without an edge-review reason, from the readback record.

    Unavailable for the arm when any of its records is, never zero: the
    readback reports no count for a writer that cannot record a reason.
    """

    if not records or any(r.get("status") != "available" for r in records):
        return {"status": "unavailable"}
    generic = sum(int(r.get("generic_added_edges") or 0) for r in records)
    reasoned = sum(int(r.get("reasons_recorded") or 0) for r in records)
    return {
        "status": "available",
        "generic_added_edges": generic,
        "with_reason": reasoned,
        "without_reason": generic - reasoned,
    }


# --------------------------------------------------------------------------
# The whole run.
# --------------------------------------------------------------------------


def key_name(situation_id: str) -> str:
    """SCHEMA.md's naming: ``dNN`` has key ``dkNN`` and ``hNN`` has ``kNN``."""

    prefix = {"d": "dk", HELD_OUT_PREFIX: "k"}.get(situation_id[:1])
    if prefix is None:
        raise ScoreRefused(f"situation id {situation_id!r} is neither dNN nor hNN")
    return f"{prefix}{situation_id[1:]}.yaml"


def unknown_fields(value: object, schema: object, where: str = "key") -> list[str]:
    """Each field of ``value`` that ``schema`` does not name, as a dotted path."""

    if schema is ... or not isinstance(value, (dict, list)):
        return []
    if isinstance(schema, list):
        if not isinstance(value, list):
            return [f"{where} is not a list"]
        return [
            p
            for i, item in enumerate(value)
            for p in unknown_fields(item, schema[0], f"{where}[{i}]")
        ]
    if not isinstance(value, dict):
        return [f"{where} is not a mapping"]
    found = []
    for name, item in value.items():
        if name not in schema:
            found.append(f"{where}.{name}")
        else:
            found += unknown_fields(item, schema[name], f"{where}.{name}")
    return found


def _has(value: object, path: str) -> bool:
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            return False
        value = value[part]
    return value is not None


def missing_fields(key: Mapping) -> list[str]:
    """Each field the key's rule, answer views and edge entries need but lack."""

    rule = (key.get("state_success") or {}).get("rule")
    if rule not in RULES:
        return [f"state_success.rule (no implemented rule {rule!r})"]
    variant = key.get("variant")
    if variant not in FALSE_PRECISION_FIELDS:
        return [f"variant (no known variant {variant!r})"]
    needed = [*KEY_REQUIRED, *RULES[rule][1], *FALSE_PRECISION_FIELDS[variant]]
    needed += [f"{view}.{name}" for view in ("answer", "judge_reference_view") for name in _ANSWER]
    missing = [path for path in needed if not _has(key, path)]
    pairs = (key.get("false_precision") or {}).get("pairs") or []
    edges = [
        *(("supported_edges", e) for e in key.get("supported_edges") or []),
        *(("diagnostic_anchor_edge", e) for e in key.get("diagnostic_anchor_edge") or []),
        *(
            ("false_precision.pairs.supported_edges", e)
            for p in pairs
            for e in p.get("supported_edges") or []
        ),
    ]
    if _has(key, "state_success.required_edge"):
        edges.append(("state_success.required_edge", key["state_success"]["required_edge"]))
    missing += [f"{where}.{name}" for where, e in edges for name in _EDGE if not _has(e, name)]
    missing += [
        "false_precision.pairs.between (two references)"
        for p in pairs
        if len(p.get("between") or []) != 2
    ]
    if _has(key, "state_success.pair") and len(key["state_success"]["pair"]) != 2:
        missing.append("state_success.pair (two references)")
    return missing


def held_out(situation_id: str) -> bool:
    return situation_id.startswith(HELD_OUT_PREFIX)


def load_key(keys: Path, situation_id: str, manifest: Path | None) -> dict:
    """The situation's key; ``score`` has already checked a held-out run's custody."""

    path = keys / key_name(situation_id)
    if not path.is_file():
        raise ScoreRefused(f"{path.name} is missing from {keys}")
    if path.resolve().name != path.name:
        # A key name that links to another file could open another situation's key.
        raise ScoreRefused(f"{path.name} links to {path.resolve().name}")
    raw = path.read_bytes()
    if manifest is not None:
        expected = manifest_digests(manifest).get(path.name)
        if expected != hashlib.sha256(raw).hexdigest():
            raise ScoreRefused(f"{path.name} does not match the manifest")
    key = yaml.safe_load(raw)
    if not isinstance(key, dict):
        raise ScoreRefused(f"{path.name} is not a mapping")
    unknown = unknown_fields(key, KEY_SCHEMA)
    missing = missing_fields(key)
    if unknown or missing:
        raise ScoreRefused(f"{path.name}: unknown fields {unknown}, missing fields {missing}")
    if key["id"] != situation_id:
        raise ScoreRefused(f"{path.name} names situation {key['id']!r}, not {situation_id!r}")
    return key


def expected_situations(manifest: Path | None, expect: Sequence[str]) -> set[str]:
    """The situations the run declares: the manifest's held-out cases and any named ones."""

    declared = set(expect)
    if manifest is not None:
        declared |= {
            Path(name).stem
            for name in manifest_digests(manifest)
            if name.startswith(HELD_OUT_PREFIX) and name.endswith(".yaml")
        }
    if not declared:
        raise ScoreRefused(
            "name the run's situations: --expect for development, --manifest held out"
        )
    return declared


def _read_runs(out: Path, situation_id: str, arms: Sequence[str]) -> dict[str, dict]:
    runs = {}
    for arm in arms:
        path = out / situation_id / arm / "run.json"
        if not path.is_file():
            continue
        run = read_json(path)
        if not isinstance(run, dict):
            raise ScoreRefused(f"{path} is not a run file")
        if (run.get("situation_id"), run.get("arm")) != (situation_id, arm):
            raise ScoreRefused(f"{path} holds {run.get('situation_id')!r} {run.get('arm')!r}")
        runs[arm] = run
    return runs


def read_json(path: Path) -> object:
    """The JSON value of one input file; an unreadable file is a :class:`ScoreRefused`."""

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScoreRefused(f"{path} cannot be read as JSON: {error}") from error


def arm_roles(out: Path) -> dict[str, dict]:
    """``role -> binding record``, read from the run root's binding records.

    The role is the one ``runner.bind`` recorded as ``reason_capable``, and the
    bind's rule must hold: only the candidate differs from the base revision.
    """

    from .runner import REASON_CAPABLE

    by_capability = {capable: role for role, capable in REASON_CAPABLE.items()}
    roles: dict[str, dict] = {}
    for path in sorted(out.glob("binding-*.json")):
        binding = read_json(path)
        if not isinstance(binding, dict) or not all(
            isinstance(binding.get(name), str)
            for name in ("arm", "binding_ref", "revision", "base_revision")
        ):
            raise ScoreRefused(f"{path.name} is not a binding record")
        capable = binding.get("reason_capable")
        # A strict bool: 1 and 0 would otherwise look up the True and False roles.
        role = by_capability.get(capable) if isinstance(capable, bool) else None
        if role is None or path.name != f"binding-{binding['arm']}.json":
            raise ScoreRefused(f"{path.name} names no role or another arm")
        if (role == "candidate") == (binding["revision"] == binding["base_revision"]):
            raise ScoreRefused(
                f"{path.name}: a {role} at {binding['revision']} contradicts its base"
            )
        if role in roles:
            raise ScoreRefused(f"the run root holds two {role} bindings")
        roles[role] = binding
    if set(roles) != set(REASON_CAPABLE):
        raise ScoreRefused(
            f"the run root needs one baseline and one candidate binding, has {sorted(roles)}"
        )
    if roles["baseline"]["base_revision"] != roles["candidate"]["base_revision"]:
        raise ScoreRefused("the two bindings name different base revisions")
    return roles


def _check_pair(
    situation_id: str, key: Mapping, runs: Mapping[str, Mapping], out: Path, refs: Mapping[str, str]
) -> None:
    for arm, run in runs.items():
        if run.get("binding_ref") != refs[arm]:
            raise ScoreRefused(f"{situation_id} {arm}: binding_ref is not its arm's binding")
        problems = check_run(run, out / situation_id / arm)
        if problems:
            raise ScoreRefused(
                f"{situation_id} {arm}: the run file departs from SCHEMA.md: {problems}"
            )
        if run["situation_sha256"] != key["case_sha256"]:
            raise ScoreRefused(f"{situation_id} {arm}: the run's case file is not the key's")
        if run["client_shape"] != key["client_shape"]:
            raise ScoreRefused(
                f"{situation_id} {arm}: client shape {run['client_shape']!r} is not the key's"
            )


def _issued(judge_key: Mapping | None) -> dict[tuple[str, str], tuple[str, str]]:
    """``(situation, arm) -> (item id, answer digest)`` from an earlier judge key."""

    if judge_key is None:
        return {}
    fields = ("situation_id", "arm", "answer_sha256")
    if not isinstance(judge_key, dict) or not all(
        isinstance(entry, dict) and all(isinstance(entry.get(f), str) for f in fields)
        for entry in judge_key.values()
    ):
        raise ScoreRefused("the judge key is not a judge-key.json")
    return {
        (e["situation_id"], e["arm"]): (item, e["answer_sha256"]) for item, e in judge_key.items()
    }


def _verdicts(judgements: Mapping | None) -> dict[str, bool]:
    if judgements is None:
        return {}
    if not isinstance(judgements, dict):
        raise ScoreRefused("the judgement file is not a mapping")
    if not str(judgements.get("judge_model") or "").strip():
        raise ScoreRefused("the judgement file records no judge_model")
    verdicts = judgements.get("verdicts")
    if not isinstance(verdicts, dict):
        raise ScoreRefused("the judgement file has no verdicts mapping")
    wrong = sorted(item for item, verdict in verdicts.items() if not isinstance(verdict, bool))
    if wrong:
        raise ScoreRefused(f"verdicts must be JSON booleans; these are not: {wrong}")
    return dict(verdicts)


def score(
    out: Path,
    keys: Path,
    *,
    manifest: Path | None = None,
    expect: Sequence[str] = (),
    judgements: Mapping | None = None,
    judge_key: Mapping | None = None,
) -> dict:
    """Score the declared situations under ``out``; decide only on a complete, judged run.

    ``judge_key`` is the earlier report's unblinding key: with it the residual
    items keep their ids, so the judge's verdicts on them apply.
    """

    from membench.utility.switch_eval.state import StateError, require_default_kb_dirname

    # The servers run with the default KB folder name; so must the scorer's reading.
    try:
        require_default_kb_dirname()
    except StateError as error:
        raise ScoreRefused(str(error)) from error
    verdicts = _verdicts(judgements)
    if verdicts and judge_key is None:
        raise ScoreRefused("verdicts need the judge key that issued their item ids")
    issued = _issued(judge_key)
    declared = expected_situations(manifest, expect)
    if any(held_out(situation_id) for situation_id in declared):
        # Checked once up front, so a run whose every situation is struck still checks it.
        try:
            _refuse_held_out_without_custody("this held-out run", manifest)
        except SituationRefused as refused:
            raise ScoreRefused(str(refused)) from refused
    roles = arm_roles(out)
    baseline, candidate = roles["baseline"]["arm"], roles["candidate"]["arm"]
    refs = {binding["arm"]: binding["binding_ref"] for binding in roles.values()}
    present = {
        p.name
        for p in out.iterdir()
        if p.is_dir() and any((p / a / "run.json").is_file() for a in (baseline, candidate))
    }
    if undeclared := sorted(present - declared):
        raise ScoreRefused(f"the run root holds undeclared situations {undeclared}")
    rows, struck, incomplete, items, unblind = [], {}, [], [], {}
    for situation_id in sorted(declared):
        runs = (
            _read_runs(out, situation_id, (baseline, candidate))
            if (out / situation_id).is_dir()
            else {}
        )
        if len(runs) < 2:
            incomplete.append(situation_id)
            continue
        if any(run["status"] == "struck" for run in runs.values()):
            # A struck situation leaves both arms (section 7, Faults); its key stays closed.
            struck[situation_id] = {
                arm: [f"{f.get('kind')}: {f.get('detail')}" for f in run.get("faults") or []]
                for arm, run in runs.items()
                if run["status"] == "struck"
            }
            continue
        key = load_key(keys, situation_id, manifest)
        _check_pair(situation_id, key, runs, out, refs)
        row = {
            "situation_id": situation_id,
            "category": key["category"],
            "variant": key["variant"],
            "client_shape": key["client_shape"],
        }
        view = key["judge_reference_view"]
        for role, arm in (("baseline", baseline), ("candidate", candidate)):
            arm_row = score_arm(key, runs[arm], out / situation_id / arm)
            answer = arm_row.pop("_answer")
            if arm_row["residual"]:
                digest = hashlib.sha256(answer.encode()).hexdigest()
                item, issued_digest = issued.get((situation_id, arm), (None, None))
                if item is None:
                    if judge_key is not None:
                        raise ScoreRefused(f"the judge key has no item for {situation_id} {arm}")
                    # A random id: the judge cannot recompute it from anything it might know.
                    item = secrets.token_hex(8)
                elif issued_digest != digest:
                    raise ScoreRefused(
                        f"{situation_id} {arm}: the answer changed since its item was issued"
                    )
                unblind[item] = {"situation_id": situation_id, "arm": arm, "answer_sha256": digest}
                items.append(
                    {
                        "id": item,
                        "question": view["question"],
                        "key_fact": view["key_fact"],
                        "accepted_values": list(view["accepted_values"]),
                        "answer": answer,
                    }
                )
                if item in verdicts:
                    passed = verdicts[item]
                    arm_row["judged"] = passed
                    # A residual row already holds the state outcome and status ok.
                    arm_row["success"] = passed and not arm_row["false_precision"]
                    if not passed:
                        arm_row["omissions"].append("answer_lacks_fact")
            row[role] = arm_row
        rows.append(row)
    if stray := sorted(set(verdicts) - {item["id"] for item in items}):
        raise ScoreRefused(f"verdicts name ids that are no residual item: {stray}")
    pending = sorted(i["id"] for i in items if i["id"] not in verdicts)
    report = {
        "rows": rows,
        "arms": {"baseline": baseline, "candidate": candidate},
        "declared": sorted(declared),
        "struck": struck,
        "incomplete": incomplete,
        "judge_model": (judgements or {}).get("judge_model"),
        "judge_items": sorted(items, key=lambda item: item["id"]),
        "judge_key": unblind,
        "pending_judgement": pending,
    }
    if len(struck) > STRIKE_LIMIT:
        report["verdict"] = "void"
        report["statistics"] = f"void: {len(struck)} struck situations; no decision"
    elif incomplete:
        report["verdict"] = "incomplete"
        report["statistics"] = (
            f"incomplete: {incomplete} lack a finished run in an arm; no decision"
        )
    elif pending:
        report["verdict"] = "awaiting_judge"
        report["statistics"] = "unavailable: residual items await the blind judge"
    else:
        report["verdict"] = "decided"
        report["statistics"] = statistics(rows)
        report["descriptive"] = {
            f"{name}:{value}": tables([r for r in rows if r[name] == value])
            for name in ("client_shape", "category")
            for value in sorted({r[name] for r in rows})
        }
        report["diagnostics"] = diagnostics(rows)
    return report


def write_report(report: Mapping, to: Path) -> None:
    """``to/scores.json``, the arm key in ``to/key`` and the judge's items alone in ``to/judge``."""

    for part in ("key", "judge"):
        (to / part).mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in report.items() if k not in ("judge_items", "judge_key")}
    (to / "scores.json").write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    (to / "key" / "judge-key.json").write_text(
        json.dumps(report["judge_key"], indent=2, sort_keys=True), encoding="utf-8"
    )
    (to / "judge" / "judge-items.json").write_text(
        json.dumps(report["judge_items"], indent=2, sort_keys=True), encoding="utf-8"
    )
