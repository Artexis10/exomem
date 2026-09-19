## 0. Preconditions

- [x] 0.1 Confirm the graph's link-dependency index returns, for a casefolded bare name,
      the pages whose body links that bare name, and that the write's preflight holds
      enough page state to evaluate `counts_as_evidence` for up to sixteen of them. The
      block is allowed to miss folder-qualified and differently normalised spellings. If
      the lookup needs a vault walk or a dependency-format change, stop and report; do
      not build either without a ruling.
- [ ] 0.2 Record the before-image on a seeded synthetic vault: candidates surfaced by the
      wikilink lane today and the write responses for the 5.1 sequence.

## 1. The wikilink lane

- [x] 1.1 Red first: two distinct eligible pages fire; one page linking five times does
      not; `index.md` and `log.md` pages supply no spread and never anchor a finding; one
      eligible page plus one retired page does not fire; the grammar lane's gates are
      unchanged.
- [x] 1.2 `SPREAD_MIN_PAGES` is shared by both lanes today. Give the wikilink lane its own
      constant, 2, leaving the grammar lane at 3; exclude navigation pages from the
      wikilink lane's evidence through `find_corpus.NAVIGATION_BASENAMES`.
- [x] 1.3 The recurrence requirement is amended inside
      `complete-recurring-entity-lifecycle`'s `action-first-audit` delta (gate of two,
      navigation pages, the write-time clause, four scenarios). Verify at delivery that
      those sentences still say so; if that change was archived first, move the edits
      into a `MODIFIED` block here. If this change archives while that one is still
      active, the canonical `action-first-audit` requirement keeps saying three pages
      until that change archives: say so in the delivery note, and prefer archiving that
      change first when its owner-controlled order allows.

## 2. The candidate on the write response

- [x] 2.1 Red first: the crossing write carries the block; a third page does not; an edit
      of an already-linking page does not; a link to an active Entity yields an edge and
      no block; two notes in one mutation batch count once and the identity gets no later
      block; the block is withheld when `structural_suggestions` is `off` and when the
      graph index is warming, temporarily unavailable or quarantined, whatever
      `derived_sync` reports; an excluded-tier, retired, navigation or `Entities/` page is
      neither counted nor listed; a page whose own title is the name does not count; a
      suffixed name standing on a real file carries no block; at most sixteen rows are
      evaluated; a folder-qualified second link yields no block while the audit
      lane still fires; stateless HTTP gets the same block; `legacy` detail drops it and
      compact keeps it.
- [x] 2.2 Compute the candidate beside `capture_sweep`'s page-less-link hint, reusing its
      link parsing, identity key, registry resolution and bounds, and the dependency
      index for the cross-page count.
- [x] 2.3 Carry `entity_candidate` through the mutation terminal like
      `structure_suggestion`; MCP, CLI and REST parity.

## 3. Doctrine

- [ ] 3.1 Bootstrap guidance line at `balanced` and `maximal`; re-measure the compact byte
      budget at every level and surface.
- [x] 3.2 Scaffold skill reference for linking; keep it generic
      (`tests/test_scaffold_no_leak.py`).
- [x] 3.3 One sentence in the `body` argument description of `remember` and
      `replace_memory`.
- [x] 3.4 "Stable, and central or recurring" in all six capture texts, each pair
      byte-identical: `src/exomem/_hooks/exomem_capture_nudge.py` and
      `plugins/claude-code/hooks/exomem_capture_nudge.py` (today: "only for a stable
      recurring identity"); `src/exomem/_scaffold/_Schema/workflow-skills/exomem-capture/SKILL.md`
      and `plugins/claude-code/skills/exomem-capture/SKILL.md`;
      `src/exomem/_scaffold/_Schema/references/operations.md` and
      `plugins/claude-code/skills/exomem/references/operations.md` (today: "stable,
      recurring, central"). A test greps the tree for both old phrases.

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
- [ ] 5.3 Archive with `openspec archive` in the same delivery, after confirming 1.3.
