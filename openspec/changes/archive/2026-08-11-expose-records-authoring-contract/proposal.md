## Why

First-class Records are callable, but a generic client cannot create the first collection in an empty vault without discovering the manifest grammar one validation error at a time. The public command accepts opaque `manifest_text`, targeted inspection requires a known collection, and there is no read-only route that proves a create request is valid before mutation.

## What Changes

- Add agent-facing `describe` and read-only `validate` actions to the existing `record_memory` front door.
- Let collection-less `inspect` return a bounded, governance-filtered inventory of first-class collections and legacy trackers.
- Return the complete versioned manifest contract, all closed enums, exact constraints, and generic minimal and laboratory-panel examples only when an agent deliberately requests `describe`.
- Keep ordinary bootstrap compact: advertise the discovery and validation workflow plus the small set of facts needed to find the agent-facing contract, without exposing parser internals to the user-facing experience.
- Add structured remediation details to manifest-validation failures while preserving the existing stable codes and fail-closed parser.
- Preserve `_collection.md` as ordinary human-owned Markdown and keep `manifest_text` as the sole create representation in this change.

## Capabilities

### Modified Capabilities

- `records`: make first-collection authoring discoverable and safely preflightable.
- `structured-collections`: expose the binding manifest contract and bounded inventory without changing canonical storage.
- `command-surface`: extend the finite Records selector and classify the new actions as read-only across generated surfaces.
- `agent-bootstrap-contract`: route generic clients to the detailed agent contract without embedding implementation detail in normal output.

## Impact

- `record_memory`, collection parsing/discovery, bootstrap projection, generated MCP/REST/CLI schemas, and focused acceptance tests.
- The intentional `record_memory` tool-schema change requires regenerating the governed schema/capability artifacts and advancing only the pending tool-surface fingerprint.
- No Record migration, collection creation, item append, or existing tracker rewrite is performed by this change.
