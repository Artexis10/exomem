## 0. Preconditions

- [ ] 0.1 Record the before-image on a seeded synthetic vault: candidates surfaced by
      `entity_recurrence` today, and the graph edges to Entity pages. This is the
      comparison for 5.1.

## 1. Declaration and storage

- [ ] 1.1 Red first: `tests/test_declared_mentions.py` for parse and caps, stored
      frontmatter shape, invalid item omitted without failing the write, a write without
      `mentions` byte-identical to before, refusal inside append-only trees, and
      `declare-mentions` on an existing page under its hash guard.
- [ ] 1.2 Add `src/exomem/declared_mentions.py` (parse, normalise, caps, findings) and
      thread `mentions` through `remember`, `replace_memory` and
      `connect_memory(operation="declare-mentions")`; CLI and REST parity.

## 2. Resolution into edges

- [ ] 2.1 Red first: one match gives `mentions`, central gives `about_entity`, two
      matches give no edge and a report, removal of a name removes only the
      `declared_mention` edge, resolution failure never fails the write.
- [ ] 2.2 Add origin `declared_mention` to the `mentions` and `about_entity` relations;
      derive the edges from frontmatter in the graph build and the incremental path, so
      rebuild equals incremental.

## 3. Counting and candidates

- [ ] 3.1 Red first: same-origin pages count once, different origins count twice,
      ambiguous names are not counted, thresholds read from the registry override with
      range findings.
- [ ] 3.2 Derived count of unresolved identities by independent origin, reusing
      `entity_recurrence._origin_refs`; candidates join the `entity_recurrence` family
      with a new reason `declared_identity_recurs`; closure when an Entity resolves the
      name or the owner dismisses it.
- [ ] 3.3 `entity_candidate` block on the committed response of the crossing write, once
      per crossing, through the same terminal path as `structure_suggestion`.

## 4. Backlog and doctrine

- [ ] 4.1 `undeclared_mentions` audit family: opt-in, bounded, newest first, absent from
      due-state. Red first: due-state totals unchanged on a vault of undeclared pages.
- [ ] 4.2 Bootstrap guidance line and scaffold skill reference; the `mentions` argument
      description carries the short doctrine. Keep the scaffold generic
      (`tests/test_scaffold_no_leak.py`). Re-measure the compact byte budget at every
      level and surface.

## 5. Proof

- [ ] 5.1 End-to-end on the seeded vault: three writes by a scripted agent. The first
      declares a passing name (counted, no candidate); the second, from another origin,
      declares it again (candidate on that response); `create-entity` closes it and both
      notes hold their edge. Compare with 0.1.
- [ ] 5.2 Context compiler proof: after 5.1 the created Entity is an anchor, and a turn
      naming it resolves it. No compiler code changes.
- [ ] 5.3 Three-door parity (MCP, CLI, REST); `ask_memory` and `find` byte-identical for
      every input; write latency for a 24-item declaration inside the existing write
      budget.

## 6. Delivery

- [ ] 6.1 Regenerate derived artifacts (tool schemas and fingerprint, capabilities doc,
      plugin tree, hosted render, harness modules pin); `openspec validate --all
      --strict`; privacy gate; full sharded corpus at the delivery boundary.
- [ ] 6.2 Independent review of the diff, then archive this change with
      `openspec archive` in the same delivery.
