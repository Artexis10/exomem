## Why

`Knowledge Base/_Schema/` holds 17 product-owned markdown files — 265 KB in the
package, 404 KB as observed on a real vault — inside the user's note directory.
Exomem excludes them from its own index (`VAULT_SCAN_SKIP_DIRS`, `vault.py:203`).
Nothing else can.

Every other member of that skip set is a dot-directory: `.obsidian`, `.git`,
`.graph-coordination`, `.graph-commit-receipts`, `.trash`. `_Schema` is the only
non-dot member, and the only one sitting where Obsidian's quick switcher, graph
view, and any second indexer treat it as user notes. #488 measured the
consequence: a natural-language query against the same vault returned three
scaffold documents ahead of every real note.

The product has already decided this content is not user notes. It just placed
it where every other vault consumer decides otherwise.

## What #488 asked for that is already done

The issue's headline pairs two harms: the location, and a second copy that had
drifted against `install-skill`. **The drift half is fixed.**
`init.refresh_shipped_schema` re-deploys the product-owned files from the bundled
package on every setup, byte-comparing first so a current vault is untouched, and
`_SHIPPED_SCHEMA_GLOBS` names exactly the product-owned set so the per-vault YAML
registries are never overwritten. There is one source of truth today:
`src/exomem/_scaffold/_Schema/`.

What that leaves is the location, and it is now a smaller change than the issue
frames, because the vault copy is already a redeployable cache of the package
rather than an independent original.

## The constraint the issue did not account for

`vault._is_vault` is, literally:

```python
def _is_vault(path: Path) -> bool:
    return (path / kb_dirname() / "_Schema" / "SKILL.md").exists()
```

`SKILL.md` is the **vault sentinel**. "Stop copying them into the vault", taken at
face value, un-identifies every existing vault — `resolve_vault`, `product_invoke`,
`doctor` and the hosted runtime all key off this. Any move has to carry the
sentinel deliberately rather than discover it in production.

## What Changes

- Deploy the product-owned markdown to **`<vault>/.exomem/schema/`** — a
  dot-directory, so it inherits the treatment every other non-note directory in
  the vault already gets, from Obsidian and from any other indexer, without
  exomem having to ask for it.
- **Per-vault configuration stays** in `Knowledge Base/_Schema/`:
  `project-keys.yaml`, `relation-registry.yaml`,
  `semantic-language-registry.yaml`, `traversal-profiles.yaml`,
  `source-taxonomy.yaml`, `contracts/`, `relation-reviews/`, `private-skills/`,
  and the activation manifest. These are the user's, they are small, they are not
  markdown, and #488 explicitly scopes them out. Only the shipped markdown moves.
- Readers resolve **new location first, legacy second**, so an existing vault
  keeps working untouched until something migrates it.
- `_is_vault` accepts **either** sentinel, so a vault is still a vault before and
  after.
- Reclaiming the 404 KB is a **separate, explicit step**, not a side effect of an
  upgrade: a migration removes the legacy copies only after verifying the new
  location holds the same bytes, and only for paths matching
  `_SHIPPED_SCHEMA_GLOBS`. Nothing else in `_Schema/` is ever touched.

## Impact

- Affected specs: `vault-scaffold-layout` (new capability).
- Affected code: `init.py` (`refresh_shipped_schema`, `init_vault`), `vault.py`
  (`_is_vault`), `schema.py:62`, `hosted_runtime.py:1352`, `doctor.py:321`/`:419`,
  and the setup wizard.
- **Deliberately not a silent migration.** An upgrade that deletes 404 KB from
  inside a user's Obsidian vault without being asked is the wrong default, even
  when the bytes are reproducible — the vault is the artifact the product
  promises the user owns.
- Rollback is a redeploy: the package is the source, so the legacy location can
  be repopulated by `refresh_shipped_schema` at any time.
