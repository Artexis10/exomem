## Context

The review of an unlinked page checks one fact: the page has no qualifying connection and
a reviewer said so. The stored decision, however, is keyed to the hash of the whole page.
Any byte change, including the server's own `updated:` stamp, voids it.

## Decisions

1. **Bind to identity, carry in the shared evaluator.** `semantic_contract.evaluate`
   judges the before state first. When the before state is reviewed-none for the same
   unique stable identity, the after state of a non-create-like precommit operation
   carries it. Posthoc evaluation carries a decision whose identity is current. The
   alternative, a writer-side carry copied from the move writer, writes one decision per
   edit, cannot cover decisions kept in creation receipts, and lets audit and writes
   disagree.
2. **Find the decision through the committed slot.** After a carry, no decision matches
   the new fingerprint. `load_relation_review` reads in this order: exact decision, the
   committed `prepared.json` slot's `carried_from`, then the creation receipt. A slot is
   committed only when the page's path, source hash and fingerprint equal the slot's
   after values, so a pending slot never carries. Creation receipts cannot be named by
   `carried_from`.
3. **No new disposition kind.** An unknown kind falls through the commit plan's terminal
   branch and refuses every commit.
4. **One-call creation.** `draft_bound` is true when the caller names any of `draft_id`,
   `draft_hash`, `draft_token`. A creation that is not draft-bound takes the server's
   draft hash. `_commit_plan` is unchanged.
5. **One-call edit (INVALID_RELATION_REVIEW).** The edit path accepts reviewed-none and a
   reason without a hash when it names no `transition_token`, because the carried
   decision and the new-decision case share one rule: record a decision only if the edit
   needs one. The alternative, fixing only the remediation text, would keep a loop that
   exists only to copy a hash. A `transition_token` without a hash stays refused and
   names `relation_review_hash`.
6. **Drop the "no candidates" condition.** `relation_candidates` includes frontmatter
   `sources:` provenance and links from other pages, so that condition would exclude every
   note that cites a source.
7. **Route-neutral text.** Remediations use `capabilities.active_call_text`, so the
   `validate_only` spelling matches the active surface.

## Risks

- A page that drifts in content without a connection is not asked again. `review_renewed`
  keeps the carry visible.
- A corrupt `prepared.json` slot raises a relation review error from the loader. The
  loader fails closed, as it does for a corrupt decision file.
- An out-of-band edit to a page whose only decision is a lifecycle decision costs one more
  handshake, because the committed slot no longer matches the page. A page whose decision
  is its creation receipt is not affected.
- The move writer's own carry is now redundant. Removing it is follow-up work.
