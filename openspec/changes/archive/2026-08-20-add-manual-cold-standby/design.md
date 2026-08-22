## Context

Exomem already supports two different ends of the deployment spectrum: a standalone host that accepts downtime and a continuously available two-replica topology using a writer lease, shared OAuth state, and a Cloudflare Worker/Durable Object edge. The requested middle ground is a normally single-host Windows deployment with a deliberately activated laptop copy, one unchanged MCP URL, and no custom always-on HA control plane.

The vault remains externally replicated by Syncthing. Syncthing can report convergence but is not distributed consensus, so no script can prove that an unreachable peer is powered off rather than partitioned. The design therefore automates normal ordering and refuses ambiguous promotion; a deliberate disaster override remains an operator decision.

Each host already has its own Cloudflare named tunnel and operational hostname. The stable public hostname, GitHub OAuth callback, `EXOMEM_BASE_URL`, GitHub identity, and JWT signing key remain identical across hosts. Host-specific configuration and credentials must remain outside the repository.

## Goals / Non-Goals

**Goals:**

- Keep one stable MCP URL and one connector definition across desktop and laptop operation.
- Keep exactly one Exomem service active during normal operation and planned handoff.
- Encode configuration, status, activation, handoff, rollback, and refusal behavior in a repository-owned command.
- Require observable replication, peer, runtime, and routing evidence before exposing a host.
- Make the laptop demand-start and let the desktop recover its primary role through a guarded startup action rather than an unconditional auto-start.
- Remove the personal deployment's Worker, Durable Object, writer-lease, and shared OAuth availability dependencies only after the direct path is proven.
- Preserve the existing automatic HA product path unchanged for operators who choose it.

**Non-Goals:**

- Automatic zero-downtime failover, remote power control, or transparent failback.
- Distributed consensus, offline multi-writer merging, CRDT conversion, or claiming that a Syncthing marker is a lock.
- Recovering bytes that never replicated off a failed host.
- Eliminating the existing multi-host writer-lease or Cloudflare HA implementation from the repository.
- Making the first implementation cross-platform; the operator path targets the existing NSSM/cloudflared Windows deployment.

## Decisions

### Use distinct named tunnels and switch only the stable DNS route

Both per-host `cloudflared` connectors may remain running because they terminate distinct named tunnels. Each tunnel config accepts the stable hostname, but `cloudflared tunnel route dns --overwrite-dns <target-tunnel> <stable-hostname>` maps that hostname to exactly one tunnel at a time. The direct operational hostname exposes only bounded health/readiness paths; it must not expose `/mcp` or other mutation-capable routes once the writer lease is removed.

This is preferred over copying one named-tunnel credential to both machines: two simultaneous connectors for one named tunnel form a Cloudflare replica set and can distribute requests nondeterministically. It is also preferred over keeping the HA Worker merely as a manual router because the Worker, Durable Object, lease authority, and shared OAuth store would remain write-path dependencies. Separate connector URLs were rejected because they would require multiple MCP definitions and OAuth registrations.

### Put the operating state machine in one generic PowerShell entrypoint

Add `scripts/cold-standby.ps1` with `Configure`, `Status`, `Activate`, and `Handoff` actions plus `-WhatIf`. The same script runs on either host and loads a validated per-host JSON configuration from a user-owned location outside Git. Configuration names the local role and host ID, local and peer health URLs, stable hostname, local and peer tunnel names, Windows service names, Syncthing endpoint/folder/device identifiers, and an advisory state-file path. Secrets remain in the existing Cloudflare and Syncthing credential stores and are never printed or committed.

`Status` is strictly read-only and returns both human-readable and JSON views of service state, local and peer health, stable-endpoint health, Syncthing convergence, desired-host intent, tunnel availability, and the next safe action. `-WhatIf` evaluates the same preconditions and prints the exact action plan without changing services, files, tunnel configuration, or DNS.

The command records a bounded local operation journal and an advisory desired-host marker in a configured Syncthing-replicated location outside `Knowledge Base/`. The marker reduces accidental restart overlap and carries a monotonically increasing generation, but it is never described or used as distributed authority. Each host also publishes a redacted deployment-identity manifest containing its host ID and fingerprints of the shared base-URL/OAuth/identity/signing-key contract. Peer health, identity parity, and convergence checks remain mandatory; raw credentials and identity values are never copied into the manifest.

Each Exomem origin exposes its configured non-secret host ID in the bounded readiness response. Public verification requires both the expected stable OAuth metadata and the expected host ID, so a healthy response from stale DNS, the prior tunnel, or the wrong origin cannot complete a transition.

### Make Exomem demand-start on both hosts and guard desktop boot recovery

`Configure` sets the Exomem service to demand-start on both hosts. The laptop never activates automatically. On the desktop, `Configure` installs a current-user scheduled task triggered at logon, with a configurable delay, that invokes a guarded `Activate -IfUnserved` after network availability. The guard waits for local Syncthing convergence, reads the replicated desired-host intent, and probes both the stable and laptop operational health endpoints before starting Exomem. Any evidence that the laptop is active, or any ambiguous/unavailable prerequisite, leaves the desktop service stopped.

The distinct `cloudflared` services may remain automatic because an inactive host's direct tunnel is health-only and the stable DNS record selects only one tunnel. This removes the fragile requirement that a person remember to stop one tunnel connector before starting another.

### Promotion and handoff are fail-closed ordered transitions

`Activate` performs these phases:

1. Validate configuration, local role, service state, Cloudflare command access, and Syncthing API access.
2. Require the local folder to be idle with no pending items/bytes and, when a peer device is configured, require peer completion evidence.
3. Probe the peer and stable endpoints repeatedly over a bounded window. A healthy peer refuses activation. An unreachable peer with conflicting desired-host intent also refuses unless disaster override is explicit.
4. Write the next desired-host generation, start local Exomem, and require local `/health/ready` before changing public routing.
5. Point the stable hostname at the local named tunnel, verify the Cloudflare DNS record targets that exact tunnel, then poll the public readiness and OAuth discovery endpoints until they report the expected local host ID and stable base URL.
6. Commit the journal terminal. On a pre-route failure, stop the newly started service. On a post-route verification failure, restore the prior route when known, stop the local service, and report the incomplete/rolled-back state.

A fresh activation always refuses a pre-existing healthy stable endpoint. Replay of an interrupted local journal is a separate recovery path: it may reconcile and complete only when the journal proves the expected phase and previous route, Cloudflare reports the stable record on the local tunnel, and the public readiness response reports the local host ID. Any mismatch enters compensation or operator recovery instead of being treated as a new activation.

`Handoff` runs on the active source host. It first requires local and peer Syncthing completion, writes intent for the target, then waits until Syncthing confirms that the new intent generation reached the peer. Only then does it stop local Exomem gracefully, move the stable route to the target tunnel, and leave an explicit bounded outage until the target's guarded activation succeeds. It never starts a remote Windows service. The target can activate through its logon task or the same local command.

The handoff journal records the prior route and whether the source was running. If route movement or verification fails after the source stops, compensation first restores the prior route and proves the target is still inactive before restarting and verifying the source. If target inactivity, route restoration, or source readiness cannot be proven, the source stays stopped and the terminal reports the exact operator recovery state; compensation never risks starting both services.

### Keep disaster override explicit and narrow

Normal actions never steal authority or bypass stale-copy checks. A forced activation requires both a force switch and a literal data-loss/split-brain acknowledgement. It may bypass peer-unreachable or Syncthing-completion evidence, but it cannot bypass invalid configuration, local runtime readiness, failed DNS mutation, or failed public verification. The terminal output must name every bypassed guard.

### Treat failover authentication as reconnectable, not shared

Both hosts retain the same stable base URL, GitHub OAuth application, allowed GitHub identity, and JWT signing key. After shared OAuth storage is removed, session and dynamic-client records are local to each host; the existing connector may require a reconnect/registration handshake and GitHub reauthorization after a host switch. The URL and MCP definition do not change, and the operator command must distinguish this expected reconnect flow from a routing failure.

## Risks / Trade-offs

- **Network partition can look like peer shutdown** -> Default promotion also checks replicated intent, stable routing, repeated peer probes, and Syncthing evidence, then refuses conflicting or incomplete state. No documentation claims this equals consensus; disaster override is explicit.
- **Latest writes may not have replicated before a crash** -> Normal activation requires convergence evidence. If the source died before convergence, the runbook states that recovery can only use the newest bytes present locally.
- **DNS cutover or edge propagation can partially succeed** -> Activation journals the previous target, polls the public path, and compensates to the previous route when known. The service is stopped if the new public path cannot be proven.
- **A healthy public probe can come from the prior tunnel** -> Cloudflare route inspection and the readiness host ID must both identify the intended tunnel/origin before a transition commits.
- **Shared authentication configuration can drift between hosts** -> A replicated redacted parity manifest fingerprints the effective shared contract; activation and handoff fail before mutation on any mismatch.
- **Handoff can fail after stopping the source** -> Restore the prior route and restart the source only after proving the target inactive; otherwise preserve the single-service invariant and require explicit recovery.
- **Desktop reboot could overlap an active laptop** -> Exomem itself is demand-start; the desktop scheduled task fails closed when the stable endpoint, laptop health endpoint, desired-host marker, or sync state indicates standby activity or ambiguity.
- **Direct operational hostnames become an alternate write path** -> Tunnel ingress exposes health/readiness only on direct hostnames; MCP, OAuth, REST mutation, and transfer paths are served only through the selected stable hostname.
- **Local OAuth sessions do not follow DNS** -> Keep the connector definition, allow one expected reauthorization after a switch, and document that repeated reauthorization indicates a real configuration problem.
- **The operator depends on Cloudflare and Syncthing CLIs/APIs** -> `Configure` validates required access without printing secrets, `Status` reports actionable remediation, and the capability remains default-off.

## Migration Plan

1. Ship the script, example configuration, tests, and runbook without changing any installed service or public route.
2. Back up both `.env` files, both tunnel configs, the current Worker/Durable Object configuration, and service startup settings. Confirm both vault replicas are up to date and on compatible Exomem releases.
3. Configure each distinct tunnel to accept the stable hostname while restricting its direct operational hostname to health/readiness. Install the cold-standby config on both hosts; keep the HA edge live during this preparation.
4. Exercise `Status` and `-WhatIf` on both hosts, then test each direct tunnel through a temporary hostname without changing the connector.
5. Quiesce connector writes. Stop the laptop Exomem service. Remove `EXOMEM_WRITER_LEASE_*` and `EXOMEM_OAUTH_STORAGE_*` from both effective service environments while preserving the stable base URL, GitHub OAuth settings, JWT signing key, and vault paths.
6. Start the desktop in standalone mode, detach the HA Worker's route binding from the stable hostname, point the stable DNS record directly at the desktop tunnel, and verify OAuth discovery, connector reconnect/authorization, authenticated read, and one governed write through the existing MCP definition.
7. Configure both Exomem services for demand-start and enable the guarded desktop startup task. Run a controlled desktop-to-laptop activation and laptop-to-desktop handoff, including Syncthing convergence and connector reauthorization.
8. Keep the Worker/Durable Object deployment intact but with its stable-hostname route binding detached for a bounded rollback window. After the window, remove the personal deployment and its secrets; do not delete the repository's HA implementation.

Rollback stops the standalone service, restores the backed-up HA environment and tunnel configs, restores the Worker's stable-hostname route binding and DNS path, starts both replicas, and runs the existing HA doctor/readiness gates before reopening writes.

## Open Questions

No product-level questions remain. Probe counts, timeouts, and scheduled-task delay are implementation defaults that must be configurable and covered by deterministic tests.
