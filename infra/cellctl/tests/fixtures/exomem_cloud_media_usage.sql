-- Mirrors the Substrate migration that adds a cell's media usage columns (add-cloud-media-store 4.9) and its
-- grants stanza. It stands in for both until the contract pin in substrate-contract.json reaches a Substrate
-- commit that has them; the pinned files then replace this one.

-- A cell with no media store, or one that never reported, has all four NULL. A set time with a NULL figure
-- states that figure as unknown, and Home shows "unknown" for it, never 0.
ALTER TABLE exomem_cloud_cells
  ADD COLUMN vault_used_bytes bigint CHECK (vault_used_bytes >= 0),
  ADD COLUMN media_used_bytes bigint CHECK (media_used_bytes >= 0),
  ADD COLUMN media_allowance_bytes bigint CHECK (media_allowance_bytes >= 0),
  ADD COLUMN media_usage_observed_at timestamptz,
  ADD CONSTRAINT exomem_cloud_cells_media_usage_observed CHECK (
    (vault_used_bytes IS NULL AND media_used_bytes IS NULL AND media_allowance_bytes IS NULL)
    OR media_usage_observed_at IS NOT NULL);

-- The grants stanza. It runs after the pinned grants script, which revokes every earlier grant on the cell
-- table. cellctl already reads the whole table.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exomem_cellctl') THEN
    GRANT UPDATE (vault_used_bytes, media_used_bytes, media_allowance_bytes, media_usage_observed_at)
      ON exomem_cloud_cells TO exomem_cellctl;
  END IF;
END
$$;
