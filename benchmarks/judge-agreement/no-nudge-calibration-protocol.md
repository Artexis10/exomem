# No-nudge emergence calibration — study protocol

**Status: NOT YET RUN. The constants it exists to freeze are PROVISIONAL.**

This protocol accompanies amendment sequence 2 (`benchmarks/epistemic/contracts/
amendment-2026-08-no-nudge.v1.json`). It defines how the f20, f21 and f25 budget
and window constants in `benchmarks/epistemic/budgets.py` are set — once, by
expert annotation, and never again without a new dated §7 amendment.

The study has not been run. Task 3.5 is blocked on a founder decision about
annotator staffing and about the small-cohort fallback, and until it lands the
constants ship marked `provisional` and the whole amendment is withheld, so no
run, score or claim can be produced against them.

## Why a study rather than a judge

"Before an expert would have to intervene" is a judgment, and a judgment inside
a deterministic family would make the family non-deterministic — a judge could
then overturn a deterministic gate, which the house rules forbid. Calibrating
once and freezing the median converts the judgment into a constant. Live judging
of the intervention point never occurs in a run.

## Annotators

At least **three** expert annotators, each of whom:

- maintains a personal knowledge base of at least 500 durable notes, and
- has performed manual restructuring of an accumulating note in the last year.

Annotators work independently and never see one another's labels before
submission. No annotator may have authored the corpora.

## Materials

The corpora are generated, not hand-written, so the study can be re-run exactly:

| Family | Corpus | Generator |
|---|---|---|
| f20 | accumulating page plus four matched twins | `epistemic.corpora.no_nudge.f20_corpus` |
| f21 | recurrence corpus plus incidental twin | `epistemic.corpora.no_nudge.f21_corpus` |
| f25 | restructure fixture and its children | `epistemic.corpora.no_nudge.f25_corpus` |

Each annotator receives the corpus **as a sequence of writes**, replayed one
unit at a time, with the twins interleaved and unlabelled.

## Task

After each write, the annotator answers one question:

> Would you, as this vault's owner, now want the system to propose restructuring
> this page? (yes / no / unsure)

The **intervention point** is the first write at which an annotator answers yes
and does not later revert to no. An annotator who never answers yes records no
intervention point for that item; that is data, not a missing value.

For f21 the question is recurrence-shaped ("…propose holding this as its own
entity?"), and for f25 it is the quiet-window question ("…would a proposal to
merge these back be welcome?").

## Freezing

1. Take the **median** intervention point across annotators, per corpus.
2. Record raw per-annotator labels in `no-nudge-calibration-labels.json`.
3. Compute pairwise agreement and publish it beside the medians; agreement below
   0.6 is reported, not hidden, and is grounds for the founder to widen the
   cohort rather than to adjust the median.
4. Write the medians into `benchmarks/epistemic/budgets.py`, flip
   `CALIBRATION_STATUS` to `frozen`, and **re-date the §7 entry** with its own
   receipt. Editing the constants without that receipt is the silent retuning
   the amendment exists to prevent.

## Small-cohort fallback (founder decision pending)

If the alpha cohort cannot supply three qualifying annotators, the recorded
fallback is founder-vault-derived planted corpora with published rationale. The
choice between staffing the cohort and taking the fallback is the founder's, is
recorded in the architecture note, and precedes any labelling work.

## False-positive dual

The production false-positive budget — the rate of dismissals carrying the
irrelevant reason — receives its threshold in the same study, and is reported
beside, never instead of, the in-family zero-false-positive ceiling. No
automation metric may be published without its paired false-positive measure
from the same runs.
