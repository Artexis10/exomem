"""OM-5/OM-6: run the REAL `postgres` Ansible role against a `postgres:17`
Debian container (not a hand-rendered template rig) and assert the
properties the security review asked for directly: verify-full TLS per
role, public refusal of the exomem roles, refused cross-role writes, the
nftables/PgBouncer limits actually rendered AND live, and a full pgBackRest
backup + the real weekly restore-verify script round-tripping a marker row
against the same MinIO the rig already stands up for pgbackrest's TLS-to-S3
requirement (OM-6, round 6).

Systemd cannot run as PID 1 in this non-privileged container (confirmed:
`postgres:17` ships no `/lib/systemd/systemd` binary at all, and making one
work would need capabilities/mounts this environment's safety guard blocks:
--privileged, a host cgroup mount, a host namespace). Per the coordinator's
ruling, every task and handler that manages a systemd unit is tagged
`services`; this test runs the role with `--skip-tags services` and starts
the two long-running processes itself: `pg_ctlcluster` for PostgreSQL and
`pgbouncer -d` for PgBouncer, both as the postgres OS user, exactly as the
role's own systemd units would (`User=postgres` for pgbouncer; the
`postgresql@17-main` unit wraps pg_ctlcluster for postgresql). The systemd
units themselves (all six: postgresql@17-main, pgbouncer,
exomem-postgres-nftables, certbot.timer, exomem-pgbackrest-full.timer,
exomem-pgbackrest-restore-verify.timer) are exercised for real by the actual
`systemctl is-active` check task 6.1 adds for the real apply on the fresh
control server in P4 -- a failure there costs a rerun on a server nothing
depends on yet, so this test does not need to re-prove systemd wiring works.

Gated behind RUN_CONTROL_DB_ROLE_TEST=1, same convention as
tests/test_hosted_k3s_admission.py's RUN_K3S_RUNTIME_TEST. Skipped by
default because it needs Docker and a non-privileged --cap-add=NET_ADMIN
container.

Note on the C1 privilege table (round 6, task 4.4): this role itself creates
only the four roles; Substrate's scripts/exomem-cloud-grants.sql, the single
implementation of the C1 privilege table, is Substrate's own script, copied
byte-for-byte into infra/cellctl/tests/fixtures/exomem_cloud_grants.sql
(alongside exomem_cloud_schema.sql, a byte-for-byte copy of Substrate
migrations 0056+0057) -- the same fixture paths cellctl's own tests use, and
the ones the coordinator refreshes from Substrate lane C's worktree at
integration. The test_c1_grants_* tests below apply both fixtures for real
and assert the resulting C1 privileges live. test_cross_role_writes_refused
above is a different, still-valid scenario -- the baseline before any grants
script has ever run at all, which this role alone is responsible for (task
4.4's own scope: create the four roles with no default privileges).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ROLE_DIR = ROOT / "infra/ansible/roles/postgres"

RUN_LIVE = os.environ.get("RUN_CONTROL_DB_ROLE_TEST") == "1"
DOCKER = shutil.which("docker")
ANSIBLE_PLAYBOOK = os.environ.get("ANSIBLE_PLAYBOOK_BIN") or shutil.which("ansible-playbook")

pytestmark = [
    pytest.mark.skipif(
        not RUN_LIVE,
        reason="set RUN_CONTROL_DB_ROLE_TEST=1 to run the real postgres role in a postgres:17 container",
    ),
    # pyproject.toml sets a repo-wide 60s pytest-timeout, far too short for
    # Docker network/container setup, several apt installs, and a real
    # ansible-playbook run of the whole role. Other slow gated live tests in
    # this suite (e.g. test_hosted_k3s_governance_drill.py) override it the
    # same way.
    pytest.mark.timeout(900),
]

TAG = uuid.uuid4().hex[:8]
PRIVATE_NETWORK = f"exomem-ctl-role-priv-{TAG}"
PUBLIC_NETWORK = f"exomem-ctl-role-pub-{TAG}"
PRIVATE_SUBNET = "10.92.0.0/24"
PUBLIC_SUBNET = "10.93.0.0/24"
DB_PRIVATE_IP = "10.92.0.10"
DB_PUBLIC_IP = "10.93.0.10"
PRIVATE_PROBER_IP = "10.92.0.20"
PUBLIC_PROBER_IP = "10.93.0.20"

DB_CONTAINER = f"exomem-ctl-role-db-{TAG}"
MINIO_CONTAINER = f"exomem-ctl-role-minio-{TAG}"
PRIVATE_PROBER = f"exomem-ctl-role-priv-prober-{TAG}"
PUBLIC_PROBER = f"exomem-ctl-role-pub-prober-{TAG}"
HOSTNAME = "db.control-role-test.internal"
# Matches roles/postgres/defaults/main.yml's postgres_pgbackrest_stanza
# default; host_vars below never overrides it.
PGBACKREST_STANZA = "control"

ROLE_PASSWORDS = {
    "substrate_owner": "CHANGE_owner_pw_1",
    "substrate_app": "CHANGE_app_pw_1",
    "exomem_gateway": "CHANGE_gw_pw_1",
    "exomem_cellctl": "CHANGE_cellctl_pw_1",
}

ALL_CONTAINERS = [DB_CONTAINER, MINIO_CONTAINER, PRIVATE_PROBER, PUBLIC_PROBER]
ALL_NETWORKS = [PRIVATE_NETWORK, PUBLIC_NETWORK]


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    assert DOCKER is not None, "docker is required for RUN_CONTROL_DB_ROLE_TEST=1"
    return subprocess.run([DOCKER, *args], capture_output=True, text=True, check=check)


def _dbexec(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _docker("exec", DB_CONTAINER, *args, check=check)


def _dbexec_pg(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return _docker("exec", "-u", "postgres", DB_CONTAINER, *args, check=check)


def _cleanup_docker() -> None:
    # Scoped to this run's own TAG only (item 4, round 7): a prior version
    # swept every exomem-ctl-role-* container/network by prefix, which is
    # unsafe with two sessions running this test concurrently under
    # different TAGs -- one run's cleanup could tear down another's live
    # rig mid-test. A crashed prior run's orphaned resources under a
    # different TAG are no longer auto-swept; clean them up by hand
    # (`docker rm -f -v`/`docker network rm` by name) if a subnet-overlap
    # error ever appears.
    # -v (round 8): the postgres:17 and minio images each declare an
    # anonymous VOLUME; a bare `rm -f` detaches and leaves those volumes
    # behind forever (they carry no TAG, so nothing else here ever sweeps
    # them). -v removes each container's own anonymous volumes along with
    # it -- never a named/external volume, which this rig never creates.
    for name in ALL_CONTAINERS:
        _docker("rm", "-f", "-v", name, check=False)
    for net in ALL_NETWORKS:
        _docker("network", "rm", net, check=False)


@pytest.fixture(scope="module")
def rig(tmp_path_factory: pytest.TempPathFactory):
    _cleanup_docker()
    work = tmp_path_factory.mktemp("control-db-role-live")
    try:
        tls_dir = work / "tls"
        tls_dir.mkdir()

        _run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-days", "3",
                "-nodes", "-keyout", str(tls_dir / "ca.key"), "-out", str(tls_dir / "ca.crt"),
                "-subj", "/CN=Exomem Control Role Test CA",
            ]
        )
        _run(
            [
                "openssl", "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(tls_dir / "server.key"), "-out", str(tls_dir / "server.csr"),
                "-subj", f"/CN={HOSTNAME}",
            ]
        )
        ext_file = tls_dir / "server.ext"
        ext_file.write_text(f"subjectAltName=DNS:{HOSTNAME}\n")
        _run(
            [
                "openssl", "x509", "-req", "-in", str(tls_dir / "server.csr"),
                "-CA", str(tls_dir / "ca.crt"), "-CAkey", str(tls_dir / "ca.key"),
                "-CAcreateserial", "-out", str(tls_dir / "server.crt"), "-days", "3",
                "-extfile", str(ext_file),
            ]
        )

        # pgBackRest always speaks TLS to an S3 endpoint (there is no plain-HTTP
        # option in pgbackrest.conf.j2); repo1-storage-verify-tls=n only skips
        # certificate-chain validation, so MinIO still needs a real (self-signed
        # is fine) certificate or pgbackrest fails with a TLS handshake error.
        minio_certs_dir = work / "minio-certs"
        minio_certs_dir.mkdir()
        _run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048", "-days", "3", "-nodes",
                "-keyout", str(minio_certs_dir / "private.key"),
                "-out", str(minio_certs_dir / "public.crt"),
                "-subj", "/CN=minio", "-addext", "subjectAltName=DNS:minio",
            ]
        )

        _docker("network", "create", "--subnet", PRIVATE_SUBNET, PRIVATE_NETWORK)
        _docker("network", "create", "--subnet", PUBLIC_SUBNET, PUBLIC_NETWORK)

        # A host bind mount for the certs dir (item 5, round 7) leaves
        # root-owned residue under the host tmp dir that pytest's own tmp_path
        # cleanup cannot delete (observed as repeated "Directory not empty"
        # warnings across every round since the MinIO TLS fix). docker cp copies
        # into the container's own filesystem instead, owned by whatever the
        # container's root process already is, with nothing left on the host
        # side to clean up. The certs have to land before minio's own process
        # starts reading --certs-dir, so the container starts idle
        # (--entrypoint sleep) and the real server process is started by hand
        # afterward, the same pattern already used for DB_CONTAINER/PgBouncer.
        _docker(
            "run", "-d", "--name", MINIO_CONTAINER, "--network", PRIVATE_NETWORK,
            "--network-alias", "minio",
            "-e", "MINIO_ROOT_USER=minioadmin", "-e", "MINIO_ROOT_PASSWORD=minioadmin123",
            "--entrypoint", "sleep",
            "quay.io/minio/minio:latest", "infinity",
        )
        _docker("exec", MINIO_CONTAINER, "mkdir", "-p", "/root/.minio/certs")
        _docker("cp", str(minio_certs_dir / "private.key"), f"{MINIO_CONTAINER}:/root/.minio/certs/private.key")
        _docker("cp", str(minio_certs_dir / "public.crt"), f"{MINIO_CONTAINER}:/root/.minio/certs/public.crt")
        _docker(
            "exec", "-d", MINIO_CONTAINER,
            "minio", "server", "/data", "--certs-dir", "/root/.minio/certs",
        )
        _docker(
            "run", "--rm", "--network", PRIVATE_NETWORK, "--entrypoint", "sh",
            "quay.io/minio/mc:latest", "-c",
            "for i in $(seq 1 30); do "
            "mc alias set --insecure ctl https://minio:9000 minioadmin minioadmin123 >/dev/null 2>&1 && break; "
            "sleep 1; done; "
            "mc alias set --insecure ctl https://minio:9000 minioadmin minioadmin123 "
            "&& mc mb --insecure ctl/control-db-role-test",
        )

        _docker(
            "run", "-d", "--name", DB_CONTAINER, "--network", PRIVATE_NETWORK,
            "--ip", DB_PRIVATE_IP, "--cap-add=NET_ADMIN", "--hostname", "control-role-test",
            "postgres:17", "sleep", "infinity",
        )
        _docker("network", "connect", "--ip", DB_PUBLIC_IP, PUBLIC_NETWORK, DB_CONTAINER)

        _docker(
            "run", "-d", "--name", PRIVATE_PROBER, "--network", PRIVATE_NETWORK,
            "--ip", PRIVATE_PROBER_IP, "postgres:17", "sleep", "infinity",
        )
        _docker(
            "run", "-d", "--name", PUBLIC_PROBER, "--network", PUBLIC_NETWORK,
            "--ip", PUBLIC_PROBER_IP, "postgres:17", "sleep", "infinity",
        )
        for prober in (PRIVATE_PROBER, PUBLIC_PROBER):
            _docker(
                "exec", "--user", "root", prober, "bash", "-c",
                f"echo '{DB_PRIVATE_IP} {HOSTNAME}' >> /etc/hosts",
            )
        # DB_CONTAINER also needs to resolve its own certificate hostname: the
        # verify-full TLS assertions connect to it as a client of its own
        # PgBouncer listener (test_verify_full_tls_admits_substrate_roles_...,
        # test_cross_role_writes_refused's owner-role setup), and it never had
        # an /etc/hosts entry for HOSTNAME at all before this. Loopback is
        # correct here (not DB_PRIVATE_IP): libpq's verify-full checks the
        # server certificate's CN/SAN against the literal `host=` string given,
        # not the socket's actual peer address, so which local address is used
        # to reach the same container's own listener does not matter.
        _docker(
            "exec", "--user", "root", DB_CONTAINER, "bash", "-c",
            f"echo '127.0.0.1 {HOSTNAME}' >> /etc/hosts",
        )

        # The CA cert this test signed the server certificate with is only ever
        # written to the HOST's tls_dir -- every DSN's sslrootcert={ca} argument
        # is that host path, but the psql commands that actually use it run
        # *inside* containers via `docker exec`. Confirmed live: without this,
        # every verify-full connection from a container fails with "root
        # certificate file ... does not exist". Copying it to the identical
        # absolute path inside each container that runs a client psql (the DB
        # container tests itself; the two probers dial in from their own
        # networks) means the unmodified host-side Path string in each DSN
        # resolves correctly in every container too.
        for client_container in (DB_CONTAINER, PRIVATE_PROBER, PUBLIC_PROBER):
            _docker("exec", "--user", "root", client_container, "mkdir", "-p", str(tls_dir))
            _docker("cp", str(tls_dir / "ca.crt"), f"{client_container}:{tls_dir / 'ca.crt'}")

        # Bootstrap: a Python interpreter (for Ansible), and PostgreSQL 17 itself
        # installed and started BEFORE the role runs. The role's own
        # "Install the control database package set" apt task then finds it
        # already present (no-op), and its "Enable and start PostgreSQL" task is
        # tagged services and skipped, so the role's role-creation tasks
        # (community.postgresql.* -- which need a live connection) have
        # something to connect to. The postgres:17 image ships its own
        # /etc/apt/sources.list.d/pgdg.list with a different signed-by path than
        # the role's own "Add the PGDG apt repository" task writes; removing it
        # here avoids apt's "Conflicting values set for option Signed-By" (a
        # test-rig artifact of this base image, not a network restriction --
        # PGDG is reachable from inside this container via Ansible's own
        # get_url/apt_repository tasks, confirmed in an earlier round of this
        # test).
        # ufw is a prerequisite the postgres role's firewall tasks assume the
        # `base` role already installed (site.yml always runs base before
        # postgres in production). This test exercises the postgres role in
        # isolation, per the coordinator's scope, so it supplies that one
        # prerequisite package itself rather than also running base (which has
        # its own untagged systemd-managed services -- ssh, ufw.service,
        # fail2ban -- out of scope here).
        _dbexec(
            "bash", "-c",
            "rm -f /etc/apt/sources.list.d/pgdg.list /usr/local/share/keyrings/postgres.gpg.asc "
            "&& apt-get update -qq "
            "&& apt-get install -yqq python3 sudo ufw postgresql-17 postgresql-client-17",
        )
        # postgres:17 sets create_main_cluster=false in
        # /etc/postgresql-common/createcluster.conf (it normally manages PGDATA
        # itself via its own entrypoint, not postgresql-common clusters), so the
        # package install above does not auto-create the "main" cluster the way
        # it would on a plain Debian VM. Create it explicitly, at the same
        # default paths (/var/lib/postgresql/17/main,
        # /etc/postgresql/17/main) the role's own config templating targets.
        _dbexec("pg_createcluster", "17", "main")
        _dbexec_pg("pg_ctlcluster", "17", "main", "start")
        for _ in range(30):
            probe = _dbexec_pg("pg_isready", "-q", check=False)
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            raise RuntimeError("PostgreSQL did not become ready after pg_ctlcluster start")

        inventory = work / "inventory.ini"
        inventory.write_text(
            textwrap.dedent(
                f"""\
                [control_nodes]
                {DB_CONTAINER} ansible_connection=community.docker.docker ansible_python_interpreter=/usr/bin/python3
                """
            )
        )

        host_vars_dir = work / "host_vars"
        host_vars_dir.mkdir()
        (host_vars_dir / f"{DB_CONTAINER}.yml").write_text(
            textwrap.dedent(
                f"""\
                ---
                postgres_private_ip: "{DB_PRIVATE_IP}"
                postgres_private_network_cidr: "{PRIVATE_SUBNET}"
                postgres_database_hostname: "{HOSTNAME}"
                postgres_public_port: 6432
                # OM-3 (round 7): fed as the four separate plain-string
                # variables infra/scripts/secret_handoff.py's sops_ansible_vars
                # destination actually produces -- never the postgres_role_passwords
                # dict directly, which secret_handoff.py has no way to write.
                # The role's own defaults/main.yml reassembles the dict from
                # these; this is what actually exercises that reassembly live.
                postgres_substrate_owner_password: "{ROLE_PASSWORDS['substrate_owner']}"
                postgres_substrate_app_password: "{ROLE_PASSWORDS['substrate_app']}"
                postgres_exomem_gateway_password: "{ROLE_PASSWORDS['exomem_gateway']}"
                postgres_exomem_cellctl_password: "{ROLE_PASSWORDS['exomem_cellctl']}"
                postgres_acme_enabled: false
                postgres_tls_source_fullchain: "{tls_dir / 'server.crt'}"
                postgres_tls_source_privkey: "{tls_dir / 'server.key'}"
                postgres_pgbackrest_repo_type: s3
                postgres_pgbackrest_s3_endpoint: "minio:9000"
                postgres_pgbackrest_s3_region: us-east-1
                postgres_pgbackrest_s3_bucket: control-db-role-test
                postgres_pgbackrest_s3_key: minioadmin
                postgres_pgbackrest_s3_key_secret: minioadmin123
                postgres_pgbackrest_s3_verify_tls: false
                postgres_pgbackrest_repo_cipher_pass: "test-role-live-cipher-pass-0123456789"
                """
            )
        )

        # community.postgresql.postgresql_script's `path:` argument (used by the
        # role's "Apply the PgBouncer auth-query plumbing" task, path:
        # pgbouncer_auth.sql -- NC-C, round 7: renamed from grants.sql)
        # does not get Ansible's automatic role-relative files/ search the way
        # ansible.builtin.template/copy do -- it resolves relative to wherever
        # the *playbook file itself* lives. The real site.yml already lives
        # beside roles/ in infra/ansible/, so this works there; a wrapper
        # playbook has to live in the same place for the same lookup to
        # succeed. Written directly under infra/ansible/ (this worktree, not
        # committed, removed in the finally below) rather than a tmp dir.
        playbook = ROOT / "infra/ansible/_control_role_live_test_playbook.yml"
        playbook.write_text(
            textwrap.dedent(
                """\
                ---
                - name: Run only the postgres role against the live-role-test container
                  hosts: control_nodes
                  become: true
                  gather_facts: true
                  roles:
                    - postgres
                """
            )
        )

        try:
            ansible_result = subprocess.run(
                [
                    ANSIBLE_PLAYBOOK, "-i", str(inventory), str(playbook),
                    "--skip-tags", "services",
                    # Belt and suspenders with --skip-tags services: Ansible's
                    # mandatory implicit end-of-play handler flush runs every
                    # still-queued notified handler regardless of its own tags --
                    # only an explicit, similarly-tagged `meta: flush_handlers`
                    # respects tag filtering, and this role's config-template
                    # tasks unconditionally notify a restart handler. handlers/
                    # main.yml guards each handler with
                    # `when: postgres_manage_services`, which `when:` always
                    # honors regardless of flush timing, so this run never
                    # touches systemd even at that final implicit flush.
                    #
                    # NC-B (round 7): the role now asserts
                    # `postgres_manage_services is boolean` and refuses to
                    # proceed with it false unless
                    # postgres_unmanaged_services_test_mode is also true --
                    # production must never silently skip its own units. Both
                    # are passed as ONE JSON-style -e, not bare `-e key=value`:
                    # bare KV extra-vars are stored as the literal string
                    # "false" (confirmed with an isolated ansible-playbook
                    # repro), which fails `is boolean`; JSON-style -e preserves
                    # real Python bool/str types.
                    "-e", json.dumps({
                        "postgres_manage_services": False,
                        "postgres_unmanaged_services_test_mode": True,
                    }),
                ],
                cwd=str(ROOT / "infra/ansible"),
                capture_output=True,
                text=True,
                timeout=240,
            )
        finally:
            playbook.unlink(missing_ok=True)
        if ansible_result.returncode == 0:
            # Pick up the pg_hba.conf/postgresql.conf/TLS material the role just
            # templated -- the same effect "Restart PostgreSQL" would have had,
            # done by hand since that handler is tagged services and was
            # skipped.
            _dbexec_pg("pg_ctlcluster", "17", "main", "restart")
            for _ in range(30):
                probe = _dbexec_pg("pg_isready", "-q", check=False)
                if probe.returncode == 0:
                    break
                time.sleep(1)
            _dbexec(
                "bash", "-c",
                "mkdir -p /var/run/postgresql && chown postgres:postgres /var/run/postgresql",
            )
            _docker(
                "exec", "-d", "-u", "postgres", DB_CONTAINER,
                "pgbouncer", "-d", "/etc/pgbouncer/pgbouncer.ini",
            )
            time.sleep(2)

            # OM-6 (round 6): a pgBackRest round trip against the same MinIO
            # this rig already stands up for pgbackrest.conf.j2's TLS-to-S3
            # requirement -- host_vars above already points the role's own repo
            # config at it (S3, path-style baked into pgbackrest.conf.j2's own
            # repo1-s3-uri-style=path, the "control-db-role-test" bucket, and
            # aes-256-cbc with a test passphrase), so no new role wiring is
            # needed here, only exercising it for real. The marker row is
            # written (through PgBouncer, as the role's own DML path) before the
            # full backup so the backup round trip provably carries real data,
            # not just a clean pgbackrest exit code.
            pgbackrest_marker_note = f"om6-marker-{TAG}"
            marker_setup = _psql(
                _dsn("substrate_owner", HOSTNAME, 6432, tls_dir / "ca.crt"),
                "CREATE TABLE IF NOT EXISTS pgbackrest_marker (id int PRIMARY KEY, note text); "
                f"INSERT INTO pgbackrest_marker (id, note) VALUES (1, '{pgbackrest_marker_note}') "
                "ON CONFLICT (id) DO UPDATE SET note = EXCLUDED.note",
            )
            if marker_setup.returncode != 0:
                raise RuntimeError(
                    f"failed to write the pgBackRest marker row: {marker_setup.stderr}"
                )
            pgbackrest_backup_result = _dbexec_pg(
                "pgbackrest", f"--stanza={PGBACKREST_STANZA}", "--type=full", "backup",
                check=False,
            )

            # Task 4.4 (round 6): apply Substrate's REAL grants script
            # (infra/cellctl/tests/fixtures/exomem_cloud_grants.sql, copied
            # byte-for-byte from Substrate lane C's worktree) to the migrated
            # C1-C1d schema (exomem_cloud_schema.sql, same fixture directory --
            # also used by cellctl's own tests, per the coordinator) and prove
            # the C1 privilege table (design.md's "C1 privileges") live.
            # exomem_tenants is a pre-existing table 0056 only references by FK
            # -- its stub, and one seed cell row the privilege probes act on,
            # live in this test-setup SQL only, never in the fixture files.
            schema_fixture = (ROOT / "infra/cellctl/tests/fixtures/exomem_cloud_schema.sql").read_text()
            grants_fixture = (ROOT / "infra/cellctl/tests/fixtures/exomem_cloud_grants.sql").read_text()
            migration_sql = work / "c1-migration.sql"
            migration_sql.write_text(
                "-- Test-setup-only stub and seed row; never part of the fixture files.\n"
                "CREATE TABLE IF NOT EXISTS exomem_tenants (id uuid PRIMARY KEY);\n"
                "INSERT INTO exomem_tenants (id) VALUES ('11111111-1111-1111-1111-111111111111');\n"
                + schema_fixture
                + "\nINSERT INTO exomem_cloud_cells (cell_id, tenant_id, desired_state) "
                  "VALUES ('aaaaaaaaaaaaaaaa', '11111111-1111-1111-1111-111111111111', 'running');\n"
            )
            grants_sql_file = work / "c1-grants.sql"
            grants_sql_file.write_text(grants_fixture)
            _docker("cp", str(migration_sql), f"{DB_CONTAINER}:/tmp/c1-migration.sql")
            _docker("cp", str(grants_sql_file), f"{DB_CONTAINER}:/tmp/c1-grants.sql")

            owner_dsn = _dsn("substrate_owner", HOSTNAME, 6432, tls_dir / "ca.crt")
            privilege_snapshot_sql = (
                "SELECT string_agg(x, E'\\n' ORDER BY x) FROM ("
                "SELECT c.relname || '|' || COALESCE(c.relacl::text, '') AS x "
                "FROM pg_class c WHERE c.relname IN "
                "('exomem_cloud_cells','exomem_cloud_settings','exomem_cloud_capacity','exomem_cloud_rollout') "
                "UNION ALL "
                "SELECT c.relname || '.' || a.attname || '|' || COALESCE(a.attacl::text, '') AS x "
                "FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid "
                "WHERE c.relname = 'exomem_cloud_cells' AND a.attacl IS NOT NULL"
                ") t"
            )

            c1_migration_result = _docker(
                "exec", DB_CONTAINER, "psql", owner_dsn, "-v", "ON_ERROR_STOP=1",
                "-f", "/tmp/c1-migration.sql",
                check=False,
            )
            if c1_migration_result.returncode != 0:
                raise RuntimeError(
                    f"C1-C1d schema fixture failed to apply: {c1_migration_result.stderr}"
                )
            c1_grants_run_1 = _docker(
                "exec", DB_CONTAINER, "psql", owner_dsn, "-v", "ON_ERROR_STOP=1",
                "-f", "/tmp/c1-grants.sql",
                check=False,
            )
            c1_grants_snapshot_1 = _psql(owner_dsn, privilege_snapshot_sql).stdout
            # A second run of an idempotent grants script must change nothing.
            c1_grants_run_2 = _docker(
                "exec", DB_CONTAINER, "psql", owner_dsn, "-v", "ON_ERROR_STOP=1",
                "-f", "/tmp/c1-grants.sql",
                check=False,
            )
            c1_grants_snapshot_2 = _psql(owner_dsn, privilege_snapshot_sql).stdout
        else:
            pgbackrest_marker_note = None
            pgbackrest_backup_result = None
            c1_grants_run_1 = None
            c1_grants_run_2 = None
            c1_grants_snapshot_1 = None
            c1_grants_snapshot_2 = None

        yield {
            "tls_dir": tls_dir,
            "work": work,
            "ansible_result": ansible_result,
            "pgbackrest_backup_result": pgbackrest_backup_result,
            "pgbackrest_marker_note": pgbackrest_marker_note,
            "c1_grants_run_1": c1_grants_run_1,
            "c1_grants_run_2": c1_grants_run_2,
            "c1_grants_snapshot_1": c1_grants_snapshot_1,
            "c1_grants_snapshot_2": c1_grants_snapshot_2,
        }

    finally:
        _cleanup_docker()


def _psql(dsn: str, sql: str, *, container: str = DB_CONTAINER) -> subprocess.CompletedProcess:
    return _docker("exec", container, "psql", dsn, "-tAc", sql, check=False)


def _dsn(role: str, host: str, port: int, ca: Path, *, dbname: str = "exomem_control") -> str:
    return (
        f"host={host} port={port} dbname={dbname} user={role} "
        f"password={ROLE_PASSWORDS[role]} sslmode=verify-full sslrootcert={ca}"
    )


class TestControlDbRoleLive:
    def test_playbook_run_succeeded(self, rig):
        result = rig["ansible_result"]
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]

    def test_verify_full_tls_admits_substrate_roles_through_pgbouncer(self, rig):
        ca = rig["tls_dir"] / "ca.crt"
        for role in ("substrate_owner", "substrate_app"):
            dsn = _dsn(role, HOSTNAME, 6432, ca)
            completed = _psql(dsn, "select 1")
            assert completed.returncode == 0, completed.stderr

    def test_exomem_roles_refused_through_pgbouncer(self, rig):
        # D12 NEW-1: PgBouncer never admits exomem_gateway/exomem_cellctl at
        # all now, from any source. A bare non-zero exit also passes if the
        # rig never configured PgBouncer at all (item 3, round 7) -- assert
        # the specific refusal text PgBouncer's own HBA rejection produces.
        # Confirmed live: PgBouncer's own auth_hba_file miss does NOT reuse
        # Postgres's "no pg_hba.conf entry for host..." wording (that text
        # is Postgres's own, seen only on the direct-5432 path below) --
        # PgBouncer's is "FATAL: no authentication method is found".
        ca = rig["tls_dir"] / "ca.crt"
        for role in ("exomem_gateway", "exomem_cellctl"):
            dsn = _dsn(role, HOSTNAME, 6432, ca)
            completed = _psql(dsn, "select 1")
            assert completed.returncode != 0
            assert "no authentication method is found" in completed.stderr, completed.stderr

    def test_exomem_roles_admitted_directly_on_private_5432(self, rig):
        ca = rig["tls_dir"] / "ca.crt"
        for role in ("exomem_gateway", "exomem_cellctl"):
            dsn = _dsn(role, HOSTNAME, 5432, ca)
            completed = _psql(dsn, "select 1", container=PRIVATE_PROBER)
            assert completed.returncode == 0, completed.stderr

    def test_public_side_refuses_5432_for_exomem_roles(self, rig):
        # Postgres's own listen_addresses is loopback + the private IP only
        # -- the public-network address has nothing listening on 5432 at
        # all, from an address genuinely outside postgres_private_network_cidr.
        ca = rig["tls_dir"] / "ca.crt"
        dsn = _dsn("exomem_gateway", DB_PUBLIC_IP, 5432, ca)
        completed = _psql(dsn, "select 1", container=PUBLIC_PROBER)
        assert completed.returncode != 0
        # item 3, round 7: a bare non-zero exit also passes if the prober
        # simply can't resolve/route at all -- assert the specific "nothing
        # is listening there" text libpq produces for a refused TCP connect,
        # not some unrelated DNS/routing failure.
        assert "Connection refused" in completed.stderr, completed.stderr

        # Positive control (item 1, round 8): the refusal above also passes
        # trivially on a localhost-only default cluster that never rendered
        # postgres_private_ip into listen_addresses at all -- that would be
        # a false pass, refusing the public IP for the wrong reason. Assert
        # the role actually rendered exomem-postgres.conf.j2's
        # `listen_addresses = '127.0.0.1,{{ postgres_private_ip }}'` live,
        # so the refusal above is proven to come from "nothing is bound to
        # the public IP", not "the role never configured the private one
        # either".
        listen_addresses = _dbexec_pg("psql", "-tAc", "SHOW listen_addresses")
        assert listen_addresses.stdout.strip() == f"127.0.0.1,{DB_PRIVATE_IP}", (
            listen_addresses.stdout + listen_addresses.stderr
        )

    def test_cross_role_writes_refused(self, rig):
        # This role creates only the four roles (task 4.4); the real C1
        # grants script lives in Substrate's own repository and is not
        # present here. This tests the baseline the role alone owns: a
        # table substrate_owner creates, with no grants issued to
        # exomem_gateway at all, refuses exomem_gateway's write -- the
        # correct state before any grants script has run.
        ca = rig["tls_dir"] / "ca.crt"
        owner_dsn = _dsn("substrate_owner", HOSTNAME, 6432, ca)
        setup = _psql(
            owner_dsn,
            "CREATE TABLE IF NOT EXISTS cross_role_probe (id int, desired_state text)",
        )
        assert setup.returncode == 0, setup.stderr

        gateway_dsn = _dsn("exomem_gateway", HOSTNAME, 5432, ca)
        completed = _psql(
            gateway_dsn,
            "INSERT INTO cross_role_probe (id, desired_state) VALUES (1, 'running')",
            container=PRIVATE_PROBER,
        )
        assert completed.returncode != 0
        # item 3, round 7: a bare non-zero exit also passes on a broken
        # connection -- assert Postgres's actual privilege-denial wording so
        # this fails loudly if the refusal ever comes from something else
        # (e.g. a connection error) instead of a real grants check.
        assert "permission denied for" in completed.stderr, completed.stderr

    def test_pgbouncer_limits_live_via_admin_console(self, rig):
        # Unix-socket connections from the OS user PgBouncer itself runs as
        # (postgres) get automatic admin-console access, independent of
        # auth_hba_file/admin_users.
        completed = _docker(
            "exec", "-u", "postgres", DB_CONTAINER,
            "psql", "-h", "/var/run/postgresql", "-p", "6432", "-d", "pgbouncer",
            "-tAc", "SHOW CONFIG",
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        show_config = completed.stdout
        assert "max_user_connections" in show_config and "|50|" in show_config.replace(" ", "")
        assert "max_client_conn" in show_config and "|2000|" in show_config.replace(" ", "")
        assert "client_login_timeout" in show_config and "|10|" in show_config.replace(" ", "")

    def test_pgbouncer_admin_console_peer_auth_refuses_other_os_users(self, rig):
        # NC-A, round 7: pgbouncer_hba.conf.j2's admin-console line was
        # `local pgbouncer postgres trust` -- ANY local OS user on this
        # mode-777 unix socket dir could claim the postgres admin console
        # (the reviewer reached SHOW VERSION as `nobody`). It is now `peer`,
        # which checks the connecting process's real OS uid. Prove both
        # halves live: a non-postgres OS user is refused, postgres is not.
        # Confirmed live: PgBouncer's own peer-auth rejection does not reuse
        # Postgres's "peer authentication failed for user ..." wording (that
        # is Postgres's own message, on its own peer-auth path) -- PgBouncer
        # reports "FATAL: unix socket login rejected" for the same case.
        as_nobody = _docker(
            "exec", "-u", "nobody", DB_CONTAINER,
            "psql", "-h", "/var/run/postgresql", "-p", "6432", "-d", "pgbouncer",
            "-U", "postgres", "-tAc", "SHOW VERSION",
            check=False,
        )
        assert as_nobody.returncode != 0
        assert "unix socket login rejected" in (as_nobody.stderr + as_nobody.stdout), (
            as_nobody.stdout + as_nobody.stderr
        )

        as_postgres = _docker(
            "exec", "-u", "postgres", DB_CONTAINER,
            "psql", "-h", "/var/run/postgresql", "-p", "6432", "-d", "pgbouncer",
            "-U", "postgres", "-tAc", "SHOW VERSION",
            check=False,
        )
        assert as_postgres.returncode == 0, as_postgres.stderr

    def test_nftables_ruleset_parses_and_has_burst(self, rig):
        completed = _dbexec(
            "bash", "-c",
            "apt-get install -yqq nftables >/dev/null 2>&1; "
            "nft -c -f /etc/nftables.d/exomem-postgres.nft && "
            "cat /etc/nftables.d/exomem-postgres.nft",
        )
        rendered = completed.stdout
        assert "burst 100 packets" in rendered
        assert "ip6 saddr" in rendered
        assert "600/minute" in rendered
        assert "over 200" in rendered

    def test_systemd_analyze_verify_or_documented_skip(self, rig):
        has_systemd_analyze = _dbexec("which", "systemd-analyze", check=False)
        if has_systemd_analyze.returncode != 0:
            print(
                "systemd-analyze verify skipped: postgres:17 has no systemd "
                "package at all (confirmed: no /lib/systemd/systemd binary "
                "either), so there is no systemd-analyze binary in this image "
                "to run it with."
            )
            pytest.skip("systemd-analyze not present in postgres:17")
        unit_files = [
            "/etc/systemd/system/exomem-postgres-nftables.service",
            "/etc/systemd/system/exomem-pgbackrest-full.service",
            "/etc/systemd/system/exomem-pgbackrest-full.timer",
            "/etc/systemd/system/exomem-pgbackrest-restore-verify.service",
            "/etc/systemd/system/exomem-pgbackrest-restore-verify.timer",
        ]
        completed = _dbexec("systemd-analyze", "verify", *unit_files)
        assert completed.returncode == 0, completed.stderr

    def test_pgbackrest_full_backup_succeeded(self, rig):
        result = rig["pgbackrest_backup_result"]
        assert result is not None, "backup was not attempted (playbook run itself failed)"
        assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]

    def test_pgbackrest_restore_verify_round_trip(self, rig):
        # Runs the REAL, role-rendered weekly restore-verify script
        # (roles/postgres/templates/exomem-pgbackrest-restore-verify.sh.j2,
        # installed at /usr/local/bin/exomem-pgbackrest-restore-verify.sh)
        # completely unmodified, against the real MinIO-backed repository
        # the backup above just wrote to.
        assert rig["pgbackrest_backup_result"] is not None
        assert rig["pgbackrest_backup_result"].returncode == 0
        rig_work: Path = rig["work"]

        # While the script's throwaway instance is confirmed up (its
        # postmaster.pid exists under the scratch dir the script mktemp's),
        # check specifically THAT postmaster PID's own open socket fds --
        # not a whole-container TCP table scan. listen_addresses is a
        # postmaster-lifetime-fixed GUC (bound at startup or never bound,
        # for its entire life), so one sample while the pid file is present
        # is conclusive for this process. A first attempt scanned the whole
        # container's /proc/net/tcp instead and caught an unrelated,
        # already-gone-by-the-next-instant ephemeral socket belonging to
        # some other process entirely (confirmed live: a second, later
        # lookup for its owning process found nothing at that inode any
        # more) -- scoping to this PID's own fds is what the assertion
        # actually needs, and avoids that container-wide noise.
        list_fds_script = rig_work / "list-socket-inodes.sh"
        list_fds_script.write_text(
            "#!/bin/sh\n"
            'pid="$1"\n'
            'for fd in /proc/"$pid"/fd/*; do\n'
            '  t=$(readlink "$fd" 2>/dev/null)\n'
            "  case \"$t\" in\n"
            "    socket:\\[*\\])\n"
            "      echo \"$t\" | tr -dc '0-9'; echo\n"
            "      ;;\n"
            "  esac\n"
            "done\n"
        )
        _docker("cp", str(list_fds_script), f"{DB_CONTAINER}:/tmp/list-socket-inodes.sh")
        _dbexec("chmod", "+x", "/tmp/list-socket-inodes.sh")

        proc = subprocess.Popen(
            [DOCKER, "exec", DB_CONTAINER, "/usr/local/bin/exomem-pgbackrest-restore-verify.sh"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        postmaster_listen_ports: set[int] | None = None
        deadline = time.time() + 60
        try:
            while proc.poll() is None and time.time() < deadline:
                pidfile = _docker(
                    "exec", DB_CONTAINER, "bash", "-c",
                    "cat /var/lib/postgresql/17/restore-verify.*/postmaster.pid 2>/dev/null | head -1",
                    check=False,
                )
                pg_pid = pidfile.stdout.strip()
                if pidfile.returncode == 0 and pg_pid.isdigit():
                    fds = _dbexec("/tmp/list-socket-inodes.sh", pg_pid, check=False)
                    postmaster_inodes = {line.strip() for line in fds.stdout.splitlines() if line.strip()}

                    # /proc/net/tcp{,6} needs no extra package (unlike
                    # ss/netstat, which postgres:17 does not ship) --
                    # fourth field "0A" is TCP_LISTEN; tenth field is the
                    # socket inode; second field is local_address as
                    # "IP:PORTHEX".
                    tcp = _docker("exec", DB_CONTAINER, "cat", "/proc/net/tcp", check=False)
                    tcp6 = _docker("exec", DB_CONTAINER, "cat", "/proc/net/tcp6", check=False)
                    listen_inode_to_port = {}
                    for src in (tcp.stdout, tcp6.stdout):
                        for line in src.splitlines()[1:]:
                            fields = line.split()
                            if len(fields) > 9 and fields[3] == "0A":
                                listen_inode_to_port[fields[9]] = int(fields[1].split(":")[1], 16)

                    postmaster_listen_ports = {
                        listen_inode_to_port[i]
                        for i in postmaster_inodes
                        if i in listen_inode_to_port
                    }
                    break
                time.sleep(0.1)
        finally:
            stdout, _ = proc.communicate(timeout=60)

        assert proc.returncode == 0, stdout
        assert f"pgBackRest restore verification succeeded for stanza {PGBACKREST_STANZA}" in stdout
        assert postmaster_listen_ports is not None, (
            "never caught the throwaway instance's postmaster.pid -- cannot "
            "make the no-TCP-listener assertion at all\n" + stdout
        )
        # The throwaway postmaster's own open fds must include zero TCP
        # listening sockets -- proving listen_addresses='' actually held,
        # scoped to that specific process rather than the whole container.
        assert postmaster_listen_ports == set(), (
            f"restore-verify's throwaway postmaster itself was listening on "
            f"TCP: {postmaster_listen_ports}"
        )

        # The real script above only ever proves basic connectivity
        # (`SELECT 1` against the default "postgres" database) -- by design
        # (OM-4), it knows nothing about this repository's actual schema.
        # This second pass proves the round trip carries real data, not
        # just a clean exit code, by restoring the SAME repository the real
        # script just validated into a second throwaway instance -- the
        # identical recipe the script itself uses, never the live data
        # directory -- then querying the marker row written before the
        # backup.
        scratch = "/var/lib/postgresql/17/om6-marker-restore-check"
        pg_bin = "/usr/lib/postgresql/17/bin"
        work: Path = rig["work"]
        _dbexec("rm", "-rf", scratch, check=False)
        _dbexec("mkdir", "-p", scratch)
        _dbexec("chmod", "0700", scratch)
        try:
            restore = _dbexec(
                "pgbackrest", f"--stanza={PGBACKREST_STANZA}", f"--pg1-path={scratch}",
                "restore", "--delta",
                check=False,
            )
            assert restore.returncode == 0, restore.stderr

            conf_file = work / "marker-check-postgresql.conf"
            conf_file.write_text(
                "listen_addresses = ''\nport = 55433\n"
                f"unix_socket_directories = '{scratch}'\n"
            )
            hba_file = work / "marker-check-pg_hba.conf"
            hba_file.write_text(
                "local   all             all                                     trust\n"
            )
            _docker("cp", str(conf_file), f"{DB_CONTAINER}:{scratch}/postgresql.conf")
            _docker("cp", str(hba_file), f"{DB_CONTAINER}:{scratch}/pg_hba.conf")
            _dbexec("chown", "-R", "postgres:postgres", scratch)
            _dbexec_pg(f"{pg_bin}/pg_ctl", "-D", scratch, "-l", f"{scratch}/verify.log", "-w", "start")
            try:
                found = _docker(
                    "exec", "-u", "postgres", DB_CONTAINER,
                    f"{pg_bin}/psql", "-h", scratch, "-p", "55433",
                    "-d", "exomem_control", "-tAc",
                    "SELECT note FROM pgbackrest_marker WHERE id = 1",
                    check=False,
                )
            finally:
                _dbexec_pg(
                    f"{pg_bin}/pg_ctl", "-D", scratch, "-m", "fast", "-w", "stop", check=False
                )
        finally:
            _dbexec("rm", "-rf", scratch, check=False)

        assert found.returncode == 0, found.stderr
        assert found.stdout.strip() == rig["pgbackrest_marker_note"]

    def test_c1_grants_applied_and_idempotent(self, rig):
        assert rig["c1_grants_run_1"] is not None, "playbook run itself failed"
        assert rig["c1_grants_run_1"].returncode == 0, rig["c1_grants_run_1"].stderr
        assert rig["c1_grants_run_2"].returncode == 0, rig["c1_grants_run_2"].stderr
        # Every GRANT in the script is a plain idempotent GRANT (its own
        # docstring's claim) -- a live pg_class.relacl/pg_attribute.attacl
        # snapshot taken after each run proves the second run changed
        # nothing, not just that it didn't error.
        assert rig["c1_grants_snapshot_1"] == rig["c1_grants_snapshot_2"]

    def test_c1_grants_cellctl_updates_observed_columns_only(self, rig):
        ca = rig["tls_dir"] / "ca.crt"
        dsn = _dsn("exomem_cellctl", HOSTNAME, 5432, ca)
        observed = _psql(
            dsn,
            "UPDATE exomem_cloud_cells SET observed_state = 'running' "
            "WHERE cell_id = 'aaaaaaaaaaaaaaaa'",
            container=PRIVATE_PROBER,
        )
        assert observed.returncode == 0, observed.stderr

        desired = _psql(
            dsn,
            "UPDATE exomem_cloud_cells SET desired_state = 'stopped' "
            "WHERE cell_id = 'aaaaaaaaaaaaaaaa'",
            container=PRIVATE_PROBER,
        )
        assert desired.returncode != 0
        # item 3, round 7: assert the actual privilege-denial wording, not
        # just a non-zero exit (which a broken connection would also give).
        assert "permission denied for" in desired.stderr, desired.stderr

        # Not C1b: cellctl has SELECT on exomem_cloud_settings but no
        # INSERT/UPDATE grant on it at all (only substrate_app and the
        # owner release route write it).
        c1b = _psql(
            dsn,
            "INSERT INTO exomem_cloud_settings (key, value) VALUES ('probe', '1')",
            container=PRIVATE_PROBER,
        )
        assert c1b.returncode != 0
        # item 1, round 8: tightened to the specific table -- a
        # "permission denied for" substring alone would also match a
        # denial on some other object entirely.
        assert "permission denied for table exomem_cloud_settings" in c1b.stderr, c1b.stderr

    def test_c1_grants_gateway_selects_three_columns_only(self, rig):
        ca = rig["tls_dir"] / "ca.crt"
        dsn = _dsn("exomem_gateway", HOSTNAME, 5432, ca)
        allowed = _psql(
            dsn,
            "SELECT cell_id, tenant_id, desired_state FROM exomem_cloud_cells",
            container=PRIVATE_PROBER,
        )
        assert allowed.returncode == 0, allowed.stderr

        denied = _psql(
            dsn, "SELECT storage_gib FROM exomem_cloud_cells", container=PRIVATE_PROBER,
        )
        assert denied.returncode != 0
        # item 1, round 8: tightened to the specific table, same reasoning
        # as the exomem_cloud_settings check above.
        assert "permission denied for table exomem_cloud_cells" in denied.stderr, denied.stderr

        # "none on C1-C1d" beyond that one SELECT -- no write path at all.
        write_denied = _psql(
            dsn,
            "UPDATE exomem_cloud_cells SET desired_state = 'stopped' "
            "WHERE cell_id = 'aaaaaaaaaaaaaaaa'",
            container=PRIVATE_PROBER,
        )
        assert write_denied.returncode != 0
        assert "permission denied for table exomem_cloud_cells" in write_denied.stderr, write_denied.stderr

    def test_c1_grants_substrate_app_updates_cancellation_notice(self, rig):
        ca = rig["tls_dir"] / "ca.crt"
        dsn = _dsn("substrate_app", HOSTNAME, 6432, ca)
        completed = _psql(
            dsn,
            "UPDATE exomem_cloud_cells SET cancellation_notice_sent_at = now() "
            "WHERE cell_id = 'aaaaaaaaaaaaaaaa'",
        )
        assert completed.returncode == 0, completed.stderr

        # item 3, round 7 (missing check): substrate_app's own grant is
        # scoped to cancellation_notice_sent_at, not the whole row -- it
        # must be refused on observed_state, the column cellctl alone owns
        # (C1a), the same way exomem_gateway is refused on desired_state
        # above.
        observed_denied = _psql(
            dsn,
            "UPDATE exomem_cloud_cells SET observed_state = 'running' "
            "WHERE cell_id = 'aaaaaaaaaaaaaaaa'",
        )
        assert observed_denied.returncode != 0
        assert "permission denied for" in observed_denied.stderr, observed_denied.stderr
