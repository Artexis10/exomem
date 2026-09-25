## ADDED Requirements

### Requirement: One Terraform variable adds or removes a K3s agent node

The foundation root SHALL declare K3s agent nodes as one map variable, `k3s_agent_nodes`, whose default is empty. Each entry SHALL produce exactly one Hetzner server, which:

- is attached to the existing private subnet at the entry's declared address;
- is in the same location as the existing fleet node;
- carries the shared agent firewall;
- is labelled as a K3s agent, together with its entry key.

Adding or removing an entry MUST NOT replace or change any other entry's server. The map MUST reject:

- an address outside the subnet, or one that is not a usable host address;
- a duplicate address;
- an address reserved for the fleet server or the control database;
- a server type outside the allow-list of x86 types with enough memory for the node's attachment slots.

#### Scenario: Adding a node

- **WHEN** the operator adds one entry to `k3s_agent_nodes`
- **THEN** the plan creates one server with the declared private address, the agent firewall and the agent labels, and changes nothing else

#### Scenario: Removing one of several nodes

- **WHEN** the operator removes one entry while others remain
- **THEN** the plan destroys only that entry's server, and every remaining agent server keeps its address

#### Scenario: Colliding address

- **WHEN** an entry declares the fleet server's or the control database's private address
- **THEN** planning fails validation before any resource is proposed

### Requirement: An agent node's declared exposure is 443 and restricted SSH

The agent firewall SHALL declare exactly two inbound rules:

- TCP 443 from any address;
- TCP 22 only from the declared administrator CIDRs.

It MUST NOT declare any other inbound rule. K3s cluster traffic SHALL use only the private network. On every K3s node, the host firewall SHALL admit the following only from the other K3s nodes' declared private addresses, on the private interface:

- the K3s API, on the server;
- the Flannel VXLAN port;
- the kubelet port.

It SHALL converge those rules to the current inventory on every run.

#### Scenario: Declared agent firewall

- **WHEN** the agent firewall is planned for any non-empty set of agents
- **THEN** its inbound rules are exactly TCP 443 from anywhere and TCP 22 from the administrator CIDRs

#### Scenario: A node leaves the inventory

- **WHEN** a K3s node's address is no longer in the inventory, and the join or removal playbook runs
- **THEN** no remaining node's host firewall still admits that address on the inter-node ports

#### Scenario: Control database on the same subnet

- **WHEN** the host firewall rules are rendered for any inventory
- **THEN** no inter-node rule names the control database's address or the subnet as a whole

### Requirement: An agent joins with the existing node's hardening and a dedicated, CA-pinned agent token

Joining SHALL apply to an agent the same hardening as the existing node:

- the same base hardening;
- the same pinned, checksum-verified K3s binary;
- the same kernel settings;
- the same kubelet limits.

The agent SHALL authenticate with a dedicated agent token that the server is configured with, which is distinct from the server token. The agent SHALL present that token in the secure format that pins the cluster CA. An agent's host MUST NOT receive the server token, the etcd snapshot credentials or a cluster-admin kubeconfig. The server play MUST NOT run on an agent. Rerunning the join on a converged cluster MUST change nothing.

#### Scenario: First join

- **WHEN** the join playbook runs against a new agent
- **THEN** the node registers with the agent token alone, carries the agent node-pool label and reports Ready

#### Scenario: Rerun

- **WHEN** the join playbook runs again against the joined cluster
- **THEN** no task reports a change and K3s is not restarted

#### Scenario: Wrong token

- **WHEN** a node presents a token that is neither the agent token nor the server token
- **THEN** the server refuses the join

#### Scenario: A peer does not yet admit the new agent

- **WHEN** an agent joins while another K3s node's host firewall does not admit it
- **THEN** the join fails, naming the missing rule, instead of completing

### Requirement: A joined agent publishes capacity without a controller change

When the Hetzner CSI driver is installed, the join SHALL complete only once the agent's CSINode publishes a volume-attachment limit for that driver, so that the existing controller counts the node's cell slots from its own observation. When the driver is not yet installed, the join SHALL say so, and the node SHALL remain uncounted until the driver publishes its limit. No controller configuration or code change SHALL be needed to count a new node, or to stop counting a removed one.

#### Scenario: Node added on a platform with the CSI driver

- **WHEN** a new agent's join completes on a cluster running the Hetzner CSI driver
- **THEN** its CSINode carries an allocatable attachment count, and the controller's next pass publishes cell slots for it

#### Scenario: Node removed

- **WHEN** an agent's Kubernetes node is deleted
- **THEN** its CSINode is gone, and the controller publishes zero slots for it

### Requirement: Removing an agent refuses when cells would not fit and never strands a live node

Removing an agent SHALL, in order:

1. refuse unless the target is an agent, and not a server node;
2. refuse while any cell maintenance hold is present;
3. refuse unless the cell volumes in the cluster fit the remaining nodes' published slots. Slots are computed as the controller computes them: allocatable, minus headroom, minus non-cell attachments, net of the target's non-cell attachments;
4. cordon the agent and, when it is Ready, drain it without force;
5. stop K3s and every pod container on it, unmount every pod and CSI volume mount, and close the volumes' encryption mappings. Treat the stop as confirmed only when a reachable host shows no container process, no pod or CSI volume mount and no open volume mapping, or on the operator's explicit confirmation that the server is gone;
6. only once the stop is confirmed, force remaining pods and volumes off it, then delete its Kubernetes node and confirm it stays absent;
7. converge the remaining nodes' inter-node firewall rules without it.

The removal MUST NOT delete the Kubernetes node of an agent whose stop is unconfirmed. It MUST NOT force a volume off a node that may still run containers. It MUST be safe to rerun after any step until the agent's server is destroyed. A removed agent's host MUST NOT rejoin through the join playbook.

#### Scenario: Not enough room elsewhere

- **WHEN** the cluster's cell volumes exceed the remaining nodes' slots
- **THEN** removal stops before cordoning, and nothing changes

#### Scenario: Unreachable agent without confirmation

- **WHEN** the target cannot be reached and the operator has not confirmed that the server is gone
- **THEN** removal fails before deleting the node

#### Scenario: Rerun after the node is gone

- **WHEN** removal runs again for an agent whose node was already deleted, before its server is destroyed
- **THEN** it completes without error and changes nothing in the cluster

#### Scenario: Join playbook after removal

- **WHEN** the join playbook runs against an agent host that removal has stopped
- **THEN** the agent play fails for that host before changing it, and the node does not re-register

### Requirement: Public ingress stays on the server node

The platform ingress SHALL be scheduled only on the control-plane node, so that adding an agent never moves the public entrypoint off the address its DNS record targets. Its rollout MUST NOT require a second pod to schedule beside the running one.

#### Scenario: Agent added while ingress is running

- **WHEN** an agent joins and the ingress pod is rescheduled or rolled
- **THEN** the ingress pod runs on the control-plane node, and the rollout completes with one replica
