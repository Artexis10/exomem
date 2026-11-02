-- Mirrors Substrate migration 0082_exomem_cloud_operation_grants.sql and its stanzas in
-- scripts/exomem-cloud-grants.sql. It stands in for both until the contract pin in
-- substrate-contract.json reaches a Substrate commit that has them; the pinned files
-- then replace this one.

-- Migration 0082: the signed grant for an export and for an import, nullable while no grant key is configured.
ALTER TABLE exomem_cloud_exports ADD COLUMN operation_grant text;
ALTER TABLE exomem_cloud_imports ADD COLUMN operation_grant text;

-- The grants stanzas. Substrate's role writes the grant, once with an export request and once when an import
-- upload completes. exomem_cellctl reads it with its table SELECT and holds no write on it.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'substrate_app') THEN
    GRANT INSERT (operation_grant) ON exomem_cloud_exports TO substrate_app;
    GRANT UPDATE (operation_grant) ON exomem_cloud_imports TO substrate_app;
  END IF;
END
$$;
