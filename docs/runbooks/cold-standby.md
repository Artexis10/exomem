# Manual cold standby (Windows)

Manual cold standby is the middle option between accepting one-host downtime and
operating automatic writer-lease HA. The desktop is normally the active host.
The laptop is a recoverable, manually activated copy. This profile preserves one
stable MCP URL and one connector definition, but it deliberately does **not**
provide automatic failover or distributed consensus.

Use this runbook only after installing the opt-in cold-standby tooling. A normal
Exomem install or upgrade does not change service start mode, Cloudflare routing,
OAuth, Syncthing, or any existing HA deployment.

For a neutral comparison with single-host and automatic writer-lease HA, see
[deployment options](../deployment.md#deploying-on-a-second-machine-multi-host).

## What is active

There are two distinct Cloudflare named tunnels, one per host. Both `cloudflared`
connectors may stay running. The stable hostname is a CNAME to exactly one tunnel
at a time; switching it uses:

```powershell
cloudflared tunnel route dns --overwrite-dns <target-tunnel> <stable-hostname>
```

That is not the same thing as starting two connectors for one tunnel. Do not copy
one tunnel credential to both computers: Cloudflare can treat them as replicas and
send requests to either origin.

Each host also has a direct operational hostname. Its tunnel ingress must expose
only bounded health/readiness endpoints, such as `/health/ready`. It must not
forward `/mcp`, OAuth, REST mutation, artifact transfer, or other write-capable
paths. The stable hostname is the only public MCP/OAuth endpoint.

The `scripts/cold-standby.ps1` command records a bounded local journal and a
replicated desired-host marker. The marker is helpful evidence, not a lock or a
claim of authority. Syncthing is replication, not consensus.

## Prerequisites

Before enabling the profile, have all of the following in place:

- Two Windows hosts with independently installed, compatible Exomem releases and
  a replicated vault. Keep the desktop as the intended normal primary and the
  laptop as standby.
- Syncthing working between the hosts. Its REST API key must be available through
  the environment-variable name configured for that host, and the target folder
  must be fully idle with no pending files or bytes before a normal transition.
- Two **distinct** Cloudflare named tunnels and two direct operational hostnames.
  The signed-in `cloudflared` identity on each host must be permitted to change
  the stable DNS route. A scoped Cloudflare API token with DNS-read access and the
  zone ID must be available through the configured environment-variable names so
  the command can independently verify the exact CNAME target. See Cloudflare's
  [tunnel DNS routing documentation](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/dns/).
- One stable public hostname, for example `https://kb.example.com`. It remains
  the value of `EXOMEM_BASE_URL` and the existing connector URL on both hosts.
- A local Exomem readiness response configured with a public-safe,
  non-secret `EXOMEM_INSTANCE_ID` unique to each host. It is the identity used to
  prove the public hostname reached the intended origin.
- A host-owned configuration JSON file outside the repository, plus a local
  journal path and a replicated desired-host-marker path outside `Knowledge Base/`.
  Do not put credentials, raw identities, or the marker in the Git checkout.

The command reads Cloudflare and Syncthing credentials only from the named local
environment variables. It must not print, commit, journal, or replicate them.

## Configure both hosts and prove identity parity

Start from `deploy/cold-standby/config.example.json`, copy it to a protected
location on each host, and replace only generic placeholders. The two files have
opposite local/peer values; they are not a single shared config file. Required
values cover the local role and host IDs, local/peer/stable readiness URLs,
service names, distinct tunnel names and UUIDs, the stable hostname, both direct
operational hostnames, the local
`cloudflared` config path, Cloudflare token/zone environment-variable names,
Syncthing folder/device/API, and paths for the journal, marker, peer manifests,
and effective Exomem `.env` file.

Keep this effective identity contract identical on both hosts:

- normalized `EXOMEM_BASE_URL` and stable OAuth callback;
- GitHub OAuth client ID and client secret;
- allowed GitHub login and numeric user ID; and
- `EXOMEM_JWT_SIGNING_KEY`.

The command compares the named values in the configured service `.env` with the
actual NSSM `AppEnvironmentExtra` snapshot used by the Windows service;
it derives the callback as `<EXOMEM_BASE_URL>/auth/callback`, and requires that
`EXOMEM_INSTANCE_ID` match the configured local host ID. It publishes and compares
only fingerprints of the shared values. A mismatch
refuses activation and handoff; never “fix” it by copying a secret into a
Syncthing folder or pasting it into terminal output.

Configure only after both files and tunnel ingress rules are ready:

```powershell
$config = '<protected-config-path>/desktop.json'
pwsh -File scripts/cold-standby.ps1 Configure -ConfigPath $config -WhatIf
pwsh -File scripts/cold-standby.ps1 Configure -ConfigPath $config
```

`Configure` puts Exomem into demand-start mode on both hosts. The laptop remains
manual. On the desktop it installs a delayed, current-user logon task that runs
the same guarded `Activate -IfUnserved` transition after the network is expected
to be available. The task leaves Exomem stopped if the laptop, the stable endpoint,
Syncthing state, or replicated intent is active or ambiguous.

`Configure` also verifies that the named `cloudflared` Windows service actually
runs the configured YAML and local tunnel. When it changes the managed ingress
block, it restarts that service and waits for the named tunnel to reconnect before
reporting success. That expected restart can make a long-lived MCP session perform
its normal reconnect handshake; routine status and transitions do not restart the
tunnel.

The service installer snapshots `.env` into NSSM and can reset service start
settings. After changing `.env`, re-running `scripts/install-service.ps1`, or
replacing the service registration, reinstall/restart the service, rerun `Configure`
on that host, and inspect `Status` before relying on boot recovery.

## Read status before every live action

`Status` is read-only. It reports local and peer service health, stable-endpoint
health, Syncthing convergence, desired-host intent, tunnel evidence, identity
fingerprints, and the next safe action. It does not infer that an unreachable
peer is powered off.

```powershell
pwsh -File scripts/cold-standby.ps1 Status -ConfigPath $config
pwsh -File scripts/cold-standby.ps1 Status -ConfigPath $config -Json
```

Interpret it conservatively:

- A ready desktop with a stopped laptop and a healthy stable endpoint means no
  transition is needed.
- A ready peer or ready stable endpoint means do not activate locally.
- Both origins ready, unknown tunnel/Syncthing evidence, stale replication, or
  conflicting desired-host intent is an unsafe state. Do not select a winner by
  guesswork; leave services and DNS unchanged and resolve the named evidence.

For any mutating action, first dry-run the same configuration:

```powershell
pwsh -File scripts/cold-standby.ps1 Activate -ConfigPath $config -WhatIf
pwsh -File scripts/cold-standby.ps1 Handoff -ConfigPath $config -WhatIf
```

`-WhatIf` evaluates the transition plan but must not alter services, scheduled
tasks, files, tunnel configuration, or DNS.

## Planned laptop activation and return to desktop

Do not promote the laptop while the desktop or the stable endpoint is healthy.
For planned movement, run the handoff on the active source first. It checks source
and peer replication plus identity parity, writes the next desired-host generation,
and proves that exact generation reached the peer before stopping the source and
pointing the stable hostname at the target tunnel. It never starts a remote Windows
service. On the first-ever handoff the marker may be absent; that bootstrap is
accepted only while the source service, source readiness, stable route, target
inactivity, replication, and identity parity all agree.

On the desktop:

```powershell
$desktopConfig = '<protected-config-path>/desktop.json'
pwsh -File scripts/cold-standby.ps1 Handoff -ConfigPath $desktopConfig -WhatIf
pwsh -File scripts/cold-standby.ps1 Handoff -ConfigPath $desktopConfig
```

Expect a bounded outage: the source is stopped before the target starts. On the
laptop, start the target through the same local guard:

```powershell
$laptopConfig = '<protected-config-path>/laptop.json'
pwsh -File scripts/cold-standby.ps1 Activate -ConfigPath $laptopConfig
```

Activation validates configuration and parity, requires conclusive Syncthing and
peer/stable evidence, writes intent, starts the local service, verifies local
readiness and its instance ID, moves stable DNS, then verifies both the Cloudflare
target and public readiness/OAuth metadata. It succeeds only when the stable URL
reports the expected local instance ID and stable base URL.

Returning to desktop is symmetric: run `Handoff` on the active laptop, then run
`Activate` locally on the desktop. A desktop reboot is not a shortcut around this
ordering: its delayed task uses `Activate -IfUnserved` and should remain stopped
when any laptop or stable evidence is unsafe.

## One URL and expected connector behaviour

Keep the same connector definition and URL:

```text
https://<stable-hostname>/mcp
```

Do not create a second connector for the laptop and do not change the GitHub OAuth
callback to a direct hostname. After shared OAuth storage is removed, the target
host may not have the client registration or session that the previous host held.
The existing connector URL remains correct; reconnect it, allow Dynamic Client
Registration to run again if requested, and complete GitHub authorization again.

One reconnect/registration/reauthorization after a host switch is expected. Repeated
reauthorization, a changed URL, or an identity-parity failure is not normal failover
behaviour—stop and inspect `Status`, the service logs, and the public OAuth
discovery endpoint.

## Unplanned desktop recovery and disaster activation

Normal recovery remains fail-closed. On the desktop, wait for Syncthing evidence,
run `Status`, and use a normal local `Activate` only when the laptop and stable
endpoint are conclusively inactive and the replicated intent permits desktop
service. Peer unreachability is not proof that the laptop is off; a partition can
make both machines believe they are alone.

If the source is unavailable and the recovery cannot satisfy only the allowed
peer/Syncthing guards, an operator can make an explicit disaster decision:

```powershell
pwsh -File scripts/cold-standby.ps1 Activate -ConfigPath $desktopConfig `
  -Force `
  -Acknowledge 'I ACKNOWLEDGE SPLIT-BRAIN AND DATA-LOSS RISK'
```

The acknowledgement must match the literal shown above. `-Force` may bypass only
peer-unreachable, conflicting-intent, or Syncthing-completion evidence. It cannot
bypass invalid configuration, shared-identity mismatch, local readiness, DNS route
mutation, tunnel verification, or public readiness/OAuth verification. Record the
terminal output: it names every guard actually bypassed.

Forced recovery can create a fork or discard newer writes present only on the
unreachable host. Bytes that had not replicated before that host failed are
unrecoverable by standby activation. Reconcile Syncthing conflicts and inspect the
vault before treating a forced recovery as complete.

## Partial transition and journal recovery

Do not blindly rerun `Activate` after an interrupted DNS transition. First collect
the local operation journal, current stable DNS target, direct readiness from both
operational hostnames, public stable readiness, and Syncthing state. Do not paste
secrets or raw identity values into incident notes.

An interrupted activation may be completed only when all of these agree: the
journaled phase permits replay, Cloudflare reports the stable record on the local
tunnel, and the public readiness response reports the expected local instance ID.
Otherwise use compensation: restore the known previous route, stop a newly started
target, and verify both origins before retrying. If the previous route is unknown
or the target might be active, leave the source stopped and follow the command's
`operator_recovery_required` terminal rather than risking two writers.

For a handoff that stopped the source but could not move or verify the route, first
prove the target inactive. Compensation then writes and proves delivery of a newer
cancellation generation naming the source before it restores the prior route and
restarts/verifies the source. If target inactivity cannot be proven, preserve the
single-active-service invariant and recover manually from the exact journal state.

## Staged personal cutover from automatic HA

This is a deployment migration, not an in-place toggle. The repository's existing
automatic writer-lease HA implementation remains supported; do not remove it from
the product while adopting this profile for one personal deployment.

1. Ship and test the tooling first. It is default-off: do not change service
   startup, public routing, OAuth, or HA environment yet.
2. Back up both effective `.env` files, both tunnel configurations, service startup
   settings, the Worker/Durable Object configuration, and its secrets. Confirm both
   vault replicas are fully converged and run compatible Exomem releases.
3. Create/harden the two distinct named tunnels. Give both tunnel configs the
   stable hostname, but make each direct operational hostname health/readiness-only.
   Install the local cold-standby configuration on both hosts while the HA edge
   remains live.
4. Run `Status` and all `-WhatIf` actions on both hosts. Prove each direct tunnel
   via a temporary hostname; do not alter the connector or stable hostname yet.
5. Quiesce connector writes and stop the laptop service. Remove
   `EXOMEM_WRITER_LEASE_*` and `EXOMEM_OAUTH_STORAGE_*` from the effective service
   environments on both hosts. Preserve the stable base URL, GitHub OAuth settings,
   allowed identity, JWT signing key, and vault paths.
6. Start the desktop in standalone mode. Detach the HA Worker's stable-hostname
   route binding **before** direct tunnel routing. Verify the direct desktop path,
   then point stable DNS to the desktop tunnel and verify OAuth discovery, a
   reconnect/authorization using the existing connector definition, an authenticated
   read, and one governed write.
7. Re-run `Configure` to enforce demand-start and desktop boot recovery. Exercise a
   controlled desktop-to-laptop handoff and laptop-to-desktop return, including
   Syncthing convergence and any expected connector reauthorization.
8. Keep the Worker/Durable Object deployment and rollback material intact, with the
   stable-hostname route detached, for a bounded rollback window. Only after that
   window closes may you retire the personal Worker/Durable Object deployment and
   its secrets.

If direct routing, OAuth, authenticated reads, or a governed write fails, stop the
standalone service, restore the backed-up HA environment and tunnel configuration,
restore the Worker's stable-hostname route binding and DNS path, start the existing
replicas, run their HA doctor/readiness gates, and only then reopen writes. Do not
delete the Worker, Durable Object, or rollback secrets before this window has passed.
