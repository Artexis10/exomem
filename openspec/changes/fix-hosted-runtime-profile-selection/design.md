## Context

See proposal.md for the observed failure. `HostedCellConfig.active_agent_profile` currently returns v1 or v2 from `lifecycle_actions_enabled`. The provisioner already has `runtimeTarget.agentProfile`, but `_fixed_helm_values` does not forward it. The cell chart has no corresponding environment input. Existing v3/v4 route tests replace the profile-selection property.

## Goals / Non-Goals

The selected deployment profile must reach the authenticated runtime contract unchanged. Older charts must continue to boot with their original v1/v2 behavior. Public callers cannot select a different profile. This repair does not change command membership, Records lifecycle authorization, admission, or promotion.

## Decisions

1. Add optional `agent_profile` to `HostedCellConfig`, parsed from `EXOMEM_HOSTED_AGENT_PROFILE`. Resolve an explicit nonempty value using the canonical hosted profile registry. Unknown values fail closed before routes are registered. Profiles containing `record_memory` require Records reader version 2. The existing lifecycle-enabled/reader-version check remains in force.
2. With no explicit profile, preserve the current v1/v2 derivation. Use an empty default for the new optional cell chart value `agentProfile` and omit its environment variable when empty. This supports existing rendered values and older deployments without changing their exposed surface.
3. `_fixed_helm_values` forwards `target.agentProfile` when present. The target already follows the authenticated and verified deployment contract; no additional request-controlled selection is introduced. The serve StatefulSet passes the value to the runtime. Initialization does not serve agent routes and does not need selection to provision the filesystem.
4. Keep Records lifecycle action enablement independent. Explicit v3/v4 selection with actions disabled still serves the selected profile; the existing `revise`/`rebaseline` guard continues to reject those actions. Do not widen the existing provider feature-flag policy as part of this fix.
5. Regression tests must follow producer output: provisioner target to chart values, rendered StatefulSet environment to `HostedCellConfig.from_env`, then real authenticated profile contract routes. The v4 assertion derives command membership and digests from the canonical builder. Never stub `active_agent_profile` in this regression.

## Risks / Trade-offs

- A new provisioner with an old runtime can still select an unsupported profile. Existing private health identity verification remains mandatory and prevents binding a mismatched cell.
- Explicit profile selection can expose newer existing commands. The selected target is trusted configuration, registry resolution rejects unknown names, and all existing command authorization and protected-tree guards remain active.
- Updating the running failed candidate requires a new verified image composition. Preserve the failed cell until recovery eligibility is established; this implementation does not directly patch its filesystem, database, or admission records.

## Migration Plan

Build and verify updated runtime and provisioner candidates, compose them using the existing deployment-lock workflow, and deploy through the existing operator path. Confirm the selected profile's authenticated contract and command digest before binding or promotion. Legacy deployment rollback retains its original profile selection when the new field is absent.
