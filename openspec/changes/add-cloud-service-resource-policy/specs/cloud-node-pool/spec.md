## MODIFIED Requirements

### Requirement: One Terraform variable adds or removes a K3s agent node

The foundation root SHALL declare K3s agent nodes as one map variable, `k3s_agent_nodes`, whose default is empty. Each entry SHALL produce exactly one Hetzner server, which:

- is attached to the existing private subnet at the entry's declared address;
- is in the same location as the existing fleet node;
- carries the shared agent firewall;
- is labelled as a K3s agent, together with its entry key.

Adding or removing an entry MUST NOT replace or change any other entry's server. The map MUST reject:

- an address outside the subnet, or one that is not a usable host address;
- a duplicate address;
- an address reserved for the fleet server;
- a server type outside the existing general x86 allow-list, except CX33 with a nonempty exact sixteen-character lowercase base32 `dedicated_cell_id`;
- a nonempty `dedicated_cell_id` that is not an exact cell identifier.

The optional reservation SHALL default to empty, survive module and root outputs, and be delivered by generated inventory as `k3s_agent_dedicated_cell`. A reserved agent SHALL publish zero general-admission slots, retain observed attachment accounting, and remain excluded from removal-capacity calculations. Removing an agent SHALL refuse while any desired cell template still selects its reservation. An 8 GiB CX33 reservation MUST NOT become a general agent by clearing its reservation while retaining that server type. Workload rollback SHALL stop and finish volume users, change placement, then resume while retaining the node reservation until relocation is verified. Current same-location availability and an authorized saved plan are prerequisites for purchase; this contract does not authorize provision or join.

#### Scenario: Adding a node

- **WHEN** the operator adds one entry to `k3s_agent_nodes`
- **THEN** the plan creates one server with the declared private address, the agent firewall and the agent labels, and changes nothing else

#### Scenario: Removing one of several nodes

- **WHEN** the operator removes one entry while others remain
- **THEN** the plan destroys only that entry's server, and every remaining agent server keeps its address

#### Scenario: Colliding address

- **WHEN** an entry declares the fleet server's private address
- **THEN** planning fails validation before any resource is proposed

#### Scenario: Restricted small dedicated agent

- **WHEN** one agent declares CX33 and a valid nonempty `dedicated_cell_id`
- **THEN** its plan accepts the type, its generated inventory preserves the reservation, and the joined node contributes zero general slots

#### Scenario: Small agent loses its reservation

- **WHEN** an existing CX33 agent entry clears `dedicated_cell_id`
- **THEN** planning fails sizing validation before the node can become general capacity

### Requirement: A joined agent publishes capacity without a controller change

When the Hetzner CSI driver is installed, the join SHALL complete only once the agent's CSINode publishes a volume-attachment limit for that driver, so that the reservation-aware controller counts an unreserved node's general cell slots from its own observation. A node with either the dedicated label or taint SHALL publish zero general slots while preserving observed attachment counts. Unknown nodes MUST NOT contribute positive slots through a configured fallback before they are positively observed unreserved. The reservation-aware controller and its node-read permissions MUST be deployed before any restricted dedicated node joins. When the driver is not yet installed, the join SHALL say so, and the node SHALL remain uncounted until the driver publishes its limit. No controller configuration or code change SHALL be needed to count a new node, or to stop counting a removed one.

#### Scenario: Node added on a platform with the CSI driver

- **WHEN** a new unreserved agent's join completes on a cluster running the Hetzner CSI driver
- **THEN** its CSINode carries an allocatable attachment count, and the controller's next pass publishes cell slots for it

#### Scenario: Node removed

- **WHEN** an agent's Kubernetes node is deleted
- **THEN** its CSINode is gone, and the controller publishes zero slots for it

