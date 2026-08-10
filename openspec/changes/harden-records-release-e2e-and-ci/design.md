## Context

The Records implementation has exhaustive in-process coverage for the X3 chronological log, a file-per-item vehicle collection, dataset reads, governance-before-reduction, safe mutation, direct-edit visibility/audit-gap detection, and recall isolation. The repository's release-level product loop independently builds a wheel, installs it into a clean virtual environment, initializes a temporary vault, and drives real stdio MCP plus HTTP and restart paths. Those two proofs do not currently meet: `scripts/e2e_product_loop.py` does not discover or call `record_memory`. Designing that black-box journey exposed one accepted-contract gap: the internal governed manifest projector round-trips `links.plans`, while the public `record_memory(action="inspect")` response omits it despite the canonical Records requirement that inspection return the authorized descriptor unchanged.

The accepted `close-technical-memory-gaps` change originally introduced that product loop but remained unarchived, so its `product-e2e`, stable-reference, schema-evolution, and related delta requirements had not reached canonical specs. This change first synchronizes that already-shipped contract into `openspec/specs/`; its own delta then has one unambiguous canonical capability to modify. Archiving the historical change remains mechanical follow-up and is not allowed to block this product correction.

The lean matrix normally completes in about sixteen minutes per Python lane. A contended runner took thirty-nine minutes and exposed four tests whose correctness assertions used production time budgets or absolute wall-clock thresholds. The production boundaries behaved as designed; the test assumptions did not.

The pre-change red evidence is retained in GitHub Actions rather than recreated after the fix. Run `31364355540`, job `93379461964`, recorded the 39-minute Python 3.11 lane with `MUTATION_BUSY` after the production five-second wait, a two-second Records future timeout, and a 3,020.2ms hold measurement against the 1,800ms host-speed threshold. Run `31363972628`, job `93378330587`, recorded the expired-checkpoint semantic test returning zero removals after its intentional 50ms production prune budget was exhausted. The replacement tests reproduce the relevant held-boundary, admission, boundary-state, and constrained-budget conditions while asserting semantic outcomes instead of replaying wall-clock failures.

## Goals / Non-Goals

**Goals:**

- Make one representative Records journey a release-blocking installed-wheel/real-MCP proof.
- Exercise human file ownership and agent-safe mutation in the same canonical collection across a process restart.
- Preserve the detailed lower-level Records matrices instead of reproducing every case in the product loop.
- Replace runner-speed assertions with deterministic semantic assertions in the four observed flaky tests.
- Bound a pathological lean lane and retain useful failure/timing evidence.
- Correct the existing public Records inspection projection so the already-specified opaque Planning descriptor is actually reachable through the installed product surface.

**Non-Goals:**

- No new Records action/tool, storage format, governance policy, lock timeout, or prune budget change. The existing `inspect` response gains only the already-specified governed Planning descriptor.
- No full Planning implementation; the fixture only proves the existing opaque Planning-reference contract.
- No suite deletion, Python-version coverage reduction, `xdist`, broad sharding, coverage gate, or mutation-testing rollout.
- No claim that every Records edge case is a black-box E2E scenario.

## Decisions

### Extend the existing product loop instead of creating another E2E harness

The current script already proves wheel construction, clean installation, CLI initialization, real stdio MCP, restart persistence, HTTP lifecycle, and writer coordination. The Records phase will be added to its two stdio sessions and `record_memory` will become a required installed tool. A second harness would duplicate expensive setup and could drift from the release path.

The script will create a small X3-compatible chronological-log collection inside its temporary vault using ordinary file writes. It will create an ordinary editable template and insert one completed block into the canonical log before the first MCP call. The fixture is self-contained in the product script rather than importing `tests/fixtures`, so success proves the installed product rather than a repository-only fixture dependency.

During the first stdio session the product loop will:

1. query the manually inserted session through `record_memory`;
2. append an identified session using the returned container/source guard;
3. query and target that item with its current item version;
4. update one field with both drift guards;
5. request a bounded derived Markdown or CSV view;
6. inspect and round-trip the manifest's opaque Planning reference/query descriptor; and
7. ask ordinary memory for a row-only sentinel and prove the canonical log is not returned as semantic recall.

With the server stopped, the outer harness will directly edit the canonical log. The second stdio session will query the collection again and prove the human edit and prior guarded mutation both survive restart without a migration or derived source of truth. It will then inspect the collection and require a positive bounded audit gap for the unaudited human edit; visibility is not silently described as reconciliation or repair. The existing non-owner governance-before-reduction tests remain the exhaustive disclosure proof. The current no-auth HTTP E2E server deliberately represents local owner mode, so fabricated headers must not be used to pretend it is remote. Instead, the harness will launch the installed `python -m exomem --transport http` path, which enables auth, on a second temporary port. Its clean environment will explicitly override `EXOMEM_BASE_URL`, `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `EXOMEM_GITHUB_USERNAME`, a positive numeric `EXOMEM_GITHUB_USER_ID`, and `EXOMEM_JWT_SIGNING_KEY`; no inherited real credential may participate.

The harness will send raw `POST /mcp` with a protocol-valid JSON-RPC `tools/call` body naming `record_memory`, `Accept: application/json, text/event-stream`, `Content-Type: application/json`, and no `Authorization` header. It must receive exactly `401` plus a Bearer `WWW-Authenticate` challenge naming the local protected-resource metadata URL. The assertion reads the raw response bytes—FastMCP's missing-auth response can be empty—and confirms they disclose no collection path, row, Planning reference, or aggregate value. A generic HTTP error, parse failure, or 404/405/406/415/500 does not pass. This certifies installed remote fail-closed routing, not execution of the command-level governance projector; stdio separately proves the owner product path.

### Close the existing inspection-projection gap at the narrowest boundary

`links.plans` is already parsed, validated, stored, and projected through the Records governance layer, and the canonical Records spec already requires inspection to return the authorized descriptor unchanged. The release journey must not call the unused internal `project_manifest()` helper or import product internals, because that would falsely certify an unavailable product path. Instead, `inspect_collection()` will include the same bounded `_project_plan_link` results in its public contract projection. The implementation will reuse the inspection's existing released manifest and `_LinkProjector`, preserve authorization of the containing manifest/source before projection, and perform no Planning lookup or comparison. Opaque `plan.reference` values are deliberately not resolved or target-authorized: missing and hidden targets must remain indistinguishable, while link-typed values inside the bounded Records query continue through `_LinkProjector` filtering.

The nested egress boundary remains default-deny. `_validate_record_inspection` must explicitly accept, strictly reconstruct, and bound `contract.plans` rather than passing through arbitrary nested mappings. Collection inspection contracts always carry a list, including `plans: []`; the validator will require at most the manifest limit of 32 descriptors, exact `{reference, query}` descriptor keys, canonical bounded opaque references, exact query keys limited to `filters` and `limit`, bounded field names/plain-JSON filter values, and `1 <= limit <= HARD_ROW_CAP`. Hostile or extra nested data must still withhold the entire invalid inspection payload. Tests cover hidden/missing Planning-target parity, withheld nested query-link values, and denied collection/source non-disclosure as well as hostile validator input. This is an additive correction to the existing `inspect` result, not a new command or a general manifest API.

### Assert concurrency state, not elapsed time

The affected tests will preserve their original invariants while changing their observation mechanism:

- The twenty-write test will deliberately produce pre-commit `MUTATION_BUSY`, prove every refused write is non-committed, then retry the refused writes in concurrent waves after release until all commit under one generous deadlock-only test deadline, retaining the original complete-vault/index/log/no-residue assertions. It will not widen the production five-second timeout or replace the successful-contention invariant with a backpressure-only test.
- The Records mutation-matrix test will use positive attempt/admission/release events around the real mutation guard. Different vaults must both admit before release; a second same-vault attempt must not reach the guarded manifest loader until the first releases. Generous waits exist only as deadlock guards.
- The critical-section test will record the active mutation state from the relation-review evaluation seam. Narrow mode must observe `free`; the wide-boundary kill switch must observe `held`. No sleep or millisecond threshold remains.
- The continuation-prune semantic test will use a test-only generous prune budget, leaving the separate production-budget tests unchanged.

Increasing production timeouts was rejected because it would hide correct backpressure and make slow CI define product behavior.

### Use two nested CI ceilings and durable timing evidence

The matrix pytest process will receive `--session-timeout=1500`, which pytest-timeout checks between test items rather than interrupting an already-running item. The existing sixty-second per-item timeout remains the item ceiling, and the GitHub job receives `timeout-minutes: 30` as the actual hard process bound. The requested inner stop should end normal collection/test execution cleanly enough to print the slowest tests and write JUnit; the outer ceiling catches collection, teardown, plugin, or runner hangs.

Each Python lane will print its slowest fifty tests above a one-second floor and write a JUnit XML path containing `${{ matrix.python-version }}`. An `if: always()` artifact step will use an artifact name containing the same matrix version and `if-no-files-found: warn`, so immutable v4 uploads cannot collide and a missing file cannot mask the original test failure. The workflow remains serial within each lane and retains both supported Python versions in this change.

### Keep performance restructuring separate

The new evidence can support a later file-level shard layout and test consolidation. Doing that now would change coverage topology without reliable timings and would mix release correctness with a broader test-governance project. A pathological run becomes bounded now; reducing a normal sixteen-minute lane to the five-to-eight-minute target is a separate measured change.

## Risks / Trade-offs

- [Risk] The product E2E becomes a second exhaustive Records suite. → Keep one chronological-log happy path plus restart/refusal containment; leave vehicle, dataset, aggregate leak, ambiguity, crash-prefix, and scale cases in focused tests.
- [Risk] A fixture embedded in the script drifts from the canonical X3 adapter. → Use the same manifest grammar and assert the manual/agent/restart behavior that defines compatibility; the full fixture fidelity test remains authoritative for the real files.
- [Risk] The inner session timeout does not fire during collection or interpreter teardown. → Retain the independent thirty-minute GitHub job ceiling.
- [Risk] JUnit is unavailable after a hard job kill. → Set the pytest ceiling five minutes earlier so normal failures upload evidence; the job cap is only the final hang guard.
- [Risk] More generous deadlock guards are mistaken for performance budgets. → Tests assert state transitions immediately after positive synchronization; generous waits only fail a hang and never define acceptable speed.

## Migration Plan

No user data or runtime migration exists. Merge adds release proof, the additive governed `inspect.contract.plans` projection, and CI diagnostics. Rollback removes the added projection, product-loop phase, deterministic test rewrites, and workflow bounds without touching any vault or production configuration.

## Open Questions

None. Broader suite restructuring and multi-horizon Planning remain separately bounded follow-ups.
