## 1. Test Harness and Configuration Contract

- [x] 1.1 Add a focused pytest harness that invokes PowerShell with temporary host configuration and fake service, HTTP, Syncthing, Cloudflare, scheduled-task, filesystem, and clock adapters.
- [x] 1.2 Add failing tests for default-off behavior, required-field validation, host/tunnel identity conflicts, shared-configuration fingerprint mismatch, secret and identity redaction, human/JSON terminals, `Status`, and mutation-free `-WhatIf`.
- [x] 1.3 Implement the generic `scripts/cold-standby.ps1` command shell, validated external JSON configuration, dependency adapters, bounded journal schema, advisory desired-host state format, and redacted per-host identity manifest until the configuration/status tests pass.
- [x] 1.4 Add a generic example configuration with no personal hostnames, account identifiers, credentials, or vault paths.

## 2. Readiness and Safety Planning

- [x] 2.1 Add failing tests for local service/readiness checks including the non-secret host ID, repeated peer and stable-endpoint probes, named-tunnel availability, Syncthing idle/pending-byte state, peer completion, desired-host generations, shared-configuration parity, and unknown dependency handling.
- [x] 2.2 Implement bounded read-only probes and the pure transition planner so every refusal names the failed evidence and the next safe action.
- [x] 2.3 Add regression tests proving that a healthy peer, stale or unknown replication, conflicting intent, and ambiguous network evidence refuse before service or DNS mutation.

## 3. Activation, Compensation, and Disaster Override

- [x] 3.1 Add failing tests for the complete activation order: validate, converge and compare host manifests, probe, write intent, start local service, verify local readiness, switch stable DNS, verify the Cloudflare tunnel target, verify public readiness host identity/OAuth, then commit the journal terminal.
- [x] 3.2 Implement normal `Activate` and `Activate -IfUnserved` using the tested planner and injected mutation adapters.
- [x] 3.3 Add failing tests for failures before routing, failures after routing, a healthy response from the previous tunnel, known-route compensation, unknown-route recovery output, and idempotent replay after a completed or interrupted local journal. Prove interrupted replay completes only when the journal phase, Cloudflare tunnel target, and public host ID all agree.
- [x] 3.4 Implement compensation so failed activation restores the known previous route when possible, stops a newly started service, and reports a decisive rolled-back or operator-recovery terminal.
- [x] 3.5 Add tests and implementation for the two-part disaster override, allowed bypass set, non-bypassable local/routing checks, and explicit enumeration of every bypassed guard.

## 4. Service and Tunnel Configuration

- [x] 4.1 Add failing tests that `Configure` changes only explicitly selected cold-standby resources, sets Exomem demand-start on both roles, keeps laptop activation manual, and remains idempotent.
- [x] 4.2 Implement guarded Windows service startup configuration and the delayed desktop current-user logon task that calls the same activate-if-unserved transition after network availability.
- [x] 4.3 Add failing tests for distinct named-tunnel validation, stable-hostname ingress on both tunnels, health-only direct operational ingress, atomic config backup/write, and refusal to expose direct MCP/OAuth/REST/transfer paths.
- [x] 4.4 Implement tunnel-config preparation and stable DNS routing through `cloudflared tunnel route dns --overwrite-dns`, preserving existing unrelated ingress rules and producing recoverable backups.

## 5. Handoff

- [x] 5.1 Add failing tests for source and peer convergence and identity-parity gates, desired-target generation, confirmed delivery of that new generation, graceful local stop before route movement, bounded outage reporting, and the prohibition on remote service start.
- [x] 5.2 Add failing compensation tests for route or verification failure after source shutdown: restore/restart only when the target is conclusively inactive, otherwise keep the source stopped and emit exact recovery state.
- [x] 5.3 Implement `Handoff` and its compensation/recovery terminals using the same journal, Syncthing, service, and routing adapters.
- [x] 5.4 Add a desktop-to-laptop and laptop-to-desktop simulated round trip proving that only one fake Exomem service is active at every committed transition.

## 6. Operator Documentation

- [x] 6.1 Add `docs/runbooks/cold-standby.md` covering prerequisites, configuration and identity parity, normal status, laptop activation, desktop recovery, expected connector reconnect/reauthorization, disaster override, partial-transition recovery, and rollback.
- [x] 6.2 Update `docs/deployment.md` to compare single-host, manual cold standby, and automatic writer-lease HA without presenting one as universally preferred.
- [x] 6.3 Document the staged personal cutover: backup, tunnel hardening, dry runs, HA Worker route detachment, standalone desktop verification, stable DNS switch, HA environment removal, controlled failover/handoff, rollback window, and only then retirement of the personal Worker/Durable Object deployment.
- [x] 6.4 Explicitly document that Syncthing intent is advisory rather than consensus, network partitions normally refuse, forced recovery can lose or fork state, and unreplicated bytes are unrecoverable.

## 7. Verification and Delivery

- [x] 7.1 Run the focused cold-standby and service-installer tests with sandbox-safe temporary paths, plus Ruff on any Python test/support code.
- [x] 7.2 Run `openspec validate add-manual-cold-standby --strict` and the existing public-artifact/leak guards for all committed examples and documentation.
- [x] 7.3 Exercise `Status`, `Configure -WhatIf`, `Activate -WhatIf`, and `Handoff -WhatIf` against sanitized representative desktop and laptop configurations and preserve the command evidence in the PR.
- [x] 7.4 Independently review split-brain refusal, direct-origin exposure, secret handling, compensation, default-off behavior, and the promise that the existing automatic HA implementation remains unchanged.
