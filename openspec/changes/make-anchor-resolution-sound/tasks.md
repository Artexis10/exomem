## 1. Instruments first

- [x] 1.1 `scripts/activation_real_turns.py` with the repository-refusal guard; unit tests
      on a temp vault. Record the owner's before-image on the current resolver (private).
- [x] 1.2 Seeded probe corpus, standalone from the pre-registered fixtures: dense
      cluster, `T10`, `T11`, `C10`. Red first: on the current resolver `T10` and `T11`
      must FAIL (false activation) and `C10` must fail to resolve. If any passes, the
      corpus does not reproduce the defect; fix the corpus.

## 2. Soundness

- [x] 2.1 Red first: unit tests for retrieved-only → `partial` (any count, any
      qualifiers), neighbour-hit is not contact, corroboration needs a worded partner.
- [x] 2.2 `candidates_for`: drop the neighbourhood clause. `add_graph_corroboration`:
      worded-partner rule. `_status_for`: worded-contact rule. Name the two families as
      constants beside `CONTACT_KINDS`.

## 3. Reach

- [x] 3.1 Red first: rare single term, common single term, plural folding, derived short
      name unique / not unique / retired by a second page.
- [x] 3.2 Index: title-and-alias term counts, derived names, schema version bump;
      rebuild-equals-incremental still holds.
- [x] 3.3 Resolver: `rare_term` kind (added to `EVIDENCE_KINDS`, weak-worded family),
      folded lexical comparison, and the three-clause resolution rule.

## 4. Proof

- [x] 4.1 Probe corpus green on `T10`, `T11`, `C10`; every pre-registered case stays
      inside its bounds or the change is reported with the numbers.
- [x] 4.2 Real-turn run on the owner's snapshot: every negative turn abstains, no anchor
      is served without a worded contact or `exact_alias`, and the listed references
      reach their anchors as `resolved` or as a listed `partial`. Evidence to the
      owner's knowledge base, not the repository.
- [x] 4.3 Latency gate at 2k and 8k notes; egress suite unchanged and green.

## 4a. Words in every script (found after merge)

- [x] 4a.1 Red first: the tokeniser splits accented words ("Ausrüstung" gives two
      fragments), drops non-Latin scripts entirely, and lets two unrelated titles
      overlap on a shared word ending. Tests for the four new scenarios, plus a
      property test that basic Latin input tokenises exactly as before.
- [x] 4a.2 Tokenise maximal runs of letters, digits and combining marks in any script,
      keeping the existing fast path for basic Latin text; bump the activation index
      `SCHEMA_VERSION` so stored terms and aliases rebuild.
- [ ] 4a.3 Probe corpus, deterministic activation audit and latency gate unchanged and
      green; real-turn run on the owner's snapshot shows no negative turn resolving.

## 5. Delivery

- [ ] 5.1 Derived artifacts, `openspec validate --all --strict`, privacy gate, full
      sharded corpus at the boundary.
- [ ] 5.2 Independent review of the diff; archive with `openspec archive`.
- [ ] 5.3 After release: ask for `EXOMEM_DISABLE_WORKING_SET` to be removed from the
      owner's cell and run the live smoke through the real connector.
