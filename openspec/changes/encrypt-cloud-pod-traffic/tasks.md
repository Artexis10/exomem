## 1. Source and rehearsal

- [ ] 1.1 In the `k3s` role, add `k3s_flannel_backend` (default `vxlan`) and `k3s_flannel_wireguard_port` (default 51830). With `wireguard-native`, render the flannel configuration file with `Type: wireguard`, the port, `PersistentKeepaliveInterval: 25` and `MTU` from `k3s_smallest_link_mtu`. Derive `k3s_flannel_pod_mtu` from the backend's overhead (50 or 80). Admit the WireGuard port from the other cluster nodes and delete the 8472 rule. Refuse to configure K3s on a node where the WireGuard module cannot load. Evidence: rendered-template tests for both backends, including a `wgexomem` host, and a refused port equal to `k3s_wireguard_port`.
- [ ] 1.2 Extend the K3s link rehearsal with the switch, the capture, the don't-fragment MTU probe over a WireGuard underlay and the rollback (design D5). Evidence: the CI run, with the capture summary and the probe sizes.
- [ ] 1.3 Add the switch window and the rollback to the runbooks, with the outcome checks: every node's flannel backend annotation, `wg show flannel-wg` peers, no 8472 rule, and healthy cells. Evidence: the runbook steps marked as rehearsed by 1.2.

## 2. Production

- [ ] 2.1 In an operator window, merge and run the commit that sets `wireguard-native`, before the first agent joins. Evidence: the outcome checks from 1.3, recorded from the live cluster, and a recall on a live cell.
- [ ] 2.2 When the first agent joins, capture a gateway request to a cell on that agent on the private link. Evidence: the capture summary holds only WireGuard UDP to the flannel port.

## 3. Claims

- [ ] 3.1 After 2.1, update `docs/cloud-security-model.md` to state that pod traffic between servers is encrypted, with the enforcement point and the host-traffic limit. Update the privacy copy (separate repository) to match. Evidence: the copy review against this change.
