## Context

A rich semantic unit is an ATX heading whose label resolves to a governed kind, followed by leading `- key: value` metadata rows and a body. Seven metadata keys are reserved: `category`, `id`, `tags`, `context`, `relations`, `verdict`, and `check_by`. `verdict` is a closed categorical enum over `semantic_units.EPISTEMIC_OUTCOMES`; `check_by` is a strict ISO calendar date. Both are rich-form only. `parse_semantic_units` normalises them onto `SemanticUnit.verdict` and `SemanticUnit.check_by` and binds every unit a `fingerprint` over its authored state, so a fingerprint moves exactly when the authored unit moves.

A unit's `- relations:` rows carry `kind: target` entries. The kind is resolved against the relation registry, whose core vocabulary includes `supports`, `contradicts`, `resolves`, and `evidenced_by`. Resolution is needed rather than raw string matching, because a registered alias normalises to a different canonical key than the label the author typed.

`structured_filters` already types `unit.check_by` as a date, so `find` can answer "which predictions are due" when asked. Nothing asks.

`audit.py` holds the read-only check registry; `attention.py` fuses selected audit queues into one ranked surface, dedups by anchor path — partitioned by `meta.review_partition` where a finding supplies one — and binds durable review state through a fingerprint over the item's reasons. `bridge_review` established the partition mechanism so one page can carry several independent review items.

## Goals / Non-Goals

**Goals:**

- Close the epistemic loop's last step: make an authored `check_by` produce review work when its window closes.
- Keep the check a pure measurement over authored Markdown — no model, no inference about whether the prediction *came true*, only whether anyone has said so.
- Make each due prediction its own review item with its own review state, so a decision about one prediction never disposes of another.
- Make an edit to a prediction resurface it honestly, rather than letting a dismissal recorded against different text carry over.
- Put a due prediction on the daily review surface by default, because a queue that only answers when explicitly asked has not removed the remembering it exists to remove.

**Non-Goals:**

- No judgment of the prediction. The check never decides whether a prediction was right, and never writes a `verdict`.
- No inbound-edge resolution in this change. See the decision below — this is deferred deliberately, with a named successor, not overlooked.
- No new MCP tool, tool argument, or tool-docstring edit. The pinned tool surface must not move.
- No new relation kind, unit kind, page type, sidecar, or index.
- No ranking effect. `find` ordering is unchanged.
- No expiry or decay. A due prediction stays due until someone records something; nothing ages it out.

## Decisions

### The resolution predicate is unit-local, and that is forced by the graph today

A prediction counts as unresolved when the unit itself carries neither a `verdict` nor an outbound relation in `{supports, contradicts, resolves, evidenced_by}`. Evidence living elsewhere — a later note that `contradicts [[…#unit-abc]]`, say — does **not** clear it.

This is the conservative direction, and it is chosen rather than merely tolerated, for a concrete reason: **inbound fragment edges are not addressable in the graph today.** `epistemic_graph.py` strips the `#fragment` off a relation target before resolving it, in each of the three places a target is normalised. An edge authored against `[[Some/Note#unit-abc]]` therefore lands on `Some/Note`, not on unit `abc`. Building the predicate on inbound edges right now would mean either accepting page-level granularity — where *any* inbound `contradicts` on the parent page silently clears *every* due prediction on it, including ones nobody looked at — or shipping a second, private, fragment-aware traversal that would immediately disagree with the graph everyone else queries. Both are worse than a bounded false positive.

Restoring fragment-target addressing is real work with its own blast radius across edge construction, traversal, and the neighbour surfaces, and it is filed separately. When it lands, widening this predicate to honour an inbound resolving edge is a small, well-scoped follow-up to this check — and it can only ever *remove* findings, never add them, so it cannot destabilise a queue users have started trusting.

The cost is stated plainly: a prediction resolved only by an inbound edge from another note keeps surfacing. The reader's recourse is the ordinary one — record the `verdict` on the prediction itself, which is where the loop's own documentation says the judgment belongs, or dismiss the item. Both are one action, and the first is the action the epistemic loop wants anyway.

### The trigger is `check_by`, not the `prediction` kind

Any rich unit may carry `check_by`; the parser reserves it for every governed kind, not for `prediction` alone. An author who writes `## Hypothesis` with `- check_by: 2026-08-01` has authored a due date and means it.

Keying the check on `kind == "prediction"` would silently ignore that, and would do so in the most confusing possible way: the field would validate, filter, and serialise correctly while producing no review work, which is precisely the failure this change exists to fix, relocated one field over.

So the predicate keys on the authored obligation — a due `check_by` — and carries `kind` in `meta` for a reader who wants to filter. The category keeps the name `prediction_window` because it names the epistemic act (a prediction window closing), and `check_by` is the governed way to author one whatever kind of unit carries it.

### The unit fingerprint is both the signal version and the review partition

`meta.signal_version` is the unit's fingerprint, so the review-state fingerprint that `attention` computes moves exactly when the authored prediction moves. Editing the prediction's text, its date, or its relations resurfaces the item rather than silently inheriting a dismissal that was recorded against different words. That is the honest behaviour: a dismissal means "I looked at *this* and decided nothing is needed", and it should not survive the thing it was about changing.

`meta.review_partition` is the same fingerprint, which makes each due prediction its own item under the shared parent path rather than collapsing a page's predictions into one. Without it, a page holding three due predictions would produce one review item, and dismissing it would dispose of all three — including two the reader never saw. The mechanism is not new: `bridge_review` already partitions one path into several items by audience, and `attention._apply_review_state` already folds a single partition into the item identity.

The trade-off is that a partition keyed on the fingerprint changes whenever the unit changes, so review state does not survive an edit. That is the intended semantics here, and it is the same trade-off the fingerprint itself already makes.

### It joins the default union, because there is no backlog for opt-in to protect

`prediction_window` joins both `audit.ALL_CATEGORIES` and `attention.DEFAULT_ATTENTION_CATEGORIES`.

The activation-manifest precedent says a new review category must surface grandfathered items as review candidates, never as blocking findings, and never by displacing a surface users already rely on. The operative word is *grandfathered*. That precedent protects against a population of pre-existing items arriving all at once; where no such population can exist, it has nothing to protect and opt-in is pure cost.

`check_by` and the `prediction` kind shipped with the epistemic loop primitives (`74d74578`, 2026-08-15) — one day before this change. It is not merely unlikely that a vault holds a backlog of due predictions; it is structurally impossible, because the field needed to author one has existed for about a day. Every `check_by` this queue will ever surface was authored after the queue was designed.

Its sibling `close-experiment-lifecycle` is the opposite case and stays opt-in for exactly that reason: `started` and `duration` predate the package rename (`9f30990e`, 2026-07-02) and earlier still, so an established vault can hold dozens of long-closed windows. Same mechanism, opposite grandfathered population, opposite default. `audit.EPISTEMIC_REVIEW_CATEGORIES` carries the opt-in one and says so in a comment, so the split reads as a decision rather than an inconsistency.

Opt-in here would also cost the change its entire point. `attention` is the daily front door; `check_by` exists to answer "what is due". A due prediction that surfaces only when the reader already thought to ask about predictions has not closed the loop — it has moved the remembering one step earlier, which is precisely the work the queue was supposed to remove.

The spec cost is real and paid deliberately. The attention-queue capability pins the default union and its tiebreak order normatively, so this change **modifies** two existing requirements — the union requirement and the RRF tiebreak requirement — reproducing both verbatim before editing. `close-experiment-lifecycle` touches neither, so only one change carries a delta against them and the two remain independently reviewable.

### Second in the tiebreak order: authored commitments outrank inferred signals

The default order becomes `bridge_review`, `prediction_window`, `corpus_contradictions`, `stale_review`, `unprocessed_source`, `relation_debt`.

The organising principle, previously implicit and now stated in the spec, is that the order runs from what a human explicitly *wrote down* to what the system *inferred*. `bridge_review` fires on a governance review date someone committed to. `prediction_window` fires on an epistemic check date someone committed to. Everything below is inference: `corpus_contradictions` is a cosine band that by its own documentation cannot tell agreement from contradiction, `stale_review` is an age-and-degree heuristic, `unprocessed_source` is an empty-field scan, `relation_debt` is a missing-edge scan.

Placing an authored deadline behind a proximity measurement would rank a guess above a promise. It sits after `bridge_review` rather than before it because a bridge review carries an external commitment to another audience, where a check date is a commitment to oneself; when both tie, the one with someone else waiting on it goes first.

This is a tiebreak only. It decides nothing about which items are surfaced and has no effect on `find`.

### Ordering is most-overdue-first

The queue sorts by days past `check_by`, descending, with the parent path and unit fingerprint as deterministic tiebreaks. Unlike an experiment — where the primary cost is the decay of the context needed to write the result up — a prediction's `check_by` is an explicitly authored commitment, so "how far past the date you set" is the honest urgency signal and the author already chose the scale.

### Scope guards match the surrounding queues

Only read-write pages participate; index and log pages are skipped; and `superseded`, `archived`, and `draft` pages are excluded. A unit inherits its page's standing by the loop primitives' own rule, so a due prediction on a superseded page is not outstanding work. A cheap `check_by` substring prefilter runs before parsing, so pages that cannot possibly match are never re-parsed.

## Risks / Trade-offs

**A prediction resolved by an inbound edge keeps surfacing.** The central accepted cost, argued above. It fails toward *more* review work rather than false silence, which is the correct direction for a queue whose failure mode would otherwise be an obligation nobody sees. It is retired by the fragment-target refinement, and retiring it can only remove findings.

**Re-parsing semantic units costs audit time.** Bounded by the substring prefilter and by the same page scoping the surrounding queues use, and it is the same parse shape `relation_debt` already performs on a broader page set. A vault with no `check_by` rows pays a substring scan and nothing else.

**Partitioning by fingerprint means review state does not survive an edit.** Intended: a dismissal is a statement about specific text. The alternative — partitioning on the authored `id` anchor — would preserve state across edits but would silently apply a dismissal to a rewritten prediction, and would not work at all for a unit with no authored anchor.

**This is the one change on the branch that is not inert on upgrade.** Default `audit()` gains an `info` category and default `attention()` gains a queue. Accepted on the backlog argument above — the vocabulary is a day old, so the queue starts empty for every existing vault and fills only with predictions authored after it existed. The bound is checked rather than assumed: a vault with no `check_by` rows produces no findings, so an upgrader who has not adopted the primitives sees no change at all.

**The default tiebreak order is now widened, and a future addition could widen it thoughtlessly.** Mitigated by pinning the exact tuple in a test whose whole purpose is to fail on an unargued change, and by the spec now stating the ordering *principle* (authored before inferred) rather than only the resulting list — so the next person adding a queue has a criterion to argue against instead of a list to append to.

**`resolves` and `evidenced_by` clear a prediction without saying which way it went.** Deliberate. The check measures whether anyone has engaged with the prediction, not what they concluded; concluding is the `verdict`'s job. A unit that cites its resolving evidence but records no verdict is arguably still incomplete, but flagging it here would conflate "nobody checked" with "somebody checked and did not write a verdict row", and only the first is what this queue is for.

### Named follow-ups

1. **Fragment-target relation resolution** — make `[[Note#unit-abc]]` resolve to the unit rather than the page in `epistemic_graph.py`, then widen this predicate to honour an inbound resolving edge. Prerequisite for retiring the accepted false positive above.
2. **Tool-docstring refresh** — `review_memory`'s pinned `mode` description enumerates its modes and is already stale on `origin/main`: commit `14b524a5` added `mode="plan-progress"` without updating the docstring, so the shipped tool description omits a mode the tool serves. `op_attention`'s docstring likewise still says "the four measurement-only epistemic queues" after `bridge_review` made it five. Both are deliberately **not** fixed here: editing any MCP tool docstring regenerates `tests/fixtures/mcp_tool_schemas.json` and `src/exomem/tool_surface_contract.json`, and a moved ChatGPT plugin fingerprint is release-blocking. They should be swept together in one docstring-refresh change that pays the fingerprint cost once, deliberately, with the plugin refresh planned as part of it.
