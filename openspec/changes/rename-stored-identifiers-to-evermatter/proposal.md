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

- **Note identity.** Readers accept `evermatter_id` and `exomem_id` behind one accessor, and writers emit `evermatter_id`. Then a governed maintenance job rewrites each vault's notes with a backup, once per vault. The job is resumable and reversible. Cloud vaults run it after the Cloud moves to the `evermatter.ai` hosts.
- **Reference scheme.** New references use `evermatter://`. Every `exomem://` reference keeps resolving forever. The job does not rewrite references inside note text.
- **Python package.** The import package becomes `evermatter`. An `exomem` package stays through the compatibility window and re-exports it.
- **State and configuration paths.** They move to `evermatter` directories through the existing crash-safe state relocation, which runs only on an explicit command that stops the service first.
- **Cloud infrastructure names.** The Helm release, namespaces, Kubernetes secrets, images and cellctl names move one family at a time. Each family gets its own runbook and a production grant.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `exomem-identity`: stored identifiers move to the product name without data loss.

## Impact

- About 330 uses of `exomem_id` in 57 modules, and about 150 uses of `exomem://` in 49 modules, move behind one accessor and one emitter each.
- Every module and test imports the package, so the package rename is a mechanical sweep with an alias package.
- Users' own Obsidian queries and templates that name `exomem_id` stop matching after the rewrite. The job's preview lists the notes that contain such queries.
- Vaults under git or a sync service see one change to every note.
- The Cloud rename needs production grants and runs after `move-cloud-to-evermatter-hosts`.
