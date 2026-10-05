## MODIFIED Requirements

### Requirement: An agent node's declared exposure is 443 and restricted SSH

The agent firewall SHALL declare these inbound rules:

- TCP 443 from any address;
- TCP 22 only from the declared administrator CIDRs, and only while a temporary break-glass CIDR is declared.

With no declared administrator CIDR, the agent firewall SHALL admit only TCP 443; routine administration uses the company NetBird. It MUST NOT declare any other inbound rule. K3s cluster traffic SHALL use only the private network. On every K3s node, the host firewall SHALL admit the following only from the other K3s nodes' declared private addresses, on the private interface:

- the K3s API, on the server;
- the Flannel VXLAN port;
- the kubelet port.

It SHALL converge those rules to the current inventory on every run.

#### Scenario: Declared agent firewall

- **WHEN** the agent firewall is planned for any non-empty set of agents and no administrator CIDR is declared
- **THEN** its only inbound rule is TCP 443 from anywhere

#### Scenario: Break-glass SSH window

- **WHEN** the agent firewall is planned while a temporary administrator CIDR is declared
- **THEN** its inbound rules are exactly TCP 443 from anywhere and TCP 22 from the administrator CIDRs

#### Scenario: A node leaves the inventory

- **WHEN** a K3s node's address is no longer in the inventory, and the join or removal playbook runs
- **THEN** no remaining node's host firewall still admits that address on the inter-node ports

#### Scenario: Control database on the same subnet

- **WHEN** the host firewall rules are rendered for any inventory
- **THEN** no inter-node rule names the control database's address or the subnet as a whole
