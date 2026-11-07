# Tasks

## 1. Bounded status

- [x] 1.1 Add summary and revision-bound public detail pages; prove bounded replies and exact counts through the rendered MCP tool.
- [x] 1.2 Preserve the full internal browser status and validate invalid bounds and stale revisions with scoped surface tests.
- [x] 1.3 Generate tool contracts and document the compact inspection route; verify generated artifacts and privacy checks.

## 2. Exact invocation recovery

- [x] 2.1 Persist invocation identity and resolved scope before publication; prove a failed-only interrupted invocation still creates its owed note.
- [x] 2.2 Reclaim exact abandoned import identities under the normal writer guard; turn the real MCP post-chunk crash reproduction green.
- [x] 2.3 Preserve generic unknown, active-owner isolation, mismatch rejection and replay; prove A resumes its original scope after B runs.
- [x] 2.4 Prove checkpoint-free complete-chunk recovery and explicit unresolved legacy scope or partial-publication cuts without changing files or identity.

## 3. Interrupted publication

- [x] 3.1 Freeze chunk post-images before publication and complete missing owned components; prove Source-before-artifact and rename-before-Source crash cuts.
- [x] 3.2 Reconcile canonical index and log updates once; prove repeated recovery leaves exact Source counts and original hashes unchanged.
- [x] 3.3 Reconcile note, final-run-save and signed-receipt crash cuts; prove terminal replay and truthful remaining counts.
- [x] 3.4 Document version-specific recovery steps and solved or unresolved boundaries; validate each supported step against the synthetic workflow.

## 4. Delivery and verification

- [x] 4.1 Obtain one independent adversarial review of the integrated batch and recheck corrections; record the exact revision and C4/C6 verdict.

Source review: PR #2424, comment 6653336259, approved head `e939d30f8679f5f0541e802d8a24e83ff542f47c` against base `aacf82f85b93d8439f29bf3c92350228ad46a5ce`.
Patch `9766fdf8e8706802011c94407cb0762f37c13a35` received APPROVE with no blocking findings; the reviewer called ship, C4 absent and C6 sound.
Merge `c8a718a7fd4c3b59ad68b81198bef268571f9f25` retains the approved tree. Publication and released-byte acceptance remain open below.

- [x] 4.2 Run required completion checks, synchronize and archive this change, then deliver one ready PR through coordinated review and CI.
- [x] 4.3 Merge under standing authority and coordinate the containing release with its existing owner; verify published artifact identity.
- [x] 4.4 Run the isolated crash and large-status outcome check on released bytes, preserve durable evidence and remove owned scratch resources.

Release evidence: tag `v0.162.0` at `e1941d1a1259e0112918cab43ec13c9c30ee0a52` contains merge `c8a718a7fd4c3b59ad68b81198bef268571f9f25`.
Publisher run 42300743417 attempt 1 reports Python `qualified` for the exact producer artifact and the official GitHub and PyPI bytes.
The wheel SHA-256 is `80a49c87c42471ec3c7bbb6e2665ae26d6212500b0e510b1d33fde4ae63d086a`; the sdist SHA-256 is `9b7f05c933e6be69f029e392d7a80c30d3d542856c64c1846c63b796bdf2eb24`.
The 14 crash-cut and large-status cases in `tests/test_import_recovery_mcp.py` pass with every loaded `exomem` module taken from that official wheel.
Those cases cover each supported guide step: bounded summary and detail pages, the oversized row, and the single exact repeat of the interrupted apply.
The import guide names v0.162.0 as the first server with bounded status and same-identity recovery.
Cloud images and customer rollout are separate boundaries; this change records neither as verified.
