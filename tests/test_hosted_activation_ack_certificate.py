"""The activation acknowledgement listener certificate, from issuance to artifact.

The fake SOPS here keys recorded plaintext by ciphertext bytes rather than by
path, because the seal encrypts into a temporary file and hard-links it into
place. Keying by path would make a renewal unable to read back the artifact it
just published.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization

ROOT = Path(__file__).resolve().parents[1]
INFRA = ROOT / "infra"
SCRIPT = INFRA / "scripts" / "activation_ack_certificate_handoff.py"
MATRIX = INFRA / "contracts" / "secret-destinations-v1.json"
NAMESPACE = "exomem-platform"
DNS_NAME = "exomem-activation-ack.exomem-platform.svc.cluster.local"


def _load_module():
    spec = importlib.util.spec_from_file_location("activation_ack_certificate_handoff", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _matrix_with_certificate_route(tmp_path: Path) -> Path:
    matrix = json.loads(MATRIX.read_text(encoding="utf-8"))
    matrix["secrets"]["activation_ack_tls_pair"] = {
        "value_shape": "file",
        "sources": [{"kind": "generated-activation-ack-tls"}],
        "destinations": {
            "k3s.activation-ack-tls.active": {
                "kind": "sops_k8s_tls_secret",
                "slot": "active",
                "target": "infra/secrets/platform/activation-ack-tls.{version}.sops.json",
                "namespace": NAMESPACE,
                "kubernetes_secret": "exomem-activation-ack-tls",
            }
        },
    }
    matrix["secrets"]["activation_ack_ca_private_key"] = {
        "value_shape": "file",
        "sources": [{"kind": "generated-activation-ack-tls"}],
        "destinations": {
            "escrow.activation-ack-ca.active": {
                "kind": "sops_escrow",
                "slot": "active",
                "target": "infra/secrets/escrow/activation-ack-ca.{version}.sops.json",
                "secret_key": "ca_private_key",
            }
        },
    }
    path = tmp_path / "certificate-matrix.json"
    path.write_text(json.dumps(matrix), encoding="utf-8")
    path.chmod(0o644)
    return path


def _seal(value: object) -> object:
    if isinstance(value, dict):
        return {key: _seal(item) for key, item in value.items()}
    if isinstance(value, str):
        return "ENC[test]"
    return value


def _install_fake_sops(monkeypatch: pytest.MonkeyPatch, module) -> dict[bytes, dict]:
    """Replace SOPS everywhere this command reaches it, recording plaintext."""

    plaintext_by_ciphertext: dict[bytes, dict] = {}
    handoff = module._load_handoff()
    real_run = subprocess.run

    def _runner(command, **kwargs):
        command = list(command)
        if len(command) < 2 or command[1] not in {"encrypt", "decrypt"}:
            return real_run(command, **kwargs)
        if command[1] == "encrypt":
            document = json.loads(kwargs["input"])
            output_path = Path(command[command.index("--output") + 1])
            ciphertext = _seal(document)
            assert isinstance(ciphertext, dict)
            ciphertext["sops"] = {"mac": "ENC[test]"}
            payload = json.dumps(ciphertext).encode("utf-8")
            output_path.write_bytes(payload)
            plaintext_by_ciphertext[payload] = document
            return subprocess.CompletedProcess(command, 0, stdout=b"", stderr=b"")
        payload = Path(command[-1]).read_bytes()
        return subprocess.CompletedProcess(
            command,
            0,
            stdout=json.dumps(plaintext_by_ciphertext[payload]).encode("utf-8"),
            stderr=b"",
        )

    monkeypatch.setattr(module.subprocess, "run", _runner)
    monkeypatch.setattr(handoff.subprocess, "run", _runner)
    monkeypatch.setenv("SOPS_AGE_RECIPIENTS", "age1testrecipient")
    return plaintext_by_ciphertext


def _mint(
    module,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str = "v1",
    **overrides,
) -> tuple[dict, Path, Path, dict[bytes, dict]]:
    recorded = _install_fake_sops(monkeypatch, module)
    matrix_path = _matrix_with_certificate_route(tmp_path)
    trust_bundle = tmp_path / "public" / "activation-ack-ca.pem"
    arguments = {
        "matrix_path": matrix_path,
        "repository_root": tmp_path,
        "version": version,
        "platform_namespace": NAMESPACE,
        "trust_bundle_path": trust_bundle,
        "sops_bin": "sops",
        "new_authority": True,
        "authority_artifact": None,
        "certificate_lifetime_days": 90,
        "authority_lifetime_days": 3650,
        "test_authority": False,
    }
    arguments.update(overrides)
    report = module.execute_certificate_handoff(**arguments)
    return report, matrix_path, trust_bundle, recorded


def test_a_new_authority_publishes_the_pair_the_escrow_and_the_trust_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    report, _, trust_bundle, recorded = _mint(module, tmp_path, monkeypatch)

    assert report["dns_name"] == DNS_NAME
    assert report["rotated_authority"] is True

    tls_artifact = tmp_path / "infra/secrets/platform/activation-ack-tls.v1.sops.json"
    escrow_artifact = tmp_path / "infra/secrets/escrow/activation-ack-ca.v1.sops.json"
    assert tls_artifact.is_file()
    assert escrow_artifact.is_file()
    assert stat.S_IMODE(tls_artifact.stat().st_mode) == 0o600
    assert stat.S_IMODE(escrow_artifact.stat().st_mode) == 0o600

    sealed = recorded[tls_artifact.read_bytes()]
    assert sealed["type"] == "kubernetes.io/tls"
    assert set(sealed["stringData"]) == {"tls.crt", "tls.key"}

    # The bundle is the CA only. The digest reported here is what the deployment
    # lock pins and what names the cell's immutable trust ConfigMap.
    bundle = trust_bundle.read_text(encoding="utf-8")
    assert "PRIVATE KEY" not in bundle
    assert bundle.count("BEGIN CERTIFICATE") == 1
    assert report["trust_bundle_sha256"] == module.hashlib.sha256(bundle.encode()).hexdigest()

    # No private key reaches any published artifact in the clear.
    private_key = sealed["stringData"]["tls.key"].encode("utf-8")
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert private_key not in path.read_bytes()


def test_the_issued_certificate_is_the_one_the_cell_will_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _, _, trust_bundle, recorded = _mint(module, tmp_path, monkeypatch)
    tls_artifact = tmp_path / "infra/secrets/platform/activation-ack-tls.v1.sops.json"
    leaf = x509.load_pem_x509_certificate(
        recorded[tls_artifact.read_bytes()]["stringData"]["tls.crt"].encode("ascii")
    )

    names = leaf.extensions.get_extension_for_class(
        x509.SubjectAlternativeName
    ).value.get_values_for_type(x509.DNSName)
    assert names == [DNS_NAME]
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    # The two extensions path validation requires and internal issuance forgets.
    assert leaf.extensions.get_extension_for_class(x509.AuthorityKeyIdentifier)
    authority = x509.load_pem_x509_certificate(trust_bundle.read_bytes())
    assert authority.extensions.get_extension_for_class(x509.KeyUsage).value.key_cert_sign

    configuration = module._load_activation_ack_configuration()
    assert configuration.validate_activation_ack_server_certificate(
        recorded[tls_artifact.read_bytes()]["stringData"]["tls.crt"],
        platform_namespace=NAMESPACE,
        trust_pem=trust_bundle.read_text(encoding="utf-8"),
        now=dt.datetime.now(dt.UTC),
    )


def test_renewal_reuses_the_authority_and_leaves_every_cell_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    first, matrix_path, trust_bundle, recorded = _mint(module, tmp_path, monkeypatch)
    bundle_before = trust_bundle.read_bytes()
    escrow_artifact = tmp_path / "infra/secrets/escrow/activation-ack-ca.v1.sops.json"

    second = module.execute_certificate_handoff(
        matrix_path=matrix_path,
        repository_root=tmp_path,
        version="v2",
        platform_namespace=NAMESPACE,
        trust_bundle_path=trust_bundle,
        sops_bin="sops",
        new_authority=False,
        authority_artifact=escrow_artifact,
        certificate_lifetime_days=90,
        authority_lifetime_days=3650,
        test_authority=False,
    )

    assert second["rotated_authority"] is False
    assert second["serial_number"] != first["serial_number"]
    # The whole point of the routine tier: same bundle, same digest, so the
    # deployment lock and every cell's trust ConfigMap are unchanged.
    assert trust_bundle.read_bytes() == bundle_before
    assert second["trust_bundle_sha256"] == first["trust_bundle_sha256"]
    assert not (tmp_path / "infra/secrets/escrow/activation-ack-ca.v2.sops.json").exists()

    renewed = tmp_path / "infra/secrets/platform/activation-ack-tls.v2.sops.json"
    assert renewed.is_file()
    leaf = x509.load_pem_x509_certificate(
        recorded[renewed.read_bytes()]["stringData"]["tls.crt"].encode("ascii")
    )
    authority = x509.load_pem_x509_certificate(bundle_before)
    assert leaf.issuer == authority.subject


def test_renewal_refuses_an_authority_that_is_not_in_the_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _, matrix_path, trust_bundle, _ = _mint(module, tmp_path, monkeypatch)
    foreign_key, foreign = module.build_authority(
        now=dt.datetime.now(dt.UTC), lifetime_days=3650, common_name="Someone else"
    )
    foreign_escrow = tmp_path / "foreign.sops.json"
    foreign_escrow.write_text(
        json.dumps(
            {
                "ca_private_key": foreign_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                ).decode("ascii")
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module,
        "_read_escrowed_authority",
        lambda **_kwargs: foreign_key,
    )
    assert foreign.subject

    with pytest.raises(module.CertificateHandoffError, match="does not appear in the trust bundle"):
        module.execute_certificate_handoff(
            matrix_path=matrix_path,
            repository_root=tmp_path,
            version="v2",
            platform_namespace=NAMESPACE,
            trust_bundle_path=trust_bundle,
            sops_bin="sops",
            new_authority=False,
            authority_artifact=foreign_escrow,
            certificate_lifetime_days=90,
            authority_lifetime_days=3650,
            test_authority=False,
        )
    assert not (tmp_path / "infra/secrets/platform/activation-ack-tls.v2.sops.json").exists()


def test_a_certificate_that_would_fail_preflight_never_reaches_an_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spent version number is not recoverable, so validation precedes sealing."""

    module = _load_module()
    _install_fake_sops(monkeypatch, module)
    matrix_path = _matrix_with_certificate_route(tmp_path)

    with pytest.raises(ValueError, match="lifetime is unbounded"):
        module.execute_certificate_handoff(
            matrix_path=matrix_path,
            repository_root=tmp_path,
            version="v1",
            platform_namespace=NAMESPACE,
            trust_bundle_path=tmp_path / "public" / "activation-ack-ca.pem",
            sops_bin="sops",
            new_authority=True,
            authority_artifact=None,
            certificate_lifetime_days=400,
            authority_lifetime_days=3650,
            test_authority=False,
        )

    assert not (tmp_path / "infra/secrets/platform/activation-ack-tls.v1.sops.json").exists()
    assert not (tmp_path / "infra/secrets/escrow/activation-ack-ca.v1.sops.json").exists()
    assert not (tmp_path / "public" / "activation-ack-ca.pem").exists()


def test_a_disposable_authority_is_marked_and_refused_as_live_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _, _, trust_bundle, recorded = _mint(module, tmp_path, monkeypatch, test_authority=True)
    configuration = module._load_activation_ack_configuration()

    bundle = trust_bundle.read_text(encoding="utf-8")
    assert configuration.ACTIVATION_ACK_TEST_TRUST_MARKER in bundle_subject(bundle)

    tls_artifact = tmp_path / "infra/secrets/platform/activation-ack-tls.v1.sops.json"
    certificate = recorded[tls_artifact.read_bytes()]["stringData"]["tls.crt"]
    with pytest.raises(ValueError, match="disposable test authority"):
        configuration.validate_activation_ack_server_certificate(
            certificate,
            platform_namespace=NAMESPACE,
            trust_pem=bundle,
            now=dt.datetime.now(dt.UTC),
        )


def bundle_subject(bundle: str) -> str:
    return x509.load_pem_x509_certificate(bundle.encode("ascii")).subject.rfc4514_string()


def test_the_command_refuses_both_modes_and_neither(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _load_module()
    _install_fake_sops(monkeypatch, module)
    matrix_path = _matrix_with_certificate_route(tmp_path)
    common = {
        "matrix_path": matrix_path,
        "repository_root": tmp_path,
        "version": "v1",
        "platform_namespace": NAMESPACE,
        "trust_bundle_path": tmp_path / "public" / "activation-ack-ca.pem",
        "sops_bin": "sops",
        "certificate_lifetime_days": 90,
        "authority_lifetime_days": 3650,
        "test_authority": False,
    }

    for new_authority, artifact in ((True, tmp_path / "some.sops.json"), (False, None)):
        with pytest.raises(module.CertificateHandoffError, match="exactly one"):
            module.execute_certificate_handoff(
                **common, new_authority=new_authority, authority_artifact=artifact
            )


def test_the_cli_reports_only_public_facts_and_stays_quiet_on_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    module = _load_module()
    _install_fake_sops(monkeypatch, module)
    matrix_path = _matrix_with_certificate_route(tmp_path)
    trust_bundle = tmp_path / "public" / "activation-ack-ca.pem"

    assert (
        module.main(
            [
                "--matrix",
                str(matrix_path),
                "--repository-root",
                str(tmp_path),
                "--version",
                "v1",
                "--platform-namespace",
                NAMESPACE,
                "--trust-bundle",
                str(trust_bundle),
                "--new-authority",
            ]
        )
        == 0
    )
    captured = capsys.readouterr()
    report = json.loads(captured.out)
    assert set(report) == {
        "dns_name",
        "not_valid_after",
        "serial_number",
        "trust_bundle_sha256",
        "rotated_authority",
    }
    assert "PRIVATE KEY" not in captured.out
    assert captured.err == ""

    # Reusing a spent version must fail, and must say nothing about why.
    assert (
        module.main(
            [
                "--matrix",
                str(matrix_path),
                "--repository-root",
                str(tmp_path),
                "--version",
                "v1",
                "--platform-namespace",
                NAMESPACE,
                "--trust-bundle",
                str(trust_bundle),
                "--new-authority",
            ]
        )
        == 2
    )
    refused = capsys.readouterr()
    assert refused.out == ""
    assert refused.err.strip() == "activation acknowledgement certificate handoff rejected"
