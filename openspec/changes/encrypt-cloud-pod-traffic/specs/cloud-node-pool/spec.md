## ADDED Requirements

### Requirement: Pod traffic between nodes is encrypted

The cluster SHALL carry all pod traffic between nodes through flannel's `wireguard-native` backend, so that every packet between two nodes is encrypted and authenticated by WireGuard, whatever the underlying private link. The cluster SHALL NOT accept flannel VXLAN between nodes, except after an operator rollback of the backend, which the security model SHALL then report. Flannel's WireGuard port SHALL differ from the port of any WireGuard private link on the same host. The pod MTU SHALL leave room for flannel's WireGuard overhead on the smallest link in use. K3s SHALL NOT be configured on a node where the WireGuard kernel module cannot load.

The backend SHALL change only through a reviewed configuration change, run in an operator window, with a rehearsed rollback. While the cluster has one server and no agent, the switch SHALL happen before the first agent joins.

Host-network traffic (the kubelet port, the API server and disk unlock) is outside this requirement. Descriptions SHALL say that it keeps its own TLS or Tang's protocol.

#### Scenario: A request crosses nodes

- **WHEN** the gateway on the server forwards a request to a cell on an agent
- **THEN** a capture on the private link shows only UDP to flannel's WireGuard port, with no VXLAN packet and no plaintext HTTP

#### Scenario: A node sends VXLAN

- **WHEN** a VXLAN packet from another cluster node reaches a node on UDP 8472
- **THEN** the node's firewall drops it

#### Scenario: A host whose private link is WireGuard

- **WHEN** a node joins over a WireGuard private link
- **THEN** flannel's WireGuard listens on a different port, and a don't-fragment packet at the pod MTU crosses both layers while a packet one byte larger is refused

#### Scenario: A node without the WireGuard module

- **WHEN** the platform run reaches a node whose kernel cannot load the WireGuard module
- **THEN** the run stops before it configures K3s on that node, and names the missing module

#### Scenario: Rollback

- **WHEN** the operator reverts the backend change in a window
- **THEN** pod traffic between nodes runs over VXLAN again, and the rehearsal has already run the same steps on the pinned K3s version
