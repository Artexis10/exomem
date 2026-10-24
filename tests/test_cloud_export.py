"""The owner export Job: the canonical archive, uploaded to presigned parts over TLS."""
from __future__ import annotations

import datetime
import errno
import hashlib
import ipaddress
import json
import os
import ssl
import subprocess
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from exomem import cloud_export, vault_portability

# The export Job runs only on a Linux cell: it measures its volume with statvfs.
pytestmark = pytest.mark.skipif(os.name == "nt", reason="the cell export Job runs only on Linux cells")

CELL = "abcdefghijklmnop"
# Shared with cellctl's own test (infra/cellctl/tests/test_exports.py).
DIGEST_VECTOR = "91124627b9b8d6bffcbbfb35d09b5fb3cea67f87e4e84d341fac87ba0d0586da"


class PartStore(BaseHTTPRequestHandler):
    """Object storage at its presigned-URL boundary: one part per PUT, S3's MD5 ETag back."""

    parts: dict[str, bytes] = {}

    def do_PUT(self):  # noqa: N802 - http.server's handler name
        body = self.rfile.read(int(self.headers["Content-Length"]))
        self.parts[self.path] = body
        self.send_response(200)
        self.send_header("ETag", '"' + hashlib.md5(body).hexdigest() + '"')  # noqa: S324 - S3's part ETag
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, *_args):
        pass


@pytest.fixture
def storage(tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Disposable export storage")])
    now = datetime.datetime.now(datetime.UTC)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=1))
                   .not_valid_after(now + datetime.timedelta(hours=1))
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                                  critical=False)
                   .sign(key, hashes.SHA256()))
    cert_path, key_path = tmp_path / "storage.crt", tmp_path / "storage.key"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    PartStore.parts = {}
    server = ThreadingHTTPServer(("127.0.0.1", 0), PartStore)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_path, key_path)
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"base": f"https://127.0.0.1:{server.server_address[1]}", "ca": cert_path, "parts": PartStore.parts}
    finally:
        server.shutdown()
        server.server_close()


def _cell_export(vault: Path, tmp_path: Path, storage: dict, *, part_bytes: int, urls: list[str],
                 max_bytes: int = 1024**3) -> tuple[int, dict]:
    staging, log = tmp_path / "staging", tmp_path / "termination-log"
    args = [sys.executable, "-m", "exomem", "cell-export", "--export-id", str(uuid.uuid4()),
            "--part-bytes", str(part_bytes), "--max-bytes", str(max_bytes), "--vault", str(vault),
            "--staging", str(staging), "--termination-log", str(log)]
    for url in urls:
        args += ["--part-url", url]
    env = {**os.environ, "EXOMEM_CLOUD_CELL": "1", "EXOMEM_CLOUD_CELL_ID": CELL, "SSL_CERT_FILE": str(storage["ca"])}
    result = subprocess.run(args, env=env, capture_output=True, timeout=300)
    return result.returncode, json.loads(log.read_text(encoding="ascii"))


def test_a_held_cells_export_uploads_a_verifiable_archive_and_reports_only_its_shape(vault, tmp_path, storage):
    """Defect: the owner receives an archive that does not verify, or the report misdescribes the upload."""
    part_bytes = 64 * 1024
    urls = [f"{storage['base']}/cells/{CELL}/exports/archive.zip?partNumber={number}" for number in range(1, 201)]
    status, report = _cell_export(vault, tmp_path, storage, part_bytes=part_bytes, urls=urls)
    assert status == 0, report
    uploaded = [storage["parts"][f"/cells/{CELL}/exports/archive.zip?partNumber={number}"]
                for number in range(1, report["parts"] + 1)]
    assert report["parts"] > 1 and len(storage["parts"]) == report["parts"]
    assert all(len(part) == part_bytes for part in uploaded[:-1])
    archive = tmp_path / "reassembled.zip"
    archive.write_bytes(b"".join(uploaded))
    verified = vault_portability.verify_export_archive(archive, expected_cell_id=CELL)
    assert (verified.archive_sha256, verified.archive_size) == (report["sha256"], report["bytes"])
    etags = ['"' + hashlib.md5(part).hexdigest() + '"' for part in uploaded]  # noqa: S324 - S3's part ETag
    assert report["etag_digest"] == cloud_export.etag_digest(etags)
    assert set(report) == {"bytes", "sha256", "parts", "etag_digest", "used", "total"}
    assert 0 <= report["used"] <= report["total"]
    assert not list((tmp_path / "staging").glob("*.zip"))  # the node's staging disk is freed


def test_an_archive_larger_than_its_parts_is_refused_before_any_upload(vault, tmp_path, storage):
    """Defect: a too-small upload is completed as a truncated archive, or blamed on the owner's vault."""
    status, report = _cell_export(vault, tmp_path, storage, part_bytes=1024,
                                  urls=[f"{storage['base']}/cells/{CELL}/exports/archive.zip?partNumber=1"])
    assert status == 1 and report["error"] == "EXPORT_FAILED"
    assert storage["parts"] == {}


def test_the_cells_size_bounds_the_export_in_place_of_the_default_limit(vault, tmp_path, storage):
    """Defect: the 10 GiB default refuses every export of a cell grown past it."""
    status, report = _cell_export(vault, tmp_path, storage, part_bytes=64 * 1024, max_bytes=100,
                                  urls=[f"{storage['base']}/cells/{CELL}/exports/archive.zip?partNumber=1"])
    assert status == 1 and report["error"] == "EXPORT_FAILED"
    assert report["diagnostic"]["code"] == "RESOURCE_LIMIT_EXCEEDED"
    assert report["diagnostic"]["limit"] == "max_total_bytes"
    assert storage["parts"] == {}


def test_only_a_file_above_the_per_file_limit_reads_as_too_large(vault, tmp_path, storage):
    """Defect: the owner is told to shrink a vault that any other failure stopped."""
    with (vault / "recording.bin").open("wb") as sparse:
        sparse.truncate(vault_portability.PortabilityLimits().max_file_bytes + 1)
    status, report = _cell_export(vault, tmp_path, storage, part_bytes=64 * 1024, max_bytes=4 * 1024**3,
                                  urls=[f"{storage['base']}/cells/{CELL}/exports/archive.zip?partNumber=1"])
    assert status == 1 and report["error"] == "EXPORT_TOO_LARGE"
    assert storage["parts"] == {}


def test_a_part_url_without_tls_is_refused(vault, tmp_path, storage):
    """Defect: a misconfigured endpoint sends the owner's plaintext archive unencrypted."""
    status, report = _cell_export(vault, tmp_path, storage, part_bytes=64 * 1024,
                                  urls=[f"http://127.0.0.1:9/cells/{CELL}/exports/archive.zip?partNumber=1"])
    assert status == 1 and report["error"] == "EXPORT_FAILED"
    assert storage["parts"] == {}


def test_the_part_digest_matches_cellctls_vector():
    """Defect: the cell and cellctl disagree on the digest, so every export fails."""
    assert cloud_export.etag_digest(['"0cc175b9c0f1b6a831c399e269772661"',
                                     '"92eb5ffee6ae2fec3ad71c777531578f"']) == DIGEST_VECTOR


def test_a_job_failure_reports_its_cause_without_source_paths_or_urls(vault, tmp_path, monkeypatch):
    """Defect: operators lose a packing failure's cause or receive private exception text."""
    private_message = "private-record https://storage.example/part?signature=do-not-log"

    def denied(*_args, **_kwargs):
        raise PermissionError(errno.EACCES, private_message, str(vault / "private-record.md"))

    monkeypatch.setenv("EXOMEM_CLOUD_CELL", "1")
    monkeypatch.setenv("EXOMEM_CLOUD_CELL_ID", CELL)
    monkeypatch.setattr(cloud_export, "run_export", denied)
    log = tmp_path / "termination-log"
    status = cloud_export.main(["--export-id", str(uuid.uuid4()), "--part-url", "https://unused.example/part",
                                "--part-bytes", "65536", "--max-bytes", "1048576", "--vault", str(vault),
                                "--termination-log", str(log)])
    report = json.loads(log.read_text())
    assert status == 1 and report["error"] == "EXPORT_FAILED"
    assert report["diagnostic"] == {"type": "PermissionError", "function": "denied", "errno": errno.EACCES}
    assert "private-record" not in log.read_text() and "signature" not in log.read_text()
