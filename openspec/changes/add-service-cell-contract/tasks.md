# Tasks

Stages 0 and 1 start after the passage-evidence and captured-source-inventory work merges to `main`. No task here changes that work's contract.

## 1. Stage 0: service captures stay off shared summaries

- [ ] 1.1 Skip the Sources catalog, top-level index and activity log writes for a capture bound to a server-issued capture attempt. Keep every other capture path unchanged. Verify: an attempt-bound capture leaves the three summary pages byte-identical and appears in captured-Source inventory; a capture without an attempt still updates all three.

## 2. Stage 1: typed service API, version 1

- [ ] 2.1 Declare service operations in the command registry: operation name, rendered command, fixed or narrowed arguments, request model, result model, refusal codes and the identifier that makes a repeat safe. Declare the D1 version 1 set. Verify: a registry test fails for an operation whose command is missing or whose refusal code the leaf cannot raise.
- [ ] 2.2 Give each rendered leaf a typed result model. The MCP door keeps its current reply. Verify: each leaf's result validates against its model in the existing leaf tests.
- [ ] 2.3 Serve `POST /service/<version>/<operation>` on the Cloud cell's listener for versions that `EXOMEM_SERVICE_API` lists. Run the existing credential and trusted-routing checks, then strict request validation, then the leaf. Map codes to the D4 status table and set `Retry-After` on 503. Verify: with the setting unset, every service path answers as an unknown route and runs no leaf; an unknown field returns 400 naming the field.
- [ ] 2.4 Write one call-ledger row for each authenticated service call, with the service transport, version and operation, and return its request id. Verify: the ledger's content-free test covers a service passage search and a refused service call.
- [ ] 2.5 Generate the version 1 OpenAPI 3.1 document from the registry, reusing the personal REST facade's generation. Commit the generated copy. Verify: a test fails when the generated document differs from the committed copy; every successful response in the conformance run validates against the document.
- [ ] 2.6 Add a maintained OpenAPI diff tool, such as `oasdiff`, to the pull-request tier. It compares the committed document with the base branch's copy and fails on a breaking difference within one version. Verify: a removed response field fails the check, and an added optional request field passes.
- [ ] 2.7 Add the `service_api` section to cell discovery: each served version, its document path, the document's SHA-256 digest and any retirement date. Serve the document without a credential. Verify: the advertised digest equals the digest of the served document; with the setting unset, discovery lists no version.
- [ ] 2.8 Route the service path through the Cloud edge with the same trusted-routing checks as the MCP path. Verify: on a disposable cell, a credential for another cell receives 401 before any leaf runs.
- [ ] 2.9 Run the conformance check on a disposable cell: each operation through both doors with equal arguments on equal state. Verify: the service result equals the MCP leaf result for every operation, and a restricted credential receives the same authority code through both doors.
- [ ] 2.10 Measure passage search through both doors in one run on the passage reference corpus and hardware. Verify: the service door meets the one-caller and four-caller p95 bars; record both doors' results in the delivery evidence.
- [ ] 2.11 Write the integrator guide: the day-one steps from D1 and D2, the status table, retry safety for each operation and the versioning rule. Verify: an integrator can generate a client and capture, settle, search and inventory one Source from the guide alone on a disposable cell.
- [ ] 2.12 Record the first consumer's acceptance run with a client generated from the version 1 document on a disposable cell. Verify: the consumer's capture, convergence, passage search and verification pass with its service transport setting on, and its MCP transport still passes with the setting off.

## 3. Later stages

- [ ] 3.1 Open a successor change that carries S2 to S5 from this design. Each stage writes its requirements and deltas there before its code starts. Verify: `openspec validate --all --strict` passes with the successor change.

## 4. Closure

- [ ] 4.1 Close this change after Stages 0 and 1 ship and the successor change exists. Synchronize this delta into the canonical specs and archive it with `openspec archive`. Verify: `openspec validate --all --strict` passes before and after the archive.
