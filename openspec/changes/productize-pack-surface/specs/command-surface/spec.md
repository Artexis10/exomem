# command-surface

## MODIFIED Requirements

### Requirement: Product surface metadata is registry-derived
The command registry SHALL expose product metadata that marks tools as primary
or advanced, maps simple user actions to typed tools, and provides pack-aware
guidance for first-run and selected-pack workflows without duplicating business
logic across MCP, REST, and CLI surfaces.

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