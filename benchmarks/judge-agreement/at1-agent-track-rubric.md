# AT-1 — the agent-judged half of the no-nudge programme

AT-1 is the membench **agent track** (tracks D/E), not part of the pre-registered
deterministic set. It exists because two of the properties the no-nudge audit
cares about are genuinely judgments — whether an intervention was *useful*, and
whether a continuation packet was *sufficient* — and a judgment cannot sit
inside a deterministic family without making that family non-deterministic.

**Deterministic gates are never overturnable by a judge.** If f20 says a signal
did not surface, no AT-1 score changes that. AT-1 measures quality *given* the
deterministic result, and is reported separately.

## Dimensions

| id | Dimension | Question put to the judge |
|---|---|---|
| AT-1a | Intervention usefulness | Given this page's history, was the surfaced proposal one a careful owner would act on? |
| AT-1b | Continuation sufficiency | Could a fresh agent resume the work from this packet alone, without asking the user to re-explain? |

Each is scored 1-5. There is no aggregate across dimensions and no aggregate
with any deterministic family: a single number over a judged and an unjudged
quantity would hide exactly the disagreement worth seeing.

## Protocol

- **Blind.** The judge never sees which provider, variant, or arm produced a
  sample, nor any deterministic result for it.
- **Randomized.** Sample order is shuffled per judging session with a recorded
  seed, so order effects are reproducible rather than merely absent.
- **At least three samples** per dimension per arm. Fewer is reported as
  underpowered and produces no published score.
- **Ties are recorded**, never broken by the judge's preference.

## Judge-agreement gate

AT-1 results are publishable only behind the existing judge-agreement gate: the
judge must first reproduce the calibration set in
`no-nudge-at1-calibration.json` at the agreement threshold the harness already
enforces for other judged tracks. A judge that fails the calibration set is
reported as failing it; its scores are withheld, not down-weighted.

## Relationship to the deterministic families

| Deterministic family | AT-1 dimension it does *not* replace |
|---|---|
| f20/f21 — did a signal surface at all, within budget | AT-1a — was surfacing it a good idea |
| f24/f26 — is the packet complete, delivered, and current | AT-1b — was it enough to actually resume |

Reporting either half without the other is what the amendment's
"no metric without its dual" rule forbids.
