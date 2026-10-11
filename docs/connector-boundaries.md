<!-- authority:non-specification -->

# Connector content boundaries

A host can give authenticated clients of one owner different content ceilings.
The ceiling does not change owner identity or grant write authority.
Ordinary policy and RAW protection still apply.

This capability covers connector admission, portable protection, and private vocabulary instances.
Managed-vault import remains a separate delivery and requires separate operational authority.

## Host configuration

Set `EXOMEM_CONNECTOR_BOUNDARY_CONFIG` to an absolute JSON file path outside the vault.
The host operator owns this file. Connector requests cannot amend it.
The service and maintenance process must read the same configuration.

```json
{
  "version": 1,
  "default_denied_scope_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"],
  "capture_paths": ["Knowledge Base/Capture"],
  "clients": [
    {
      "issuer": "https://memory.example.test",
      "client_id": "registered-client-id",
      "denied_scope_ids": ["01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    }
  ]
}
```

Replace the example Scope ID, issuer, client ID, and folder with host values.
Scope IDs must name existing canonical Scope definitions.
The default set must be nonempty and contain every client's denied Scope set.
An empty `denied_scope_ids` for a configured client permits content that passes the remaining policy and RAW checks.

Client mappings use verified OAuth or local authentication records.
Client names, user agents, supplied headers, and tool arguments do not establish a mapping.
Unknown clients, new registrations, legacy delegated capabilities, and shared cell credentials receive the restricted default.
Hosted cells need verified end-client provenance before they can select an individual client mapping.

## Arm a stopped destination

Arming requires a stopped, drained runtime with its existing external state available.
Enrollment makes the protection durable; removing the configuration cannot disable it.
An older runtime that lacks this compatibility capability must refuse the armed state.

1. Stop and drain the runtime through its service manager, leaving no active writers.
2. Configure protective Scopes and capture folders, ensuring that the restricted default admits every existing capture item.
3. Set the host configuration path for maintenance and service startup, using the same JSON file.
4. Run the maintenance command below, expecting compatibility enrollment and publication of the portable requirement.
5. Start the runtime through its service manager, expecting startup to validate the configuration and portable requirement.

```sh
exomem maintain --migrate-state --offline --arm-connector-boundary --vault "$EXOMEM_VAULT_PATH"
```

Maintenance also prepares the existing immutable semantic activation manifest.
The activation manifest remains unavailable to limited clients.
If arming stops after compatibility enrollment, startup refuses the incomplete state.
Repeat the maintenance command with the valid configuration to complete publication.

## Capture and maintenance

Limited clients create new objects only in configured capture folders.
An explicit destination outside those folders receives an unavailable result before collision checks.
Exomem does not silently redirect an explicit destination.
Existing admitted content can still be edited outside capture folders.

Capture folders must remain visible to the restricted default across all writers, including unrestricted owners.
Placing private metadata in a capture folder does not make that metadata public.
The writer refuses a change that violates the folder's visibility requirement.
All writers use the configured folder spelling. Arming refuses existing portable aliases of that folder or its parents.
Limited clients cannot read or amend whole-policy state, global inventories, or protected internal artifacts.

Client mappings take effect on subsequent admission checks and result consumption.
Changing the protected Scope set, its selectors, or capture folders requires stopped maintenance and compatible protection state.
Do not delete the portable requirement or compatibility enrollment to repair a configuration error.

## Vocabulary assignment

Each instance combines the shipped core with one extension.
Private instances do not inherit the public extension.
The same extension key, alias, or folder can therefore have separate meanings in public and private instances.

Add a `vocabulary` member to host configuration before stopped arming:

```json
{
  "public": {
    "namespace": "Knowledge Base/_Schema/public",
    "history": "public",
    "overrides": {
      "entity-types": {
        "overlay": "Knowledge Base/_Schema/entity-types.yaml",
        "history": "entity-types"
      }
    }
  },
  "private": {
    "01ARZ3NDEKTSV4RRFFQ69G5FAV": {
      "namespace": "Knowledge Base/_Schema/private",
      "history": "private"
    }
  },
  "destinations": {},
  "selections": {}
}
```

`namespace` holds the instance overlays. `history` names its prefix under `_Schema/history/`.
Registry definitions supply filenames and history stems within these explicitly assigned locations.
Overrides name physical registry storage, so source kinds and domains share the `source-taxonomy` override.
Assign every existing legacy overlay and history explicitly; the example assigns only the entity registry.
Storage for different instances cannot overlap.

Private keys must name canonical protective Scopes in the restricted default.
Canonical page membership selects applicable private instances.
If several apply, `selections` maps the page's vault-relative path to one applicable Scope ID.
`destinations` maps an independently configured destination folder to a private Scope ID or `null` for public.
These bindings do not grant read, write, or creation authority.

Registry operations select public when `registry_scope` is omitted.
Supply a canonical Scope ID for an admitted private instance.
Prospective creation can explicitly select `public` or a canonical private Scope ID before resolving a type or folder.
The resulting destination and authored metadata must agree with that selection.
Ambiguous bindings make dependent vocabulary operations unavailable; ordinary admitted reads remain available.

Private saves and restores keep full reasons and hashes in protected instance history.
They do not read, change, or rotate the shared operation log.
Public saves retain their ordinary log entries.
Instance usage counts remain unavailable until the maintained projection can separate their contributors.

Stopped arming writes these bindings into version 2 of the existing portable requirement.
Runtime membership reads that requirement; connector mappings remain external host configuration.
An older runtime rejects the new requirement before serving.
Legacy version 1 keeps its protection but requires explicit stopped assignment before dependent registry operations become available.

## Export and restore

Armed exports use manifest version 2 and carry the protective Scope selectors, their fingerprint, and capture folders.
Assigned vocabulary namespaces, overlays, history, and page bindings travel with the same protected export.
Unarmed exports retain manifest version 1.
Content-limited callers cannot export the whole vault.

A stopped restore installs missing protective definitions through the destination's canonical policy owner.
An existing Scope ID with different selectors refuses the restore.
The export does not transfer source client mappings, sessions, grants, rules, audiences, or custody.
The destination host must supply compatible configuration before the restored runtime can serve requests.

A failed protected publication retains the destination directory and its external state for an exact retry.
Keep that directory in place: its identity can bind destination authority.
Retry with the same archive, operation, destination, and compatible host configuration.
Restore verifies complete archive bytes without treating unresolved configuration as readable content.
If optional rebuilding changes protected bytes, restore retains the evidence and reports an integrity failure.

Supported backup and restore retain protection across a new external state root.
Raw filesystem copies and arbitrary historical binaries are outside this portability contract.

Mutation retry keys now distinguish verified connectors, including before arming.
The same connector keeps its new retry history across reauthentication.
The runtime does not adopt older retry results that identify only the owner.
An unguarded append retried across this upgrade can therefore append again.
Upload and download capabilities are minted fresh and never enter the durable retry cache.
