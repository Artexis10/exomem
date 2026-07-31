## 1. Prompt and skill repair (no schema change)

- [ ] 1.1 Replace the deferring empty-provenance next-action text in the write-feedback
      builder with present-tense guidance, emitted as a distinct signal when provenance is
      empty and body wikilinks are zero. Check first whether any test pins the current
      literal string.
- [ ] 1.2 Add one provenance step to the bootstrap authoring contract's canonical loop and
      one provenance route key to its intent routing. Confirm the dumped fixture stores no
      output schema for bootstrap, so this stays fingerprint-neutral.
- [ ] 1.3 Promote linking to a numbered rule in the scaffold contract's non-negotiable
      write-discipline list, adjacent to the mandatory-frontmatter rule. The rule states the
      obligation, the back-reference mechanic, and — explicitly — that honest zero is
      legitimate and no minimum edge count exists.
- [ ] 1.4 Split the scaffold write loop's drafting step into identify-provenance and draft,
      and name the three write-feedback connectivity fields in the inspect step.
- [ ] 1.5 Per workflow skill, insert one or two lines into the workflow list **above** the
      semantic-authoring block: ingest (pass the captured path as provenance), capture
      (restore link suggestion to an early step), reflect, media (also add a link-suggestion
      step), research, defrag (preserve merged edges on the survivor), curate (name the
      link-suggestion and relation-debt operations), review (name provenance). Continue is
      read-only and unchanged.
- [ ] 1.6 Move each workflow skill's output-contract, save-rules, and mistakes sections above
      the semantic-authoring block so the block is the file tail. Pure block move; the
      projection validator only requires the block to occur exactly once.
- [ ] 1.7 Regenerate the plugin skill tree from the scaffold and commit both trees. Add the
      provenance clause to the three relevant hosted micro-skills, honouring their token
      denylist and required-tools constraint.
- [ ] 1.8 Run the scaffold leak guard, plugin sync, bootstrap, and package/install skill
      tests.

## 2. Provenance warning and review visibility (red first)

- [ ] 2.1 Tests: empty provenance warns for each required compiled type, is silent for
      experiment and production log, and never blocks the write.
- [ ] 2.2 Tests: the new review category flags only the required types at informational
      severity, honours the relation-debt exclusion set, is absent from the default
      attention set, and no repair pass ever writes a provenance value.
- [ ] 2.3 Tests: the disposition census counts every evaluated page including satisfied
      pages that emit no finding, and survives the payload byte-budget shrink.
- [ ] 2.4 Emit the warning from both the tier-2 and legacy note paths, after provenance
      normalization, wording it to accept honest zero. Leave the validator untouched so it
      cannot block.
- [ ] 2.5 Report provenance state in write feedback: cited count, planned back-references,
      required flag, missing flag.
- [ ] 2.6 Add the dedicated optional informational review category modelled on relation debt,
      reusing its exclusion set. Do not extend the required-frontmatter table.
- [ ] 2.7 Add the fixed-size disposition census over all evaluations in the post-hoc batch
      payload, and route it into audit metadata.

## 3. Connectivity measurement — provenance and excluded families (red first)

- [ ] 3.1 Tests: the typed-edge predicate is unchanged for every excluded family, asserted
      directly against that predicate so the two predicates can never be merged.
- [ ] 3.2 Tests: cited provenance to an append-only source satisfies connectivity; the target
      is absent from the eligible governed set but present in the connectable set.
- [ ] 3.3 Tests: an excluded-family typed row satisfies connectivity, is reported distinctly,
      and emits the typed-edge-absent warning at warning severity with no blocking.
- [ ] 3.4 Test: a first compiled page in a vault containing only captured sources still
      receives the bootstrap disposition. This is the cold-start guard.
- [ ] 3.5 Test: inbound links and automatically written back-references never satisfy
      connectivity.
- [ ] 3.6 Test: a connectivity-satisfied commit succeeds and writes a qualifying receipt,
      pinning that it never reaches the commit planner's terminal else branch.
- [ ] 3.7 Test: a reviewed-none decision submitted for a connectivity-satisfied page is
      reported not applicable, with error text naming the connectivity case.
- [ ] 3.8 Add the connectable-target predicate beside the eligible-governed predicate,
      admitting append-only tier and the source type while reusing every other eligibility
      rule. Extract the shared body rather than duplicating it. Do not widen the eligible
      governed type set.
- [ ] 3.9 Carry the connectable flag on page state and the connectable path set on the corpus
      context, including their serialized forms.
- [ ] 3.10 Add the connectivity predicate beside the typed predicate, factoring the shared
      structural checks into one private helper both call.
- [ ] 3.11 Partition the disposition into two lanes: typed first, unchanged; connectivity
      second over outbound facts only; then the existing reviewed-none, bootstrap, and
      missing fall-through, unchanged.
- [ ] 3.12 Add the qualifying-signal, review-reason, and review-reference fields to the
      disposition with backward-compatible defaults, and populate the review fields from the
      loaded decision. Introduce no new disposition kind.
- [ ] 3.13 Emit the typed-edge-absent warning only when connectivity satisfied the
      disposition, and route it into the existing relation-disposition audit group.
- [ ] 3.14 Plumb the three new fields through bounded write feedback, bounding the reason text.

## 4. Connectivity measurement — body wikilinks (red first)

- [ ] 4.1 Tests: a resolved outbound body wikilink to a connectable page satisfies
      connectivity with the warning and no blocking; a wikilink outside the governed tree or
      to an inactive page does not; facts are deduplicated by normalized target and capped
      per page.
- [ ] 4.2 Carry the page's body wikilinks on page state, computed once during page-state
      construction where the body is already in scope, using the existing fence-aware
      scanner. Do not re-read the file during fact derivation.
- [ ] 4.3 Emit wikilink-origin facts, skipping lines that already carry a typed relation,
      deduplicating by normalized target, and capping per page. No registry change is needed.

## 5. Measurement and gates

- [ ] 5.1 Record baseline write-latency and governance-overhead numbers on a quiesced machine
      before section 4.
- [ ] 5.2 Re-run both after section 4 in the same quiesced session and record both figures in
      the design document.
- [ ] 5.3 If the ratio regresses, fall back to a precomputed per-page connectivity tuple at
      corpus build and re-measure.

## 6. Command surface and pinned fingerprint (last, once)

- [ ] 6.1 Restore the provenance parameter documentation on the remember command: wikilink
      form, concrete example, back-reference mechanic, and the warning-not-error behaviour.
      Do not bump the authoring-contract version.
- [ ] 6.2 Regenerate the tool schema fixture, the packaged tool-surface contract, and the
      capabilities document; review the diff for the single intended change plus the new
      fingerprint.
- [ ] 6.3 Advance the pending fingerprint in the connector contract by hand, leaving the
      registered fingerprint, refresh flag, and rollout state untouched.
- [ ] 6.4 Validate the change with the strict OpenSpec validator, run the lint pass, and
      confirm the full suite on Linux CI.
- [ ] 6.5 After deploy, re-verify connector **content** reads, not only connection.
