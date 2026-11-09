# Proposal

## Why

On 2026-11-06 a managed upgrade to a release that raised the graph schema version could not install.
The standby proved that the inherited graph sidecar belonged to the older schema, then waited for an adoptable snapshot until its warm budget expired.
The supervisor discarded the standby and kept the old worker serving, so the release could not be installed through the managed upgrade.
No wait can make an older build's sidecar adoptable, and the promoted worker rebuilds the graph whether it cuts over or starts cold.

## What Changes

- A standby reports the graph snapshot component settled when its adoption proof declines with a verdict that waiting is not expected to clear: graph work is disabled, or the sidecar is absent, another build's or corrupt. A rare transient that reads the same way costs one full graph handoff after promotion, not a refused upgrade.
- Promotion records such a snapshot as `unproven` and carries no graph handoff, so the promoted worker runs the whole graph handoff, which is the one-time rebuild.
- A decline that can clear, such as an unreadable predecessor or an oversized residue, still holds cutover until the budget expires.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `managed-service-upgrades`: a graph snapshot found unusable for the build no longer holds cutover readiness, in the same way that a failed semantic corpus build does not.
- `instant-start`: the cutover component set names the graph snapshot as ready when it is proved or found unusable for the build.

## Impact

The change affects `service_standby.cutover_components` and adds one property to `epistemic_graph.SnapshotAdoption`.
It changes no promotion step, budget or supervisor behaviour. An older supervisor reads the target release's standby readiness, so the fix takes effect without a supervisor upgrade.
