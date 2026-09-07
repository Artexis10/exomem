from __future__ import annotations

import hashlib
import json
import os
import plistlib
import subprocess
import sys
import time
from pathlib import Path

import pytest

from exomem import native_owner_maintenance
from exomem.native_owner_reviews import OwnerReviewStore


def _systemd_unit(tmp_path: Path, *, vault: Path, state: Path) -> Path:
    if sys.platform != "linux":
        pytest.skip("systemd service fixtures require Linux")
    authority = tmp_path / "authority"
    custody = authority / "custody"
    authority.mkdir(mode=0o700)
    custody.mkdir(mode=0o700)
    env_file = tmp_path / "service.env"
    unit = tmp_path / "exomem.service"
    values = {
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_STATE_ROOT": str(state),
        "EXOMEM_GITHUB_USER_ID": "123",
        "EXOMEM_VOCABULARY_AUTHORITY_DIR": str(authority),
        "EXOMEM_OWNER_SERVICE_UNIT": str(unit),
        "EXOMEM_JWT_SIGNING_KEY": "never-print-this",
    }
    env_file.write_text(
        "".join(f'{key}="{value}"\n' for key, value in values.items()), encoding="utf-8"
    )
    unit.write_text(
        "[Service]\n"
        f"EnvironmentFile={env_file}\n"
        f'ExecStart="{Path(sys.prefix) / "bin" / "python"}" -m exomem '
        "--transport streamable-http --port 8765\n",
        encoding="utf-8",
    )
    return unit


def _launchd_unit(tmp_path: Path, *, vault: Path, state: Path) -> Path:
    if os.name == "nt":
        pytest.skip("native owner maintenance is unsupported on Windows")
    authority = tmp_path / "authority"
    (authority / "custody").mkdir(parents=True, mode=0o700)
    unit = tmp_path / "io.exomem.test.plist"
    environment = {
        "EXOMEM_VAULT_PATH": str(vault),
        "EXOMEM_STATE_ROOT": str(state),
        "EXOMEM_GITHUB_USER_ID": "123",
        "EXOMEM_VOCABULARY_AUTHORITY_DIR": str(authority),
        "EXOMEM_OWNER_SERVICE_UNIT": str(unit),
    }
    encoded = plistlib.dumps(
        {
            "Label": "io.exomem.test",
            "EnvironmentVariables": environment,
            "ProgramArguments": [
                str(Path(sys.prefix) / "bin" / "python"),
                "-m",
                "exomem",
                "--port",
                "8765",
            ],
        },
        sort_keys=False,
    )
    unit.write_bytes(encoded.replace(b"<dict>", b"<!-- managed service -->\n<dict>", 1))
    return unit


def test_loads_fixed_systemd_environment_and_redacts_secrets_from_binding(
    tmp_path: Path,
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")

    environment = native_owner_maintenance.load_service_environment(unit)
    binding = native_owner_maintenance.service_binding(unit)

    assert environment["EXOMEM_JWT_SIGNING_KEY"] == "never-print-this"
    encoded = json.dumps(binding.as_dict(), sort_keys=True)
    assert binding.vault_root == vault.resolve()
    assert binding.port == 8765
    assert "never-print-this" not in encoded
    assert "JWT" not in encoded


def test_refuses_conflicting_unit_binding_and_nonfixed_command(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    env_file = Path(native_owner_maintenance.service_binding(unit).binding_path)
    env_file.write_text(env_file.read_text().replace(str(unit), str(tmp_path / "other.service")))
    with pytest.raises(native_owner_maintenance.NativeOwnerMaintenanceUnavailable):
        native_owner_maintenance.service_binding(unit)

    unit.write_text(unit.read_text().replace("-m exomem", "-c 'print(1)'"))
    with pytest.raises(native_owner_maintenance.NativeOwnerMaintenanceUnavailable):
        native_owner_maintenance.service_binding(unit)


def test_preflight_requires_current_accepted_review_before_stop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    binding = native_owner_maintenance.service_binding(unit)
    review = type(
        "Review",
        (),
        {
            "state": "prepared",
            "expired": False,
            "action": "migration",
            "body": {
                "service_unit": str(unit),
                "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
                "service_interpreter": str(binding.interpreter),
                "service_environment_digest": binding.environment_digest,
                "service_target_environment_digest": "b" * 64,
                "runtime_version": native_owner_maintenance._runtime_version(),
                "custody_environment": {},
            },
        },
    )()
    monkeypatch.setattr(
        native_owner_maintenance,
        "OwnerReviewStore",
        lambda _vault: type("Store", (), {"get": lambda self, *_args, **_kwargs: review})(),
    )

    with pytest.raises(native_owner_maintenance.NativeOwnerMaintenanceDenied):
        native_owner_maintenance.preflight(unit, "owner-review-" + "a" * 32, now=1)


def test_preflight_binds_exact_unit_vault_environment_and_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    binding = native_owner_maintenance.service_binding(unit)
    custody = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": str(tmp_path / "authority/custody/keyring.json"),
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": str(tmp_path / "authority/custody/control.json"),
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": str(tmp_path / "authority/custody/membership.json"),
        "EXOMEM_AUTH_SESSION_REPLICA_ID": "replica-1",
    }
    review = type(
        "Review",
        (),
        {
            "state": "accepted",
            "expired": False,
            "action": "migration",
            "body": {
                "service_unit": str(unit.resolve()),
                "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
                "service_interpreter": str(binding.interpreter),
                "service_environment_digest": binding.environment_digest,
                "service_target_environment_digest": native_owner_maintenance.planned_environment_digest(
                    unit, custody
                ),
                "runtime_version": native_owner_maintenance._runtime_version(),
                "custody_environment": custody,
            },
        },
    )()
    monkeypatch.setattr(
        native_owner_maintenance,
        "OwnerReviewStore",
        lambda _vault: type("Store", (), {"get": lambda self, *_args, **_kwargs: review})(),
    )

    checked = native_owner_maintenance.preflight(unit, "owner-review-" + "a" * 32, now=1)

    assert checked.binding == binding
    assert dict(checked.custody_environment) == custody
    assert (
        hashlib.sha256(Path(binding.binding_path).read_bytes()).hexdigest()
        == binding.environment_digest
    )


def test_preflight_refuses_a_changed_service_unit_after_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    binding = native_owner_maintenance.service_binding(unit)
    custody = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": str(tmp_path / "authority/custody/keyring.json"),
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": str(tmp_path / "authority/custody/control.json"),
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": str(tmp_path / "authority/custody/membership.json"),
        "EXOMEM_AUTH_SESSION_REPLICA_ID": "replica-1",
    }
    review = type(
        "Review",
        (),
        {
            "state": "accepted",
            "expired": False,
            "action": "migration",
            "body": {
                "service_unit": str(unit.resolve()),
                "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
                "service_interpreter": str(binding.interpreter),
                "service_environment_digest": binding.environment_digest,
                "service_target_environment_digest": native_owner_maintenance.planned_environment_digest(
                    unit, custody
                ),
                "runtime_version": native_owner_maintenance._runtime_version(),
                "custody_environment": custody,
            },
        },
    )()
    monkeypatch.setattr(
        native_owner_maintenance,
        "OwnerReviewStore",
        lambda _vault: type("Store", (), {"get": lambda self, *_args, **_kwargs: review})(),
    )
    unit.write_text(unit.read_text().replace("--port 8765", "--port 9999"))

    with pytest.raises(native_owner_maintenance.NativeOwnerMaintenanceDenied):
        native_owner_maintenance.preflight(unit, "owner-review-" + "a" * 32, now=1)


def test_launchd_completed_resume_uses_the_exact_planned_target_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _launchd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    source = native_owner_maintenance.service_binding(unit)
    custody = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": str(tmp_path / "authority/custody/keyring.json"),
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": str(tmp_path / "authority/custody/control.json"),
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": str(tmp_path / "authority/custody/membership.json"),
        "EXOMEM_AUTH_SESSION_REPLICA_ID": "replica-1",
    }
    target_digest = native_owner_maintenance.planned_environment_digest(unit, custody)
    review = type(
        "Review",
        (),
        {
            "state": "completed",
            "expired": True,
            "started_at": 9,
            "expires_at": 10,
            "action": "migration",
            "result": {"status": "completed"},
            "body": {
                "service_unit": str(unit.resolve()),
                "service_unit_digest": source.unit_digest,
                "service_interpreter": str(source.interpreter),
                "service_environment_digest": source.environment_digest,
                "service_target_environment_digest": target_digest,
                "runtime_version": native_owner_maintenance._runtime_version(),
                "custody_environment": custody,
            },
        },
    )()
    monkeypatch.setattr(
        native_owner_maintenance,
        "OwnerReviewStore",
        lambda _vault: type("Store", (), {"get": lambda self, *_args, **_kwargs: review})(),
    )
    checked = native_owner_maintenance.preflight(
        unit, "owner-review-" + "a" * 32, now=11, resume=True
    )

    native_owner_maintenance._persist_custody(checked)

    assert hashlib.sha256(unit.read_bytes()).hexdigest() == target_digest
    assert (
        native_owner_maintenance.preflight(
            unit, "owner-review-" + "a" * 32, now=11, resume=True
        ).state
        == "completed"
    )


def test_resume_allows_only_an_expired_started_applying_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    binding = native_owner_maintenance.service_binding(unit)
    custody = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": str(tmp_path / "authority/custody/keyring.json"),
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": str(tmp_path / "authority/custody/control.json"),
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": str(tmp_path / "authority/custody/membership.json"),
        "EXOMEM_AUTH_SESSION_REPLICA_ID": "replica-1",
    }

    def review(state: str, started_at: int | None):
        return type(
            "Review",
            (),
            {
                "state": state,
                "expired": True,
                "started_at": started_at,
                "expires_at": 10,
                "action": "migration",
                "body": {
                    "service_unit": str(unit.resolve()),
                    "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
                    "service_interpreter": str(binding.interpreter),
                    "service_environment_digest": binding.environment_digest,
                    "service_target_environment_digest": native_owner_maintenance.planned_environment_digest(
                        unit, custody
                    ),
                    "runtime_version": native_owner_maintenance._runtime_version(),
                    "custody_environment": custody,
                },
            },
        )()

    selected = [review("applying", 9)]
    monkeypatch.setattr(
        native_owner_maintenance,
        "OwnerReviewStore",
        lambda _vault: type("Store", (), {"get": lambda self, *_args, **_kwargs: selected[0]})(),
    )
    assert native_owner_maintenance.preflight(
        unit, "owner-review-" + "a" * 32, now=11, resume=True
    ).review_id

    for item in (review("accepted", None), review("applying", None), review("applying", 10)):
        selected[0] = item
        with pytest.raises(native_owner_maintenance.NativeOwnerMaintenanceDenied):
            native_owner_maintenance.preflight(
                unit, "owner-review-" + "a" * 32, now=11, resume=True
            )


def test_completed_resume_accepts_only_the_exact_reviewed_environment_extension(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    source = native_owner_maintenance.service_binding(unit)
    custody = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": str(tmp_path / "authority/custody/keyring.json"),
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": str(tmp_path / "authority/custody/control.json"),
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": str(tmp_path / "authority/custody/membership.json"),
        "EXOMEM_AUTH_SESSION_REPLICA_ID": "replica-1",
    }
    review = type(
        "Review",
        (),
        {
            "state": "completed",
            "expired": True,
            "started_at": 9,
            "expires_at": 10,
            "action": "migration",
            "result": {"status": "completed"},
            "body": {
                "service_unit": str(unit.resolve()),
                "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
                "service_interpreter": str(source.interpreter),
                "service_environment_digest": source.environment_digest,
                "service_target_environment_digest": native_owner_maintenance.planned_environment_digest(
                    unit, custody
                ),
                "runtime_version": native_owner_maintenance._runtime_version(),
                "custody_environment": custody,
            },
        },
    )()
    env_file = Path(source.binding_path)
    with env_file.open("a", encoding="utf-8") as stream:
        for name, value in sorted(custody.items()):
            stream.write(f'{name}="{value}"\n')
    monkeypatch.setattr(
        native_owner_maintenance,
        "OwnerReviewStore",
        lambda _vault: type("Store", (), {"get": lambda self, *_args, **_kwargs: review})(),
    )

    checked = native_owner_maintenance.preflight(
        unit, "owner-review-" + "a" * 32, now=11, resume=True
    )

    assert checked.state == "completed"
    with env_file.open("a", encoding="utf-8") as stream:
        stream.write('UNREVIEWED="drift"\n')
    with pytest.raises(native_owner_maintenance.NativeOwnerMaintenanceDenied):
        native_owner_maintenance.preflight(unit, "owner-review-" + "a" * 32, now=11, resume=True)


def test_completed_resume_does_not_reapply_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    checked = type(
        "Checked",
        (),
        {
            "state": "completed",
            "review_id": "owner-review-" + "a" * 32,
            "result": {"status": "completed"},
            "binding": object(),
        },
    )()
    monkeypatch.setattr(native_owner_maintenance, "preflight", lambda *_args, **_kwargs: checked)
    monkeypatch.setattr(native_owner_maintenance, "_verify_interpreter", lambda _binding: None)
    monkeypatch.setattr(native_owner_maintenance, "_verify_receipt", lambda *_args: None)
    persisted = []
    monkeypatch.setattr(native_owner_maintenance, "_persist_custody", persisted.append)

    result = native_owner_maintenance.apply_maintenance(
        Path("unit"), checked.review_id, Path("receipt"), now=11, resume=True
    )

    assert result["terminal"] == {"status": "completed"}
    assert persisted == [checked]


def test_completed_resume_persists_reviewed_custody_from_source_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    environment = native_owner_maintenance.load_service_environment(unit)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    source = native_owner_maintenance.service_binding(unit)
    custody = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": str(tmp_path / "authority/custody/keyring.json"),
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": str(tmp_path / "authority/custody/control.json"),
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": str(tmp_path / "authority/custody/membership.json"),
        "EXOMEM_AUTH_SESSION_REPLICA_ID": "replica-1",
    }
    store = OwnerReviewStore(vault)
    review = store.prepare(
        owner_id="github:123",
        action="activation",
        body={
            "service_unit": str(unit.resolve()),
            "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
            "service_interpreter": str(source.interpreter),
            "service_environment_digest": source.environment_digest,
            "service_target_environment_digest": native_owner_maintenance.planned_environment_digest(
                unit, custody
            ),
            "runtime_version": native_owner_maintenance._runtime_version(),
            "custody_environment": custody,
            "renewal": "same-authority",
        },
        display={"title": "Activate"},
        binding_digest="a" * 64,
        expires_at=200,
        now=100,
    )
    store.accept(
        review.review_id,
        owner_id="github:123",
        expected_binding="a" * 64,
        now=100,
    )
    store.begin(review.review_id, owner_id="github:123", now=100)
    store.complete(
        review.review_id,
        owner_id="github:123",
        result={"status": "completed"},
        now=100,
    )
    monkeypatch.setattr(native_owner_maintenance, "_verify_interpreter", lambda _binding: None)
    monkeypatch.setattr(native_owner_maintenance, "_verify_receipt", lambda *_args: None)

    result = native_owner_maintenance.apply_maintenance(
        unit, review.review_id, Path("receipt"), now=201, resume=True
    )

    persisted = native_owner_maintenance.load_service_environment(unit)
    assert result["terminal"] == {"status": "completed"}
    assert {name: persisted[name] for name in custody} == custody
    with Path(source.binding_path).open("a", encoding="utf-8") as stream:
        stream.write('UNREVIEWED="drift"\n')
    with pytest.raises(native_owner_maintenance.NativeOwnerMaintenanceDenied):
        native_owner_maintenance.apply_maintenance(
            unit, review.review_id, Path("receipt"), now=202, resume=True
        )


def test_exec_phase_uses_service_environment_without_printing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    captured = {}
    monkeypatch.delenv("EXOMEM_OWNER_MAINTENANCE_MANAGED", raising=False)
    monkeypatch.setattr(
        os,
        "execve",
        lambda executable, argv, environment: captured.update(
            executable=executable, argv=argv, environment=environment
        ),
    )

    native_owner_maintenance.exec_managed_phase(unit, ["metadata", "--unit-file", str(unit)])

    assert captured["environment"]["EXOMEM_JWT_SIGNING_KEY"] == "never-print-this"
    assert captured["environment"]["EXOMEM_OWNER_MAINTENANCE_MANAGED"] == "1"


def test_managed_venv_interpreter_runs_metadata_and_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = tmp_path / "vault"
    vault.mkdir(mode=0o700)
    unit = _systemd_unit(tmp_path, vault=vault, state=tmp_path / "state")
    environment = native_owner_maintenance.load_service_environment(unit)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    binding = native_owner_maintenance.service_binding(unit)
    now = int(time.time())
    custody = {
        "EXOMEM_AUTH_SESSION_KEYRING_FILE": str(tmp_path / "authority/custody/keyring.json"),
        "EXOMEM_AUTH_SESSION_CONTROL_FILE": str(tmp_path / "authority/custody/control.json"),
        "EXOMEM_AUTH_SESSION_MEMBERSHIP_FILE": str(tmp_path / "authority/custody/membership.json"),
        "EXOMEM_AUTH_SESSION_REPLICA_ID": "replica-1",
    }
    review = OwnerReviewStore(vault).prepare(
        owner_id="github:123",
        action="migration",
        body={
            "service_unit": str(unit.resolve()),
            "service_unit_digest": hashlib.sha256(unit.read_bytes()).hexdigest(),
            "service_interpreter": str(binding.interpreter),
            "service_environment_digest": binding.environment_digest,
            "service_target_environment_digest": native_owner_maintenance.planned_environment_digest(
                unit, custody
            ),
            "runtime_version": native_owner_maintenance._runtime_version(),
            "custody_environment": custody,
        },
        display={"title": "Migrate"},
        binding_digest="a" * 64,
        expires_at=now + 300,
        now=now,
    )
    OwnerReviewStore(vault).accept(
        review.review_id,
        owner_id="github:123",
        expected_binding="a" * 64,
        now=now,
    )
    base = [
        str(Path(sys.prefix) / "bin" / "python"),
        "-m",
        "exomem.native_owner_maintenance",
    ]

    metadata = subprocess.run(
        [
            *base,
            "metadata",
            "--unit-file",
            str(unit),
            "--request-id",
            review.review_id,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    preflight = subprocess.run(
        [
            *base,
            "preflight",
            "--unit-file",
            str(unit),
            "--request-id",
            review.review_id,
        ],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert metadata.returncode == 0, metadata.stderr
    assert json.loads(metadata.stdout)["service_id"] == "exomem"
    assert preflight.returncode == 0, preflight.stderr
    assert json.loads(preflight.stdout) == {
        "status": "accepted",
        "review_id": review.review_id,
    }
