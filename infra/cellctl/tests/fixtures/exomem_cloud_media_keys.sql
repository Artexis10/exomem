-- Mirrors Substrate migration 0081_exomem_cloud_media_keys.sql and its stanza in
-- scripts/exomem-cloud-grants.sql. It stands in for both until the contract pin in
-- substrate-contract.json reaches a Substrate commit that has them; the pinned files
-- then replace this one.

-- Migration 0081: a cell's media key bundle, wrapped under its cell root key.
ALTER TABLE exomem_cloud_cells
  ADD COLUMN media_key_wrapped bytea,
  ADD COLUMN media_key_version integer;

-- The grants stanza. It runs after the pinned grants script, which revokes every earlier
-- grant on the cell table. cellctl already reads the whole table.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exomem_cellctl') THEN
    GRANT UPDATE (media_key_wrapped, media_key_version) ON exomem_cloud_cells TO exomem_cellctl;
  END IF;
END
$$;
