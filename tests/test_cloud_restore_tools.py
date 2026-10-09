"""The Cloud restore tools' own logic: the vault digest the drill and the
in-place restore trust for their verdict, and the restore resources they render
with cellctl (infra/scripts/cloud_vault_digest.py and
cloud_restore_manifests.py, driven by infra/scripts/cloud_restore.sh).

The digest runs exactly as the tools run it: as a script, writing a hash list
to a file and comparing two lists."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIGEST = ROOT / "infra" / "scripts" / "cloud_vault_digest.py"
MANIFESTS = ROOT / "infra" / "scripts" / "cloud_restore_manifests.py"
CELL_ID = "abcdefghijklmnop"
SNAPSHOT = "0123456789abcdef" * 4
IMAGE = "ghcr.io/example/exomem@sha256:" + "a" * 64

posix_trees = pytest.mark.skipif(
    os.name == "nt", reason="the restore tools run on the Linux node; these trees use POSIX names and symlinks"
)


def _digest(*args: str | Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(DIGEST), *map(str, args)], capture_output=True, text=True, check=False
    )


def _hash(root: Path, out: Path) -> Path:
    result = _digest("hash", root)
    assert result.returncode == 0, result.stderr
    out.write_text(result.stdout, encoding="utf-8")
    return out


def _vault(root: Path) -> Path:
    (root / "Knowledge Base" / "Projects").mkdir(parents=True)
    (root / "Knowledge Base" / "Projects" / "plan.md").write_text("# Plan\n", encoding="utf-8")
    (root / "Knowledge Base" / "inbox.md").write_text("first\n", encoding="utf-8")
    (root / "attachments").mkdir()
    (root / "attachments" / "scan.pdf").write_bytes(bytes(range(256)) * 64)
    return root


@posix_trees
def test_a_restore_identical_to_its_reference_passes(tmp_path: Path) -> None:
    reference = _vault(tmp_path / "reference")
    # Names a line-based list would split or mangle.
    (reference / "Knowledge Base" / "two\nlines.md").write_text("x\n", encoding="utf-8")
    (reference / os.fsdecode(b"latin-\xe9.md")).write_text("y\n", encoding="utf-8")
    restored = tmp_path / "restored"
    shutil.copytree(reference, restored, symlinks=True)

    result = _digest(
        "compare", _hash(restored, tmp_path / "r.list"), _hash(reference, tmp_path / "f.list"), tmp_path
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == (
        "restored=5 reference=5 identical=5 differ=0 only_restored=0 only_reference=0"
    )


@posix_trees
@pytest.mark.parametrize(
    ("change", "listed_in"),
    [
        ("edit", "differ.txt"),
        ("remove", "only_reference.txt"),
        ("add", "only_restored.txt"),
        ("empty", None),
    ],
)
def test_a_restore_that_differs_from_its_reference_fails(tmp_path: Path, change: str, listed_in: str | None) -> None:
    reference = _vault(tmp_path / "reference")
    restored = tmp_path / "restored"
    shutil.copytree(reference, restored)
    inbox = restored / "Knowledge Base" / "inbox.md"
    if change == "edit":
        inbox.write_text("second\n", encoding="utf-8")
    elif change == "remove":
        inbox.unlink()
    elif change == "add":
        (restored / "Knowledge Base" / "stray.md").write_text("z\n", encoding="utf-8")
    else:
        # Two empty vaults agree on every file, and still prove nothing.
        shutil.rmtree(reference)
        shutil.rmtree(restored)
        reference.mkdir()
        restored.mkdir()
    out = tmp_path / "names"
    out.mkdir()

    result = _digest("compare", _hash(restored, tmp_path / "r.list"), _hash(reference, tmp_path / "f.list"), out)

    assert result.returncode == 3, result.stdout + result.stderr
    # The run log carries counts; the names stay in the memory-backed directory.
    assert ".md" not in result.stdout
    if listed_in:
        assert (out / listed_in).read_text(encoding="utf-8").count(".md") == 1


@posix_trees
def test_a_symlink_is_recorded_by_its_target_and_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.md").write_text("not in the vault\n", encoding="utf-8")
    reference = _vault(tmp_path / "reference")
    (reference / "copy.md").write_text("# Plan\n", encoding="utf-8")
    restored = _vault(tmp_path / "restored")
    (restored / "copy.md").symlink_to(restored / "Knowledge Base" / "Projects" / "plan.md")
    (restored / "elsewhere").symlink_to(outside, target_is_directory=True)

    restored_list = _hash(restored, tmp_path / "r.list")
    result = _digest("compare", restored_list, _hash(reference, tmp_path / "f.list"), tmp_path)

    assert result.returncode == 3
    assert "differ=1 only_restored=1" in result.stdout
    assert "secret.md" not in restored_list.read_text(encoding="utf-8")


@posix_trees
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root reads any directory")
def test_an_unreadable_directory_stops_the_hash(tmp_path: Path) -> None:
    vault = _vault(tmp_path / "vault")
    locked = vault / "Knowledge Base" / "Projects"
    locked.chmod(0)
    try:
        result = _digest("hash", vault)
    finally:
        locked.chmod(0o700)

    assert result.returncode == 1
    # Only the error class reaches the log, never the path.
    assert result.stderr.strip() == "hash failed: PermissionError"


def _manifests():
    spec = importlib.util.spec_from_file_location("cloud_restore_manifests", MANIFESTS)
    module = importlib.util.module_from_spec(spec)
    # Its dataclass resolves annotations through sys.modules.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_a_scratch_restore_never_creates_a_runtime_or_a_cell(tmp_path: Path) -> None:
    manifests = _manifests()
    scratch = f"exo-scratch-{CELL_ID}-0123abcd"

    documents = manifests.scratch_documents(
        cell_id=CELL_ID, scratch=scratch, snapshot=SNAPSHOT, image=IMAGE, storage_gib=10,
        credential={"backup-password": "p", "b2-key-id": "i", "b2-key-secret": "s"},
        settings=manifests.PlatformSettings("bucket", "https://s3.example.test", ("10.0.0.0/8",)),
        started_at="2026-10-09T10:00:00+00:00",
    )

    assert sorted(d["kind"] for d in documents) == [
        "Job", "Namespace", "NetworkPolicy", "NetworkPolicy", "PersistentVolumeClaim", "Secret",
    ]
    namespace = documents[0]
    assert namespace["metadata"]["name"] == scratch
    # cellctl reconciles every namespace labelled as a cell; the scratch one must never be.
    assert "exomem.io/cloud-cell" not in namespace["metadata"]["labels"]
    assert namespace["metadata"]["labels"]["exomem.io/scratch-of"] == CELL_ID
    assert all(d["metadata"]["namespace"] == scratch for d in documents[1:])
    secret = next(d for d in documents if d["kind"] == "Secret")
    assert "cell-token" not in secret["data"]


def test_the_in_place_restore_job_runs_where_the_cell_runs() -> None:
    manifests = _manifests()
    placement = {
        "nodeSelector": {"exomem.io/dedicated-cell": CELL_ID},
        "tolerations": [{"key": "exomem.io/dedicated-cell", "operator": "Equal", "value": CELL_ID,
                         "effect": "NoSchedule"}],
    }
    statefulset = {"spec": {"replicas": 0, "template": {"spec": {
        **placement,
        "containers": [{"name": "exomem", "image": IMAGE}],
    }}}}

    job = manifests.in_place_job(
        cell_id=CELL_ID, statefulset=statefulset, snapshot=SNAPSHOT,
        settings=manifests.PlatformSettings("bucket", "https://s3.example.test", ()),
        started_at="2026-10-09T10:00:00+00:00",
    )

    pod = job["spec"]["template"]["spec"]
    assert job["metadata"]["namespace"] == f"exo-cell-{CELL_ID}"
    assert pod["containers"][0]["image"] == IMAGE
    # A dedicated or shared-profile cell's Job without them waits Pending while the cell is down.
    assert pod["nodeSelector"] == placement["nodeSelector"]
    assert pod["tolerations"] == placement["tolerations"]
