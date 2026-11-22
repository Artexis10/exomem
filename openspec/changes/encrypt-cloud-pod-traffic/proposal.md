## Why

Exomem Cloud encrypts the connection from a client to the edge. Inside the cluster, every pod-to-pod hop is plain HTTP: the gateway to a cell, `cellctl` to a cell's health port, and a cell to its artifact broker, media edge and slot lease. Flannel carries that traffic between nodes as unencrypted VXLAN. The provider's private network, or a dedicated host's VLAN, is then the only protection. Cells are moving to agent hosts (`move-cloud-cells-to-local-storage`), so every one of these hops will soon cross a wire.

## What Changes

- Flannel uses its `wireguard-native` backend on every node, so all pod traffic between nodes is encrypted and authenticated by WireGuard. VXLAN between nodes is closed.
- Flannel's WireGuard listens on its own port, separate from the private-link WireGuard that hosts outside the provider's network already use. Pod MTU leaves room for the WireGuard header, including on hosts whose link is itself WireGuard.
- A node without the WireGuard kernel module does not join.
- The backend switch is rehearsed with its rollback on the disposable K3s link job. Production switches in one planned window, before the first agent joins when the cluster has one server.
- No TLS is added between the gateway and a cell. Across nodes, WireGuard already encrypts the hop. Within one node, the traffic never leaves the node's kernel, and only node root could observe it. Node root can read the cell directly in any case. Design D2 records the reasons.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `cloud-node-pool`: adds the requirement that pod traffic between nodes is encrypted.

## Impact

- `infra/ansible/roles/k3s`: backend variable, flannel configuration file, MTU, firewall and a join precondition.
- `infra/cloud-rehearsal` and `.github/workflows/cloud-k3s-link.yml`: the switch, rollback, capture and MTU checks.
- Runbooks: the switch window and its rollback.
- After the live switch: `docs/cloud-security-model.md` and the privacy copy (separate repository) may state that pod traffic between servers is encrypted.
