"""The f34 scorer: PREREGISTRATION section 7 sequence 8, with ``protocol.yaml``.

It reads run directories (``OUT/<situation>/<arm>/run.json``) and the key files
of the situations those runs name, and nothing else. It makes no model call.
A residual answer goes to a blind judge as a five-field item; the scorer
never judges one, and a judge never overturns a deterministic result.

The key files carry no published schema. The scorer reads these fields only:
``id``, ``case_sha256``, ``category``, ``variant``, ``endpoints``,
``referent.match_tokens``, ``state_success`` (``rule``, ``pair``, ``folder``,
``folders``, ``required_edge``), ``supported_edges``, ``false_precision``
(``pairs``, ``wrong_core_types``, ``wrong_homes``, ``twin_promoted``),
``promoted_entry.key`` and ``answer`` (``question``, ``key_fact``,
``accepted_values``). It refuses a rule it does not implement.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .runfile import _result_text
from .situation import manifest_digests

#: The reference relations of the protocol's end-state reading: a closed set that
#: the core relation pack fixes, so a code constant is the protocol's own list.
REFERENCE_RELATIONS = frozenset({"links_to", "mentions", "about_entity", "cites"})
#: The key schema's local reference for the referent page; its value is prose.
REFERENT_REF = "R"
#: z for a 95% interval, as protocol.yaml states it.
Z = 1.959963984540054
#: More struck situations than this void the run (section 7, Faults).
STRIKE_LIMIT = 2


class ScoreRefused(RuntimeError):
    """An input the scorer cannot score without guessing."""


# --------------------------------------------------------------------------
# Matching (protocol.yaml normalization and answer_reading).
# --------------------------------------------------------------------------


def normalize(text: str) -> str:
    """NFKC, casefold, every dash character read as ``-``, whitespace collapsed, trimmed."""

    folded = unicodedata.normalize("NFKC", text or "").casefold()
    # A dash character is Unicode general category Pd, the standard's dash class.
    dashed = "".join("-" if unicodedata.category(ch) == "Pd" else ch for ch in folded)
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
        from membench.utility.switch_eval.state import require_default_kb_dirname

        # The servers run with the default KB folder name; so must the scorer's reading.
        require_default_kb_dirname()
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
    """The key's items resolved against one arm's end state."""

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


# Each rule returns ``(state outcome, omission codes)``. The rule names and the
# omission codes are the closed tokens of protocol.yaml's category table.


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
    return False, _pair_omission(s, edges, reference_code=False) or ["promoted_entry_not_used"]


RULES = {
    "pair_edge_supported": _pair_edge_supported,
    "none_required": _none_required,
    "referent_entity_exists_with_fitting_type": _referent_fits,
    "placed_under_home": _placed_under_home,
    "placed_outside_homes": _placed_outside_homes,
    "referent_type_lineage_includes_promoted": _referent_type_promoted,
    "pair_edge_lineage_includes_promoted": _pair_edge_promoted,
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

    rule = key["state_success"]["rule"]
    if rule not in RULES:
        raise ScoreRefused(f"{key['id']}: rule {rule!r} is not implemented")
    situation = Situation(key, EndState.read(run["end_state"]))
    state, omissions = RULES[rule](situation)
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


def statistics(rows: Sequence[Mapping]) -> dict:
    success = _table(rows, "success")
    false_precision = _table(rows, "false_precision")
    # Situations with false precision in the candidate minus those in the baseline.
    fp_excess = false_precision["b"] - false_precision["c"]
    gain = success["lower"] is not None and success["lower"] > 0
    return {
        "success": success,
        "false_precision": false_precision,
        "false_precision_excess": fp_excess,
        "decision_rule": {
            "gain_established": gain,
            "false_precision_guard": fp_excess <= 1,
            "merge": gain and fp_excess <= 1,
        },
    }


# --------------------------------------------------------------------------
# The whole run.
# --------------------------------------------------------------------------


def key_name(situation_id: str) -> str:
    """SCHEMA.md's naming: ``dNN`` has key ``dkNN`` and ``hNN`` has ``kNN``."""

    prefix = {"d": "dk", "h": "k"}.get(situation_id[:1])
    if prefix is None:
        raise ScoreRefused(f"situation id {situation_id!r} is neither dNN nor hNN")
    return f"{prefix}{situation_id[1:]}.yaml"


def load_key(keys: Path, situation_id: str, manifest: Path | None) -> dict:
    path = keys / key_name(situation_id)
    raw = path.read_bytes()
    if manifest is not None:
        expected = manifest_digests(manifest).get(path.name)
        if expected != hashlib.sha256(raw).hexdigest():
            raise ScoreRefused(f"{path.name} does not match the manifest")
    elif situation_id.startswith("h"):
        raise ScoreRefused(f"{path.name} is held out and needs the custody manifest")
    key = yaml.safe_load(raw)
    if key.get("id") != situation_id:
        raise ScoreRefused(f"{path.name} names situation {key.get('id')!r}, not {situation_id!r}")
    return key


def judge_id(situation_id: str, arm: str, answer: str) -> str:
    """An opaque item id: neither the situation nor the arm can be read from it."""

    return hashlib.sha256(f"{situation_id}\0{arm}\0{answer}".encode()).hexdigest()[:16]


def score(
    out: Path,
    keys: Path,
    *,
    baseline: str,
    candidate: str,
    manifest: Path | None = None,
    judgements: Mapping | None = None,
) -> dict:
    """Score every situation under ``out`` that has a run for both arms."""

    verdicts = (judgements or {}).get("verdicts") or {}
    if judgements is not None and not (judgements.get("judge_model") or "").strip():
        raise ScoreRefused("the judgement file records no judge_model")
    rows, struck, incomplete, items, unblind = [], [], [], [], {}
    for situation_dir in sorted(p for p in out.iterdir() if p.is_dir()):
        runs = {}
        for arm in (baseline, candidate):
            path = situation_dir / arm / "run.json"
            if path.is_file():
                runs[arm] = json.loads(path.read_text(encoding="utf-8"))
        if not runs:
            continue
        situation_id = next(iter(runs.values()))["situation_id"]
        if len(runs) < 2:
            incomplete.append(situation_id)
            continue
        if any(run["status"] == "struck" for run in runs.values()):
            # A struck situation leaves both arms (section 7, Faults).
            struck.append(situation_id)
            continue
        key = load_key(keys, situation_id, manifest)
        row = {
            "situation_id": situation_id,
            "category": key["category"],
            "variant": key["variant"],
            "client_shape": next(iter(runs.values()))["client_shape"],
        }
        for role, arm in (("baseline", baseline), ("candidate", candidate)):
            run = runs[arm]
            if run["situation_sha256"] != key["case_sha256"]:
                raise ScoreRefused(f"{situation_id} {arm}: the run's case file is not the key's")
            arm_row = score_arm(key, run, situation_dir / arm)
            answer = arm_row.pop("_answer")
            if arm_row["residual"]:
                item = judge_id(situation_id, arm, answer)
                unblind[item] = {"situation_id": situation_id, "arm": arm}
                items.append(
                    {
                        "id": item,
                        "question": key["answer"]["question"],
                        "key_fact": key["answer"]["key_fact"],
                        "accepted_values": list(key["answer"]["accepted_values"]),
                        "answer": answer,
                    }
                )
                if item in verdicts:
                    passed = bool(verdicts[item])
                    arm_row["judged"] = passed
                    arm_row["success"] = passed and not arm_row["false_precision"]
                    if not passed:
                        arm_row["omissions"].append("answer_lacks_fact")
            row[role] = arm_row
        rows.append(row)
    pending = [i["id"] for i in items if i["id"] not in verdicts]
    report = {
        "rows": rows,
        "struck": struck,
        "incomplete": incomplete,
        "void": len(struck) > STRIKE_LIMIT,
        "judge_items": sorted(items, key=lambda item: item["id"]),
        "judge_key": unblind,
        "pending_judgement": sorted(pending),
    }
    if pending:
        # Success is unknown for these rows, so no table is computed from them.
        report["statistics"] = "unavailable: residual items await the blind judge"
        return report
    report["statistics"] = statistics(rows)
    report["descriptive"] = {
        f"{name}:{value}": statistics([r for r in rows if r[name] == value])
        for name in ("client_shape", "category")
        for value in sorted({r[name] for r in rows})
    }
    return report


def write_report(report: Mapping, to: Path) -> None:
    """The scores, then the judge's view and its unblinding key, each in its own file."""

    to.mkdir(parents=True, exist_ok=True)
    body = {k: v for k, v in report.items() if k not in ("judge_items", "judge_key")}
    (to / "scores.json").write_text(json.dumps(body, indent=2, sort_keys=True), encoding="utf-8")
    (to / "judge-items.json").write_text(
        json.dumps(report["judge_items"], indent=2, sort_keys=True), encoding="utf-8"
    )
    (to / "judge-key.json").write_text(
        json.dumps(report["judge_key"], indent=2, sort_keys=True), encoding="utf-8"
    )
