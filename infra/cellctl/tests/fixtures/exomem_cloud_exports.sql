-- Migration 0066 (Exomem Cloud part) — C6 owner exports, C1 measured disk use, export hold.
-- The shared C6 contract fixes these state, hold and error tokens; none is tenant vocabulary.
ALTER TABLE exomem_cloud_cells DROP CONSTRAINT exomem_cloud_cells_hold_kind_check;
ALTER TABLE exomem_cloud_cells ADD CONSTRAINT exomem_cloud_cells_hold_kind_check
  CHECK (hold_kind IS NULL OR hold_kind IN ('upgrade', 'backup', 'restore', 'import', 'export'));

-- Observed by cellctl from every backup and export Job's statvfs report. All three are
-- written together, so a reader sees either a whole measurement or none (unknown).
ALTER TABLE exomem_cloud_cells
  ADD COLUMN used_bytes bigint,
  ADD COLUMN filesystem_bytes bigint,
  ADD COLUMN usage_observed_at timestamptz,
  ADD CONSTRAINT exomem_cloud_cells_usage_check CHECK (
    (used_bytes IS NULL) = (filesystem_bytes IS NULL)
    AND (used_bytes IS NULL) = (usage_observed_at IS NULL)
    AND (used_bytes IS NULL OR (filesystem_bytes > 0 AND used_bytes >= 0 AND used_bytes <= filesystem_bytes))
  );

CREATE TABLE exomem_cloud_exports (
  export_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cell_id text NOT NULL REFERENCES exomem_cloud_cells(cell_id) ON DELETE RESTRICT,
  -- Substrate writes the request and the link request.
  requested_state text NOT NULL DEFAULT 'requested'
    CHECK (requested_state IN ('requested', 'superseded')),
  link_requested_at timestamptz,
  -- cellctl writes the observation.
  observed_state text CHECK (observed_state IS NULL OR observed_state IN ('packing', 'ready', 'failed', 'expired')),
  error_code text,
  archive_bytes bigint CHECK (archive_bytes IS NULL OR archive_bytes > 0),
  -- SHA-256 is a fixed hexadecimal format, not content classification.
  archive_sha256 text CHECK (archive_sha256 IS NULL OR archive_sha256 ~ '^[a-f0-9]{64}$'),
  -- The object version cellctl verified at completion; every link pins it.
  archive_version_id text,
  upload_id text,
  ready_at timestamptz,
  expires_at timestamptz,
  download_url text,
  download_url_expires_at timestamptz,
  link_issued_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((archive_sha256 IS NULL) = (archive_bytes IS NULL)),
  CHECK (observed_state IS DISTINCT FROM 'ready'
    OR (archive_sha256 IS NOT NULL AND ready_at IS NOT NULL AND expires_at IS NOT NULL)),
  CHECK (observed_state IS DISTINCT FROM 'failed' OR error_code IS NOT NULL),
  CHECK ((download_url IS NULL) = (download_url_expires_at IS NULL))
);

-- One export per cell is being packed at a time.
CREATE UNIQUE INDEX exomem_cloud_exports_open_cell_idx ON exomem_cloud_exports (cell_id)
  WHERE observed_state IS NULL OR observed_state = 'packing';
CREATE INDEX exomem_cloud_exports_cell_created_idx ON exomem_cloud_exports (cell_id, created_at DESC);

-- A request, a supersede and a link request wake cellctl; its own observation writes do not.
CREATE FUNCTION exomem_cloud_exports_touch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF ROW(NEW.export_id, NEW.cell_id, NEW.created_at) IS DISTINCT FROM ROW(OLD.export_id, OLD.cell_id, OLD.created_at)
       OR (OLD.requested_state = 'superseded' AND NEW.requested_state <> 'superseded') THEN
      RAISE EXCEPTION 'export request identity is immutable and a supersede is final' USING ERRCODE = '23514';
    END IF;
  END IF;
  NEW.updated_at := now();
  IF TG_OP = 'INSERT' THEN
    PERFORM pg_notify('exomem_cloud_cells', NEW.cell_id);
  ELSIF ROW(NEW.requested_state, NEW.link_requested_at)
        IS DISTINCT FROM ROW(OLD.requested_state, OLD.link_requested_at) THEN
    PERFORM pg_notify('exomem_cloud_cells', NEW.cell_id);
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER exomem_cloud_exports_touch_trigger
BEFORE INSERT OR UPDATE ON exomem_cloud_exports
FOR EACH ROW EXECUTE FUNCTION exomem_cloud_exports_touch();
