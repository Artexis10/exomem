-- add-cloud-media-store D5: the cell's media key bundle, wrapped under its cell root key.
-- Stand-in for a Substrate migration that has not shipped: both columns stay unset until
-- cellctl mints a bundle for a cell the media edge serves, and stay unset for any other cell.
ALTER TABLE exomem_cloud_cells
  ADD COLUMN media_key_wrapped bytea,
  ADD COLUMN media_key_version integer;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exomem_cellctl') THEN
    GRANT UPDATE (media_key_wrapped, media_key_version) ON exomem_cloud_cells TO exomem_cellctl;
  END IF;
END
$$;
