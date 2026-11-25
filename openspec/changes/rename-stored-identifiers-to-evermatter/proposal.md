# Proposal

## Why

The product is Evermatter, and the rename now covers every name, not only the ones an agent sees. Stored identifiers still carry the old name:
- the `exomem_id` key in every note's frontmatter;
- the `exomem://` scheme in every reference that agents read and write;
- the state and configuration paths;
- the Python import package;
- the Cloud infrastructure names.

Each one is stored in users' notes, on their machines or in live Cloud resources. A rename must therefore lose no data, keep every existing reference resolving, and be reversible until it is complete.

## What Changes

- **Protocol constants stay.** Salts, token tags, audiences, the issuer, hash domains, stored fences and schema ids keep their bytes, because stored data depends on them. The in-vault `.exomem` directory also keeps its name.
- **Note identity.** Readers accept `evermatter_id` and `exomem_id` behind one accessor, and writers emit `evermatter_id`. A governed maintenance job then renames the key in a vault's notes when its owner asks. It skips any note edited since its plan, and its rollback is the same rename in reverse.
- **Reference scheme.** New references and MCP resources use `evermatter://`. Every `exomem://` reference keeps resolving forever.
- **Python package.** The import package becomes `evermatter`. An `exomem` alias package keeps the same module objects and `python -m exomem` working through the compatibility window.
- **Machine-local state.** An explicit command renames the state directory, the mode config and the import journals. Readers fall back to the old paths until then.
- **Cloud infrastructure.** Labels, metric names, units, the Helm release and images move readers first, one family at a time. Namespaces, secrets and cellctl tables keep their names, and new resources take the new name.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `exomem-identity`: stored identifiers move to the product name without data loss.

## Impact

- About 330 uses of `exomem_id` in 57 modules, and about 150 uses of `exomem://` in 49 modules, move behind one accessor and one emitter each.
- Every module and test imports the package, so the package rename lands in an announced freeze window. It changes no protocol constant.
- Users' own Obsidian queries and templates that name `exomem_id` stop matching after the rewrite. The job's preview lists the notes that contain such queries.
- Vaults under git or a sync service see one change to every note.
- The Cloud rename needs production grants and runs after `move-cloud-to-evermatter-hosts`.
