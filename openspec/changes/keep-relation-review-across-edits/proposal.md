## Why

An agent that edits a page holding a reviewed-none relation decision is refused with
`RELATION_DISPOSITION_STALE`, even when the page still has no connection and nothing
about its relations changed. The cause is the binding. `review_content_fingerprint`
hashes the whole normalized page, including the `updated:` stamp that the server writes
on every edit. So each edit voids the decision it was made under.

The agent then repeats three calls: validate, copy `relation_review_hash`, commit. Each
repeat writes a new immutable decision file under the page's lifecycle directory, and a
page accepts 256 of them before it refuses edits with `RELATION_REVIEW_HISTORY_LIMIT`.
The review shows no link targets, so the repeat is a hash copy, not a review. An outcome
benchmark run on 2026-11-06 refused 41 of 98 writes for these two codes (figures reported
by the benchmark owner; not re-measured here).

A fresh creation has the same cost in smaller form. It must call `validate_only` first,
only to read a hash that the server computes from the same page.

Two remediation texts also name a route the called tool does not offer: the existing-page
text names `validate_only`, and the creation text names the draft hash.

## What Changes

The decision this change records: **a reviewed-none decision binds page identity until
its connection state changes, not exact bytes.**

- An edit of a page that held a reviewed-none decision before the edit keeps that
  decision, reports `review_renewed: true`, and writes no new decision file. The decision
  lapses only when the page held a qualifying connection before the edit, so an edit that
  removes the page's last connection is still refused.
- A creation that names no draft field commits reviewed-none in one call with
  `relation_disposition="reviewed_none"` and a reason. The server derives the hash from
  the normalized final content.
- An edit that sends reviewed-none and a reason without `transition_token` and without a
  hash commits in one call, and records a decision only when the edit needs one.
- The evaluator for audit and the watcher carries a decision whose identity is current, so
  audit and writes agree.
- Each relation remediation names the route that the called tool offers, spelled for the
  active surface. The bootstrap rule texts get shorter.
- The disposition kinds stay `reviewed_none` and `bootstrap`. No kind is added.
- Not changed: P2 (an edit that removes the last connection is blocked), P3 (the
  bootstrap pass expires with a second page), the grandfathered-page rules, the creation
  receipt format, and parameter descriptions.

## Capabilities

### Modified Capabilities
- `semantic-write-contract`: the binding of a reviewed-none decision, the one-call
  creation clause, and the existing-page decision loader.
- `agent-bootstrap-contract`: the reviewed-none guidance names the one-call route.

## Impact

- Code: `semantic_contract.py`, `relation_review.py`, `semantic_writes.py`, `note.py`,
  `create_file.py`, `mutation_terminal.py`, `cli_ops.py`, and the bootstrap rule texts in
  `commands.py`.
- Scaffold references `writing.md` and `mutation-results.md`; the plugin copies are
  regenerated.
- Existing decision files stay valid. A vault that already holds many decision files per
  page is not cleaned by this change.
