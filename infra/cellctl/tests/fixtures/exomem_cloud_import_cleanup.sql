-- Imported from Substrate 123f86d3b43fc4faaa5853a0fa97610c91fd6012:migrations/0061_exomem_cloud_import_cleanup.sql
-- C5 cleanup remains open while the controller retries bounded cleanup Jobs.
-- These states and discard intent are fixed by the shared import protocol.
ALTER TABLE exomem_cloud_imports
  DROP CONSTRAINT exomem_cloud_imports_observed_state_check;
ALTER TABLE exomem_cloud_imports
  ADD CONSTRAINT exomem_cloud_imports_observed_state_check
  CHECK (observed_state IS NULL OR observed_state IN (
    'key_ready', 'importing', 'cleanup_pending', 'done', 'failed', 'expired', 'discarded'
  ));

DROP INDEX exomem_cloud_imports_open_cell_idx;
CREATE UNIQUE INDEX exomem_cloud_imports_open_cell_idx ON exomem_cloud_imports (cell_id)
  WHERE observed_state IS NULL OR observed_state IN ('key_ready', 'importing', 'cleanup_pending')
    OR (requested_state = 'discard_requested' AND observed_state IS DISTINCT FROM 'discarded');
