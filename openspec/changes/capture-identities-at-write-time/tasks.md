## 0. Preconditions

- [ ] 0.1 Confirm the graph's link-dependency index can answer "which eligible pages link
      this identity" for an unresolved target without a vault walk, keyed compatibly with
      the wikilink lane's identity key. If it cannot, stop and report; do not add a
      vault walk to the write path without a ruling.
- [ ] 0.2 Record the before-image on a seeded synthetic vault: candidates surfaced by the
      wikilink lane today and the write responses for the 5.1 sequence.

## 1. The wikilink lane

- [ ] 1.1 Red first: two distinct eligible pages fire; one page linking five times does
      not; `index.md` and `log.md` pages supply no spread and never anchor a finding; one
      eligible page plus one retired page does not fire; the grammar lane's gates are
      unchanged.
- [ ] 1.2 Give the wikilink lane its own spread constant, 2, leaving the grammar lane's
      gates as they are; exclude navigation pages from its evidence.

## 2. The candidate on the write response

- [ ] 2.1 Red first: the crossing write carries the block; a third page does not; an edit
      of an already-linking page does not; a link to an active Entity yields an edge and
      no block; two notes in one mutation batch count once; the block is withheld when
      `structural_suggestions` is `off` and when derived sync is deferred; stateless HTTP
      gets the same block; `legacy` detail drops it and compact keeps it.
- [ ] 2.2 Compute the candidate beside `capture_sweep`'s page-less-link hint, reusing its
      link parsing, identity key, registry resolution and bounds, and the dependency
      index for the cross-page count.
- [ ] 2.3 Carry `entity_candidate` through the mutation terminal like
      `structure_suggestion`; MCP, CLI and REST parity.

## 3. Doctrine

- [ ] 3.1 Bootstrap guidance line at `balanced` and `maximal`; re-measure the compact byte
      budget at every level and surface.
- [ ] 3.2 Scaffold skill reference; keep it generic (`tests/test_scaffold_no_leak.py`).
- [ ] 3.3 One sentence in the `body` argument description of `remember` and
      `replace_memory`.
- [ ] 3.4 Capture hook wording ("stable, and central or recurring") in the plugin hook and
      its packaged copy, byte-identical.

## 4. Proof

- [ ] 4.1 End-to-end on the seeded vault with a scripted agent: a note links a page-less
      name (no block); a second note links it (block on that response); `create-entity`
      closes the candidate and both notes hold their edge without a full rebuild.
      Compare with 0.2.
- [ ] 4.2 Context compiler proof: after 4.1 the created Entity is an anchor and a turn
      naming it resolves it. No compiler code changes.
- [ ] 4.3 `ask_memory` and `find` byte-identical for every input; write latency with the
      candidate computation inside the existing write budget.
- [ ] 4.4 Real-vault check on the owner's snapshot: the lane surfaces the identities
      measured at two pages, and no `index.md`-only identity. Evidence to the owner's
      knowledge base, not the repository.

## 5. Delivery

- [ ] 5.1 Regenerate derived artifacts (tool schemas and fingerprint, capabilities doc,
      plugin tree, hosted render, harness modules pin); `openspec validate --all
      --strict`; privacy gate; full sharded corpus at the delivery boundary.
- [ ] 5.2 Independent review of the diff.
- [ ] 5.3 Archive with `openspec archive` after `complete-recurring-entity-lifecycle` is
      archived, refreshing this change's `action-first-audit` block onto that change's
      text first.
