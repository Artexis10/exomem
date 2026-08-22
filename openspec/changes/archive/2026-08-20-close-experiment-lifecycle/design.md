## Context

`audit.py` holds a flat registry of read-only checks. `ALL_CATEGORIES` runs by default; `OPTIONAL_CATEGORIES` runs only when named. Each check is a pure function over already-parsed `ParsedPage` objects that returns `AuditFinding`s carrying a `path`, a `severity`, a one-line `detail`, a `proposed_fix`, and a `meta` dict. `attention.py` fuses selected audit queues into one ranked review surface with Reciprocal Rank Fusion, dedups by anchor path, and binds each item to durable review state through `review_state.fingerprint(...)`, which hashes the item's reasons — `meta` included. A check therefore participates in review state simply by emitting a stable `signal_version` in `meta`.

The `experiment` page type is fully authorable. `note.py` validates `domain`, `started` (strict ISO), and `duration` (free text, e.g. `"30 days"`, `"2 weeks"`, `"ongoing"`), writes `n` and optional `hypothesis`, and — since the epistemic loop primitives change — accepts `status: concluded` and a categorical `outcome:` drawn from `semantic_units.EPISTEMIC_OUTCOMES`. `audit_fix.py` already parses `started` + `concluded` to backfill a missing `duration`, so the codebase already treats `duration` as a span in whole days.

What is missing is the terminal question. No check reads `started` against `duration`. `concluded:` is never inspected by audit. The `experiment` type appears in `audit.py` only in the required-frontmatter table and in `_RELATION_DEBT_TYPES`.

Two artifacts assert otherwise. The `stale_review` scope comment says time-bounded records "have lifecycle checks", and the shipped scaffold's `audit-checks.md` advertises an "Unfinished experiments" check to every new user. Both statements are false today, and the scaffold one is user-facing.

## Goals / Non-Goals

**Goals:**

- Give the `experiment` lifecycle a terminal check, so an experiment whose declared window has closed without a recorded result becomes visible instead of dormant.
- Make the shipped documentation true. The predicate a user reads must be the predicate the code runs.
- Make the `stale_review` exclusion comment cite a check that exists, and stop it from implying one that does not.
- Keep the check a pure measurement over frontmatter the vault already records — no new sidecar, no inference, no model.
- Keep the upgrade path safe for a grandfathered corpus: an existing vault with dozens of long-dormant experiments must not have its daily review surface replaced by them.

**Non-Goals:**

- No auto-conclusion. Nothing writes `status: concluded`, nothing infers an `outcome`, nothing archives an experiment. The check surfaces; the reader decides.
- No production-log lifecycle check. The scaffold advertises one of those too, and it also does not exist; closing that is separate work, named below.
- No new MCP tool, tool argument, or tool-docstring edit. The pinned tool surface must not move.
- No ranking effect. `find` ordering is unchanged, and the new category contributes nothing to default retrieval.
- No change to the `experiment` frontmatter contract or to `note.py`'s enums.

## Decisions

### The predicate is `started` + elapsed-past-`duration` + no `outcome`, not `status: active`

The scaffold's advertised predicate keys on `status: active`. That is the wrong trigger, and implementing it verbatim would ship a check that misses its most important case.

`status` and `outcome` answer different questions. `status: concluded` says the experiment stopped; `outcome:` says what it showed. An experiment marked `concluded` with no `outcome` recorded is the *purest* instance of an unfinished experiment — someone closed the loop administratively without writing down the result. Keying on `status: active` would let exactly that case pass silently.

So the implemented predicate is: `started` parses to a date, `duration` parses to a finite span, `today - started > duration`, and no non-empty `outcome:` is recorded. The scaffold entry is rewritten to state this, which is the documentation half of the fix.

`status` still participates, but as scope rather than trigger: `archived`, `superseded`, and `draft` experiments are excluded, matching every other measurement queue in `audit.py`. An archived experiment is deliberately out of rotation and a draft never started.

### An unparseable or open-ended `duration` is never overdue

`duration` is free text by contract, and `"ongoing"` is a documented, legitimate value. The check parses a leading count with a `day`/`week`/`month`/`year` unit (and a bare integer as days) and treats everything else — `"ongoing"`, `"until it stops helping"`, an empty value — as **no finite window**, which cannot be exceeded.

The rejected alternative was to flag an unparseable duration as its own finding. That converts a schema question into an epistemic one and puts noise in a review queue whose value depends entirely on every item being real work. An experiment that declares it has no deadline has not missed one. If unparseable durations are worth surfacing, `frontmatter_compliance` is the check that owns field-shape questions, not this one.

### Registered as an attention category, deliberately not a default one

`unfinished_experiments` joins `audit.ALL_CATEGORIES` — a default `audit()` run reports it, which is precisely the contract the scaffold already promised — but it is added to `attention.ATTENTION_CATEGORIES` **without** joining `DEFAULT_ATTENTION_CATEGORIES`.

This follows the activation-manifest precedent: a new review category surfaces grandfathered items as review candidates, never as blocking findings, and never by displacing the surface a user already relies on. A vault that has been running for two years may hold dozens of experiments whose windows closed long ago. Dropping all of them into the default daily queue on upgrade would not be a feature; it would be an eviction of the queue's existing signal.

It also keeps this change honest about the spec it touches. The attention-queue capability pins the default union and its exact tiebreak order as a normative requirement with its own scenarios. Leaving the default untouched means this change **adds** a requirement rather than rewriting a load-bearing one, and it means the byte-for-byte-revert property is real: any category selection that omits `unfinished_experiments` reproduces prior behaviour exactly.

Promoting the category into the default union later is a deliberate, separately-argued change, and it should be argued with evidence from real vaults about queue volume.

### The gate is backlog profile, so the sibling prediction queue is default and this one is not

`add-prediction-window-review` lands the same shape of lifecycle check on the same branch and **does** join the default union. That asymmetry is the decision, not an inconsistency, and it is worth stating plainly because a reader comparing the two changes will otherwise read one of them as an oversight.

The test is whether a grandfathered population can exist:

| | `unfinished_experiments` | `prediction_window` |
|---|---|---|
| Fields read | `started`, `duration`, `outcome` | `check_by`, `verdict` |
| Fields introduced | before the package rename (`9f30990e`, 2026-07-02) and earlier | epistemic loop primitives (`74d74578`, 2026-08-15) |
| Possible backlog on upgrade | dozens of long-closed windows in an established vault | structurally none — the field is a day old |
| Default union | **no** | **yes** |

The activation-manifest precedent exists to stop a pre-existing population from arriving on the daily surface all at once. Where such a population is possible, as here, opt-in is the protection. Where it is structurally impossible, the precedent has nothing to protect and opt-in only hides a queue from the people it was built for.

So this check waits. The right way to promote it is the way its sibling earned its place: an argument about this specific queue's volume in real vaults, not a general appeal to symmetry between the two.

`audit.EPISTEMIC_REVIEW_CATEGORIES` — the opt-in tuple this category belongs to — carries this reasoning in a comment at the definition, so the split is legible from the code without reading either proposal.

### Severity is `info`, and the fix text defers every decision

`info` is the severity every measurement-only queue in this module uses (`stale_review`, `relation_debt`, `unprocessed_source` when fresh). An overdue experiment is not a defect: the honest reading may be "extend the duration", "this quietly became ongoing", or "write it up". The `proposed_fix` names all three and auto-applies none, in the same register as the surrounding queues.

### Ordering is oldest-first by elapsed age

The queue sorts by elapsed days since `started`, descending, with the vault-relative path as a deterministic tiebreak. Age is the right ordering signal because the cost of an unrecorded result compounds with time — the details needed to write it up decay. `overdue_days` (elapsed minus the declared window) is carried in `meta` for a reader who wants to sort differently, but it is not the primary key: a 30-day experiment 300 days late and a 300-day experiment 30 days late are not equally urgent, and the older one is the one whose context is more nearly gone.

### `note.py` needs no change

This change was scoped expecting to add a terminal state to `STATUS_EXPERIMENT`. It already has one: commit `74d74578` added `concluded`, and `EXPERIMENT_OUTCOME_VALUES` is already `semantic_units.EPISTEMIC_OUTCOMES`. The enum work is done; only the check that reads it was missing. Recorded here because a reader of the audit report that motivated this change would otherwise expect an enum diff and find none.

## Risks / Trade-offs

**A grandfathered vault sees a new `info` category in default `audit()` output.** Accepted, and it is the point: the scaffold has been promising this check to users since the skill shipped. The blast radius is bounded to `audit()`, which is a lint report a human reads, not a gate. `attention()` — the daily surface — is unchanged.

**The documented predicate changes under users who read the old scaffold text.** A user who internalised "flags `status: active` experiments" will now also see `concluded`-without-`outcome` experiments. This is a widening, not a narrowing, and the scaffold is corrected in the same change so the shipped documentation never lags the behaviour by even one release.

**`duration` parsing is a small heuristic and heuristics rot.** Mitigated by failing *closed* in the safe direction: anything unrecognised means "no window", which means "never flagged". A parser bug therefore produces silence, not false accusations. `"month"` is normalised to 30 days and `"year"` to 365, which is imprecise for a multi-year experiment; the imprecision is bounded by the fact that the check only fires once elapsed time already exceeds the window, so a few days of unit slop shifts *when* an item appears, never *whether* a genuinely-overdue one does.

**The scaffold still advertises a "Unfinished production lifecycles" check that does not exist.** Not fixed here, deliberately. It is a second missing check with its own predicate (`status: recorded` or earlier, `published` null for >60 days) and its own scope questions, and folding it in would make this change two changes wearing one coat. The `stale_review` comment is corrected to say plainly that the production-log exclusion currently has *no* backing check, so the code no longer asserts something false while the gap is open.

### Named follow-ups

1. **`close-production-log-lifecycle`** — implement the second check the scaffold advertises, or delete the claim. Leaving a user-facing document describing a check that does not run is the same defect this change exists to fix; it is filed rather than fixed only to keep the changes separable.
2. **Tool-docstring refresh** — `review_memory`'s pinned `mode` description enumerates its modes and is already stale on `origin/main`: commit `14b524a5` added `mode="plan-progress"` without updating the docstring, so the shipped tool description omits a mode the tool serves. `op_attention`'s docstring likewise still says "the four measurement-only epistemic queues" after `bridge_review` made it five. Both are deliberately **not** fixed here: editing any MCP tool docstring regenerates `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json`, and a moved ChatGPT plugin fingerprint is release-blocking. These should be swept together in one docstring-refresh change that pays the fingerprint cost once, deliberately, with the plugin refresh planned as part of it.
