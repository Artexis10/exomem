## 1. Instruments first

- [ ] 1.1 `scripts/activation_real_turns.py` with the repository-refusal guard; unit tests
      on a temp vault. Record the owner's before-image on the current resolver (private).
- [ ] 1.2 Seeded corpus: dense cluster, `T10`, `T11`, `C10`. Red first: on the current
      resolver `T10` and `T11` must FAIL (false activation) and `C10` must fail to
      resolve. If any passes, the fixture does not reproduce the defect; fix the fixture.

## 2. Soundness

- [ ] 2.1 Red first: unit tests for retrieved-only → `partial` (any count, any
      qualifiers), neighbour-hit is not contact, corroboration needs a worded partner.
- [ ] 2.2 `candidates_for`: drop the neighbourhood clause. `add_graph_corroboration`:
      worded-partner rule. `_status_for`: worded-contact rule. Name the two families as
      constants beside `CONTACT_KINDS`.

## 3. Reach

- [ ] 3.1 Red first: rare single term, common single term, plural folding, derived short
      name unique / not unique / retired by a second page.
- [ ] 3.2 Index: title-and-alias term counts, derived names, schema version bump;
      rebuild-equals-incremental still holds.
- [ ] 3.3 Resolver: `rare_term` kind (added to `EVIDENCE_KINDS`, weak-worded family),
      folded lexical comparison, and the three-clause resolution rule.

## 4. Proof

- [ ] 4.1 Seeded audit green including `T10`, `T11`, `C10`; every existing case stays
      inside its pre-registered bounds or the change is reported with the numbers.
- [ ] 4.2 Real-turn run on the owner's snapshot: every negative turn abstains, no anchor
      is served without a worded contact or `exact_alias`, and the listed references
      reach their anchors as `resolved` or as a listed `partial`. Evidence to the
      owner's knowledge base, not the repository.
- [ ] 4.3 Latency gate at 2k and 8k notes; egress suite unchanged and green.

## 5. Delivery

- [ ] 5.1 Derived artifacts, `openspec validate --all --strict`, privacy gate, full
      sharded corpus at the boundary.
- [ ] 5.2 Independent review of the diff; archive with `openspec archive`.
- [ ] 5.3 After release: ask for `EXOMEM_DISABLE_WORKING_SET` to be removed from the
      owner's cell and run the live smoke through the real connector.
