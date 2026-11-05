"""OpenSSH's versioned PAM data never exposes raw authentication blobs."""

import base64
import hashlib
import importlib.util
from pathlib import Path

SOURCE = (
    Path(__file__).resolve().parents[3]
    / "infra/ansible/roles/access_record/files/exomem-pam-session.py"
)
spec = importlib.util.spec_from_file_location("pam_session", SOURCE)
pam_session = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pam_session)


def test_key_fingerprint_hashes_decoded_blob_and_discards_method_details(tmp_path):
    import subprocess

    key = tmp_path / "identity"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    key_type, encoded, *_ = key.with_suffix(".pub").read_text().split()
    blob = base64.b64decode(encoded)
    metadata = pam_session.authentication_metadata(
        f"publickey {key_type} {encoded} private-detail\n"
    )
    assert metadata["EXOMEM_FINGERPRINT"] == "SHA256:" + base64.b64encode(
        hashlib.sha256(blob).digest()
    ).decode().rstrip("=")
    assert metadata["EXOMEM_FINGERPRINT_KIND"] == "public_key_blob"
    assert encoded not in str(metadata)
    assert "private-detail" not in str(metadata)


def test_certificate_fingerprint_does_not_claim_to_identify_subject_key(tmp_path):
    import subprocess

    key = tmp_path / "identity"
    ca = tmp_path / "ca"
    for target in (key, ca):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(target)], check=True
        )
    subprocess.run(
        [
            "ssh-keygen",
            "-q",
            "-s",
            str(ca),
            "-I",
            "synthetic",
            "-n",
            "synthetic",
            str(key.with_suffix(".pub")),
        ],
        check=True,
    )
    key_type, encoded, *_ = (tmp_path / "identity-cert.pub").read_text().split()
    metadata = pam_session.authentication_metadata(f"publickey {key_type} {encoded}")
    assert metadata["EXOMEM_FINGERPRINT_KIND"] == "certificate_blob"


def test_malformed_versioned_authentication_data_stays_unavailable():
    assert pam_session.authentication_metadata("publickey ssh-ed25519 not-base64") == {}
    assert pam_session.authentication_metadata("") == {}
