# Tasks — add-entity-recurrence-sensor

## 1. Red-first fixtures

- [x] 1.1 Acceptance fixture (D6.1): three pages carrying `[[Unresolved Name]]`
      → RED on the untouched base with verbatim output captured before any
      sensor code.
- [x] 1.2 The three quiet twins (D6.2–D6.4): plain-text mentions, registry-
      resolved alias, below-spread — zero findings asserted positively.

## 2. Sensor

- [x] 2.1 Recurrence collection: one pass over the audit's parsed pages,
      `vault.find_body_wikilinks` per body, `entity_candidates.identity_key`
      normalisation (imported), per-page dedup, spread gate
      `SPREAD_MIN_PAGES` (PROVISIONAL), self-link and `Entities/` exclusions,
      wikilink display/heading forms (`[[a|b]]`, `[[a#h]]`) normalised to the
      target page identity.
- [x] 2.2 Registry resolution + near-match assist: candidate excluded when it
      NFKC-resolves against titles/aliases; near-matches by shared identity
      tokens, `MAX_NEAR_MATCHES` cap, deterministic order.
- [x] 2.3 Finding composition: reason `unresolved_identity_recurs`,
      `meta["signal_version"] = content_hash(identity_key)[:16]`, anchor =
      lexicographically smallest mentioning page, sorted page list in meta.

## 3. Delivery through existing machinery

- [x] 3.1 Category `entity_recurrence` in `EPISTEMIC_REVIEW_CATEGORIES` and the
      sweep wired in `audit.py` (scope_divergence_semantic is the template);
      family registration and attention admission verified derivational (no
      registry restated).
- [x] 3.2 Resolution by state change (D6.5) and S6 integration (D6.6) tested:
      entity page create/delete both directions; target-page create; family
      `off`; dismissal survives incidental edits.

## 4. Gates

- [x] 4.1 Focused suites: the new sensor tests, audit suites, review-state and
      attention suites. Mutation proofs in a scratch copy under
      /tmp/claude-1000/** (never the worktree): every gate constant and
      exclusion rule names a test that fails when deleted.
- [x] 4.2 Cost bound measured and recorded: one pass over already-parsed
      bodies; no second corpus scan; number for a large synthetic vault.
- [x] 4.3 `uvx ruff check src/exomem --select F`; `openspec validate --all
      --strict` (npm exec @fission-ai/openspec@1.10.0); no tool-surface pin
      move (`git status` proof).
- [x] 4.4 Determinism across page-insertion orders (D6.7).
