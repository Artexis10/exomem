"""Disposable Ubuntu proof for the real OpenSSH/PAM/sudo metadata boundary."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import socket
import subprocess
import time
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
ROLE = ROOT / "infra/ansible/roles/access_record"
RUN_NODE = os.environ.get("RUN_ACCESS_RECORD_NODE_TEST") == "1"


def run(argv, *, input=None, check=True):
    return subprocess.run(
        argv, input=input, text=True, capture_output=True, check=check, timeout=30
    )


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def audit_source_state():
    # These are the two systemd units that own the journal audit transport.
    return {
        unit: run(["systemctl", "show", unit, "--property=ActiveState,SubState,UnitFileState"],
                  check=False).stdout.splitlines()
        for unit in ("systemd-journald-audit.socket", "systemd-journald.service")
    }


@pytest.mark.skipif(not RUN_NODE, reason="requires an explicitly disposable Ubuntu node")
def test_real_ssh_session_boundaries_and_sudo_discard_content(tmp_path):
    assert os.geteuid() == 0, "Run only on an owned disposable Ubuntu node"
    release = platform.freedesktop_os_release()
    assert release["ID"] == "ubuntu" and release["VERSION_ID"] == "24.04"
    vector = os.environ["VECTOR_BIN"]
    versions = run(
        ["dpkg-query", "-W", "openssh-server", "libpam-modules", "sudo", "systemd"]
    ).stdout
    print("Node package versions:\n" + versions)
    assert run(["ssh", "-V"], check=False).stderr.startswith("OpenSSH_9.6")
    import yaml

    print("Audit producer before setup:", audit_source_state())
    audit_setup = tmp_path / "audit-source.yml"
    audit_setup.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "tasks": [{"ansible.builtin.import_tasks": str(ROLE / "tasks/audit-source.yml")}],
    }]))
    try:
        run([os.environ.get("ANSIBLE_PLAYBOOK_BIN", "ansible-playbook"),
             "-c", "local", "-i", "localhost,", str(audit_setup)])
    finally:
        print("Audit producer after setup:", audit_source_state())
    pam = Path("/etc/pam.d/sshd")
    original = pam.read_bytes()
    adapter = ROLE / "files/exomem-pam-session.py"
    # The test modifies only this disposable VM's PAM file and restores it below.
    port = free_port()
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "host")])
    run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / "identity")])
    public = (tmp_path / "identity.pub").read_text().split()[1]
    expected = "SHA256:" + base64.b64encode(
        hashlib.sha256(base64.b64decode(public)).digest()
    ).decode().rstrip("=")
    config = tmp_path / "sshd_config"
    config.write_text(f"""Port {port}
ListenAddress 127.0.0.1
HostKey {tmp_path}/host
PidFile {tmp_path}/sshd.pid
AuthorizedKeysFile {tmp_path}/identity.pub
StrictModes no
UsePAM yes
PermitRootLogin yes
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthenticationMethods publickey
AllowTcpForwarding yes
Subsystem sftp internal-sftp
PrintMotd no
LogLevel ERROR
""")
    Path("/run/sshd").mkdir(exist_ok=True)
    started = time.time()
    sshd = subprocess.Popen(
        ["/usr/sbin/sshd", "-D", "-e", "-f", str(config)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    active = []
    common = [
        "-i",
        str(tmp_path / "identity"),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        f"UserKnownHostsFile={tmp_path}/known_hosts",
    ]
    ssh = ["ssh", *common, "-p", str(port)]
    events = []
    try:
        pam.write_bytes(original + f"\nsession optional pam_exec.so quiet {adapter}\n".encode())
        for _ in range(100):
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        run([*ssh, "root@127.0.0.1", "true"])
        run([*ssh, "-tt", "root@127.0.0.1"], input="printf 'DISCARDED_CONTENT\\n'\nexit\n")
        (tmp_path / "payload").write_text("DISCARDED_CONTENT")
        run(
            ["sftp", *common, "-P", str(port), "-b", "-", "root@127.0.0.1"],
            input=f"put {tmp_path}/payload {tmp_path}/received\n",
        )
        forward = subprocess.Popen(
            [*ssh, "-N", "-L", f"127.0.0.1:{free_port()}:127.0.0.1:{port}", "root@127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        active.append(forward)
        time.sleep(0.5)
        assert forward.poll() is None
        forward.terminate()
        forward.wait(timeout=10)
        control = tmp_path / "control"
        master = subprocess.Popen(
            [*ssh, "-M", "-N", "-S", str(control), "root@127.0.0.1"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        active.append(master)
        for _ in range(100):
            if control.exists():
                break
            time.sleep(0.05)
        run([*ssh, "-S", str(control), "root@127.0.0.1", "true"])
        run([*ssh, "-S", str(control), "root@127.0.0.1", "true"])
        run([*ssh, "-S", str(control), "-O", "exit", "root@127.0.0.1"])
        master.wait(timeout=10)
        run(["sudo", "-n", "-u", "nobody", "true"])
        assert (
            run(["runuser", "-u", "nobody", "--", "sudo", "-n", "true"], check=False).returncode
            != 0
        )
        run(["journalctl", "--sync"])
        journal = run(
            ["journalctl", "--since", f"@{int(started)}", "-o", "json", "--no-pager"]
        ).stdout
        events = [json.loads(line) for line in journal.splitlines()]
        sessions = [event for event in events if event.get("EXOMEM_ACCESS_RECORD") == "1"]
        opens = [event for event in sessions if event.get("EXOMEM_ACTION") == "open_session"]
        closes = [event for event in sessions if event.get("EXOMEM_ACTION") == "close_session"]
        assert len(opens) == 5, [
            (event.get("EXOMEM_ACTION"), event.get("EXOMEM_SESSION_ID")) for event in sessions
        ]
        assert {event["EXOMEM_SESSION_ID"] for event in opens} == {
            event["EXOMEM_SESSION_ID"] for event in closes
        }
        assert all(event.get("EXOMEM_FINGERPRINT") == expected for event in opens)
        assert all(event.get("EXOMEM_FINGERPRINT_KIND") == "public_key_blob" for event in opens)
        # Use the role's actual projection in Vector's native test engine against journal records.
        values = yaml.safe_load((ROLE / "defaults/main.yml").read_text())
        values.update(
            k3s_node_role="server",
            access_record_bucket="synthetic",
            access_record_endpoint="https://storage.example.test",
            access_record_region="test",
            access_record_metrics_address="127.0.0.1:0",
        )
        playbook = tmp_path / "render.yml"
        rendered = tmp_path / "vector.yaml"
        playbook.write_text(
            yaml.safe_dump(
                [
                    {
                        "hosts": "localhost",
                        "gather_facts": False,
                        "vars": values,
                        "tasks": [
                            {
                                "ansible.builtin.template": {
                                    "src": str(ROLE / "templates/vector.yaml.j2"),
                                    "dest": str(rendered),
                                }
                            }
                        ],
                    }
                ]
            )
        )
        run(
            [
                os.environ.get("ANSIBLE_PLAYBOOK_BIN", "ansible-playbook"),
                "-c",
                "local",
                "-i",
                "localhost,",
                str(playbook),
            ]
        )
        projected = []
        for index, event in enumerate(events):
            if (
                event.get("EXOMEM_ACCESS_RECORD") == "1"
                or event.get("AUDIT_FIELD_EXE") == "/usr/bin/sudo"
            ):
                test = {
                    "name": f"journal-{index}",
                    "inputs": [
                        {"insert_at": "journal_metadata", "type": "log", "log_fields": event}
                    ],
                    "outputs": [
                        {
                            "extract_from": "journal_metadata",
                            "conditions": [
                                {
                                    "type": "vrl",
                                    "source": 'assert!(!contains(encode_json(.), "DISCARDED_CONTENT"))\nassert!(!exists(.MESSAGE))\nassert!(!exists(.AUDIT_FIELD_CMD))',
                                }
                            ],
                        }
                    ],
                }
                projected.append(test)
        assert any(
            event.get("AUDIT_FIELD_RES") == "failed"
            and event.get("AUDIT_FIELD_EXE") == "/usr/bin/sudo"
            for event in events
        ), "sudo denial audit producer unavailable"
        assert any(
            event.get("AUDIT_FIELD_RES") == "success"
            and event.get("AUDIT_FIELD_EXE") == "/usr/bin/sudo"
            for event in events
        ), "sudo success audit producer unavailable"
        tests = tmp_path / "journal-tests.yaml"
        tests.write_text(yaml.safe_dump({"tests": projected}))
        env = {
            **os.environ,
            "ACCESS_RECORD_NODE_ID": "node-test",
            "ACCESS_RECORD_BOOT_ID": "boot-test",
            "ACCESS_RECORD_WRITER_KEY_ID": "synthetic",
            "ACCESS_RECORD_WRITER_KEY": "synthetic",
        }
        result = subprocess.run(
            [vector, "--dangerously-allow-env-var-interpolation", "test", str(rendered), str(ROLE / "files/vector-tests.yaml"), str(tests)],
            capture_output=True,
            text=True,
            env=env,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        print(
            f"Observed {len(opens)} SSH connections and {len(projected)} projected journal events; all discarded-field checks passed."
        )
    except Exception:
        print("Audit producer at failure:", audit_source_state())
        print("Journal transport counts:", dict(Counter(event.get("_TRANSPORT") for event in events)))
        print("Audit type counts:", dict(Counter(event.get("_AUDIT_TYPE_NAME") for event in events)))
        # Only presence and closed audit results leave the VM, never command-bearing fields.
        print("Audit key presence:", {key: sum(key in event for event in events) for key in (
            "AUDIT_FIELD_EXE", "AUDIT_FIELD_RES", "AUDIT_FIELD_ACCT", "_AUDIT_ID",
            "_SOURCE_REALTIME_TIMESTAMP", "_AUDIT_LOGINUID", "_AUDIT_SESSION",
        )})
        print("Sanitized sudo results:", [
            {"result": event.get("AUDIT_FIELD_RES")
             if event.get("AUDIT_FIELD_RES") in ("success", "failed") else "unknown",
             "executable_is_sudo": event.get("AUDIT_FIELD_EXE") == "/usr/bin/sudo"}
            for event in events
            if event.get("AUDIT_FIELD_EXE") == "/usr/bin/sudo" or event.get("SYSLOG_IDENTIFIER") == "sudo"
        ][:20])
        raise
    finally:
        for process in active:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
        sshd.terminate()
        sshd.wait(timeout=10)
        pam.write_bytes(original)


@pytest.mark.skipif(not RUN_NODE, reason="requires an explicitly disposable Ubuntu node")
def test_disabling_recording_stops_only_its_installed_unit_and_observation_hooks(tmp_path):
    """Restoring disabled settings must stop a former install and also succeed on an untouched node."""
    assert os.geteuid() == 0
    assert platform.freedesktop_os_release()["ID"] == "ubuntu"
    import yaml

    unit = Path("/etc/systemd/system/exomem-access-record.service")
    pam = Path("/etc/pam.d/sshd")
    sudoers = Path("/etc/sudoers.d/exomem-access-record")
    writer = Path("/etc/exomem-access-record/writer.env")
    paths = (unit, pam, sudoers, writer)
    originals = {path: path.read_bytes() if path.exists() else None for path in paths}
    assert originals[unit] is None, "this proof requires its own disposable install"
    playbook = tmp_path / "disable.yml"
    playbook.write_text(yaml.safe_dump([{
        "hosts": "localhost", "gather_facts": False,
        "vars": {"access_record_enabled": False},
        "roles": [{"role": str(ROLE)}],
    }]))
    command = [os.environ.get("ANSIBLE_PLAYBOOK_BIN", "ansible-playbook"),
               "-c", "local", "-i", "localhost,", str(playbook)]
    try:
        run(command)
        unit.write_text("[Unit]\nDescription=Disposable access record proof\n[Service]\nExecStart=/bin/sleep infinity\n[Install]\nWantedBy=multi-user.target\n")
        run(["systemctl", "daemon-reload"])
        run(["systemctl", "enable", "--now", unit.name])
        pam.write_text(pam.read_text()+"\nsession optional pam_exec.so quiet /usr/local/libexec/exomem-pam-session.py\n")
        sudoers.write_text("Defaults log_denied, log_allowed\n")
        writer.parent.mkdir(exist_ok=True)
        writer.write_text("ACCESS_RECORD_WRITER_KEY=synthetic\n")
        run(command)
        assert run(["systemctl", "is-active", unit.name], check=False).stdout.strip() == "inactive"
        assert run(["systemctl", "is-enabled", unit.name], check=False).stdout.strip() == "disabled"
        assert "/usr/local/libexec/exomem-pam-session.py" not in pam.read_text()
        assert not sudoers.exists() and not writer.exists()
    finally:
        run(["systemctl", "disable", "--now", unit.name], check=False)
        for path, content in originals.items():
            if content is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(content)
        run(["systemctl", "daemon-reload"])
