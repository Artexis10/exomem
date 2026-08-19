## 1. Location Resolution

- [x] 1.1 Add `vault.shipped_schema_root(vault_root)` returning the directory the
      shipped markdown should be READ from: `.exomem/schema/` when it holds
      `SKILL.md`, else the legacy `Knowledge Base/_Schema/`.
- [x] 1.2 Add `vault.shipped_schema_target(vault_root)` returning where it should
      be WRITTEN — always the new location, so a refresh migrates the read path
      forward without deleting anything.
- [x] 1.3 Unit-test both against the three vault shapes: legacy only, new only,
      both present (new wins).

## 2. Vault Identity

- [x] 2.1 Widen `_is_vault` to accept either sentinel.
- [x] 2.2 Test all three shapes plus a directory with neither, because this
      function decides whether `resolve_vault`, `product_invoke`, `doctor` and the
      hosted runtime will speak to a directory at all.
- [x] 2.3 Grep every `_is_vault` caller and confirm none re-derives the sentinel
      path itself instead of asking.

## 3. Deployment

- [x] 3.1 Point `refresh_shipped_schema` at `shipped_schema_target`, keeping its
      byte-compare so a current vault is still a no-op and the file watcher sees
      no churn.
- [x] 3.2 Point `init_vault` at the same target, and stop creating
      `Knowledge Base/_Schema/SKILL.md` for a new vault.
- [x] 3.3 Keep the per-vault YAML registries, `contracts/`, `relation-reviews/`,
      `private-skills/` and the activation manifest exactly where they are.
- [x] 3.4 Test that a fresh vault has no product-owned markdown under
      `Knowledge Base/` and still has its registries there.

## 4. Readers

- [x] 4.1 Route `schema.py` (references), `hosted_runtime.py` (SKILL.md) and
      `doctor.py` (both checks) through `shipped_schema_root`.
- [x] 4.2 Grep every remaining `"_Schema"` path construction and classify each as
      product-owned (route it) or per-vault (leave it); record the classification
      in the PR.
- [x] 4.3 Test a migrated vault reads from the new location and an unmigrated one
      from the legacy location, through the real consumers rather than only the
      resolver.
- [x] 4.4 Carry the index exclusion across: add the new directory to
      `VAULT_SCAN_SKIP_DIRS` so `find(scope="vault")` and the incremental
      patcher both skip it, and test both walks.

## 5. Explicit Migration

- [x] 5.1 Add a migration that removes legacy product-owned markdown only when
      the new location holds identical bytes, restricted to
      `_SHIPPED_SCHEMA_GLOBS`, returning what it removed and what it declined.
- [x] 5.2 Test the declined path: a legacy file whose bytes differ is kept and
      named.
- [x] 5.3 Test that a user-authored file inside `Knowledge Base/_Schema/` is never
      touched.
- [x] 5.4 Test that refresh and read paths remove nothing, so an upgrade cannot
      delete from a user's vault as a side effect.
- [x] 5.5 Expose it as an explicit command and report the bytes reclaimed.

## 6. Evidence

- [x] 6.1 Build a vault before and after, and record the `Knowledge Base/`
      markdown count and byte total in the PR — the measurement #488 opens with.
- [x] 6.2 Confirm the scaffold leak guard still passes.
- [x] 6.3 Validate the OpenSpec change artifacts.
