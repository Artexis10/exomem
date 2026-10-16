## ADDED Requirements

### Requirement: A cluster runs on one CPU architecture, x86 or Arm64

The foundation SHALL accept the x86 `cx33` or a Hetzner Arm64 (CAX) type as the fleet server. The K3s role SHALL install the K3s binary pinned for each node's architecture, checked against that architecture's sha256, and SHALL refuse a node whose architecture has no pin. Every node of one cluster SHALL run one architecture; agent types remain the x86 allow-list. The foundation SHALL refuse agents beside a CAX fleet server, and the K3s role SHALL refuse an agent whose architecture differs from the server's. Before a node changes architecture, every image that the cluster runs SHALL list the target architecture. A cell moved to a node of the other architecture SHALL have its vector index rebuilt before it is accepted.

#### Scenario: Arm64 fleet server

- **WHEN** the operator sets `server_type` to a CAX type
- **THEN** the foundation plans that server
- **AND** the K3s role installs the arm64 K3s binary checked against its pinned sha256

#### Scenario: Unpinned architecture

- **WHEN** a node reports an architecture with no pinned K3s binary
- **THEN** the role's validation fails before any download

#### Scenario: Unsupported server type

- **WHEN** the operator sets `server_type` to an x86 type other than `cx33`
- **THEN** the foundation plan fails validation

#### Scenario: Mixed-architecture cluster

- **WHEN** the operator plans a CAX fleet server with any agent
- **THEN** the foundation plan fails validation
- **AND** the K3s role refuses an agent whose architecture differs from the server's

#### Scenario: A cell moves to the other architecture

- **WHEN** a cell's volume moves to a node of the other architecture
- **THEN** the operator rebuilds the cell's vector index before accepting the cell
