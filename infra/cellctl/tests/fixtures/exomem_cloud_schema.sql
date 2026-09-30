-- Migration 0056 — Exomem Cloud plain-cells schema of record.
--
-- Schema of record for the shared contracts C1 (exomem_cloud_cells), C1b
-- (exomem_cloud_settings), C1c (exomem_cloud_capacity) and C1d
-- (exomem_cloud_rollout), defined in "Shared contracts with Substrate" of the
-- companion Exomem design `adopt-exomem-cloud-plain-cells`. This file must
-- match that section byte-for-byte on columns and privileges; a second copy
-- lives at Exomem's `infra/cellctl/tests/fixtures/exomem_cloud_schema.sql`
-- for cellctl's own tests. Additive only — no existing table is touched.
--
-- The repository migration runner splits on semicolons, so this migration
-- deliberately uses plain DDL plus one dollar-quoted trigger function and
-- contains no other procedural blocks.

CREATE TABLE exomem_cloud_cells (
  -- Identity and placement.
  cell_id text PRIMARY KEY,
  -- Security review finding 16: RESTRICT, not CASCADE -- a tenant row must
  -- never disappear out from under a still-live Cloud cell. The lifecycle
  -- sweep (cloud-lifecycle.ts) always deletes the cell row first and the
  -- tenant only afterwards (or leaves the tenant alone), so this never fires
  -- in the normal reconcile/export-window path; it exists to fail loudly if
  -- something ever tries to delete a tenant directly.
  tenant_id uuid NOT NULL REFERENCES exomem_tenants(id) ON DELETE RESTRICT,
  storage_gib int NOT NULL DEFAULT 10 CHECK (storage_gib > 0),
  rollout_priority int NOT NULL DEFAULT 1,

  -- Desired state, written by Substrate. Every transition here is an UPDATE
  -- of desired_state (and optionally desired_image); the trigger below is
  -- what turns that into a generation bump and a controller notification.
  desired_state text NOT NULL
    CHECK (desired_state IN ('running', 'read_only', 'stopped', 'deleted')),
  desired_image text,
  generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),

  -- Observed state, written by cellctl.
  observed_generation bigint,
  observed_state text
    CHECK (observed_state IS NULL OR observed_state IN (
      'pending', 'provisioning', 'running', 'read_only', 'stopping',
      'stopped', 'deleting', 'deleted', 'failed'
    )),
  observed_image text,
  ready boolean NOT NULL DEFAULT false,
  last_error_code text,
  observed_at timestamptz,
  node text,
  volume_id text,
  last_backup_at timestamptz,
  last_backup_snapshot text,
  backup_key_wrapped bytea,
  backup_key_version int,
  b2_key_id text,
  b2_key_wrapped bytea,
  b2_key_version int,
  hold_kind text CHECK (hold_kind IS NULL OR hold_kind IN ('upgrade', 'backup', 'restore')),
  hold_started_at timestamptz,

  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),

  CHECK (cell_id ~ '^[a-z2-7]{16}$')
);

-- At most one non-deleted cell row per tenant. A tenant may accumulate
-- several `deleted` rows over its lifetime (e.g. a future re-admission), but
-- never two live ones.
CREATE UNIQUE INDEX exomem_cloud_cells_tenant_active_idx
  ON exomem_cloud_cells (tenant_id)
  WHERE desired_state <> 'deleted';

-- Bumps generation and notifies the controller only when a desired column
-- actually changes on UPDATE, so an observed-only write from cellctl (which
-- never touches desired_state or desired_image) never fires it. Always
-- touches updated_at, independent of column-level grants: a BEFORE trigger's
-- writes to NEW are not subject to the calling role's column privileges, so
-- cellctl can keep the timestamp current even though it is never granted
-- UPDATE on the column directly (see scripts/exomem-cloud-grants.sql).
--
-- Security review finding 16: also fires BEFORE INSERT, unconditionally --
-- a brand new cell row is itself a fresh desired state the controller has
-- never seen, and previously only an immediately-following UPDATE would
-- have woken it up (or nothing would, if the row's very first desired_state
-- was its final one, e.g. admission's `stopped` starting state). No
-- generation bump on INSERT: the column's own DEFAULT 1 is already the
-- correct starting generation.
CREATE FUNCTION exomem_cloud_cells_touch() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  NEW.updated_at := now();
  IF TG_OP = 'INSERT' THEN
    PERFORM pg_notify('exomem_cloud_cells', NEW.cell_id);
  ELSIF NEW.desired_state IS DISTINCT FROM OLD.desired_state
     OR NEW.desired_image IS DISTINCT FROM OLD.desired_image THEN
    NEW.generation := OLD.generation + 1;
    PERFORM pg_notify('exomem_cloud_cells', NEW.cell_id);
  END IF;
  RETURN NEW;
END;
$$;

CREATE TRIGGER exomem_cloud_cells_touch_trigger
BEFORE INSERT OR UPDATE ON exomem_cloud_cells
FOR EACH ROW EXECUTE FUNCTION exomem_cloud_cells_touch();

-- C1b. Holds cell_image, written by Substrate's owner release route.
CREATE TABLE exomem_cloud_settings (
  key text PRIMARY KEY,
  value jsonb
);

-- C1c. Written by cellctl.
CREATE TABLE exomem_cloud_capacity (
  node text PRIMARY KEY,
  cell_slots int NOT NULL DEFAULT 0 CHECK (cell_slots >= 0),
  attachments_used int NOT NULL DEFAULT 0 CHECK (attachments_used >= 0),
  observed_at timestamptz
);

-- C1d. A single row, writable by cellctl and the owner release route.
CREATE TABLE exomem_cloud_rollout (
  id int PRIMARY KEY CHECK (id = 1),
  paused boolean NOT NULL DEFAULT false,
  error_code text,
  held_cell_id text REFERENCES exomem_cloud_cells(cell_id) ON DELETE SET NULL,
  last_good_image text,
  updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO exomem_cloud_rollout (id) VALUES (1);

-- Migration 0057 — Exomem Cloud cancellation notice dedupe column.
--
-- Item 5 / task 3.7 (`adopt-exomem-cloud-plain-cells`): a Cloud tenant whose
-- Paddle subscription becomes cancelled gets a one-time notice email stating
-- the read-only export window and the deletion date. Redelivery of the
-- triggering Paddle webhook must not send it twice, so the send is claimed
-- with the same atomic "UPDATE ... WHERE column IS NULL" idiom every other
-- one-time write in this schema already uses (e.g. exomem_invites.consumed_at).
--
-- Lives on exomem_cloud_cells rather than the shared exomem_entitlements
-- table: this is Cloud-only bookkeeping, and keeping it here needs no grant
-- changes beyond what scripts/exomem-cloud-grants.sql already gives
-- substrate_app on this table.

ALTER TABLE exomem_cloud_cells
  ADD COLUMN cancellation_notice_sent_at timestamptz;
