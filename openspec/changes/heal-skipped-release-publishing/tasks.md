## 1. One publish path

The first branch dry run reported success while skipping package and image jobs.
The implementation claims below remain open until exact-content review and the
real branch dry run verify the repair.

- [ ] 1.1 Check exact distribution identity and hashes, committed Cloud metadata, image evidence, and each discovery tag before choosing publication or handoff.
- [ ] 1.2 Run dispatch jobs after successful direct prerequisites despite the skipped Release Please ancestor. Build every artifact during dry runs.
- [x] 1.3 Remove duplicate manual publish jobs, Hosted publication and the Records rollback input from the single path.
- [ ] 1.4 Recover original files and producer evidence from release assets and Actions artifacts. Verify all platform targets and the signed invocation. Preflight every immutable alias, serialize publishers across releases, and exclude dry-run distribution producers.
- [ ] 1.5 Verify the repair with scoped regressions, independent review, and a branch dry run that builds every supported image without publication.
- [ ] 1.6 Verify that the next release publishes every artifact from its tag, and a later push finds nothing missing.

## Recovery constraints

A legacy tag without recoverable producer evidence remains incomplete. A new
release provides a complete successor; recovery does not invent its predecessor's
attestation. Historical Hosted receipts and their two immutable aliases stay
unchanged and remain verifiable with their source-commit verifier.
Dry runs may retain temporary Actions artifacts and caches for verification.
