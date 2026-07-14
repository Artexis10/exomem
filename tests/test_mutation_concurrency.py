from __future__ import annotations

import io
import os
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from exomem import commands, preserve, schema
from exomem.writer_lease import LeaseConfig, LeaseManager


def _add_command():
    return next(command for command in commands.COMMANDS if command.name == "add")


def test_twenty_concurrent_real_captures_leave_complete_vault_state(
    vault: Path, source_schema
) -> None:
    assert os.environ["EXOMEM_DISABLE_EMBEDDINGS"] == "1"
    manager = LeaseManager(LeaseConfig(state_dir=vault.parent / "mutation-state"))
    command = _add_command()
    start = threading.Barrier(20)

    def capture(number: int) -> dict:
        start.wait(timeout=5.0)
        slug = f"concurrent-capture-{number:02d}"
        return manager.invoke(
            command,
            (vault, source_schema),
            {
                "content": f"bounded concurrent payload {number}",
                "source_type": "other",
                "title": f"Concurrent Capture {number:02d}",
                "slug": slug,
            },
        )

    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(capture, range(20)))

    paths = [result["path"] for result in results]
    assert len(paths) == len(set(paths)) == 20
    sources_index = (vault / "Knowledge Base/Sources/index.md").read_text(encoding="utf-8")
    top_index = (vault / "Knowledge Base/index.md").read_text(encoding="utf-8")
    activity_log = (vault / "Knowledge Base/log.md").read_text(encoding="utf-8")
    for number in range(20):
        slug = f"concurrent-capture-{number:02d}"
        assert slug in sources_index
        assert slug in top_index
        assert slug in activity_log
    assert "|Other]] — miscellaneous captures (20)" in sources_index
    assert "- Sources: 24 " in top_index
    assert len(list((vault / "Knowledge Base/Sources/Other").glob("*.md"))) == 20

    residue = [
        path for path in vault.rglob("*") if path.is_file() and path.name.endswith((".tmp", ".bak"))
    ]
    assert residue == []


class _BarrierStream(io.BytesIO):
    def __init__(self, data: bytes, barrier: threading.Barrier):
        super().__init__(data)
        self._barrier = barrier
        self._first_read = True

    def read(self, size: int = -1) -> bytes:
        if self._first_read:
            self._first_read = False
            self._barrier.wait(timeout=2.0)
        return super().read(size)


def test_independent_vault_real_uploads_commit_concurrently(tmp_path: Path) -> None:
    fixture = Path(__file__).parent / "fixtures"
    vault_a = tmp_path / "vault-a"
    vault_b = tmp_path / "vault-b"
    shutil.copytree(fixture, vault_a)
    shutil.copytree(fixture, vault_b)
    manager = LeaseManager(LeaseConfig(state_dir=tmp_path / "mutation-state"))
    start = threading.Barrier(2)

    def upload(vault: Path, filename: str) -> str:
        with manager.mutation_guard(vault):
            result = preserve.preserve_stream(
                vault,
                scope="Concurrent",
                category="Uploads",
                filename=filename,
                stream=_BarrierStream(filename.encode(), start),
            )
        return result.path

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(upload, vault_a, "alpha.bin")
        second = pool.submit(upload, vault_b, "beta.bin")
        assert first.result(timeout=5.0).endswith("alpha.bin")
        assert second.result(timeout=5.0).endswith("beta.bin")

    assert (vault_a / "Knowledge Base/Evidence/Concurrent/Uploads/alpha.bin").exists()
    assert (vault_b / "Knowledge Base/Evidence/Concurrent/Uploads/beta.bin").exists()
    assert not (vault_a / "Knowledge Base/Evidence/Concurrent/Uploads/beta.bin").exists()
    assert not (vault_b / "Knowledge Base/Evidence/Concurrent/Uploads/alpha.bin").exists()
    assert schema.load_source_schema(vault_a)
    assert schema.load_source_schema(vault_b)
