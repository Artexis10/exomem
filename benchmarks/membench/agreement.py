"""Judge–human agreement: blind sample construction and kappa computation.

The judge scores semantic dimensions that deterministic gates cannot — chiefly
``semantic_match``: does the candidate answer actually convey the expected
answer? Publishing judged numbers without evidence that the judge agrees with a
human is the softest claim in the whole benchmark, and the first thing a hostile
reviewer should attack. This module builds the evidence.

Two halves, deliberately separated so neither can contaminate the other:

- :func:`build_sample` draws a **balanced, deterministic** sample from a run and
  writes a labelling sheet carrying only the question, the expected answer and
  the candidate answer. It never writes the judge's verdict, the deterministic
  gate verdicts, the family, or the template id — a labeller who can see any of
  those is no longer independent, and the agreement number would be worthless.
- :func:`cohen_kappa` scores the returned labels against the judge.

Balance is over the *deterministic* outcome (did the run's gates pass this
query), not over the judge's own verdict. Stratifying on the judge would bias
the sample toward cases the judge is already confident about, which is exactly
the measurement error this is meant to detect.

The other half of a valid comparison is that both raters grade **the same
input**. Candidate text is therefore passed through the judge's own
:func:`~membench.judge.blinding.normalize_for_judge`, with one shared
``SourceNumbering`` across question/expected/candidate in the same order as
:func:`~membench.judge.backends.make_judge_item`, and is never truncated. Any
divergence there would show up as disagreement and be misread as judge error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from membench.ids import stable_id
from membench.judge.blinding import SourceNumbering, normalize_for_judge
from membench.schema import ExpectedRecord, QueryRecord, load_jsonl


@dataclass(frozen=True)
class SampleItem:
    """One blind labelling row. Deliberately carries no verdict of any kind.

    ``stratum`` is ``"judged"`` when the response has semantic content for the
    judge to assess, and ``"control"`` when it is empty or an abstention. It is
    bookkeeping for the kappa split and is never rendered into the sheet.
    """

    item_id: str
    query_id: str
    question: str
    expected: str
    candidate: str
    stratum: str = "judged"


def _is_contentful(answer_row: dict) -> bool:
    """Does this response contain anything the judge must actually read?

    An abstention or an empty response is decided by looking at it, by either
    rater, in a second. Abstention is additionally covered by `gate_abstention`
    — a deterministic gate, and deterministic gates are final — so the judge's
    semantic verdict there is not load-bearing.
    """

    if answer_row.get("abstained"):
        return False
    return bool((answer_row.get("answer_text") or "").strip())


def _expected_text(record: ExpectedRecord) -> str:
    answer = record.answer
    if answer.kind == "none":
        return "(no answer — the corpus does not record this; abstention is correct)"
    if answer.values:
        return " / ".join(answer.values)
    return f"({answer.kind})"


def _outcome(gate_rows: list[dict]) -> str:
    """Deterministic outcome for stratification: did every applicable gate pass."""

    decided = [g for g in gate_rows if g.get("status") in {"pass", "fail"}]
    if not decided:
        return "undecided"
    return "pass" if all(g["status"] == "pass" for g in decided) else "fail"


def build_sample(
    run_dir: Path, corpus_dir: Path, *, size: int = 50, control_share: float = 0.2
) -> list[SampleItem]:
    """Draw a balanced, deterministic sample of labelling rows from a run.

    Deterministic because the ordering key is :func:`stable_id` over the query
    id — the same run always yields the same sheet, so a disputed agreement
    number can be recomputed rather than argued about.
    """

    run_dir, corpus_dir = Path(run_dir), Path(corpus_dir)
    queries = {q.query_id: q for q in load_jsonl(QueryRecord, corpus_dir / "queries.jsonl")}
    expected = {e.query_id: e for e in load_jsonl(ExpectedRecord, corpus_dir / "expected.jsonl")}
    answers = {
        row["query_id"]: row
        for row in (
            json.loads(line)
            for line in (run_dir / "answers.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        )
    }
    scores = json.loads((run_dir / "deterministic-scores.json").read_text(encoding="utf-8"))

    buckets: dict[str, list[str]] = {"pass": [], "fail": [], "undecided": []}
    for row in scores.get("per_query", []):
        query_id = row.get("query_id")
        if query_id in queries and query_id in expected and query_id in answers:
            buckets[_outcome(row.get("gates", []))].append(query_id)

    for ids in buckets.values():
        ids.sort(key=lambda q: stable_id("JUDGEAGREE", q))

    # Split each outcome bucket by whether there is anything for the judge to
    # *judge*. An empty or abstained response carries no semantic content: both
    # raters decide it instantly and agree trivially, so such rows inflate kappa
    # exactly the way a dominant class does — the inflation this statistic was
    # chosen to avoid. They are also redundant: abstention is already decided by
    # `gate_abstention`, and deterministic gates are final, so the judge's
    # verdict there is not load-bearing.
    #
    # They are not dropped entirely. A small control stratum is kept as a sanity
    # check: agreement on it should be ~1.0, and if it is not, the judge is
    # broken in a gross way we want to see. Kappa is reported per stratum, with
    # the judged stratum as the headline because that is where the judge can
    # actually be wrong.
    judged_share = max(0, size - round(size * control_share))
    strata: dict[str, list[str]] = {"judged": [], "control": []}
    for key in ("pass", "fail", "undecided"):
        for query_id in buckets[key]:
            stratum = "judged" if _is_contentful(answers[query_id]) else "control"
            strata[stratum].append((key, query_id))  # type: ignore[arg-type]

    def _round_robin(pairs: list[tuple[str, str]], limit: int) -> list[str]:
        by_outcome: dict[str, list[str]] = {"pass": [], "fail": [], "undecided": []}
        for outcome, query_id in pairs:
            by_outcome[outcome].append(query_id)
        picked: list[str] = []
        cursors = dict.fromkeys(by_outcome, 0)
        while len(picked) < limit and any(
            cursors[k] < len(by_outcome[k]) for k in by_outcome
        ):
            for key in ("pass", "fail", "undecided"):
                if cursors[key] < len(by_outcome[key]) and len(picked) < limit:
                    picked.append(by_outcome[key][cursors[key]])
                    cursors[key] += 1
        return picked

    judged = _round_robin(strata["judged"], judged_share)  # type: ignore[arg-type]
    control = _round_robin(strata["control"], size - len(judged))  # type: ignore[arg-type]
    stratum_of = {q: "judged" for q in judged} | {q: "control" for q in control}
    ordered = judged + control

    items: list[SampleItem] = []
    for index, query_id in enumerate(ordered, start=1):
        text = (answers[query_id].get("answer_text") or "").strip()
        if answers[query_id].get("abstained"):
            text = f"(the system declined to answer) {text}".strip()

        # The human and the judge must label *the same input*, or the resulting
        # kappa measures this sheet rather than the judge. Two asymmetries were
        # measured on the v0.1 run and are closed here:
        #
        #   - Truncation. An earlier revision cut candidates at 1200 characters
        #     while the judge receives the whole answer; 31 of 140 non-empty
        #     answers exceeded that, so on 22% of items the human would have
        #     graded strictly less text than the judge.
        #   - Blinding. Raw answers carry `[ref:SRC-…]` sentinels, vault paths
        #     and product names — 33 of the first 60 answers tripped
        #     `leakage_scan`. The judge never sees those (they become `[ctx:N]`
        #     and a neutral system token), so an unblinded sheet would hand the
        #     human provider-identifying information the judge is denied.
        #
        # One shared numbering across question/expected/candidate, applied in
        # the same order as `judge.backends.make_judge_item`, so a given source
        # carries the same `[ctx:N]` for both raters.
        numbering = SourceNumbering()
        question = normalize_for_judge(queries[query_id].prompt_text, numbering)
        expected_text = normalize_for_judge(_expected_text(expected[query_id]), numbering)
        candidate = normalize_for_judge(text, numbering) if text else "(empty response)"

        items.append(
            SampleItem(
                item_id=f"J{index:03d}",
                query_id=query_id,
                question=question,
                expected=expected_text,
                candidate=candidate,
                stratum=stratum_of[query_id],
            )
        )
    return items


_LABEL_PLACEHOLDER = "<yes | no | unsure>"
# Tolerates `**Your label:** yes` (colon inside the emphasis), `J001: yes`,
# and `**J004**: `yes``. Humans produce all three.
_LABEL_LINE = re.compile(
    r"^\s*\*{0,2}(?:Your label|(J\d{3}))\*{0,2}\s*[:.]\s*\*{0,2}\s*(.*?)\s*$", re.I
)
_ITEM_HEADING = re.compile(r"^##\s+(J\d{3})\b")
_YES = {"yes", "y", "true", "match"}
_NO = {"no", "n", "false", "mismatch"}
_UNSURE = {"unsure", "?", "skip", "maybe"}


def _render_label(existing: dict[str, bool | None] | None, item_id: str) -> str:
    """An already-entered label, or the blank placeholder."""

    if existing is None or item_id not in existing:
        return _LABEL_PLACEHOLDER
    value = existing[item_id]
    return "unsure" if value is None else ("yes" if value else "no")


def render_answer_form(
    items: list[SampleItem], existing: dict[str, bool | None] | None = None
) -> str:
    """One line per item, for labelling without scrolling the full sheet.

    Equivalent to filling the sheet in place; :func:`parse_labels` reads either.
    """

    lines = [
        "# Judge–human agreement — answer form",
        "",
        "One line per item. Replace each placeholder with `yes`, `no` or",
        "`unsure`. Read the items in the sheet next to this file; the ids match.",
        "",
        "```",
    ]
    lines += [
        f"{item.item_id}: {_render_label(existing, item.item_id)}" for item in items
    ]
    lines += ["```", ""]
    return "\n".join(lines)


def parse_labels(text: str) -> dict[str, bool | None]:
    """Read labels from either the filled sheet or the filled answer form.

    Returns ``{item_id: True | False | None}``; ``None`` is an explicit
    ``unsure``. Unfilled placeholders are omitted entirely rather than guessed —
    a half-finished sheet must not silently become a half-sized sample.
    """

    labels: dict[str, bool | None] = {}
    current: str | None = None
    for line in text.splitlines():
        heading = _ITEM_HEADING.match(line)
        if heading:
            current = heading.group(1)
            continue
        match = _LABEL_LINE.match(line)
        if not match:
            continue
        item_id, raw = match.group(1), match.group(2).strip()
        target = item_id or current
        if target is None or not raw or raw == _LABEL_PLACEHOLDER:
            continue
        word = raw.strip("`*_ ").lower()
        if word in _YES:
            labels[target] = True
        elif word in _NO:
            labels[target] = False
        elif word in _UNSURE:
            labels[target] = None
        else:
            raise ValueError(
                f"{target}: cannot read label {raw!r} — use yes, no, or unsure"
            )
    return labels


def render_sheet(
    items: list[SampleItem], existing: dict[str, bool | None] | None = None
) -> str:
    """Markdown labelling sheet. Contains no verdict a labeller could anchor on.

    ``existing`` re-applies labels already entered by hand, so the sheet can be
    improved and regenerated without destroying work in progress. Hand-entered
    labels are the one input here that cannot be recomputed.
    """

    lines = [
        "# Judge–human agreement — blind labelling sheet",
        "",
        f"{len(items)} items. For each one, answer exactly this question:",
        "",
        "> **Does the candidate response convey the expected answer?**",
        "",
        "## How to fill this in",
        "",
        "Every item ends with a line that looks like this:",
        "",
        "```",
        "**Your label:** <yes | no | unsure>",
        "```",
        "",
        "Replace the whole `<yes | no | unsure>` placeholder with one word.",
        "Nothing else on the line, no quotes, no punctuation. Leave the rest of",
        "the item untouched. Capitalisation does not matter.",
        "",
        "Use `unsure` only when the item is genuinely ambiguous. Unsure rows are",
        "excluded from the agreement statistic and reported separately as a",
        "count — they cost nothing, but they do not help either.",
        "",
        "If you would rather not scroll this file, there is a one-line-per-item",
        "form next to it (`…-answers.md`). Fill in either; both are accepted.",
        "",
        "## Two worked examples",
        "",
        "These two are **examples only** — they are not in the sheet and are not",
        "scored. Real items start at J001.",
        "",
        "### Example A → `yes`",
        "",
        "**Question:** What is the current review cadence for Project Sablereach?",
        "",
        "**Expected answer:** every Tuesday",
        "",
        "**Candidate response:**",
        "",
        "```",
        "# Project Sablereach budget amendment",
        "",
        "Only the budget clause of the Project Sablereach steering decision changes.",
        "The pilot budget for Project Sablereach is now 51000 credits.",
        "Reviews for Project Sablereach stay on every Tuesday, unchanged.",
        "",
        "[ctx:1]",
        "```",
        "",
        "**Your label:** yes",
        "",
        "Why: the text states the cadence, *every Tuesday*, even though it is a",
        "raw note about the budget and never phrases it as an answer. Content is",
        "what counts, not presentation.",
        "",
        "### Example B → `no`",
        "",
        "**Question:** What is the current delivery deadline for Project Quarrypoint?",
        "",
        "**Expected answer:** 2025-03-28",
        "",
        "**Candidate response:**",
        "",
        "```",
        "# Project Quarrypoint kickoff brief",
        "",
        "Arden Labs approved the kickoff of Project Quarrypoint.",
        "The delivery deadline for Project Quarrypoint is 2025-03-14.",
        "",
        "[ctx:1]",
        "```",
        "",
        "**Your label:** no",
        "",
        "Why: it looks authoritative and is clearly about the right project, but",
        "the date is the superseded one. A confident wrong answer is still `no`.",
        "",
        "## What to resist",
        "",
        "- Do **not** reward a fluent, well-structured response that never states",
        "  the fact.",
        "- Do **not** punish an ugly document dump that does state it.",
        "- Do **not** try to guess what the automated judge would say. The point",
        "  is to find out where it disagrees with you.",
        "",
        "The judge's verdict and every deterministic gate result are deliberately",
        "absent from this file. If you find you can infer them anyway, say so —",
        "that is itself a finding about the sheet.",
        "",
        "## About `[ctx:1]`, `[ctx:2]`, … — you can ignore them",
        "",
        "**Each `[ctx:N]` is one source document.** Same number = same document,",
        "different numbers = different documents. Numbering restarts on every item,",
        "so `[ctx:1]` in J001 has nothing to do with `[ctx:1]` in J002.",
        "",
        "A single response is often **several whole documents stacked together**,",
        "each ending with its own marker. For instance one response might be:",
        "",
        "```",
        "title: Project Sablereach budget amendment      →  [ctx:1]",
        "title: Project Sablereach steering decision     →  [ctx:2]",
        "title: Project Cindergate hosting decision      →  [ctx:3]",
        "```",
        "",
        "That is three separate notes returned at once — and the third is about a",
        "different project entirely. **That is normal and is not your problem.**",
        "Whether a system cites the right sources is scored separately, by an",
        "automatic check. Off-topic extra documents do not make an item a `no`.",
        "",
        "Your question stays the same: **is the expected answer stated anywhere in",
        "this text?** If yes, `yes` — even if it is buried in the third document",
        "under two irrelevant ones.",
        "",
        "(The markers are anonymised on purpose. The real references name the",
        "product that produced them, which would tell you which system you are",
        "grading.)",
        "",
        "## A note on length",
        "",
        "Most items have real content to read. A handful are empty or an explicit",
        "refusal to answer — those take a second, and they are deliberately kept",
        "few, because rows that decide themselves tell us nothing about the judge.",
        "",
        "---",
        "",
    ]
    for position, item in enumerate(items, start=1):
        lines += [
            f"## {item.item_id}  ({position} of {len(items)})",
            "",
            f"**Question:** {item.question}",
            "",
            f"**Expected answer:** {item.expected}",
            "",
            "**Candidate response:**",
            "",
            "```",
            item.candidate,
            "```",
            "",
            f"**Your label:** {_render_label(existing, item.item_id)}",
            "",
            "---",
            "",
        ]
    return "\n".join(lines)


def cohen_kappa(pairs: list[tuple[bool, bool]]) -> float:
    """Cohen's kappa for two binary raters.

    Reported instead of raw agreement because raw agreement is inflated when one
    label dominates — and on a benchmark where most answers are wrong in the
    lexical profile, it would be.
    """

    total = len(pairs)
    if total == 0:
        raise ValueError("no labelled pairs")
    return _kappa(pairs)


def _kappa(pairs: list[tuple[bool, bool]]) -> float:
    total = len(pairs)
    observed = sum(1 for a, b in pairs if a == b) / total
    a_true = sum(1 for a, _ in pairs if a) / total
    b_true = sum(1 for _, b in pairs if b) / total
    expected = a_true * b_true + (1 - a_true) * (1 - b_true)
    if expected == 1.0:
        return 1.0 if observed == 1.0 else 0.0
    return (observed - expected) / (1 - expected)


def agreement_report(
    items: list[SampleItem],
    human: dict[str, bool | None],
    judge: dict[str, bool],
) -> dict:
    """Kappa per stratum plus the counts needed to read it honestly.

    The **judged** figure is the headline: it covers the rows where the response
    had semantic content, which is the only place the judge's verdict is
    load-bearing. The **control** figure covers empty and abstained responses,
    where both raters decide by inspection; it should sit at or near 1.0 and is
    a sanity check, not a result. A combined figure is reported too, and is the
    one to distrust — it is the strata mixed together, so it drifts toward
    whichever stratum happens to be larger.

    ``unsure`` rows are excluded from every kappa and counted separately: a
    labeller declining to decide is information, not a vote.
    """

    by_id = {item.item_id: item for item in items}
    strata: dict[str, list[tuple[bool, bool]]] = {"judged": [], "control": []}
    unsure = 0
    missing: list[str] = []

    for item_id, human_label in human.items():
        item = by_id.get(item_id)
        if item is None:
            raise ValueError(f"{item_id} is not in this sample")
        if human_label is None:
            unsure += 1
            continue
        if item_id not in judge:
            missing.append(item_id)
            continue
        strata[item.stratum].append((human_label, judge[item_id]))

    report: dict = {
        "labelled": len(human),
        "unsure": unsure,
        "unjudged": missing,
        "strata": {},
    }
    combined: list[tuple[bool, bool]] = []
    for name, pairs in strata.items():
        combined += pairs
        report["strata"][name] = {
            "n": len(pairs),
            "kappa": _kappa(pairs) if pairs else None,
            "raw_agreement": (
                sum(1 for a, b in pairs if a == b) / len(pairs) if pairs else None
            ),
        }
    report["combined"] = {
        "n": len(combined),
        "kappa": _kappa(combined) if combined else None,
    }
    return report
