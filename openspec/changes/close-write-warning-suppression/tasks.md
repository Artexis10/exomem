## 1. Contract and red-first acceptance

- [ ] 1.1 Add the `command-surface` delta covering fingerprint suppression, material-change resurfacing, fail-open emission, stance composition, and the unified relation-debt predicate.
- [ ] 1.2 Add the `attention-queue` delta covering the write-advisory review-state namespace and triage semantics.
- [ ] 1.3 Red-first: a test proving today's behaviour — a dismissed-equivalent advisory re-fires verbatim on the next write — then flip it by implementing suppression. The test must fail when the suppression mechanism is removed.
- [ ] 1.4 Red-first: a test proving write feedback and audit currently disagree on relation debt for a cited-but-unconnected page, then flip it by unifying the predicate.

## 2. Implementation

- [ ] 2.1 Derive stable review identities and fingerprints for the three advisory kinds from endpoint refs and content signal versions; namespace them apart from every existing review identity.
- [ ] 2.2 Consult portable review state before emission; fail open when unreadable; honor snooze expiry; honor the shipped competing-alternatives pair stance without restating its contract.
- [ ] 2.3 Route the new namespace through the explicit triage dispatch with a required reason.
- [ ] 2.4 Unify the relation-debt predicate in the write-result feedback and report provenance presence separately.
- [ ] 2.5 Update the post-write guidance text (bootstrap and scaffold skill) where it describes warning behaviour, and regenerate the packaged skill copies through their existing path.

## 3. Verification

- [ ] 3.1 Focused suites for suppression, resurfacing, fail-open, namespace isolation, and debt unification; mechanism-removal checks for both red-first tests.
- [ ] 3.2 Lean suite and write-latency gates green; no mutation-terminal envelope key changes; tool-surface fingerprint untouched.
