# Tasks

## 1. Access and exact ownership

- [ ] 1.1 Reconcile control-host SSH recovery with its canonical private Ansible input, preserving managed Bitwarden identity and host pin; verify key-only login and the guest/provider allowlist receipts.
- [ ] 1.2 Resolve the intended NetBird Cloud account, Free plan and secure API/enrollment bindings; inventory company and owner phone/workstation/Moshi VPN, SSH and service dependencies plus exact shared foundation/durability resource addresses, provider IDs, consumers and recovery contracts. Verify a private manifest against live read-only state and the two source workspaces.

## 2. Shared source and administration

- [x] 2.1 Create `substrate-systems/substrate-infra` with proportional OpenSpec contracts, extracted pinned management roots and guarded `substrate-control-foundation` / `substrate-control-durability` workspaces; verify state locking, version history, explicit execution, no automatic apply and no global state sharing before initialization.
- [x] 2.2 Extract PostgreSQL/TLS/backup configuration and one shared base-role dependency, preserving source contracts and encrypted custody versions; preserve the exact backup bucket name independently of the source shared random suffix, retire application-key secret-output dependencies in favour of existing BWS/SOPS custody, and verify isolated convergence without credential rotation.
- [x] 2.3 Replace the unshipped Tailscale role with the shared pinned NetBird role and restricted peer-group administration policy, correcting the company account's default allow-all policy. Verify exact account/peer enrollment, effective configuration, authorised managed SSH, unauthorised-port denial and reboot recovery without subnet advertisement, enrollment-side DNS changes or provider SSH interception. Prove desktop coexistence and mobile tunnel switching during migration; supply opt-in mobile DNS separately and verify replacement phone/workstation/Moshi service paths under separate groups. Retire Tailscale dependencies only after those replacements pass.
- [x] 2.4 Make base-role validation transport-aware and explicitly remove obsolete managed UFW allowances while preserving unrelated rules; adopt control-host tailnet administration and update the managed profile through its source deployment path, verifying console recovery and allowed/denied access before restriction plus effective guest/provider ingress and a fresh managed connection afterward.
- [x] 2.5 Publish the versioned non-secret dependency contract and update Exomem to consume it; verify incompatible/missing versions refuse deployment and selected endpoints/transport remain identical without target state sharing or shared apply permission.

Task 2.3 adoption checkpoint: the shared NetBird source is merged in Substrate-infra #4/#5; Exomem, Q, TU/n8n and control managed SSH worked from both workstations at adoption. Yadm #568 completes the source-managed peer wiring. Substrate-infra #6 adds phone-only DNS; #7 converges company workstation instances to IPv4 and clears the pinned client's stale SOCKS listener by restarting only their user services. Fresh authenticated SSH works in both directions after that correction, and independent verification preserves desktop POLLY's enrollment, process and package. The owner Android peer is identity-verified in `owner_devices` and the separate DNS group. Desktop Moshi connects. The laptop error 111 was the saved Moshi target: the native peer did not serve port 2222. The owner confirmed laptop SSH after selecting the isolated company endpoint. Terraform now excludes that native peer from owner services; the phone sees only the two serving endpoints. Substrate-infra #8 imports the two existing serving peer IDs and gives them endpoint-specific names, `laptop-wsl` and `desktop-wsl`, without changing enrollment, flags or access grants; its post-apply plan is empty. Reserve separate Windows names for separately enrolled and verified Windows services. The operator confirmed named phone SSH to both workstation endpoints on 2026-10-04. Disposable reboot recovery passed on 2026-10-05: a cx23 enrolled through the shared role, with public inbound closed at both the provider firewall and UFW, resumed managed NetBird SSH after a soft reboot (12.5 s) and a hard reset (35 s), stayed publicly unreachable and could request a provider console; every resource was deleted afterwards. Tailscale stays on owner devices as the operator's chosen fallback rather than being retired, so task 2.3 is complete. tu-n8n's public SSH UFW rules were removed the same day, and its bootstrap no longer reopens them.

## 3. Recovery and state-only handover

Desktop and future laptop coexistence in task 2.3 requires concurrent POLLY and Substrate access, not profile switching. Prove the isolated user-owned netstack arrangement in design decision 9 before adopting either workstation; a successful laptop native-client enrollment alone does not close that requirement.

- [x] 3.1 Extend the existing inspector with state-only plans and manifest-bound snapshot comparison. Rehearse native forward, inverse and interrupted state transfer in explicit local replicas with pinned providers and read-only provider access; prove preserved IDs, resource contents, independent BWS/SOPS recovery and no duplicate active owners.
- [x] 3.2 Restore a selected recent backup into an isolated environment with production consumers and external effects blocked; verify lineage, selected integrity checks and retained encryption/retention contracts without exposing data.
- [x] 3.3 Freeze Terraform and Ansible source/target writers in both lifecycle domains. Retain private state-version IDs and manifests; independently review exact snapshot hashes, configuration revisions and saved state-only plans. Verify workspace identities immediately before each push.
- [x] 3.4 Transfer foundation and durability ownership one domain at a time with native moves and source-before-target pushes. Reconcile remote state after each checkpoint; prove one target owner per ID and no source ownership. Keep writers frozen until source retirement and live checks pass.

## 4. Integrated closure

Task 3.1 local recovery checkpoint (2026-10-08): an independent verifier ran
55 native Terraform commands in explicit local backends. Fresh pulls proved
source removal, the interrupted ownership gap, target adoption, and the inverse
source adoption for all six resources. Full resource attributes, private state,
sensitive markings, unrelated resources, and backend lineages remained intact.
No forced push, manual state edit, provider installation, credential, or live
backend was used. Private evidence: `local-push-recovery/receipt.json` and its
command log. Production backend acceptance remains pending.

Task 3.2 recovery checkpoint (2026-10-08): the independently reviewed probe restored
`20261008-030313F` with nine selected WAL segments. Its scratch PostgreSQL instance
used a separate network namespace, no network listener, no archiving or replication,
and remapped tablespaces. Cluster lineage matched, recovery completed, and
`pg_amcheck --all --heapallindexed --parent-check` passed. Production PostgreSQL
and PgBouncer process IDs stayed unchanged. The owned unit stopped and scratch
was removed. Private receipt: `restore-retry-receipt.json` in the operator's
protected handover evidence. The earlier attempt rejected an unsupported flag
before restoring data; its cleanup passed. Ownership transfer remains pending.

Shared ownership checkpoint for tasks 2.1, 2.2 and 2.5 (2026-10-08):
Substrate-infra #12 (merge `496676d`) and Exomem #1629 (squash `57b432e`)
merged with independent approvals.
- 2.1: the HCP bootstrap guard verified both `substrate-control-*` workspaces
  before their first state version: local execution, no automatic apply, no
  VCS, no remote-state sharing, Terraform 1.15.8, and lock and history access.
- 2.2: the extracted roles converged in an isolated PostgreSQL and MinIO test
  without credential rotation, and the eight SOPS ciphertexts are unchanged.
  The backup bucket name now comes from an explicit input, and Exomem no
  longer outputs the pgBackRest application key.
- 2.5: Exomem consumes the typed `shared_control` dependency, and a mocked
  test refuses an incompatible schema version. The selected endpoint, ports
  and `verify-full` transport are unchanged.

Live handover checkpoint for tasks 3.3, 3.4, 4.1 and 4.2 (2026-10-08):
1. The operator froze all four HCP workspaces before the merges.
2. Native moves from fresh frozen pulls produced the candidates. An
   independent reviewer approved the candidates and the push driver.
3. Pushes ran one domain at a time, source removal before target adoption.
   Each pushed state matched its reviewed candidate.
4. Reviewed reconciliation plans updated outputs and refreshed resource
   metadata. No provider resource changed.
5. Fresh ordinary plans in all four workspaces reported no changes.
6. Final HCP reads gave each of the six provider IDs exactly one owner, in
   the `substrate-control-foundation` or `substrate-control-durability`
   workspace, and none in Exomem.

Live checks against the pre-handover baselines:
- PostgreSQL and PgBouncer PIDs, the certificate, roles and the system
  identifier were unchanged. The host probe ran over managed NetBird SSH.
- WAL archiving advanced, and the read-only verify-full application
  connection was unchanged.
- DNS, the address and the private attachment were unchanged.
- The long-running Exomem Cloud pods kept their identities and restart
  counts; only scheduled CronJob pods rotated.

An independent verifier accepted the evidence before the operator lifted the
freeze, and listed the items it could not check itself. The private receipts
are under the operator's protected handover evidence. Physical project
relocation (section 5) remains open.

- [x] 4.1 Independently verify preserved DNS/IP/private attachment, managed tailnet SSH, database TLS/role boundaries, backup/WAL continuity and representative Substrate/Exomem consumer health; keep any failed check explicitly open.
- [x] 4.2 Retire transferred product management code and old control-node targeting, deliver repository dependency updates, and verify no recreation/configuration paths from actual merged delivery revisions before releasing writer freezes; move shared contracts into their owning repository through strict OpenSpec closure before archiving this change.
- [ ] 4.3 Inventory the remaining organisation-owned server consumers and deliver the shared administration standard in governed rollout batches; verify each host's authorised/denied connectivity and recovery receipt before recording adoption, excluding client systems without their own authority.

Task 2.4/4.3 cutover checkpoint (2026-10-05): all four known organisation servers now drop public SSH and is administered over the company NetBird. Exomem #1588 closed alpha and the control host: `site.yml --tags admin_ssh` over NetBird retired the managed rules, the operator removed the remaining uncommented public 22 rules, and a targeted foundation plan removed only the SSH rule from the alpha, control and agent firewalls. Q #1032 did the same for q-k3s-01 through its own IaC, and tu-n8n's public UFW rules were removed by hand with its bootstrap fixed in tu-n8n #54. Fresh managed NetBird SSH works on all four, and public 22 times out. Console recovery was proven before the restriction on a disposable server in the Exomem Hetzner project; the per-host console requests for alpha, control and q-k3s-01 succeeded after the cutover. The operator's foundation input now carries an empty `admin_ssh_cidrs`, so a routine plan keeps SSH closed. A read-only listing of every server in the Exomem and Q Hetzner projects on 2026-10-06 found three (exomem-alpha-01, exomem-control-db-01, q-k3s-01), all adopted and with no provider SSH rule. Still open under 4.3: tu-n8n's Hetzner project, which can't be listed or console-checked because its credential is not in operator custody. The #1513 control-host rename is not applied live: an untargeted foundation plan renames the server, firewall and primary IP and recomputes the primary IP's assignment, so it needs its own reviewed plan.

## 5. Project relocation (decision 10; after 3.4, 4.1 and 4.2)

- [x] 5.1 Rename the Exomem Hetzner project to Exomem Cloud and create the Substrate project with a scoped API token in BWS custody (owner console actions). Evidence: project names and the token's custody receipt, no secret values. Done: the Hetzner projects Exomem Cloud and Substrate exist. The Substrate token is BWS secret `608a4778…` (`SUBSTRATE_HCLOUD_CONTROL_RW_TOKEN`), bound as `control-hcloud` in substrate-infra `infra/contracts/bws-shared-v1.json`. Receipts (private `relocation-2026-10-09/`): `hetzner-final-state.json`.
- [x] 5.2 Rehearse the transfer on disposable servers with a Primary IP, a firewall and a private-network attachment: detach, transfer, reattach or replace the address, then the rollback. Record whether the Primary IP and firewall move, the outage length and the step order. Delete the disposable resources. Done (2026-10-08/09): the server ID, Primary IP ID and address survive the transfer; the firewall does not. The Console refuses to transfer a delete-protected Primary IP, and the IP must be reassigned by API while the server is off. The forward outage was 611 s. The return leg restored the original firewall, and cleanup left both projects empty. Receipts (private `relocation-2026-10-09/`): `rehearsal/adopt.json`, `rehearsal/findings.json`, `rehearsal/restore.json`, `rehearsal/cleanup.json`.
- [x] 5.3 Amend the cloud-cell requirement and scenarios that keep the gateway and cellctl roles private-only to decision 10's property, and list `cloud-cell` in the proposal's Modified Capabilities. The requirement exists only in the active change `adopt-exomem-cloud-plain-cells`, so the amendment is made in place there. A MODIFIED delta here could not archive before that change, and the two changes would state the requirement differently until then. Extend the dependency-contract requirement to the consumer-published /32 through substrate-infra's change `admit-consumer-database-clients`. Publish the version that carries it: foundation `fleet_dependency` schema 2 with `database_client_ipv4_cidr`. Open until both repositories merge and a foundation apply records the schema 2 output. Done: Exomem #1634 and substrate-infra #14 merged, and the foundation apply on 2026-10-09 published `fleet_dependency` schema 2. The foundation apply after #1636 published schema 3. Receipts (private `relocation-2026-10-09/`): `exomem-foundation-schema2-apply.log`, `f-apply.log`.
- [x] 5.4 Admit the new path on the database before any consumer moves: `listen_addresses = '*'`, UFW and pg_hba for the two Exomem roles from the server node's /32 only, and a Hetzner firewall rule for 5432 from that /32. The listener change restarts PostgreSQL, so announce it as a short window. Verify that another source is refused. Done (2026-10-08): collection 0.2.0 and the control-foundation 5432 rule are live. A non-allowlisted source gets no connection on 5432, and 6432 stays open. Receipts (private `relocation-2026-10-09/`): `../receipt-5.4.json`, `external-checks.txt`.
- [x] 5.5 Move Exomem's consumers: pin cellctl and the gateway to the server node, empty the `cloudDatabase` host alias, and set their NetworkPolicy egress to the database's public /32. If 5.2 shows the address changes, create the target Primary IP in the Substrate project and stage its /32 too. Verify cellctl passes and gateway requests over `verify-full`. Done: platform Helm revision 82 pins cellctl and the gateway, empties the alias and adds 167.233.57.60/32 database egress. Both roles connect from 178.105.120.141. Receipts (private `relocation-2026-10-09/`): `helm-rev82-apply.json`, `host-checks.txt`, `gateway-sessions-after-move.txt`.
- [x] 5.6 In an announced window, freeze both repositories' writers, transfer the control host, and restore its public address and firewall in the Substrate project. If the address changed, import the target Primary IP into `substrate-infra` under the new project token first, then change DNS through its owning Terraform. Evidence: Substrate auth and billing health, cellctl passes, gateway requests, and backup/WAL continuity. Done (2026-10-09): `pgbackrest check` passed and PostgreSQL stopped cleanly at 06:45:19Z. After the transfer and a Substrate-token firewall apply, PostgreSQL served again at 07:32:25Z, after an outage of 2,826 s that was mostly the Console transfer. `verify-full` TLS passes. cellctl's LISTEN session and the gateway's requests come from the /32. A Substrate control API read succeeded through PgBouncer and stands in for the auth and billing check. WAL archiving advances, and `pgbackrest check` passes. Deviation: the orchestrator took no HCP lock and sent no wider announcement, because it was the only infrastructure writer, the workflows are dispatch-only, and the owner is the database's only user. Receipts (private `relocation-2026-10-09/`): `stop.json`, `adopt.json`, `start.json`, `host-checks.txt`, `external-checks.txt`, `gateway-sessions-after-move.txt`.
- [x] 5.7 Before releasing the freeze, refresh the moved resources in the `substrate-infra` workspaces under the new project token; their IDs survive the transfer, so nothing is imported. Retire the old firewall (the Primary IP moves with the server), and drop the private /32 from the NetworkPolicies. Remove the private attachment and every remaining private-address reference: foundation `reserved_private_ips`, the postgres role's assert and private listener, the inventory generator, the migration runbook, and the reserved control-database address in the cloud-node-pool requirement, the cloud-cell requirement's transitional private-network clause, and the chart's private `cloudDatabase` alias. The "Control database on the same subnet" scenario stays: it forbids inter-node rules naming that address, which remains true, and OpenSpec refuses to drop a scenario through MODIFIED. The canonical cloud-node-pool reservation clause changes when `add-cloud-service-resource-policy` archives with its in-place edit. Correct the substrate-infra comments in `pgbouncer.ini.j2` and `pgbouncer_hba.conf.j2` that still say the Exomem roles use only the private network; editing them restarts PgBouncer, so they wait for this window. Verify ordinary plans in both repositories show no changes. Done: substrate-infra #15 and Exomem #1636 merged. Collection 0.3.0 removed the private pg_hba lines and UFW rule. The network detached in place. Helm revision 83 limits database egress to the public /32. The old firewall is deleted (404). Ordinary plans in all four workspaces show no changes. The server, firewall and guest hostname are renamed `substrate-control`, and the Primary IP `substrate-control-ipv4`. Receipts (private `relocation-2026-10-09/`): `detach-apply.log`, `adopt-apply.log`, `helm-rev83-apply.json`, `hetzner-final-state.json`, `final-*.log`, `rename-apply.log`.
