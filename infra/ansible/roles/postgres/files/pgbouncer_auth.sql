-- Managed by Ansible (role: postgres). Applied to the exomem_control database.
--
-- This file creates only the PgBouncer auth-query plumbing (D12): a
-- restricted, LOGIN-only role that PgBouncer uses to look up SCRAM
-- verifiers through a SECURITY DEFINER function, so no static userlist file
-- has to be kept in sync with role passwords by hand. It does not touch the
-- C1 tables or their privileges -- that is Substrate's own
-- scripts/exomem-cloud-grants.sql (task 4.4), copied byte-for-byte into
-- infra/cellctl/tests/fixtures/exomem_cloud_grants.sql for the role test.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'pgbouncer_auth') THEN
        CREATE ROLE pgbouncer_auth LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS pgbouncer AUTHORIZATION postgres;

CREATE OR REPLACE FUNCTION pgbouncer.get_auth(p_usename TEXT)
RETURNS TABLE(usename TEXT, passwd TEXT)
LANGUAGE sql SECURITY DEFINER
-- A SECURITY DEFINER function runs with the defining role's (postgres,
-- the schema owner) privileges but, without this, the caller's search_path
-- -- which a lower-privileged caller could otherwise juggle, e.g. by
-- creating an object that shadows pg_catalog.pg_shadow ahead of it on their
-- own search_path. Pinning it removes that class of attack entirely.
SET search_path = pg_catalog AS
$$
    -- round 8, nit: a superuser's real SCRAM verifier must never be
    -- returned through this function. NOT filtering the row out entirely
    -- (an earlier version of this fix did, via `AND NOT usesuper`): the
    -- admin unix socket's own `local pgbouncer postgres ... peer` line
    -- (pgbouncer_hba.conf.j2, NC-A) needs get_auth('postgres') to resolve
    -- to a row at all for PgBouncer to recognize `postgres` as a known
    -- user in the first place -- confirmed live that PgBouncer's own
    -- lookup-before-dispatch (it resolves the username via auth_query
    -- before applying whichever HBA line's auth method actually admits or
    -- refuses the connection) reports "FATAL: no such user" for either
    -- caller (postgres itself, or a refused OS user) once the row was
    -- filtered out entirely, breaking the admin console outright and
    -- masking the peer-auth refusal test behind an unrelated error. `peer`
    -- never inspects the passwd column anyway (it checks the OS uid, not a
    -- credential), so nulling passwd for a superuser satisfies "the
    -- verifier is never returned" without breaking that recognition step.
    SELECT usename::TEXT, (CASE WHEN usesuper THEN NULL ELSE passwd END)::TEXT AS passwd
    FROM pg_catalog.pg_shadow
    WHERE usename = p_usename;
$$;

REVOKE ALL ON FUNCTION pgbouncer.get_auth(TEXT) FROM PUBLIC;
GRANT USAGE ON SCHEMA pgbouncer TO pgbouncer_auth;
GRANT EXECUTE ON FUNCTION pgbouncer.get_auth(TEXT) TO pgbouncer_auth;
