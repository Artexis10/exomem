## ADDED Requirements

### Requirement: Lease Status And Release CLI

An ops-only CLI, `exomem lease status|release [--yes] [--json]`, SHALL let an
operator inspect writer-lease state and release lease authority without
touching the coordinator's storage directly. `status` SHALL report the
current holder, fencing token, TTL remaining, and renewer liveness. `release`
SHALL work against the currently configured replica or, given the target
holder's replica id and fencing token, against a different replica's held
lease, without requiring any coordinator or worker protocol change. This CLI
is not exposed as an MCP tool or REST endpoint.

#### Scenario: An operator inspects lease state

- **WHEN** an operator runs `exomem lease status`
- **THEN** the current holder, fencing token, and TTL remaining are reported

#### Scenario: An operator releases the local replica's lease

- **WHEN** an operator runs `exomem lease release --yes` on the current
  holder
- **THEN** the lease is released and another replica can subsequently acquire
  it

#### Scenario: An operator releases another replica's lease cross-device

- **WHEN** an operator runs `exomem lease release` naming a different
  replica's held fencing token
- **THEN** that replica's lease authority is released through the
  coordinator without requiring a change to the coordinator or worker

#### Scenario: Destructive takeover is not offered

- **WHEN** an operator inspects the lease CLI's available subcommands
- **THEN** no `steal` or `force-acquire` subcommand exists, because release
  combined with preferred-writer reclaim already hands over authority within
  roughly one lease TTL using the existing fencing proof
