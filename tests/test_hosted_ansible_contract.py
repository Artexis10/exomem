from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
ANSIBLE = ROOT / "infra/ansible"
ANSIBLE_PLAYBOOK = (
    Path(os.environ["ANSIBLE_PLAYBOOK_BIN"]) if "ANSIBLE_PLAYBOOK_BIN" in os.environ else None
)


def _read(relative: str) -> str:
    return (ANSIBLE / relative).read_text(encoding="utf-8")


def test_site_playbook_is_idempotent_by_construction_and_never_fetches_admin_state() -> None:
    site = _read("site.yml")
    base = _read("roles/base/tasks/main.yml")
    k3s = _read("roles/k3s/tasks/main.yml")
    combined = "\n".join((site, base, k3s)).lower()

    assert "roles:" in site
    assert "- base" in site
    assert "- k3s" in site
    assert "become: true" in site
    assert "ansible.builtin.command" not in base
    assert "ansible.builtin.shell" not in combined
    assert "ansible.builtin.fetch" not in combined
    assert "ansible.builtin.slurp" not in combined
    assert "notify:" in base
    assert "notify:" in k3s
    assert "no_log: true" in k3s


def test_base_role_hardens_ssh_firewall_time_logging_and_disk_support() -> None:
    defaults = _read("roles/base/defaults/main.yml")
    tasks = _read("roles/base/tasks/main.yml")
    ssh = _read("roles/base/templates/99-exomem-hardening.conf.j2")
    fail2ban = _read("roles/base/templates/exomem-sshd.local.j2")
    journald = _read("roles/base/templates/99-exomem-storage.conf.j2")

    for package in ("cryptsetup", "fail2ban", "ufw", "unattended-upgrades"):
        assert package in defaults
    assert "base_admin_ssh_cidrs" in tasks
    assert "ansible.builtin.apt" in tasks
    assert "ansible.builtin.systemd_service" in tasks
    assert "PermitRootLogin prohibit-password" in ssh
    assert "PasswordAuthentication no" in ssh
    assert "KbdInteractiveAuthentication no" in ssh
    assert "bantime = 1h" in fail2ban
    assert "SystemMaxUse=512M" in journald
    journal_directory = "path: /etc/systemd/journald.conf.d"
    journal_policy = "name: Install bounded journal storage policy"
    assert journal_directory in tasks
    assert tasks.index(journal_directory) < tasks.index(journal_policy)


def test_k3s_role_pins_binary_and_hardens_single_server_configuration() -> None:
    defaults = _read("roles/k3s/defaults/main.yml")
    tasks = _read("roles/k3s/tasks/main.yml")
    config = _read("roles/k3s/templates/config.yaml.j2")
    service = _read("roles/k3s/templates/k3s.service.j2")
    audit = _read("roles/k3s/files/audit-policy.yaml")
    admission = _read("roles/k3s/files/admission-config.yaml")

    assert 'k3s_version: "v1.35.6+k3s1"' in defaults
    assert "2b52a2c1ca6eb502e2a0ffa1a4cf79eef94875926577c1e43347ed292cc92432" in defaults
    assert "get_url:" in tasks
    assert 'checksum: "sha256:{{ k3s_sha256_amd64 }}"' in tasks
    assert "cluster-init: true" in config
    assert "secrets-encryption: true" in config
    assert 'write-kubeconfig-mode: "0640"' in config
    assert "disable:\n  - traefik\n  - servicelb\n  - local-storage" in config
    assert "service-account-max-token-expiration=24h" in config
    assert "image-gc-high-threshold=75" in config
    assert "container-log-max-size=10Mi" in config
    assert "audit-log-path=/var/lib/rancher/k3s/server/logs/audit.log" in config
    assert "admission-control-config-file=/etc/rancher/k3s/admission-config.yaml" in config
    assert 'etcd-snapshot-schedule-cron: "*/30 * * * *"' in config
    assert "etcd-s3: true" in config
    assert "etcd-s3-secret-key:" in config
    assert "ExecStart=/usr/local/bin/k3s server" in service
    assert "omitStages:" in audit
    assert "kind: PodSecurityConfiguration" in admission
    assert "enforce: baseline" in admission
    assert "audit: restricted" in admission
    assert "warn: restricted" in admission
    assert "- exomem-storage-init" in admission
    assert "- exomem-platform" in admission


def test_k3s_role_resolves_the_private_interface_from_the_declared_node_ip() -> None:
    defaults = _read("roles/k3s/defaults/main.yml")
    tasks = _read("roles/k3s/tasks/main.yml")
    config = _read("roles/k3s/templates/config.yaml.j2")

    assert 'k3s_private_interface: ""' in defaults
    assert "k3s_private_interface_candidates" in tasks
    assert 'selectattr("ipv4.address", "equalto", private_node_ip)' in tasks
    assert "k3s_private_interface_candidates | length == 1" in tasks
    assert "k3s_resolved_private_interface" in tasks
    assert "flannel-iface: {{ k3s_resolved_private_interface | to_json }}" in config


def test_inventory_generator_emits_only_non_sensitive_host_coordinates(tmp_path: Path) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    inventory = tmp_path / "inventory.yml"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "access_service_token_client_secret": {
                    "sensitive": True,
                    "value": "must-never-appear",
                },
            }
        ),
        encoding="utf-8",
    )
    terraform_output.chmod(0o600)

    result = subprocess.run(
        [
            "python3",
            str(generator),
            str(terraform_output),
            str(inventory),
            "--user",
            "alpha-admin",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert inventory.stat().st_mode & 0o777 == 0o600
    rendered = inventory.read_text(encoding="utf-8")
    assert "192.0.2.10" in rendered
    assert "10.50.1.10" in rendered
    assert "alpha-admin" in rendered
    assert "must-never-appear" not in rendered
    assert "secret" not in rendered.lower()
    parsed = json.loads(rendered)
    assert "_meta" not in parsed
    assert parsed["all"]["children"]["hosted_nodes"]["hosts"]["exomem-alpha"] == {
        "ansible_host": "192.0.2.10",
        "ansible_user": "alpha-admin",
        "private_node_ip": "10.50.1.10",
    }


def test_site_playbook_provisions_control_database_server_separately() -> None:
    site = _read("site.yml")

    assert "hosts: control_nodes" in site
    assert site.count("- base") == 2
    assert "- postgres" in site
    # Two independent plays, so a control-database failure never touches the
    # fleet-node play, and vice versa.
    assert site.count("any_errors_fatal: true") == 2


def test_postgres_role_pins_version_and_separates_public_from_private_roles() -> None:
    defaults = _read("roles/postgres/defaults/main.yml")
    tasks = _read("roles/postgres/tasks/main.yml")
    pg_conf = _read("roles/postgres/templates/exomem-postgres.conf.j2")
    pg_hba = _read("roles/postgres/templates/pg_hba.conf.j2")
    pgbouncer_ini = _read("roles/postgres/templates/pgbouncer.ini.j2")
    pgbouncer_hba = _read("roles/postgres/templates/pgbouncer_hba.conf.j2")

    assert 'postgres_major_version: "17"' in defaults
    for role in ("substrate_owner", "substrate_app", "exomem_gateway", "exomem_cellctl"):
        assert role in defaults

    assert '"postgresql-{{ postgres_major_version }}"' in tasks
    assert "pgbouncer" in tasks
    assert "pgbackrest" in tasks
    assert "nftables" in tasks
    assert "certbot" in tasks
    # The pgbouncer fail2ban jail was removed (controls-must-justify-
    # themselves: what it prevented -- SCRAM brute force -- is already
    # infeasible against high-entropy passwords and blunted by the nftables
    # limits below; what it cost when it fired wrongly was a 1-hour,
    # all-ports ban of a shared Vercel egress IP, an outage for every
    # Substrate user behind it). The base role's own SSH jail is untouched.
    assert "fail2ban" not in tasks
    assert "community.postgresql.postgresql_user" in tasks
    assert "community.postgresql.postgresql_db" in tasks
    assert "ansible.builtin.shell" not in tasks

    # OM-1: no dedicated "pgbouncer" OS user. PgBouncer runs as postgres
    # (Debian's/PGDG's pgbouncer.service hardcodes User=postgres; see
    # pg_ident.conf.j2), so looping the TLS-group task over
    # ["postgres", "pgbouncer"] would create a superfluous "pgbouncer"
    # system account purely as a side effect of ansible.builtin.user
    # defaulting to state=present. PgBouncer's own files are postgres:postgres.
    assert "name: Add the PostgreSQL service account to the TLS group" in tasks
    assert "name: postgres\n    groups: exomem-tls" in tasks
    assert "owner: pgbouncer" not in tasks
    assert "group: pgbouncer" not in tasks
    assert "dest: /etc/pgbouncer/pgbouncer.ini\n    owner: postgres\n    group: postgres" in tasks
    assert (
        'dest: "{{ postgres_config_dir }}/pgbouncer_hba.conf"\n    owner: postgres\n    group: postgres'
        in tasks
    )

    # Postgres itself never listens publicly.
    assert "listen_addresses = '127.0.0.1,{{ postgres_private_ip }}'" in pg_conf
    assert "ssl = on" in pg_conf
    assert "archive_command = 'pgbackrest" in pg_conf

    # D12 NEW-1: exomem_gateway and exomem_cellctl (including cellctl's
    # direct LISTEN) connect straight to Postgres on the private network,
    # never through PgBouncer -- so both get a private-CIDR hostssl line and
    # neither has a loopback (127.0.0.1/32) line at all. Only
    # substrate_owner/substrate_app are loopback-only, because only
    # PgBouncer forwards them.
    assert "exomem_gateway       {{ postgres_private_network_cidr }}" in pg_hba
    assert "exomem_cellctl       {{ postgres_private_network_cidr }}" in pg_hba
    for line in pg_hba.splitlines():
        if "127.0.0.1/32" in line:
            assert "exomem_gateway" not in line
            assert "exomem_cellctl" not in line
    assert "host    all             all             0.0.0.0/0                       reject" in pg_hba

    # No `trust` auth method anywhere in pg_hba.conf as an actual directive:
    # a prior `local`/`hostssl ... 127.0.0.1/32 trust` pair let any local OS
    # user read every role's SCRAM verifier through pgbouncer.get_auth()
    # with no credential at all. pgbouncer_auth now authenticates only via
    # unix-socket peer, mapped through pg_ident.conf.
    hba_directive_lines = [
        line for line in pg_hba.splitlines() if line.strip() and not line.strip().startswith("#")
    ]
    assert not any(re.search(r"\btrust\b", line) for line in hba_directive_lines)
    assert "pgbouncer_auth       peer map=pgbouncer_auth_map" in pg_hba
    assert "pgbouncer_auth" not in pgbouncer_hba

    pg_ident = _read("roles/postgres/templates/pg_ident.conf.j2")
    assert "pgbouncer_auth_map postgres pgbouncer_auth" in pg_ident

    assert "dest: \"{{ postgres_config_dir }}/pg_ident.conf\"" in tasks

    # PgBouncer's own auth_query connection goes over the unix socket (the
    # only path pg_hba.conf now admits pgbouncer_auth on), not TCP/127.0.0.1
    # like the four roles' own pooled connections.
    assert "pgbouncer_auth_db = host=/var/run/postgresql" in pgbouncer_ini
    assert "auth_dbname = pgbouncer_auth_db" in pgbouncer_ini

    # A single public listener; PgBouncer's own HBA file -- not a second
    # port -- is what keeps exomem_gateway/exomem_cellctl off the internet.
    assert "listen_addr = *" in pgbouncer_ini
    assert "listen_port = {{ postgres_public_port }}" in pgbouncer_ini
    assert "auth_hba_file = {{ postgres_config_dir }}/pgbouncer_hba.conf" in pgbouncer_ini
    assert "client_tls_sslmode = require" in pgbouncer_ini
    # auth_hba_file is only consulted when auth_type selects it: with any
    # concrete method (e.g. scram-sha-256) here instead, PgBouncer ignores
    # the HBA file entirely and every role reaches every listener from
    # anywhere -- confirmed against a live PgBouncer 1.25 instance, where
    # this was the actual behavior before the fix.
    assert "auth_type = hba" in pgbouncer_ini
    # Every hostssl line in pg_hba.conf covers the loopback backend
    # connection PgBouncer itself makes (auth_query and every pooled
    # connection); `disable` here makes Postgres refuse it outright
    # ("pg_hba.conf rejects connection ... no encryption", confirmed live).
    assert "server_tls_sslmode = require" in pgbouncer_ini
    assert "pool_mode=session" in pgbouncer_ini

    assert "substrate_owner  0.0.0.0/0 scram-sha-256" in pgbouncer_hba
    assert "substrate_app    0.0.0.0/0 scram-sha-256" in pgbouncer_hba
    # D12 NEW-1: PgBouncer serves only substrate_app/substrate_owner now --
    # exomem_gateway and exomem_cellctl connect directly to Postgres on the
    # private network instead, so neither role appears in PgBouncer's own
    # admission-control HBA file at all, on any line, for any source.
    pgbouncer_hba_directive_lines = [
        line
        for line in pgbouncer_hba.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("exomem_gateway" in line for line in pgbouncer_hba_directive_lines)
    assert not any("exomem_cellctl" in line for line in pgbouncer_hba_directive_lines)

    # PgBouncer's own connection limits (D12 NEW-1): a per-role cap (not
    # per-source-IP -- that's nftables' job below), a global ceiling, and a
    # login-handshake timeout.
    assert "postgres_pgbouncer_max_user_connections: 50" in defaults
    assert "postgres_pgbouncer_max_client_conn: 2000" in defaults
    assert "postgres_pgbouncer_client_login_timeout: 10" in defaults
    assert "max_user_connections = {{ postgres_pgbouncer_max_user_connections }}" in pgbouncer_ini
    assert "max_client_conn = {{ postgres_pgbouncer_max_client_conn }}" in pgbouncer_ini
    assert "client_login_timeout = {{ postgres_pgbouncer_client_login_timeout }}" in pgbouncer_ini

    # OM-5/NC-A: PgBouncer's own admin console has no unconditional same-uid
    # bypass the way Postgres's own peer auth does -- with auth_type=hba, a
    # unix-socket connection to the "pgbouncer" database still needs a
    # matching HBA rule, and a matching rule alone still isn't admin access
    # (that's admin_users). The rule itself is `peer`, not `trust` (NC-A,
    # round 7): this unix socket directory is mode 777, so `trust` let ANY
    # local OS user claim postgres and reach the admin console -- confirmed
    # live by the reviewer reaching SHOW VERSION as `nobody`. `peer` checks
    # the connecting process's real OS uid against the requested database
    # user, so admin access is reachable only by a process already running
    # as postgres. Both are scoped to the unix socket only (`local`, never
    # `host`/`hostssl`) -- never over TCP, from any source.
    assert (
        "local   pgbouncer       postgres                                       peer"
        in pgbouncer_hba
    )
    for line in pgbouncer_hba.splitlines():
        if "pgbouncer" in line and "postgres" in line and not line.strip().startswith("#"):
            assert line.strip().startswith("local")
    assert "admin_users = postgres" in pgbouncer_ini


def test_postgres_role_issues_its_own_certificate_and_reloads_on_renewal() -> None:
    tasks = _read("roles/postgres/tasks/main.yml")
    hook = _read("roles/postgres/templates/exomem-cert-deploy-hook.sh.j2")

    assert "--dns-cloudflare" in tasks
    assert "--preferred-challenges dns-01" in tasks
    assert "certbot.timer" in tasks
    assert "postgres_acme_enabled" in tasks
    assert "RENEWED_LINEAGE" in hook
    assert "systemctl reload-or-restart postgresql@" in hook
    assert "systemctl reload-or-restart pgbouncer.service" in hook


def test_postgres_role_limits_connections_without_touching_ufw_state() -> None:
    defaults = _read("roles/postgres/defaults/main.yml")
    nft = _read("roles/postgres/templates/exomem-postgres.nft.j2")
    unit = _read("roles/postgres/templates/exomem-postgres-nftables.service.j2")
    postgres_dir = ANSIBLE / "roles/postgres"

    nft_statements = "\n".join(
        line for line in nft.splitlines() if not line.strip().startswith("#")
    )
    assert "flush ruleset" not in nft_statements
    assert "table inet exomem_postgres" in nft
    # Flood-only thresholds, not brute-force-only (controls must justify
    # themselves: Vercel production shares egress IPs and opens pooled
    # connections per function instance, so a burst of legitimate traffic
    # from one IP is expected; a tight limit's wrong-firing cost is an
    # outage for every Substrate user behind that IP).
    assert "postgres_nft_conn_limit_per_source: 200" in defaults
    assert "postgres_nft_new_conn_rate_per_minute: 600" in defaults
    assert "ct count over {{ postgres_nft_conn_limit_per_source }}" in nft
    # D12 NEW-2: an explicit burst, not just a bare rate -- nftables' default
    # burst for `limit rate` is a single packet, which would make the meter
    # trip on the very first packet past a sustained-rate boundary rather
    # than tolerating a short legitimate spike.
    assert "limit rate over {{ postgres_nft_new_conn_rate_per_minute }}/minute burst 100 packets" in nft
    # Mirrored for ip6 saddr so the limit still holds if IPv6 is ever
    # enabled, even though the server keeps ipv6_enabled = false.
    assert (
        "ip6 saddr limit rate over {{ postgres_nft_new_conn_rate_per_minute }}/minute burst 100 packets"
        in nft
    )
    assert "ip6 saddr ct count over {{ postgres_nft_conn_limit_per_source }}" in nft
    assert "ExecStart=/usr/sbin/nft -f /etc/nftables.d/exomem-postgres.nft" in unit

    # The pgbouncer fail2ban jail and filter are gone entirely: no template,
    # no install task, no defaults, no reference anywhere in the role.
    assert not (postgres_dir / "templates/exomem-pgbouncer-jail.local.j2").exists()
    assert not (postgres_dir / "templates/exomem-pgbouncer-filter.conf.j2").exists()
    assert "fail2ban" not in defaults
    handlers = _read("roles/postgres/handlers/main.yml")
    assert "fail2ban" not in handlers


def test_postgres_role_archives_wal_and_verifies_restores_weekly() -> None:
    tasks = _read("roles/postgres/tasks/main.yml")
    backrest_conf = _read("roles/postgres/templates/pgbackrest.conf.j2")
    full_timer = _read("roles/postgres/templates/exomem-pgbackrest-full.timer.j2")
    verify_service = _read("roles/postgres/templates/exomem-pgbackrest-restore-verify.service.j2")
    verify_timer = _read("roles/postgres/templates/exomem-pgbackrest-restore-verify.timer.j2")
    verify_script = _read("roles/postgres/templates/exomem-pgbackrest-restore-verify.sh.j2")

    assert "stanza-create" in tasks
    assert "repo1-type={{ postgres_pgbackrest_repo_type }}" in backrest_conf
    assert "repo1-retention-full={{ postgres_pgbackrest_full_retention }}" in backrest_conf
    # Client-side repository encryption: B2 server-side encryption alone
    # protects only against B2 itself, not an overbroad key or a
    # misconfigured bucket. pgBackRest 2.59 supports exactly "none" and
    # "aes-256-cbc" (checked directly with `pgbackrest help backup
    # repo-cipher-type`; there is no aes-256-gcm to prefer).
    assert "repo1-cipher-type=aes-256-cbc" in backrest_conf
    assert "repo1-cipher-pass={{ postgres_pgbackrest_repo_cipher_pass }}" in backrest_conf
    defaults = _read("roles/postgres/defaults/main.yml")
    assert 'postgres_pgbackrest_repo_cipher_pass: ""' in defaults
    assert "postgres_pgbackrest_repo_cipher_pass | length > 0" in tasks
    # OM-2: /etc/pgbackrest must exist, owned postgres:postgres 0750, before
    # pgbackrest.conf (which contains the repo cipher passphrase and S3
    # credentials) is templated into it -- otherwise the directory the
    # package leaves behind may have looser permissions than the file placed
    # inside it.
    pgbackrest_dir_task_index = tasks.index("path: /etc/pgbackrest\n    state: directory")
    pgbackrest_conf_task_index = tasks.index("dest: /etc/pgbackrest/pgbackrest.conf")
    assert pgbackrest_dir_task_index < pgbackrest_conf_task_index
    assert (
        "path: /etc/pgbackrest\n    state: directory\n    owner: postgres\n"
        "    group: postgres\n    mode: \"0750\""
    ) in tasks
    assert "OnCalendar=*-*-* 03:00:00" in full_timer
    assert "OnCalendar=Sun *-*-* 04:00:00" in verify_timer
    assert "exomem-pgbackrest-restore-verify.sh" in verify_service
    assert "restore --delta" in verify_script
    # Debian/Ubuntu never puts versioned PostgreSQL binaries on PATH, so the
    # script must resolve pg_ctl explicitly rather than relying on the
    # postgres login shell's PATH (verified against a real container: a bare
    # `pg_ctl` there is "command not found").
    assert 'PG_BINDIR="/usr/lib/postgresql/{{ postgres_major_version }}/bin"' in verify_script
    assert '"${PG_BINDIR}/pg_ctl" -D ' in verify_script
    # The scratch instance never overwrites the live data directory.
    assert "{{ postgres_data_dir }}" not in verify_script
    # Debian keeps postgresql.conf/pg_hba.conf outside $PGDATA, so a restored
    # backup has neither; the script must write throwaway ones rather than
    # copying the live cluster's (which would carry the live data_directory
    # and archive_command straight into the "never touches live data"
    # verification instance).
    assert "${SCRATCH}/postgresql.conf" in verify_script
    # OM-4: the scratch cluster never listens on any network interface, not
    # even loopback, and its unix socket directory is a private 0700 temp
    # dir, never the shared /var/run/postgresql one the live cluster and
    # PgBouncer use.
    assert "listen_addresses = ''" in verify_script
    assert "listen_addresses = '127.0.0.1'" not in verify_script
    assert "unix_socket_directories = '${SCRATCH}'" in verify_script
    verify_script_code_lines = [
        line
        for line in verify_script.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert not any("/var/run/postgresql" in line for line in verify_script_code_lines)
    assert 'chmod 0700 "${SCRATCH}"' in verify_script
    assert "${SCRATCH}/pg_hba.conf" in verify_script


def test_postgres_role_creates_only_the_four_roles_and_leaves_grants_to_substrate() -> None:
    # NC-C: renamed from grants.sql to pgbouncer_auth.sql -- it has never
    # granted anything on the C1 tables; that is Substrate's own
    # scripts/exomem-cloud-grants.sql (task 4.4), a different script.
    grants_sql = (ANSIBLE / "roles/postgres/files/pgbouncer_auth.sql").read_text(encoding="utf-8")
    statements = "\n".join(
        line for line in grants_sql.splitlines() if not line.strip().startswith("--")
    )

    assert "exomem_cloud_cells" not in grants_sql
    assert "GRANT EXECUTE ON FUNCTION pgbouncer.get_auth" in statements
    # Only the auth-query plumbing's own two grants (function EXECUTE and
    # USAGE on its own schema -- without which pgbouncer_auth cannot even
    # reach the function, confirmed against a live PostgreSQL 15+ default,
    # which grants schema USAGE to nobody but the owner) are present; no
    # grant ever touches a C1 table or Substrate's schema.
    ungranted = statements.replace("GRANT EXECUTE ON FUNCTION pgbouncer.get_auth(TEXT) TO pgbouncer_auth;", "")
    ungranted = ungranted.replace("GRANT USAGE ON SCHEMA pgbouncer TO pgbouncer_auth;", "")
    assert "GRANT" not in ungranted
    assert "scripts/exomem-cloud-grants.sql" in grants_sql

    # A SECURITY DEFINER function without a pinned search_path lets a
    # lower-privileged caller juggle their own search_path to shadow the
    # unqualified catalog reference it depends on.
    assert "SET search_path = pg_catalog" in grants_sql


def test_postgres_role_gates_service_management_for_the_gated_live_test() -> None:
    tasks = _read("roles/postgres/tasks/main.yml")
    handlers = _read("roles/postgres/handlers/main.yml")
    defaults = _read("roles/postgres/defaults/main.yml")

    # OM-5: every task that manages a systemd unit is tagged `services`, so
    # the gated live-role test can run the role with --skip-tags services
    # and start postgres/pgbouncer/nftables itself inside a container that
    # has no systemd (PID 1) at all.
    systemd_task_blocks = re.findall(
        r"- name: .*\n(?:(?!\n- name: ).*\n)*?\s*ansible\.builtin\.systemd_service:\n(?:(?!\n- name: ).*\n)*",
        tasks,
    )
    assert len(systemd_task_blocks) == 5
    for block in systemd_task_blocks:
        assert "tags: services" in block

    # Ansible's mandatory implicit end-of-play handler flush ignores a
    # handler's own tags -- only an explicit, similarly-tagged
    # `meta: flush_handlers` respects tag filtering -- so tagging the
    # systemd tasks and the four explicit flush points alone still lets a
    # config-only task's notify() fire a handler at that final flush.
    # `tags: services` on every handler suppresses the four explicit,
    # mid-play flush points; `when: postgres_manage_services | bool` is what
    # suppresses that unconditional final one, since handlers honor `when:`
    # exactly like tasks do regardless of flush timing. The `| bool` filter
    # is required, not decoration: the gated live test passes
    # `-e postgres_manage_services=false` on the command line, and bare
    # (non-JSON) `-e key=value` extra-vars are stored as the literal string
    # "false" -- a non-empty string, which is truthy under a plain
    # `when: postgres_manage_services` and would fire the handler anyway.
    # Confirmed with an isolated two-line reproduction against a live
    # ansible-playbook run before applying this fix to the real role.
    flush_handler_tasks = re.findall(
        r"- name: .*\n\s*ansible\.builtin\.meta: flush_handlers\n\s*tags: services\n",
        tasks,
    )
    assert len(flush_handler_tasks) == 4

    for handler_name in (
        "Reload systemd",
        "Restart PostgreSQL",
        "Reload PostgreSQL",
        "Restart PgBouncer",
        "Restart the PgBouncer connection-limit table",
    ):
        handler_block_match = re.search(
            rf"- name: {re.escape(handler_name)}\n(?:(?!\n- name: ).*\n)*",
            handlers,
        )
        assert handler_block_match, f"handler {handler_name!r} not found"
        handler_block = handler_block_match.group(0)
        assert "tags: services" in handler_block
        assert "when: postgres_manage_services | bool" in handler_block

    # Defaults to true so production's own restarts are unaffected; only the
    # gated live test overrides it to false via -e.
    assert "postgres_manage_services: true" in defaults


def test_postgres_role_service_management_guard_cannot_be_bypassed_by_skip_tags() -> None:
    tasks = _read("roles/postgres/tasks/main.yml")

    # NC-B (round 8): postgres_manage_services staying a real, defaulted-true
    # boolean says nothing about whether the CLI was invoked with
    # --skip-tags services directly -- that flag skips every
    # services-tagged enable/start/restart task with no dependency on the
    # variable at all, so the original assert (postgres_manage_services is
    # boolean, and it or the test-mode override) never saw that bypass.
    # ansible_skip_tags is Ansible's own magic var reflecting --skip-tags.
    assert_block_match = re.search(
        r"- name: Validate service-management mode\n(?:(?!\n- name: ).*\n)*",
        tasks,
    )
    assert assert_block_match, "the service-management assert task was not found"
    assert_block = assert_block_match.group(0)
    assert (
        "'services' not in ansible_skip_tags or (postgres_unmanaged_services_test_mode | bool)"
        in assert_block
    )
    # tags: always: this assert must run under every --tags/--skip-tags
    # combination, including ones that would otherwise skip an untagged
    # task, or a differently-scoped invocation could bypass the guard the
    # same way --skip-tags services itself did before this check existed.
    assert "tags: always" in assert_block


def test_postgres_role_stages_the_grants_script_on_the_target_before_running_it() -> None:
    tasks = _read("roles/postgres/tasks/main.yml")

    # community.postgresql.postgresql_script's `path:` is documented (see
    # `ansible-doc community.postgresql.postgresql_script`) as a path on the
    # TARGET machine -- unlike ansible.builtin.template/copy, it does not do
    # Ansible's controller-side role-relative file search/transfer on its
    # own. A bare `path: grants.sql` only ever worked by coincidence of
    # controller and target sharing a filesystem, and fails against a real
    # remote Hetzner host -- confirmed by running the real role end to end
    # for the first time in this round (OM-5).
    copy_idx = tasks.index("name: Copy the PgBouncer auth-query plumbing script to the target")
    apply_idx = tasks.index("name: Apply the PgBouncer auth-query plumbing")
    cleanup_idx = tasks.index("name: Remove the auth-query plumbing script from the target")
    assert copy_idx < apply_idx < cleanup_idx

    staged_path = "/tmp/exomem-postgres-pgbouncer-auth.sql"
    copy_block = tasks[copy_idx:apply_idx]
    apply_block = tasks[apply_idx:cleanup_idx]
    cleanup_block = tasks[cleanup_idx:cleanup_idx + 300]

    assert "ansible.builtin.copy" in copy_block
    # NC-C: renamed from grants.sql -- it has never granted anything on the
    # C1 tables; that is Substrate's own scripts/exomem-cloud-grants.sql.
    assert "src: pgbouncer_auth.sql" in copy_block
    assert f"dest: {staged_path}" in copy_block

    assert "community.postgresql.postgresql_script" in apply_block
    assert f"path: {staged_path}" in apply_block
    # Never a bare, controller-relative path again -- that was the bug.
    assert "path: pgbouncer_auth.sql" not in apply_block

    assert "ansible.builtin.file" in cleanup_block
    assert f"path: {staged_path}" in cleanup_block
    assert "state: absent" in cleanup_block


def test_inventory_generator_optionally_emits_the_control_database_host(
    tmp_path: Path,
) -> None:
    generator = ROOT / "infra/scripts/generate_ansible_inventory.py"
    terraform_output = tmp_path / "foundation.json"
    inventory = tmp_path / "inventory.yml"
    terraform_output.write_text(
        json.dumps(
            {
                "server_ipv4": {"sensitive": False, "value": "192.0.2.10"},
                "private_node_ip": {"sensitive": False, "value": "10.50.1.10"},
                "control_db_server_ipv4": {"sensitive": False, "value": "192.0.2.20"},
                "control_db_private_ip": {"sensitive": False, "value": "10.50.1.20"},
            }
        ),
        encoding="utf-8",
    )
    terraform_output.chmod(0o600)

    result = subprocess.run(
        [
            "python3",
            str(generator),
            str(terraform_output),
            str(inventory),
            "--user",
            "alpha-admin",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    parsed = json.loads(inventory.read_text(encoding="utf-8"))
    assert parsed["all"]["children"]["control_nodes"]["hosts"]["exomem-control-db"] == {
        "ansible_host": "192.0.2.20",
        "ansible_user": "alpha-admin",
        "postgres_private_ip": "10.50.1.20",
    }


def test_ansible_syntax_with_pinned_binary() -> None:
    if ANSIBLE_PLAYBOOK is None:
        pytest.skip("set ANSIBLE_PLAYBOOK_BIN to run pinned Ansible syntax validation")
    result = subprocess.run(
        [str(ANSIBLE_PLAYBOOK), "--syntax-check", str(ANSIBLE / "site.yml")],
        cwd=ANSIBLE,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
