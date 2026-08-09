"""Does the LLM judge's semantic_match add anything over the deterministic gates?

Screens task 4b.19. Compares, over the full seed-1 lexical run:

  judge.semantic_match   vs   gate_value      (is the expected string present)
  judge.semantic_match   vs   gate_state      (current-state / as-of correctness)

The interesting cut is the subset whose response carries a *superseded* value:
there, "is the expected answer conveyed" is true by presence while the answer is
also asserting something retired. A judge reading for presence should say yes; a
scorer reasoning about time says no.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, "benchmarks")

from membench.agreement import _kappa  # noqa: E402
from membench.native import load_corpus_view  # noqa: E402
from membench.schema import ExpectedRecord, load_jsonl  # noqa: E402

SCRATCH = Path(
    "/tmp/claude-1000/-home-hugoa-projects-exomem/"
    "97233617-39a5-4c51-93cc-900b7b0e90f1/scratchpad"
)
CORPUS = Path("benchmarks/corpus/generated/s1")
RUN = Path("benchmarks/runs/20260801T115138Z-exomem-local-postfix-lexical-v2-30586b")


def load_judge() -> dict[str, bool]:
    verdicts: dict[str, bool] = {}
    errors = 0
    for k in range(1, 7):
        path = SCRATCH / f"judge_out_{k}.jsonl"
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            if "error" in row or "semantic_match" not in row:
                errors += 1
                continue
            verdicts[row["query_id"]] = bool(row["semantic_match"])
    print(f"judge verdicts: {len(verdicts)}  (malformed/errored rows: {errors})")
    return verdicts


def gate_status(scores: dict, gate_names: set[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in scores["per_query"]:
        for gate in row.get("gates", []):
            if gate["gate"] in gate_names:
                out[row["query_id"]] = gate["status"]
    return out


def agreement(pairs: list[tuple[bool, bool]], label: str) -> None:
    if not pairs:
        print(f"{label}: no comparable rows")
        return
    same = sum(1 for a, b in pairs if a == b)
    print(
        f"{label}: n={len(pairs)}  raw={same / len(pairs):.3f}  "
        f"kappa={_kappa(pairs):+.3f}"
    )


def main() -> None:
    judge = load_judge()
    scores = json.loads((RUN / "deterministic-scores.json").read_text(encoding="utf-8"))
    value = gate_status(scores, {"value"})
    state = gate_status(scores, {"current_state", "as_of"})

    view = load_corpus_view(CORPUS)
    claims = {c.claim_id: c for c in view.claims}
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, CORPUS / "expected.jsonl")}
    answers = {
        json.loads(line)["query_id"]: json.loads(line)
        for line in (RUN / "answers.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }

    print()
    print("=" * 66)
    print("1. Does semantic_match reproduce gate_value?")
    print("=" * 66)
    pairs = [
        (judge[q], value[q] == "pass")
        for q in judge
        if q in value and value[q] in {"pass", "fail"}
    ]
    agreement(pairs, "  judge vs gate_value")
    disagree = [
        q
        for q in judge
        if q in value and value[q] in {"pass", "fail"} and judge[q] != (value[q] == "pass")
    ]
    print(f"  disagreements: {len(disagree)}")
    jt_gf = sum(1 for q in disagree if judge[q])
    print(f"    judge=true  gate=fail : {jt_gf}   (judge more generous)")
    print(f"    judge=false gate=pass : {len(disagree) - jt_gf}   (judge stricter)")

    print()
    print("=" * 66)
    print("2. The discriminating cut: responses carrying a SUPERSEDED value")
    print("=" * 66)
    stale: list[str] = []
    for qid, exp in expected.items():
        row = answers.get(qid)
        if row is None or qid not in judge:
            continue
        text = row.get("answer_text") or ""
        forb = [
            claims[c].object.value
            for c in exp.forbidden_claims
            if c in claims and claims[c].object.value
        ]
        if forb and any(f in text for f in forb):
            stale.append(qid)
    print(f"  rows whose response contains a retired value: {len(stale)}")
    if stale:
        jt = sum(1 for q in stale if judge[q])
        print(f"    judge said MATCH on {jt} of {len(stale)}")
        gs = [q for q in stale if q in state and state[q] in {"pass", "fail"}]
        gsf = sum(1 for q in gs if state[q] == "fail")
        print(f"    state gate FAILED  {gsf} of {len(gs)} comparable")
        both = [q for q in stale if q in state and state[q] == "fail" and judge[q]]
        print(f"    judge MATCH while state gate FAILED: {len(both)}")
        if both:
            print("      -> the judge is passing answers the benchmark scores as wrong")

    print()
    print("=" * 66)
    print("3. Would dropping the judge lose information?")
    print("=" * 66)
    only_judge = [q for q in judge if q not in value or value[q] == "not_applicable"]
    print(f"  rows where gate_value is n/a but judge ruled: {len(only_judge)}")
    print(f"    of those, judge said match: {sum(1 for q in only_judge if judge[q])}")


if __name__ == "__main__":
    main()
