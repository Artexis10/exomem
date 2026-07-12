## 1. Runtime readiness contract

- [x] 1.1 Add pure readiness snapshot tests for standalone, healthy coordinated, and coordinator-unavailable replicas.
- [x] 1.2 Implement the content-free runtime compatibility/readiness snapshot.
- [x] 1.3 Expose `/health/ready` separately from unchanged `/health` liveness and cover authenticated-server routing.

## 2. Compatibility-aware HA edge

- [x] 2.1 Add Worker tests for readiness payload validation, expected replica identity, supported contract sets, and coordination requirements.
- [x] 2.2 Add Durable Object tests for admission bound to holder and fencing token, including invalidation on release/expiry/takeover.
- [x] 2.3 Gate active-holder and no-holder tool-call routing on readiness while preserving exact-once forwarding and safe non-tool fallback.
- [x] 2.4 Reuse compatible admission on the steady-state lease generation without repeated readiness probes.

## 3. HA operator diagnostics

- [x] 3.1 Add doctor tests for offline HA configuration checks and explicit cross-replica probes.
- [x] 3.2 Implement `doctor --profile ha`, repeatable `--replica-url`, compatibility comparison, and release-drift warnings.
- [x] 3.3 Document expand-roll-contract deployment, runtime parity verification, and the boundary between Exomem readiness and deployment-owned updates.

## 4. Verification and rollout

- [x] 4.1 Run focused Python, Worker, Ruff, and strict OpenSpec validation.
- [x] 4.2 Run the broader CI-relevant test set and package checks.
- [ ] 4.3 Upgrade both live replicas to the compatible release/build before enabling Worker enforcement.
- [ ] 4.4 Deploy the Worker, prove steady-state admission reuse and incompatible-replica fail-closed behavior, then verify a live MCP read and governed write.
