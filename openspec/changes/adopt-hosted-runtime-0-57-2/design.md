## Context

The Hosted platform currently selects Exomem `0.54.1` through deployment-lock v2. Exomem `0.57.2` is the newest stable release and has a successful release workflow, an immutable signed Hosted runtime candidate, and an amd64 OCI image. Its `hosted-alpha-agent-v1` command membership and command-surface fingerprint are unchanged from `0.54.1`; the contract digest changes only because `ask_memory` and `capture_source` gained additive optional fields.

Runtime selection, contract trust, and client promotion live in different planes:

- the Exomem deployment lock selects the image used to create a cell;
- Substrate's trusted-release fixtures and release-pinned production sites decide which runtime contract may be assigned and promoted;
- the reviewer harness creates the time-bounded evidence that promotes a candidate into the live client cohort.

Promotion alone does not upgrade a runtime, and changing the deployment lock does not change an existing cell. A production experiment has also shown that a direct Helm image change can preserve vault bytes, but the control plane cannot treat an out-of-band image change as trusted release identity. Existing-cell rollforward therefore remains a separate lifecycle change.

The current operator OAuth-client partition is full. Substrate can safely upsert an already stored pinned client ID without consuming another slot, but only when the existing record has the same configuration and has never been authorized for a reviewer bootstrap. The harness currently always generates a new ID and cannot use that safe server path.

## Goals / Non-Goals

**Goals:**

- Select the exact signed `0.57.2` Hosted runtime for future cells while retaining the v1 product profile and deployment-lock v2.
- Keep `0.54.1` as a mixed-version and rollback identity.
- Make existing-tenant non-impact an enforced release-adoption invariant.
- Adopt the matching contract in every Substrate release-pinned site before composing the Exomem lock that names the reviewed consumer commit.
- Let the promotion harness explicitly reuse one known eligible pinned loopback client without weakening its fail-closed behavior.
- Prove the deployed runtime and both supported client paths end to end before inviting the alpha cohort.

**Non-Goals:**

- Enabling `hosted-alpha-agent-v2` or v3, Records lifecycle actions, or deployment-lock v3.
- Implementing in-place or blue-green rollforward for existing tenant cells.
- Automatically migrating, restarting, replacing, destroying, restoring, or re-provisioning an existing cell.
- Reclaiming arbitrary OAuth clients, widening the 32-client operator partition, or reusing an already authorized bootstrap client.
- Automating human browser consent or removing the reviewer authority's 30-minute security bound.
- Inviting external alpha users before promotion and personal-account acceptance are green.

## Decisions

### 1. Adopt the exact `0.57.2` v1 release, not a moving `main` image

The release identity is the `v0.57.2` tag commit plus the immutable runtime image digest and signed runtime-candidate bytes published by the successful release workflow. Substrate fixtures and Exomem lock evidence are derived from that exact revision. The v1 command set remains frozen; additive optional fields legitimately change the schema and compatibility digests.

Alternatives rejected:

- A runtime-only image bump would leave Substrate assigning and promoting the `0.54.1` contract while new cells serve `0.57.2`.
- A mutable tag would make later verification incapable of proving which bytes were deployed.
- Moving directly to the Records/v2 profile and lock-v3 would require a separately signed reader-v2 rollback candidate that the published `0.57.2` release does not contain.

### 2. Release adoption changes only the birth runtime of future cells

Applying the new platform lock MUST NOT enqueue a lifecycle operation for an existing tenant or modify any tenant namespace, StatefulSet, PVC/PV, vault, state root, binding, credential state, entitlement, OAuth grant, or routing assignment. Under the expand lock, `0.54.1` remains in the legacy catalog and any pre-existing cell continues on its assigned image and contract.

The contract lock may be selected only after a control-plane census proves there are no routable legacy cells. If a legacy cell exists, the rollout stays in expand mode. A later existing-cell upgrade must use the separate rollforward contract: operator-authorized target, fenced lifecycle operation, preservation proof, and runtime observation before identity changes.

This is stronger than relying on a successful in-place Helm experiment. It makes the normal release-adoption path structurally incapable of touching tenant data.

### 3. Sequence the two repositories around reviewed contract authority

The Substrate companion change lands first. It imports the exact `0.57.2` v1 fixture and updates the full release-pinned set: trusted contract store, bootstrap/operator controls, lifecycle release mapping, canary mapping, admin contract catalog, gateway fixture/catalog, integration fixtures, and runbook assertions. `0.54.1` remains trusted as legacy.

After that consumer commit is reviewed, Exomem composes a new deployment-lock v2 pair from:

- the published `0.57.2` runtime candidate and attestations;
- the existing provisioner candidate, because every declared provisioner and cell-chart source-closure path is byte-identical between `0.54.1` and `0.57.2`;
- the exact `0.57.2` v1 forward contract;
- `0.54.1` added to the authoritative legacy set;
- the reviewed Substrate v1 consumer commit and corpus proof.

The resulting expand and contract locks are verified independently. Production deploys Substrate before the expand lock so the control plane can recognize the runtime it is about to assign.

### 4. Reuse is explicit and server-revalidated

`reviewer_bootstrap.py prepare` gains an optional explicit bootstrap client ID. Without it, existing behavior remains: generate a fresh unique client ID. With it, the harness calculates the exact same pinned-client configuration for that ID and submits the ordinary `register_pinned` request for the new stage.

Substrate remains the authority. Its existing upsert accepts the stored ID only if the record is operator-managed, pinned to the same platform and configuration, and `reviewer_bootstrap_ever_authorized = false`; otherwise it returns no row and the harness fails before creating an invite or authority. The harness never auto-selects a redacted list entry and never enables the client directly.

The context file records whether the client ID was generated or explicitly reused, but secrets and raw control-plane credentials remain excluded. Tests cover successful reuse at partition capacity, mismatch, prior authorization, missing explicit ID, and unchanged fresh-client behavior.

### 5. Promotion remains a two-phase, fail-closed ceremony

The free phase performs release/candidate/digest, live-cohort, active-authority, capacity, stage, connector-document, and reusable-client checks. `prepare` attaches OpenAI locks, binds the staged Claude artifact, registers or safely rebinds the bootstrap client, creates the reviewer-purpose invite, and stops before authority creation.

Only when both browser clients and the human operator are ready does `run` spend the invite and start the bounded authority. It immediately completes OAuth, starts provisioning, and issues both sibling canary credentials without waiting for cell readiness. Clean-client evidence is then observed, signed, imported, and promoted before assignment expiry.

The final personal-account acceptance proves:

- the cell reports release `0.57.2` and the candidate's exact protocol/profile/contract identities;
- Claude and OpenAI can authorize through the promoted cohort;
- bootstrap and recall succeed;
- a governed write can be read back;
- refresh/reconnect succeeds without another invite;
- no unfinished operation, stale authority, stranded capacity claim, or unintended tenant remains.

Reviewer-purpose test tenants may be destroyed only through the explicit documented cleanup action after a failed ceremony. Ordinary tenant cleanup is never part of release adoption. The successful personal alpha tenant remains live.

## Risks / Trade-offs

- **[Cross-repository drift]** A lock composed before the Substrate consumer is reviewed could bind an untrusted or moving contract. **Mitigation:** land and pin the Substrate consumer first; derive every digest from exact artifacts and reject mismatches.
- **[Legacy tenant outage at contract cutover]** Contract mode would reject an old release. **Mitigation:** a live census is a hard gate; remain in expand while any legacy cell is routable.
- **[Latest runtime regression]** A signed stable image can still contain a behavioral defect. **Mitigation:** immutable image verification, focused contract tests, K3s runtime gate, fresh reviewer cell, both-client round-trip, and no automatic existing-cell rollout.
- **[OAuth partition exhaustion]** Generating another client fails at 32/32. **Mitigation:** explicit reuse of one server-eligible never-authorized pinned client; no client deletion or bound widening.
- **[Timed promotion failure]** Browser delay can expire the assignment after the invite is spent. **Mitigation:** resolve all free dependencies before `run`, keep the human at both tabs, issue sibling credentials immediately, and observe/import evidence against the actual expiry.
- **[Rollback after a real tenant exists]** Reverting the default lock cannot truthfully relabel or downgrade a created `0.57.2` cell. **Mitigation:** do not auto-destroy or mutate it; stop promotion/admission changes, keep it isolated and intact, and require a separately authorized recovery or rollforward decision.

## Migration Plan

1. Create and verify the Substrate `0.57.2` trusted-release companion change while retaining `0.54.1`.
2. Review and land the Substrate change; record its immutable consumer commit.
3. Compose and verify Exomem's `0.57.2` deployment-lock v2 expand/contract pair and evidence from signed candidates and the reviewed consumer.
4. Review and land the Exomem change.
5. Deploy Substrate and verify the candidate catalog, gateway mapping, bootstrap authority mapping, and zero legacy-contract nulls.
6. Deploy the Exomem expand lock. Verify platform readiness and prove that no lifecycle operation or tenant resource changed.
7. Query the routable-cell census. Apply contract mode only when the legacy count is zero; otherwise leave expand active.
8. Create the fresh `0.57.2` pending candidate and run the full free preflight.
9. Prepare using the explicitly reviewed reusable pinned client, then run the timed reviewer ceremony with the operator present.
10. Import both evidence chains, promote, and complete the personal-account acceptance matrix.
11. Keep the personal alpha tenant live and record final release/cell/capacity state.

Rollback before a tenant is created restores the previous `0.54.1` platform lock and expires/fails only the new candidate stages. After any non-reviewer tenant exists, rollback MUST preserve the cell and vault; it stops the rollout and requires a separate recovery decision rather than destroying or relabelling the tenant.

## Open Questions

None before implementation. The Records/v2 profile and existing-cell rollforward remain deliberately separate changes.
