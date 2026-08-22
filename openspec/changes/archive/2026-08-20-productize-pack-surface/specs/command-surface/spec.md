# command-surface

## MODIFIED Requirements

### Requirement: Command Registry Carries Simple Action Metadata
The command registry SHALL expose enough metadata to derive the simple product
action catalog without maintaining a separate operation list. That product
metadata SHALL mark tools as primary or advanced, map simple user actions to
typed tools, and provide pack-aware guidance for first-run and selected-pack
workflows without duplicating business logic across MCP, REST, and CLI surfaces.

#### Scenario: Action metadata is registry-derived
- **WHEN** the product action catalog is built
- **THEN** it derives command routes from registry metadata
- **AND** canonical commands remain available on their original MCP, REST, and CLI surfaces

#### Scenario: Advanced tools remain discoverable
- **WHEN** a tool is not part of the primary simple action flow
- **THEN** it remains listed as advanced rather than hidden or removed
- **AND** tier-2 and destructive-operation controls continue to apply

#### Scenario: Front-door metadata is pack-aware
- **WHEN** bootstrap or documentation renders the product front door
- **THEN** each simple action maps to typed tools
- **AND** the response can include selected-pack workflows and agent
  instructions
- **AND** advanced tools remain visible but secondary

#### Scenario: Typed tools remain authoritative
- **WHEN** an agent follows a simple action such as save, ask, prove, review,
  update, adopt, or connect
- **THEN** the actual operation still routes through the existing typed tool
  contracts
- **AND** pack metadata never bypasses write governance
