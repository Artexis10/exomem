"""Cases and their key, read in the key writer's own line shape.

The key writer writes one JSONL line per case (``case_id``, ``kind``, ``group``,
``scored_turn``, ``turn_completeness``, ``expects_memory``, ``required``,
``forbidden``, ``successors``, ``excluded_refs``, ``struck``). The quoted user
turns live in separate case files: a miss case carries ``case_id`` and
``turns``; an ordinary turn carries ``id`` and ``turn``. This module joins the
two by id and never rewrites either; the private files stay where the operator
keeps them and are read by path at run time.

The synthetic producer at the bottom writes the same two shapes for the
repository's synthetic activation corpus, so one pipeline reads both.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path

#: The tree a case runs on when its key names none: the one vault snapshot.
DEFAULT_TREE = "snapshot"

#: The key writer's case kinds: a closed set its line format fixes.
KIND_MISS = "miss"
KIND_ORDINARY = "ordinary"
#: Synthetic kinds, named apart from the key writer's so a rule can never count
#: a synthetic case as a field case.
SYNTHETIC_POSITIVE = "synthetic_positive"
SYNTHETIC_TWIN = "synthetic_twin"


class CaseError(ValueError):
    """A key or case file that cannot be joined into a case honestly."""


@dataclass(frozen=True)
class Required:
    item: str
    statement: str
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Forbidden:
    item: str
    claim: str
    why: str = ""
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Successor:
    statement: str
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class Case:
    case_id: str
    split: str
    kind: str
    group: str
    tree: str
    turns: tuple[str, ...]
    scored_turn: int
    turn_completeness: str
    expects_memory: bool
    required: tuple[Required, ...] = ()
    forbidden: tuple[Forbidden, ...] = ()
    successors: Mapping[str, tuple[Successor, ...]] = field(default_factory=dict)
    excluded_refs: tuple[str, ...] = ()
    #: Lines a linked session wrote into a page that otherwise stays: ``{ref:
    #: lines}``, from the key's ``removed_lines`` (protocol amendment 1).
    removed_lines: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def no_memory(self) -> bool:
        """A no-memory case feeds the contamination measure (design, "Cases and key")."""

        return not self.expects_memory

    def expected_refs(self) -> frozenset[str]:
        """Every source ref the key cites for required items and their successors."""

        refs = {ref for item in self.required for ref in item.source_refs}
        for successors in self.successors.values():
            refs.update(ref for successor in successors for ref in successor.source_refs)
        return frozenset(refs)

    def as_dict(self) -> dict:
        payload = asdict(self)
        payload["successors"] = {
            key: [asdict(s) for s in value] for key, value in self.successors.items()
        }
        payload["removed_lines"] = {ref: list(lines) for ref, lines in self.removed_lines.items()}
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping) -> Case:
        return cls(
            case_id=str(payload["case_id"]),
            split=str(payload["split"]),
            kind=str(payload["kind"]),
            group=str(payload["group"]),
            tree=str(payload["tree"]),
            turns=tuple(payload["turns"]),
            scored_turn=int(payload["scored_turn"]),
            turn_completeness=str(payload["turn_completeness"]),
            expects_memory=bool(payload["expects_memory"]),
            required=tuple(
                Required(r["item"], r["statement"], tuple(r.get("source_refs") or ()))
                for r in payload["required"]
            ),
            forbidden=tuple(
                Forbidden(
                    f["item"], f["claim"], f.get("why", ""), tuple(f.get("source_refs") or ())
                )
                for f in payload["forbidden"]
            ),
            successors={
                key: tuple(
                    Successor(s["statement"], tuple(s.get("source_refs") or ())) for s in value
                )
                for key, value in (payload.get("successors") or {}).items()
            },
            excluded_refs=tuple(payload.get("excluded_refs") or ()),
            removed_lines=_removed_lines(payload.get("removed_lines")),
        )


def _removed_lines(raw: object) -> dict[str, tuple[str, ...]]:
    """The key writer's list of ``{ref, lines, basis}``, or this module's own map."""

    if isinstance(raw, Mapping):
        return {str(ref): tuple(lines) for ref, lines in raw.items()}
    merged: dict[str, tuple[str, ...]] = {}
    for entry in raw or ():
        ref = str(entry["ref"])
        merged[ref] = merged.get(ref, ()) + tuple(str(line) for line in entry.get("lines") or ())
    return merged


@dataclass(frozen=True)
class StruckCase:
    """A case the key writer struck before freeze; listed, never run."""

    case_id: str
    split: str
    reason: str


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for number, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as error:
            raise CaseError(f"{path}:{number}: not JSON ({error.msg})") from error
        if not isinstance(row, dict):
            raise CaseError(f"{path}:{number}: a line must be one JSON object")
        rows.append(row)
    return rows


def _turns_by_id(case_files: Iterable[Path]) -> dict[str, tuple[str, ...]]:
    turns: dict[str, tuple[str, ...]] = {}
    for path in case_files:
        for row in _read_jsonl(path):
            # The two producer shapes: a miss case quotes several turns, an
            # ordinary turn is one first turn under its sampling id.
            case_id = row.get("case_id", row.get("id"))
            quoted = row.get("turns", [row["turn"]] if "turn" in row else None)
            if not isinstance(case_id, str) or not isinstance(quoted, list) or not quoted:
                raise CaseError(f"{path}: a case line needs an id and at least one quoted turn")
            if case_id in turns:
                raise CaseError(f"{path}: case {case_id} appears in more than one case line")
            turns[case_id] = tuple(str(turn) for turn in quoted)
    return turns


def load_cases(
    *, keys: Sequence[tuple[str, Path]], case_files: Sequence[Path]
) -> tuple[tuple[Case, ...], tuple[StruckCase, ...]]:
    """Join ``(split, key path)`` pairs with the case files that quote the turns.

    A key line without quoted turns, or a scored turn outside them, is refused:
    a case the actor cannot be given is not a case the harness may score.
    """

    turns = _turns_by_id(case_files)
    cases: list[Case] = []
    struck: list[StruckCase] = []
    seen: set[str] = set()
    for split, key_path in keys:
        for row in _read_jsonl(key_path):
            case_id = str(row.get("case_id", ""))
            if not case_id or case_id in seen:
                raise CaseError(f"{key_path}: missing or repeated case_id {case_id!r}")
            seen.add(case_id)
            if row.get("struck"):
                struck.append(StruckCase(case_id, split, str(row["struck"])))
                continue
            if case_id not in turns:
                raise CaseError(f"{key_path}: case {case_id} has no quoted turns in the case files")
            quoted = turns[case_id]
            scored = int(row.get("scored_turn", 0))
            if not 0 <= scored < len(quoted):
                raise CaseError(f"{key_path}: case {case_id} scores turn {scored} of {len(quoted)}")
            payload = {
                **row,
                "split": split,
                "group": row.get("group") or case_id,
                "tree": row.get("tree") or DEFAULT_TREE,
                "turns": list(quoted),
                "scored_turn": scored,
                "turn_completeness": row.get("turn_completeness") or "complete",
                "required": row.get("required") or [],
                "forbidden": row.get("forbidden") or [],
            }
            cases.append(Case.from_dict(payload))
    return tuple(cases), tuple(struck)


def key_structure(key_path: Path) -> list[dict]:
    """Each kept case's kind, group and memory flag, and nothing else.

    The power step reads the held-out key through this reader, so it learns
    the frozen design without a case id, a turn or a keyed item.
    """

    return [
        {
            "kind": str(row["kind"]),
            "group": str(row.get("group") or row["case_id"]),
            "expects_memory": bool(row["expects_memory"]),
        }
        for row in _read_jsonl(key_path)
        if not row.get("struck")
    ]


# --------------------------------------------------------------------------
# The synthetic producer.
# --------------------------------------------------------------------------


def synthetic_tree_name(distractor_count: int) -> str:
    """The corpus tree a synthetic case runs on: one per distractor count the
    fixtures declare."""

    return f"synthetic-{distractor_count}"


def synthetic_inputs(
    out: Path, *, case_ids: Sequence[str] | None = None
) -> tuple[Path, Path, dict[str, Path]]:
    """Build the synthetic vaults and write a key and a case file for them.

    Gold maps to required facts and poison to forbidden claims (design, "Cases
    and key"), each fact phrased by the corpus's own pre-registered phrase and
    cited by the page the corpus build wrote for it. A twin joins its
    positive's group. A twin with empty gold is a no-memory case. A twin with
    a narrow gold of its own keeps that gold as required items and is not a
    no-memory case: its gold is user memory a good answer should use, so
    judging it for contamination would penalise the right answer (an open
    question for the operator, kept as the simpler reading).

    Returns ``(key path, case path, {tree name: vault path})``.
    """

    from epistemic.corpora.context_activation import FIXTURES, build_corpus, fact_for

    out = Path(out)
    selected = [f for f in FIXTURES if case_ids is None or f.case_id in case_ids]
    if case_ids is not None and len(selected) != len(set(case_ids)):
        known = {f.case_id for f in FIXTURES}
        raise CaseError(f"unknown synthetic case ids: {sorted(set(case_ids) - known)}")
    trees: dict[str, Path] = {}
    paths_by_tree: dict[str, dict[str, str]] = {}
    for count in sorted({int(f.distractor_count or 0) for f in selected}):
        name = synthetic_tree_name(count)
        vault = out / "trees" / name / "vault"
        manifest = build_corpus(vault, distractor_count=count)
        trees[name] = vault
        paths_by_tree[name] = dict(manifest.key_to_path)

    key_lines, case_lines = [], []
    for fixture in selected:
        tree = synthetic_tree_name(int(fixture.distractor_count or 0))
        key_to_path = paths_by_tree[tree]

        def refs(key: str, key_to_path: Mapping[str, str] = key_to_path) -> list[str]:
            return [key_to_path[key]] if key in key_to_path else []

        is_twin = fixture.pairs_with is not None
        key_lines.append(
            {
                "case_id": fixture.case_id,
                "kind": SYNTHETIC_TWIN if is_twin else SYNTHETIC_POSITIVE,
                "group": fixture.pairs_with or fixture.case_id,
                "tree": tree,
                "scored_turn": 0,
                "turn_completeness": "complete",
                "expects_memory": bool(fixture.gold),
                "required": [
                    {"item": key, "statement": fact_for(key)[0], "source_refs": refs(key)}
                    for key in fixture.gold
                ],
                "forbidden": [
                    {
                        "item": key,
                        "claim": fact_for(key)[0],
                        "why": "off-target",
                        "source_refs": refs(key),
                    }
                    for key in fixture.poison
                ],
                "successors": {},
                "excluded_refs": [],
                "struck": None,
            }
        )
        # The cold-start turn only, as the existing arm harness plays it.
        case_lines.append({"case_id": fixture.case_id, "turns": [fixture.turn]})
    key_path = out / "key-synthetic.jsonl"
    case_path = out / "cases-synthetic.jsonl"
    key_path.write_text(
        "".join(json.dumps(line, sort_keys=True) + "\n" for line in key_lines), encoding="utf-8"
    )
    case_path.write_text(
        "".join(json.dumps(line, sort_keys=True) + "\n" for line in case_lines), encoding="utf-8"
    )
    return key_path, case_path, trees
