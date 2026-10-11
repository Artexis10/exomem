-- Imported from Substrate 47ef7757da2a1a6089eadcf1f0cb7d6fd67583a8:migrations/0060_exomem_cloud_imports.sql
-- C5 is the content-free request/observation contract shared with cellctl.
-- The protocol fixes these state and kind enums; neither contains tenant vocabulary.
ALTER TABLE exomem_cloud_cells DROP CONSTRAINT exomem_cloud_cells_hold_kind_check;
ALTER TABLE exomem_cloud_cells ADD CONSTRAINT exomem_cloud_cells_hold_kind_check
  CHECK (hold_kind IS NULL OR hold_kind IN ('upgrade', 'backup', 'restore', 'import'));

CREATE TABLE exomem_cloud_imports (
  import_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  cell_id text NOT NULL REFERENCES exomem_cloud_cells(cell_id) ON DELETE RESTRICT,
  kind text NOT NULL CHECK (kind IN ('adopt', 'restore', 'archive_members')),
  declared_bytes bigint NOT NULL CHECK (declared_bytes >= 0),
  declared_files bigint NOT NULL CHECK (declared_files >= 0),
  requested_state text NOT NULL DEFAULT 'requested'
    CHECK (requested_state IN ('requested', 'uploaded', 'cancelled', 'discard_requested')),
  part_etags jsonb CHECK (part_etags IS NULL OR jsonb_typeof(part_etags) = 'array'),
  -- SHA-256 is a fixed hexadecimal format, not content classification.
  ciphertext_sha256 text CHECK (ciphertext_sha256 IS NULL OR ciphertext_sha256 ~ '^[a-f0-9]{64}$'),
  ciphertext_bytes bigint CHECK (ciphertext_bytes IS NULL OR ciphertext_bytes >= 0),
  recipient text,
  upload_id text,
  part_urls jsonb CHECK (part_urls IS NULL OR jsonb_typeof(part_urls) = 'array'),
  urls_expire_at timestamptz,
  observed_state text CHECK (observed_state IS NULL OR observed_state IN (
    'key_ready', 'importing', 'done', 'failed', 'expired', 'discarded'
  )),
  error_code text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CHECK ((ciphertext_sha256 IS NULL) = (ciphertext_bytes IS NULL)),
  CHECK (part_etags IS NULL OR ciphertext_sha256 IS NOT NULL),
  CHECK (requested_state <> 'uploaded' OR part_etags IS NOT NULL)
);

CREATE UNIQUE INDEX exomem_cloud_imports_open_cell_idx ON exomem_cloud_imports (cell_id)
  WHERE observed_state IS NULL OR observed_state IN ('key_ready', 'importing');
CREATE INDEX exomem_cloud_imports_cell_created_idx ON exomem_cloud_imports (cell_id, created_at DESC);

CREATE FUNCTION exomem_cloud_imports_touch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'UPDATE' THEN
    IF ROW(NEW.import_id, NEW.cell_id, NEW.kind, NEW.declared_bytes, NEW.declared_files)
       IS DISTINCT FROM ROW(OLD.import_id, OLD.cell_id, OLD.kind, OLD.declared_bytes, OLD.declared_files)
       OR (OLD.ciphertext_sha256 IS NOT NULL AND
         ROW(NEW.ciphertext_sha256, NEW.ciphertext_bytes) IS DISTINCT FROM
         ROW(OLD.ciphertext_sha256, OLD.ciphertext_bytes))
       OR (OLD.part_etags IS NOT NULL AND NEW.part_etags IS DISTINCT FROM OLD.part_etags) THEN
      RAISE EXCEPTION 'import request and accepted report are immutable' USING ERRCODE = '23514';
    END IF;
  END IF;
  NEW.updated_at := now();
  IF TG_OP = 'INSERT' THEN
    PERFORM pg_notify('exomem_cloud_cells', NEW.cell_id);
  ELSIF ROW(NEW.requested_state, NEW.ciphertext_sha256, NEW.ciphertext_bytes, NEW.part_etags)
        IS DISTINCT FROM ROW(OLD.requested_state, OLD.ciphertext_sha256, OLD.ciphertext_bytes, OLD.part_etags) THEN
    PERFORM pg_notify('exomem_cloud_cells', NEW.cell_id);
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER exomem_cloud_imports_touch_trigger
BEFORE INSERT OR UPDATE ON exomem_cloud_imports
FOR EACH ROW EXECUTE FUNCTION exomem_cloud_imports_touch();

-- The ordinary CLI uses the existing RFC 8252 loopback redirect rule.
-- Replay must never re-enable a client that an operator disabled.
INSERT INTO exomem_oauth_clients (
  client_id, admission_mode, enabled, redirect_uris, redirect_uris_digest
) VALUES (
  'exomem-cloud-cli', 'pinned', true, '["http://127.0.0.1/callback"]'::jsonb,
  digest(convert_to('["http://127.0.0.1/callback"]'::jsonb::text, 'utf8'), 'sha256')
) ON CONFLICT (client_id) DO NOTHING;
