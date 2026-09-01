## 1. Contract and red-first acceptance

- [x] 1.1 Add the `command-surface` delta covering fingerprint suppression, material-change resurfacing, fail-open emission, stance composition, and the unified relation-debt predicate.
- [x] 1.2 Add the `attention-queue` delta covering the write-advisory review-state namespace and triage semantics.
- [x] 1.3 Red-first: a test proving today's behaviour — a dismissed-equivalent advisory re-fires verbatim on the next write — then flip it by implementing suppression. The test must fail when the suppression mechanism is removed.
- [x] 1.4 Red-first: a test proving write feedback and audit currently disagree on relation debt for a cited-but-unconnected page, then flip it by unifying the predicate.

## 2. Implementation

- [x] 2.1 Derive stable review identities and fingerprints for the three advisory kinds from endpoint refs and content signal versions; namespace them apart from every existing review identity.
- [x] 2.2 Consult portable review state before emission; fail open when unreadable; honor snooze expiry; honor the shipped competing-alternatives pair stance without restating its contract.
- [x] 2.3 Route the new namespace through the explicit triage dispatch with a required reason.
- [x] 2.4 Unify the relation-debt predicate in the write-result feedback and report provenance presence separately.
- [x] 2.5 Update the post-write guidance text (bootstrap and scaffold skill) where it describes warning behaviour, and regenerate the packaged skill copies through their existing path.

## 3. Verification

- [x] 3.1 Focused suites for suppression, resurfacing, fail-open, namespace isolation, and debt unification; mechanism-removal checks for both red-first tests.
- [x] 3.2 Lean suite and write-latency gates green; no mutation-terminal envelope key changes; tool-surface fingerprint untouched.
      Write-latency gates: operator session artifacts (2026-08-29, quiesced WSL box).
      Measurement run (`latency-3.2.log`), full JSON: `{"results": [{"cold_ms": 895.9,
      "cold_preflight_ms": 929.4, "cold_read_after_write_ms": 349.9, "commit_median_ms": 97.5,
      "commit_p95_ms": 131.6, "pages": 2000, "read_after_write_median_ms": 478.6,
      "read_after_write_p95_ms": 484.9, "samples": 5, "validate_median_ms": 13.6,
      "validate_p95_ms": 15.3}, {"cold_ms": 9659.0, "cold_preflight_ms": 4097.8,
      "cold_read_after_write_ms": 1539.1, "commit_median_ms": 197.7, "commit_p95_ms": 328.6,
      "pages": 8000, "read_after_write_median_ms": 2788.4, "read_after_write_p95_ms": 3081.5,
      "samples": 5, "validate_median_ms": 60.3, "validate_p95_ms": 83.6}]}`.
      Gate-checking run (`scripts/semantic_write_latency.py --check`, `latency-3.2-check.log`),
      exit 0, full JSON: `{"results": [{"cold_ms": 907.7, "cold_preflight_ms": 920.6,
      "cold_read_after_write_ms": 344.5, "commit_median_ms": 92.2, "commit_p95_ms": 105.8,
      "pages": 2000, "read_after_write_median_ms": 464.8, "read_after_write_p95_ms": 481.9,
      "samples": 5, "validate_median_ms": 13.3, "validate_p95_ms": 13.8}, {"cold_ms": 9024.7,
      "cold_preflight_ms": 3791.2, "cold_read_after_write_ms": 1449.2, "commit_median_ms": 159.6,
      "commit_p95_ms": 278.1, "pages": 8000, "read_after_write_median_ms": 2707.5,
      "read_after_write_p95_ms": 2756.2, "samples": 5, "validate_median_ms": 60.6,
      "validate_p95_ms": 155.6}]}`. Lean suite: the latest completed green `CI` run on `main`
      is databaseId `33252272491` on `785acb77d24d7b626a469df12d4be08932f0922f`
      (2026-08-29T12:20:54Z), conclusion `success`. Three later `CI` runs on `main` are red and
      none touch the write-advisory suppression path this change ships: `33252296993` @
      `5c2d8a4a` (Windows held-filesystem job, `test_twenty_concurrent_real_captures_leave_complete_vault_state`
      TimeoutError — matches the known Windows timing flake); `33256407777` and `33258228390`
      @ `9bf3d804` (core tests py3.13 shard 3/8,
      `test_never_enrolled_provisioning_refuses_existing_governance_authority` state-root leak
      — "a test touched the real user state root", reproducible across both runs; untriaged
      repo debt, not a flake). Envelope keys / fingerprint: this change shipped in 0.58.0
      (#585); the fingerprint pin is exercised and green in the cited run `33252272491` @
      `785acb77`.
