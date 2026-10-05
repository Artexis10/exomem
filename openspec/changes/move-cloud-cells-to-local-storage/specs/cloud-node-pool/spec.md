## MODIFIED Requirements

### Requirement: A joined agent publishes capacity without a controller change

When the cell storage driver is installed, the join SHALL complete only once the agent's storage driver publishes free capacity for the node's cell storage pool, so that the reservation-aware controller counts an unreserved node's general cell slots from its own observation. A node with either the dedicated label or taint SHALL publish zero general slots while preserving observed storage use. Unknown nodes MUST NOT contribute positive slots through a configured fallback before they are positively observed unreserved. The reservation-aware controller and its node-read permissions MUST be deployed before any restricted dedicated node joins. When the driver is not yet installed, the join SHALL say so, and the node SHALL remain uncounted until the driver publishes its capacity. A node without a cell storage pool, such as the control-plane node once the configured cell storage domain is local, publishes no pool and therefore zero cell slots. No controller configuration or code change SHALL be needed to count a new node, or to stop counting a removed one.

#### Scenario: Node added on a platform with the CSI driver

- **WHEN** a new unreserved agent's join completes on a cluster running the cell storage driver
- **THEN** the driver publishes the node's free cell storage capacity, and the controller's next pass publishes cell slots for it

#### Scenario: Node removed

- **WHEN** an agent's Kubernetes node is deleted
- **THEN** its published storage capacity is gone, and the controller publishes zero slots for it

### Requirement: Removing an agent refuses when cells would not fit and never strands a live node

Removing an agent SHALL, in order:

1. refuse unless the target is an agent, and not a server node;
2. refuse while any stopping cell maintenance hold is present; wait for running hourly backups on the target to finish, and start no new one there;
3. refuse unless every non-deleted cell in the cluster fits the remaining nodes' published slots, computed by storage size as the controller computes them;
4. when the remaining nodes cannot reach the target's cell volumes, relocate each cell on the target through a fresh stopped backup and a restore, starting it only after the restore succeeds and accepting it before continuing, and keep the target's volumes until every relocated cell is accepted. A target that cannot be reached cannot produce that backup: its cells SHALL first be relocated under the node-loss requirement, and cells already bound elsewhere are skipped;
5. cordon the agent and, when it is Ready, drain it without force;
6. stop K3s, every pod container and every process in a pod cgroup on it, remove its agent credentials, unmount every pod and CSI volume mount, and close the storage encryption mappings. Treat the stop as confirmed only when a reachable host shows no container process, no pod or CSI volume mount and no open storage mapping, or on the operator's explicit confirmation that the server is gone;
7. only once the stop is confirmed, force remaining pods off it, then delete its Kubernetes node and confirm it stays absent;
8. converge the remaining nodes' inter-node firewall rules without it.

The removal MUST NOT delete the Kubernetes node of an agent whose stop is unconfirmed. It MUST NOT delete a cell volume on the target before that cell's relocated copy is accepted. It MUST be safe to rerun after any step until the agent's server is destroyed. A removed agent's host MUST NOT rejoin through the join playbook, and a lone unreachable agent MUST NOT stop the removal from reaching its confirmation check.

#### Scenario: Not enough room elsewhere

- **WHEN** the cluster's non-deleted cells exceed the remaining nodes' slots
- **THEN** removal stops before relocating or cordoning, and nothing changes

#### Scenario: Relocated cell fails acceptance

- **WHEN** a relocated cell does not answer recall, report its governance schema or accept a governed write
- **THEN** removal stops, and the original cell volume on the target is kept

#### Scenario: Unreachable agent without confirmation

- **WHEN** the target cannot be reached and the operator has not confirmed that the server is gone
- **THEN** removal fails before deleting the node

#### Scenario: Rerun after the node is gone

- **WHEN** removal runs again for an agent whose node was already deleted, before its server is destroyed
- **THEN** it completes without error and changes nothing in the cluster

#### Scenario: Join playbook after removal

- **WHEN** the join playbook runs against an agent host that removal has stopped
- **THEN** the playbook leaves that host unchanged and does not re-register its node, and it still converges every other node

## ADDED Requirements

### Requirement: A dedicated agent joins over a private link with encrypted local cell storage

A dedicated server SHALL join as an agent over a private link: a provider private network coupled to the cluster network, or an encrypted tunnel when the provider has none. It SHALL join with the existing agent hardening, its dedicated CA-pinned agent token and the inter-node firewall. Its cell storage pool SHALL sit on mirrored disks under encryption at rest. The pool SHALL unlock unattended only through a key server on the control-plane node. The agent's recovery key and the key server's own keys SHALL be escrowed through the existing secret-destination contract, and the operator runbook SHALL carry a manual unlock and a rebind to a replaced key server. The agent's K3s service SHALL start only after the pool is unlocked.

The operator SHALL also be able to create a Cloud recovery agent from Terraform, whose storage pool sits on one Cloud volume under the same encryption and unattended unlock, and remove it when it is no longer needed. A recovery agent created for a drill SHALL carry the dedicated-node reservation, so that it publishes no general slots.

#### Scenario: Dedicated agent reboots

- **WHEN** a dedicated agent reboots while the control-plane node is reachable
- **THEN** its storage pool unlocks without operator action, K3s starts after the unlock, and its cells serve again

#### Scenario: A removed disk alone

- **WHEN** one of the agent's disks is read outside the agent
- **THEN** no cell data is readable from it

#### Scenario: Key server lost with the control-plane node

- **WHEN** the control-plane node is rebuilt and an agent reboots before the key server is restored
- **THEN** the operator can unlock the agent's pool from the escrowed recovery key, or restore the key server from its escrowed keys

#### Scenario: Replacement capacity after a node loss

- **WHEN** a dedicated agent is lost and no other agent has room for its cells
- **THEN** the operator can create a Cloud recovery agent from Terraform and relocate the cells onto it
