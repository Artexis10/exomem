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
   committed when the page's path, source hash and fingerprint equal the slot's after
   values. A pending slot never reviews the page it is pending for, but its `carried_from`
   is read when the page equals the slot's before side: the pending slot copied that
   pointer from the decision it carried, and the next transition replaces the committed
   slot before it writes the page, so without the read an interrupted edit could not
   resume. Creation receipts cannot be named by `carried_from`. A slot that cannot be
   read means no carried decision for that page only.
3. **No new disposition kind.** An unknown kind falls through the commit plan's terminal
   branch and refuses every commit.
4. **One-call creation.** `draft_bound` is true when the caller names any of `draft_id`,
   `draft_hash`, `draft_token`. A creation that is not draft-bound takes the server's
   draft hash. `_commit_plan` is unchanged.
5. **One-call edit (INVALID_RELATION_REVIEW).** The edit path accepts reviewed-none and a
   reason without a hash when it names no `transition_token`, because the carried
   decision and the new-decision case share one rule: record a decision only if the edit
   needs one. The same rule holds on the validated route, so repeating the three calls
   cannot pile decision files either; a replay keeps the decision its prepared slot
   names. The alternative, fixing only the remediation text, would keep a loop that
   exists only to copy a hash. A `transition_token` without a hash stays refused and
   names `relation_review_hash`. `generic` has no one-call route, because its offers come
   from the validate result; without the hash it is refused for the missing field.
6. **Drop the "no candidates" condition.** `relation_candidates` includes frontmatter
   `sources:` provenance and links from other pages, so that condition would exclude every
   note that cites a source.
7. **Route-neutral text.** Remediations use `capabilities.active_call_text`, so the
   `validate_only` spelling matches the active surface.

## Risks

- A page that drifts in content without a connection is not asked again. `review_renewed`
  keeps the carry visible.
- A corrupt `prepared.json` slot reads as no carried decision for that page, so one
  damaged page cannot stop an audit of the others. Writers still refuse such a page when
  they plan the lifecycle transition.
- A disconnection that no edit of the page made does not lapse the decision. When an
  edit of another page, or a change outside the server, removes the page's last
  connection, the next evaluation of the page finds the older decision and carries it,
  and audit reports it as carried. The decision lapses only when an edit of the page
  removes its last connection. Main already let such a peer edit commit, because that
  write is judged on the peer page; its re-ask on the next edit of the page was a side
  effect of binding the decision to exact bytes, not a rule. The code stays as built:
  lapsing on a disconnection no edit of the page made would need a connection history
  that no artifact keeps.
- An out-of-band edit to a page whose only decision is a lifecycle decision costs one more
  handshake, because the committed slot no longer matches the page. A page whose decision
  is its creation receipt is not affected.
- A reviewed-none request on a page whose decision is already in force records nothing, and
  the result does not say that a new reason was dropped; the earlier reason stays. Creation
  reports ignored review fields, but five existing-page writers each build their own
  result, so reporting it for one would make them disagree. Left as follow-up work.
- The move writer's own carry is now redundant. Removing it is follow-up work.
