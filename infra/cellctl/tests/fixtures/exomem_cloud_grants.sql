-- Idempotent least-privilege grants for the Exomem Cloud control plane
-- (design D7). This script is the single implementation of:
--
-- 1. Schema-wide `substrate_app` DML (D7): "substrate_app receives SELECT,
--    INSERT, UPDATE and DELETE on every table in the public schema, and
--    USAGE and SELECT on every sequence. ALTER DEFAULT PRIVILEGES FOR ROLE
--    substrate_owner extends the same grants to tables and sequences
--    created by later migrations. The only exceptions are C1 through C1d."
-- 2. The C1 privilege table in the companion Exomem design's "Shared
--    contracts with Substrate" section (`adopt-exomem-cloud-plain-cells/design.md`):
--
--   | Role            | SELECT                          | INSERT / UPDATE |
--   |-----------------|----------------------------------|------------------|
--   | substrate_app   | every C1-C1d column              | insert C1 identity and desired columns; update C1 desired columns, cancellation_notice_sent_at, C1b, and C1d paused, error_code, held_cell_id |
--   | exomem_cellctl  | every C1 column, C1b, C1c, C1d   | update C1 observed columns only; insert and update C1c; update C1d paused, error_code, held_cell_id, last_good_image, updated_at |
--   | exomem_gateway  | C1 cell_id, tenant_id, desired_state | none on C1-C1d; insert and update the rate-limit buckets |
--
--    "The script revokes substrate_app's schema-wide grants on those four
--    tables and then applies the exact table. That makes the script the
--    single implementation of the C1 table, and a rerun converges to the
--    same state." (D7)
-- 3. exomem_gateway's column-scoped SELECT on exactly the OAuth tables and
--    columns `findCloudOAuthAccessToken` (cloud-oauth.ts) reads (security
--    review finding 3) -- the gateway connects to Postgres directly with
--    its own role and runs that lookup itself.
--
-- Applied by the migration runner (scripts/migrate.ts) after every run --
-- including one against a migrationsDir that never creates the Cloud
-- tables at all, as the runner's own generic-runner tests do -- and by the
-- cutover runbook after restore. Every block below checks that both the
-- granting role and the target table/role exist, so this script is a no-op
-- ahead of either precondition rather than failing the whole run: an
-- unreached IF branch is never planned, so a GRANT or REVOKE naming a role
-- or table that does not yet exist in this database causes no error. Every
-- grant is a plain idempotent GRANT, so a second run changes nothing.
--
-- `current_schema()`, not a hardcoded `public`: in production that resolves
-- to `public` (the ordinary search_path), and in a test that runs this
-- script against a per-test schema via `-c search_path=<schema>,public`, it
-- resolves to that schema -- the same one every unqualified CREATE TABLE in
-- migrations/*.sql lands in.

DO $$
DECLARE
  target_schema text := current_schema();
  cloud_role text;
BEGIN
  -- 1. Schema-wide substrate_app DML, plus default privileges for whatever a
  -- later migration creates. Guarded on substrate_app existing so a database
  -- that has provisioned no Cloud roles at all (e.g. most of this repo's own
  -- non-grants tests) sees no behaviour change.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'substrate_app') THEN
    EXECUTE format(
      'GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA %I TO substrate_app',
      target_schema
    );
    EXECUTE format(
      'GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA %I TO substrate_app',
      target_schema
    );
  END IF;

  -- ALTER DEFAULT PRIVILEGES FOR ROLE substrate_owner: guarded on the role
  -- existing (D7 round item 1). Must be run by substrate_owner itself or a
  -- superuser -- both are true of every path that applies this script
  -- (migrations run as substrate_owner through DATABASE_MIGRATION_URL, or as
  -- a superuser in a simpler local rehearsal).
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'substrate_owner')
     AND EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'substrate_app') THEN
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE substrate_owner IN SCHEMA %I GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO substrate_app',
      target_schema
    );
    EXECUTE format(
      'ALTER DEFAULT PRIVILEGES FOR ROLE substrate_owner IN SCHEMA %I GRANT USAGE, SELECT ON SEQUENCES TO substrate_app',
      target_schema
    );
  END IF;

  -- 2. C1-C1d are the only exception to the schema-wide grant above: revoke
  -- whatever it (and PUBLIC) just gave every role on these four tables, then
  -- apply the exact C1 privilege table below. A rerun of this whole script
  -- therefore always converges to the same state, never accumulating a
  -- wider grant than the table names.
  IF to_regclass('exomem_cloud_cells') IS NOT NULL THEN
    REVOKE ALL ON exomem_cloud_cells, exomem_cloud_settings, exomem_cloud_capacity, exomem_cloud_rollout
      FROM PUBLIC;
    FOREACH cloud_role IN ARRAY ARRAY['substrate_app', 'exomem_cellctl', 'exomem_gateway'] LOOP
      IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = cloud_role) THEN
        EXECUTE format(
          'REVOKE ALL ON exomem_cloud_cells, exomem_cloud_settings, exomem_cloud_capacity, exomem_cloud_rollout FROM %I',
          cloud_role
        );
      END IF;
    END LOOP;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'substrate_app')
     AND to_regclass('exomem_cloud_cells') IS NOT NULL THEN
    GRANT SELECT ON exomem_cloud_cells, exomem_cloud_settings, exomem_cloud_capacity, exomem_cloud_rollout
      TO substrate_app;
    GRANT INSERT (cell_id, tenant_id, storage_gib, rollout_priority, desired_state, desired_image)
      ON exomem_cloud_cells TO substrate_app;
    GRANT UPDATE (desired_state, desired_image) ON exomem_cloud_cells TO substrate_app;
    -- Migration 0057 (item 5 / task 3.7): the cancellation-notice-sent claim
    -- column, now reflected in the design's C1 privilege table above. Guarded
    -- on the COLUMN's own existence, not just the table's: 0057 lands one
    -- migration after exomem_cloud_cells itself (0056), and grants run after
    -- every migration invocation -- including one that, like
    -- migration-upgrade.integration.test.ts, deliberately stops partway
    -- through an upgrade path -- so a column-list GRANT naming a column the
    -- table does not yet have would otherwise fail the whole script.
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = target_schema AND table_name = 'exomem_cloud_cells'
        AND column_name = 'cancellation_notice_sent_at'
    ) THEN
      GRANT UPDATE (cancellation_notice_sent_at) ON exomem_cloud_cells TO substrate_app;
    END IF;
    GRANT INSERT, UPDATE ON exomem_cloud_settings TO substrate_app;
    GRANT UPDATE (paused, error_code, held_cell_id) ON exomem_cloud_rollout TO substrate_app;
    -- exomem_tenants and its mirror columns (reconcileCloudCellDesiredState,
    -- cloud-lifecycle.ts) are not C1-C1d and are not an exception to the
    -- schema-wide grant above -- the narrow column-scoped grants this round
    -- used to carry here are gone; the schema-wide grant already covers them.
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exomem_cellctl')
     AND to_regclass('exomem_cloud_cells') IS NOT NULL THEN
    GRANT SELECT ON exomem_cloud_cells, exomem_cloud_settings, exomem_cloud_capacity, exomem_cloud_rollout
      TO exomem_cellctl;
    GRANT UPDATE (
      observed_generation, observed_state, observed_image, ready, last_error_code, observed_at,
      node, volume_id, last_backup_at, last_backup_snapshot,
      backup_key_wrapped, backup_key_version, b2_key_id, b2_key_wrapped, b2_key_version,
      hold_kind, hold_started_at
    ) ON exomem_cloud_cells TO exomem_cellctl;
    GRANT INSERT, UPDATE ON exomem_cloud_capacity TO exomem_cellctl;
    GRANT UPDATE (paused, error_code, held_cell_id, last_good_image, updated_at)
      ON exomem_cloud_rollout TO exomem_cellctl;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exomem_gateway')
     AND to_regclass('exomem_cloud_cells') IS NOT NULL THEN
    GRANT SELECT (cell_id, tenant_id, desired_state) ON exomem_cloud_cells TO exomem_gateway;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exomem_gateway')
     AND to_regclass('exomem_rate_limit_buckets') IS NOT NULL THEN
    -- "Insert and update the rate-limit buckets" (D7) reuses the existing
    -- shared upsert (db.ts's takeRateLimit), whose ON CONFLICT DO UPDATE
    -- SET/WHERE clauses read the row's own prior window_started_at and
    -- admitted_count to decide whether to reset or increment the window.
    -- PostgreSQL requires SELECT on any column an ON CONFLICT DO UPDATE
    -- expression reads from the target table, so that grant is included
    -- here even though the C1 privilege table's SELECT column does not
    -- name it -- exomem_rate_limit_buckets is not part of C1-C1d, and
    -- withholding SELECT here would make the mandated write inoperable
    -- rather than more private: the table holds only rate-limit bucket
    -- metadata, not tenant or cell data. Guarded on its own existence,
    -- separately from exomem_cloud_cells above, because it is a
    -- pre-existing hosted table rather than part of migration 0056.
    GRANT SELECT, INSERT, UPDATE ON exomem_rate_limit_buckets TO exomem_gateway;
  END IF;

  -- 3. Security review finding 3: exomem_gateway also runs
  -- findCloudOAuthAccessToken (cloud-oauth.ts) itself, connecting to
  -- Postgres directly (per the Exomem design, never through PgBouncer).
  -- Column-scoped SELECT on exactly what that query reads -- no C1-C1d
  -- columns beyond what it already has above, and never a write on any of
  -- these tables.
  --
  -- Each grant below is guarded on its own table's existence rather than
  -- one guard for the whole block, and the exomem_oauth_clients grant is
  -- additionally guarded on its CIMD-admission columns' existence (migration
  -- 0034, three migrations after the table itself in 0025). The five tables
  -- here were not all created together -- exomem_oauth_account_blocks is
  -- 0031, exomem_oauth_admitted_cimd_hosts is 0048 -- and grants run after
  -- every migration invocation, including one that stops partway through a
  -- historical upgrade path (migration-upgrade.integration.test.ts), so a
  -- statement naming a table or column that does not exist yet at that point
  -- would otherwise fail the whole script.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'exomem_gateway') THEN
    IF to_regclass('exomem_oauth_access_tokens') IS NOT NULL THEN
      GRANT SELECT (access_digest, revoked_at, expires_at, resource, family_id, grant_id, client_id, scopes)
        ON exomem_oauth_access_tokens TO exomem_gateway;
    END IF;
    IF to_regclass('exomem_oauth_token_families') IS NOT NULL THEN
      GRANT SELECT (id, revoked_at, expires_at) ON exomem_oauth_token_families TO exomem_gateway;
    END IF;
    IF to_regclass('exomem_oauth_grants') IS NOT NULL THEN
      GRANT SELECT (id, user_id, tenant_id, revoked_at) ON exomem_oauth_grants TO exomem_gateway;
    END IF;
    IF EXISTS (
      SELECT 1 FROM information_schema.columns
      WHERE table_schema = target_schema AND table_name = 'exomem_oauth_clients'
        AND column_name = 'client_platform'
    ) THEN
      GRANT SELECT (
        id, client_id, enabled, redirect_uris_digest, redirect_uris, admission_mode,
        metadata_document_digest, metadata_fetched_at, metadata_ttl_seconds, metadata_expires_at,
        cimd_host, client_platform
      ) ON exomem_oauth_clients TO exomem_gateway;
    END IF;
    IF to_regclass('exomem_oauth_admitted_cimd_hosts') IS NOT NULL THEN
      GRANT SELECT (host, platform) ON exomem_oauth_admitted_cimd_hosts TO exomem_gateway;
    END IF;
    IF to_regclass('exomem_oauth_account_blocks') IS NOT NULL THEN
      GRANT SELECT (tenant_id, owner_user_id) ON exomem_oauth_account_blocks TO exomem_gateway;
    END IF;
  END IF;
END
$$;
