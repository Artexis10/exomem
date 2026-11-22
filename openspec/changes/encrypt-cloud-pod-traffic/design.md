## Context

The edge terminates public TLS on the server node and forwards to the gateway over plain HTTP. The gateway forwards to a cell over plain HTTP at the cell's Service (`defaultCellOrigin` in the control plane's `cloud-handler.ts`). Other pod hops are plain HTTP too:
- `cellctl` to a cell's health and traffic ports;
- memory-health to its synthetic cell;
- a cell to its artifact broker, media edge and slot lease.

The cell runtime has no egress. Cell Jobs reach object storage over TLS.

The gateway, `cellctl`, the key custodian and the edge are all pinned to the server node (`infra/helm/platform/values.yaml`). A hop crosses a wire only when a cell runs on an agent. Flannel carries that traffic as VXLAN on the node's private interface. The private interface is one of three links:
- the provider's Cloud network;
- a dedicated server's vSwitch VLAN;
- for a host outside the provider's network, the `wgexomem` WireGuard link (`k3s_private_link: wireguard`).

Only the last is encrypted.

Facts this design rests on, from primary sources for the pinned K3s v1.35:
- Flannel options are set on servers only, and must be identical on every server. Every node, agents included, needs the WireGuard kernel module (K3s networking options).
- When `flannel-conf` names a file, K3s uses it as is and ignores `flannel-backend` (`ConfOverride` in K3s `pkg/agent/flannel/setup.go`). The role already renders that file whenever `k3s_flannel_mtu` is set.
- Flannel's WireGuard backend has `Type: wireguard`. It listens on UDP 51820 by default (`ListenPort`), creates the interface `flannel-wg`, and subtracts a fixed 80-byte overhead from the MTU (flannel `pkg/backend/wireguard`). K3s's own generated configuration sets `PersistentKeepaliveInterval: 25`.
- K3s documents no procedure to switch a running cluster from VXLAN to WireGuard. An earlier K3s migration (legacy WireGuard to `wireguard-native`) updated every server and rebooted all nodes, servers first, with a short outage.

## Goals / Non-Goals

**Goals**
- All pod traffic between nodes is encrypted and authenticated, whatever the underlying link.
- A node cannot fall back to VXLAN or join without WireGuard.
- The switch and its rollback are rehearsed before production, and production switches once, in a planned window.

**Non-Goals**
- TLS between the gateway and a cell (D2).
- Host-network traffic. The kubelet port, the API server and disk unlock (Tang) do not use flannel. They keep their own TLS or Tang's protocol, and the security model says so.
- Protection from root on a node, which can read a cell's memory and volume directly. `make-cloud-cells-operator-resistant` owns that boundary.

## Decisions

### D1. Flannel's `wireguard-native` backend carries all pod traffic between nodes

The role gains `k3s_flannel_backend`, with `vxlan` as the default and `wireguard-native` as the target. With `wireguard-native`, the role always renders the flannel configuration file, because the file is the only place to set the port (D3), and it overrides the K3s flag anyway. The file sets these fields:
- `Backend.Type: wireguard`;
- `ListenPort` from `k3s_flannel_wireguard_port` (D3);
- `PersistentKeepaliveInterval: 25`;
- `MTU` from the smallest link in use.

One backend applies to the whole cluster, because flannel runs one backend per cluster. Mixed backends are not a supported state and are never planned.

A preflight task refuses to configure K3s on a node where the WireGuard module cannot load, so a node without it never joins.

### D2. No TLS from the gateway to a cell

The hop is HTTPS-shaped in the request ("why not HTTPS?"), so the choice is recorded here.

- **Across nodes**, D1 encrypts and authenticates every packet between the two nodes' WireGuard keys.
- **On one node**, the packet goes from one pod's network namespace to another through the node's kernel bridge. It never reaches a wire. Only node root could capture it, and node root can read the cell's mounted volume and memory directly.
- **Authenticating the cell** is already covered by other controls:
  - the cell namespace's default-deny NetworkPolicy, which admits only gateway pods to the cell port;
  - the gateway's NetworkPolicy, which limits its egress to cell pods on that port;
  - `contain-cloud-controller-compromise`, whose pinned trust anchors (D2) and closed pod template (D3) stop the controller from planting a fake cell.

Per-cell TLS would add a certificate authority to run. Its signing key would have to sit outside `cellctl`'s reach, or a compromised controller could mint a certificate for a fake cell. It would also add issuance and rotation for every cell, plus a trust anchor for the gateway to pin. None of that adds protection against anyone short of node root. The operator chose WireGuard between servers without TLS to cells on 2026-11-22.

Revisit D2 if any pod hop starts to leave the cluster's nodes, or if a hop starts to cross a network that WireGuard does not cover.

### D3. Port and MTU

- **Port.** `k3s_flannel_wireguard_port` defaults to 51830. Flannel's default 51820 is already `k3s_wireguard_port`, the private-link WireGuard on hosts outside the provider's network. 51821 is flannel's IPv6 default. The firewall admits the flannel port from every other cluster node on the private interface. With `wireguard-native`, the role deletes the VXLAN 8472 rule, so a stray VXLAN packet between nodes is dropped.
- **MTU.** The WireGuard backend's `MTU` is `k3s_smallest_link_mtu`. Flannel subtracts its fixed 80 bytes, so pods run at the smallest link minus 80. On a `wgexomem` host the link is 1420, so pods run at 1340. That already accounts for the outer WireGuard, because the link MTU is the outer interface's MTU. `k3s_flannel_pod_mtu` follows the backend: minus 50 for VXLAN, minus 80 for WireGuard. Today's rule stays in force: a wrong MTU drops large packets silently, so the rehearsal probes it (D5).

### D4. Rollout: one reviewed commit, in a window, server first

The default stays `vxlan`. A routine platform run therefore never restarts a production server, which is the lesson from the flannel MTU rollout (`docs/runbooks/cloud-dedicated-host.md`). Production switches through one reviewed commit that sets `k3s_flannel_backend: wireguard-native`. The commit runs in an operator window with these steps:
1. Apply the commit to the server, which restarts K3s there.
2. Restart K3s on every agent.
3. Delete `flannel.1`.
4. Restart the workloads on the pod network, as in the MTU runbook's step 7.
5. Check the outcome checks in the runbook.

When the cluster has one server and no agent, only that server restarts and no pod traffic crosses a wire during the switch. That is the cheapest time to switch, so it happens before the first agent joins. A later agent joins with the backend already in force.

**Rollback.** Revert the commit and restart in the same order. Delete `flannel-wg` and restart the pod-network workloads. The rehearsal (D5) runs the rollback as well as the switch.

### D5. Rehearsal

The disposable K3s link job (`.github/workflows/cloud-k3s-link.yml`, `infra/cloud-rehearsal`) already runs a server and an agent for the MTU, token and client-CA rotations. It gains these steps:
1. Start on VXLAN, then switch to `wireguard-native` by the runbook's steps. Check pod-to-pod traffic between the two nodes.
2. Capture on the link during a cross-node HTTP request. Check that the capture holds only UDP to the flannel WireGuard port, with no 8472 and no plaintext HTTP.
3. Send don't-fragment probes at the pod MTU over a WireGuard underlay between the two nodes, to reproduce a `wgexomem` host. A probe one byte larger must fail.
4. Roll back to VXLAN and repeat step 1.

The job's nodes share one kernel. If that kernel cannot reproduce the underlay faithfully, the MTU probe moves to a disposable two-host run, which needs the operator's approval for its spend.

## Risks / Trade-offs

- **A short outage in the window.** Pod networking restarts. Mitigation: switch while the cluster has one server, rehearse the steps, and announce the window through the fleet's maintenance procedure.
- **CPU cost.** WireGuard encrypts in the kernel, and at cell traffic rates the cost is small. The rehearsal records the before and after request latency between nodes.
- **No documented switch procedure upstream.** Mitigation: the rehearsal runs the exact production steps, including rollback, on the pinned K3s version.

## Migration Plan

1. Merge the role, rehearsal and runbook with the default `vxlan`. No production effect.
2. In an operator window, merge and run the commit that sets `wireguard-native`. Verify in the window.
3. When the first agent joins, capture a cross-node request live and record it.
4. Update `docs/cloud-security-model.md`. The privacy copy may then state that pod traffic between servers is encrypted, and that traffic within one server never leaves it.

## Open Questions

None.
