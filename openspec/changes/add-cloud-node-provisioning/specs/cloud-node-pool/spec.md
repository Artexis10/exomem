## ADDED Requirements

### Requirement: One Terraform variable adds or removes a K3s agent node

The foundation root SHALL declare K3s agent nodes as one map variable, `k3s_agent_nodes`, whose default is empty. Each entry SHALL produce exactly one Hetzner server, which:

- is attached to the existing private subnet at the entry's declared address;
- is in the same location as the existing fleet node;
- carries the shared agent firewall;
- is labelled as a K3s agent together with its entry key.

Adding or removing an entry MUST NOT replace or change any other entry's server. The map MUST reject:

- an address outside the subnet;
- a duplicate address;
- an address reserved for the fleet server or the control database;
- a non-x86 server type.

#### Scenario: Adding a node

- **WHEN** the operator adds one entry to `k3s_agent_nodes`
- **THEN** the plan creates one server with the declared private address, the agent firewall and the agent labels, and changes nothing else

#### Scenario: Removing one of several nodes

- **WHEN** the operator removes one entry while others remain
- **THEN** the plan destroys only that entry's server, and every remaining agent server keeps its address

#### Scenario: Colliding address

- **WHEN** an entry declares the fleet server's or the control database's private address
- **THEN** planning fails validation before any resource is proposed

### Requirement: An agent node exposes only 443 and restricted SSH to the internet

The agent firewall SHALL admit inbound TCP 443 from any address and TCP 22 only from the declared administrator CIDRs. It MUST NOT admit any other inbound port. K3s cluster traffic SHALL flow only over the private network. On every K3s node, the host firewall SHALL admit the K3s API (on the server), the Flannel VXLAN port and the kubelet port only from the other K3s nodes' declared private addresses.

#### Scenario: Public reachability of an agent

- **WHEN** an internet client connects to an agent's public address on any port other than 443
- **THEN** the connection is refused by the agent firewall, except SSH from an administrator CIDR

#### Scenario: Another host on the private subnet

- **WHEN** a host on the private subnet that is not a K3s node connects to a node's K3s API, VXLAN or kubelet port
- **THEN** the host firewall drops it

### Requirement: An agent joins with the existing node's hardening and a dedicated agent token

Joining SHALL apply to an agent the same base hardening, pinned and checksum-verified K3s binary, kernel settings and kubelet limits as the existing node. The agent SHALL authenticate with a dedicated agent token that the server is configured with, which is distinct from the server token. An agent's host MUST NOT receive the server token, the etcd snapshot credentials or a cluster-admin kubeconfig. Rerunning the join on a converged agent MUST change nothing.

#### Scenario: First join

- **WHEN** the join playbook runs against a new agent
- **THEN** the node registers with the agent token alone, carries the agent node-pool label and reports Ready

#### Scenario: Rerun

- **WHEN** the join playbook runs again against a joined agent
- **THEN** no task reports a change and K3s is not restarted

#### Scenario: Wrong token

- **WHEN** an agent presents a token that is neither the agent token nor the server token
- **THEN** the server refuses the join

### Requirement: A joined agent publishes capacity without a controller change

The join SHALL complete only once the agent's CSINode publishes a volume-attachment limit for the Hetzner CSI driver, so that the existing controller counts the node's cell slots from its own observation. No controller configuration or code change SHALL be needed to count a new node, or to stop counting a removed one.

#### Scenario: Node added

- **WHEN** a new agent's join completes
- **THEN** its CSINode carries an allocatable attachment count, and the controller's next pass publishes cell slots for it

#### Scenario: Node removed

- **WHEN** an agent's Kubernetes node is deleted
- **THEN** its CSINode is gone, and the controller publishes zero slots for it

### Requirement: Removing an agent drains it safely first

Removing an agent SHALL, in order:

1. refuse unless the other Ready, schedulable nodes have enough free attachment slots for the agent's attached volumes;
2. cordon the agent;
3. drain it without force, so that a pod with no controller stops the drain instead of being deleted;
4. stop K3s on it;
5. delete its Kubernetes node;
6. revoke its inter-node host-firewall rules on the remaining nodes.

The removal MUST refuse to act on a server node, and MUST be safe to rerun after any step.

#### Scenario: Not enough room elsewhere

- **WHEN** the remaining nodes have fewer free attachment slots than the agent has attached volumes
- **THEN** removal stops before cordoning, and nothing changes

#### Scenario: Rerun after the node is gone

- **WHEN** removal runs again for an agent whose node was already deleted
- **THEN** it completes without error and changes nothing in the cluster

### Requirement: Public ingress stays on the server node

The platform ingress SHALL be scheduled only on the control-plane node, so adding an agent never moves the public entrypoint off the address its DNS record targets.

#### Scenario: Agent added while ingress is running

- **WHEN** an agent joins and the ingress pod is rescheduled
- **THEN** the ingress pod runs on the control-plane node
