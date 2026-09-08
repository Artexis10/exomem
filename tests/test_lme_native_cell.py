from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from benchmarks.lme.adapter import lme_profile
from benchmarks.lme.native_cell import NativeCell, _profile_settings, copy_model_cache

ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path(sys.executable)


def _product_result(result: dict) -> dict:
    structured = result["structuredContent"]
    assert isinstance(structured, dict)
    return structured


@pytest.mark.parametrize("with_aliases", [False, True])
def test_snapshot_ignores_transient_root_filesystem_probes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, with_aliases: bool
) -> None:
    from benchmarks.lme import native_cell

    cell = NativeCell(tmp_path / "cell", python=PYTHON, product_root=ROOT)
    cell.vault.mkdir(parents=True)
    (cell.vault / "memory.md").write_text("durable memory")
    before = cell.snapshot()
    prefix = ".exomem-held-probe-" + "a" * 32
    for suffix in ("", "-renamed", "-link", "-replacement-source", "-replacement-target"):
        (cell.vault / (prefix + suffix)).write_text("probe")
    directory = cell.vault / (prefix + "-directory")
    directory.mkdir()
    if with_aliases:
        (cell.vault / (prefix + "-alias")).symlink_to(prefix)
        (cell.vault / (prefix + "-directory-alias")).symlink_to(directory)
    inventory = native_cell._regular_files_no_follow

    def inventory_then_cleanup(root: Path) -> list[Path]:
        paths = inventory(root)
        # The product can remove its capability probes after enumeration.
        for path in paths:
            if path.name.startswith(prefix):
                path.unlink()
        return paths

    monkeypatch.setattr(native_cell, "_regular_files_no_follow", inventory_then_cleanup)
    assert cell.snapshot() == before


@pytest.mark.parametrize("relative", [
    ".exomem-held-probe-invalid",
    ".exomem-held-probe-" + "a" * 32 + "-memory.md",
    "Knowledge Base/.exomem-held-probe-" + "a" * 32,
])
def test_snapshot_still_refuses_symlinks_outside_exact_root_probe_namespace(
    tmp_path: Path, relative: str
) -> None:
    cell = NativeCell(tmp_path / "cell", python=PYTHON, product_root=ROOT)
    target = tmp_path / "outside"
    target.write_text("not memory")
    link = cell.vault / relative
    link.parent.mkdir(parents=True)
    link.symlink_to(target)
    with pytest.raises(RuntimeError, match="symlink"):
        cell.snapshot()


def test_snapshot_still_fails_when_an_ordinary_memory_disappears(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from benchmarks.lme import native_cell

    cell = NativeCell(tmp_path / "cell", python=PYTHON, product_root=ROOT)
    cell.vault.mkdir(parents=True)
    memory = cell.vault / "memory.md"
    memory.write_text("must not silently disappear")
    read = native_cell._read_no_follow

    def remove_then_read(path: Path) -> bytes:
        path.unlink()
        return read(path)

    monkeypatch.setattr(native_cell, "_read_no_follow", remove_then_read)
    with pytest.raises(FileNotFoundError):
        cell.snapshot()


def test_native_cell_uses_real_public_mcp_and_keeps_the_vault(tmp_path: Path) -> None:
    cell_root = tmp_path / "cell"

    async def exercise() -> tuple[int, Path]:
        async with NativeCell(
            cell_root,
            python=PYTHON,
            product_root=ROOT,
            profile="fixture",
        ) as cell:
            assert {"bootstrap", "capture_source", "read_memory"} <= cell.schemas.keys()
            assert cell.schemas["capture_source"]["description"]
            assert cell.schemas["capture_source"]["inputSchema"]["type"] == "object"
            assert cell.bootstrap["engagement"]["level"] == "maximal"
            assert cell.runtime_receipt["vault_root"] == str(cell.vault)
            assert cell.runtime_receipt["working_directory"] == str(cell_root / "runtime")
            assert cell.runtime_receipt["attestation_stage"] == "after_build"
            assert cell.runtime_receipt["prebuild_bindings"]["vault_root"] == str(cell.vault)
            empty_snapshot = cell.snapshot()
            assert empty_snapshot["raw_count"] == 0
            assert empty_snapshot["compiled_count"] == 0

            captured = await cell.call(
                "capture_source",
                {
                    "title": "Native cell fixture",
                    "content": "The native-cell test sentinel is sapphire.",
                    "source_type": "conversation",
                },
            )
            assert captured["isError"] is False
            source_path = _product_result(captured)["path"]
            read = await cell.call("read_memory", {"path": source_path})
            assert read["isError"] is False
            assert "sapphire" in _product_result(read)["body"]

            snapshot = cell.snapshot()
            assert snapshot["stored_bytes"] > 0
            assert source_path in snapshot["files"]
            assert snapshot["raw_count"] == 1
            readiness = await cell.readiness()
            assert readiness["evidence"]["serving_corpus"]["tracked_path_count"] == 1
            return cell.runtime_receipt["child_pid"], cell.vault

    child_pid, vault = asyncio.run(exercise())
    assert vault.is_dir()
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


def test_native_cell_scrubs_caller_environment_and_denies_external_ingest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    hostile_vault = outside / "personal-vault"
    (outside / ".env").write_text(
        f"EXOMEM_VAULT_PATH={hostile_vault}\nEXOMEM_CONFIG_PATH={outside / 'config.json'}\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(outside)
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(hostile_vault))
    monkeypatch.setenv("NATIVE_CELL_SECRET_SENTINEL", "must-not-enter-child")
    monkeypatch.setenv("HF_TOKEN", "must-not-enter-child")
    cell_root = tmp_path / "isolated"

    async def exercise() -> None:
        async with NativeCell(
            cell_root, python=PYTHON, product_root=ROOT, profile="fixture"
        ) as cell:
            assert "NATIVE_CELL_SECRET_SENTINEL" not in cell.runtime_receipt["environment_keys"]
            assert "HF_TOKEN" not in cell.runtime_receipt["environment_keys"]
            assert "PYTHONDONTWRITEBYTECODE" in cell.runtime_receipt["environment_keys"]
            assert cell.runtime_receipt["vault_root"] == str(cell.vault)
            assert cell.runtime_receipt["torchinductor_cache_root"] == str(
                cell_root / "cache" / "torchinductor"
            )
            assert cell.runtime_receipt["torch_home"] == str(cell_root / "cache" / "torch")
            assert cell.runtime_receipt["joblib_temp_root"] == str(cell_root / "cache" / "joblib")
            assert cell.runtime_receipt["kmp_duplicate_lib_ok"] == "True"
            assert cell.runtime_receipt["kmp_init_at_fork"] == "FALSE"
            assert not hostile_vault.exists()
            readiness = await cell.readiness()
            assert readiness["semantic_requested"] is False
            assert readiness["semantic_verified"] is False
            assert readiness["semantic_retrieval_verified"] is None
            assert readiness["fallback_detected"] is True
            assert readiness["status"] == "ready_lexical_only"
            assert readiness["actualserverreceipt"] == cell.runtime_receipt
            with pytest.raises(PermissionError):
                await cell.call(
                    "capture_source",
                    {"title": "denied", "url": "https://example.invalid/private"},
                )
            with pytest.raises(PermissionError):
                await cell.call("preserve_artifacts", {})

    asyncio.run(exercise())


def test_native_cell_preserves_result_lists_and_reads_their_diagnostics(tmp_path: Path) -> None:
    cell = NativeCell(
        tmp_path / "cell",
        python=PYTHON,
        product_root=ROOT,
        profile="semantic",
        model_cache=tmp_path / "declared-model",
        clip_model_cache=tmp_path / "declared-clip-model",
    )

    class ListResultClient:
        calls = 0

        async def call_tool_mcp(self, name: str, arguments: dict) -> SimpleNamespace:
            assert name == "ask_memory"
            assert arguments == {"query": "sentinel"}
            self.calls += 1
            effective_mode = "hybrid" if self.calls == 1 else "vector_lexical_fallback"
            return SimpleNamespace(
                content=[],
                structuredContent={
                    "result": [{"path": "Notes/hit.md", "effective_mode": effective_mode}]
                },
                isError=False,
            )

    cell._client = ListResultClient()

    async def exercise() -> None:
        result = await cell.call("ask_memory", {"query": "sentinel"})
        assert result["structuredContent"]["result"] == [
            {"path": "Notes/hit.md", "effective_mode": "hybrid"}
        ]
        assert cell._fallback_detected is False
        assert cell._semantic_retrieval_verified is True
        await cell.call("ask_memory", {"query": "sentinel"})
        assert cell._fallback_detected is True
        assert cell._semantic_retrieval_verified is True
        json.dumps(result, allow_nan=False)

    asyncio.run(exercise())


def test_native_cells_with_conflicting_paths_do_not_share_state(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"

    async def exercise() -> None:
        async with (
            NativeCell(first_root, python=PYTHON, product_root=ROOT) as first,
            NativeCell(second_root, python=PYTHON, product_root=ROOT) as second,
        ):
            first_write = await first.call(
                "capture_source",
                {"title": "Same title", "content": "only-first", "source_type": "conversation"},
            )
            second_write = await second.call(
                "capture_source",
                {"title": "Same title", "content": "only-second", "source_type": "conversation"},
            )
            first_path = _product_result(first_write)["path"]
            second_path = _product_result(second_write)["path"]
            assert first_path == second_path
            first_read = _product_result(await first.call("read_memory", {"path": first_path}))
            second_read = _product_result(await second.call("read_memory", {"path": second_path}))
            assert "only-first" in first_read["body"]
            assert "only-second" not in first_read["body"]
            assert "only-second" in second_read["body"]
            assert first.runtime_receipt["index_root"] != second.runtime_receipt["index_root"]
            assert first.runtime_receipt["lease_root"] != second.runtime_receipt["lease_root"]

    asyncio.run(exercise())


def test_native_cell_cancellation_reaps_only_its_child(tmp_path: Path) -> None:
    cell_root = tmp_path / "cancelled"
    started = asyncio.Event()
    child_pid: list[int] = []

    async def hold() -> None:
        async with NativeCell(cell_root, python=PYTHON, product_root=ROOT) as cell:
            child_pid.append(cell.runtime_receipt["child_pid"])
            started.set()
            await asyncio.Event().wait()

    async def exercise() -> None:
        task = asyncio.create_task(hold())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid[0], 0)
    assert (cell_root / "vault" / "Knowledge Base" / "_Schema" / "SKILL.md").is_file()


def test_native_cell_refuses_existing_and_symlink_roots(tmp_path: Path) -> None:
    existing = tmp_path / "existing"
    existing.mkdir()
    nonempty = tmp_path / "nonempty"
    nonempty.mkdir()
    (nonempty / "marker").write_text("owned elsewhere", encoding="utf-8")
    target = tmp_path / "target"
    target.mkdir()
    symlink = tmp_path / "symlink"
    symlink.symlink_to(target, target_is_directory=True)

    async def enter(root: Path) -> None:
        async with NativeCell(root, python=PYTHON, product_root=ROOT):
            raise AssertionError("existing root was accepted")

    for root in (existing, nonempty, symlink):
        with pytest.raises(ValueError, match="must be a new path"):
            asyncio.run(enter(root))
    assert (nonempty / "marker").read_text(encoding="utf-8") == "owned elsewhere"


def test_native_cell_copies_only_the_selected_embedding_snapshot(tmp_path: Path) -> None:
    model_cache = tmp_path / "frozen-model"
    revision = "a" * 40
    blobs = model_cache / "blobs"
    snapshot = model_cache / "snapshots" / revision
    (model_cache / "refs").mkdir(parents=True)
    blobs.mkdir()
    snapshot.mkdir(parents=True)
    (model_cache / "refs" / "main").write_text(revision, encoding="utf-8")
    (blobs / "config-blob").write_text("{}", encoding="utf-8")
    (blobs / "weights-blob").write_bytes(b"safetensors")
    (snapshot / "config.json").symlink_to(Path("../../blobs/config-blob"))
    (snapshot / "model.safetensors").symlink_to(Path("../../blobs/weights-blob"))
    unrelated = model_cache / "snapshots" / ("b" * 40)
    unrelated.mkdir(parents=True)
    (unrelated / "private-token").write_text("not selected", encoding="utf-8")
    frozen_cache = tmp_path / "frozen-copy"
    copy_model_cache(model_cache, frozen_cache)
    assert not (frozen_cache / "snapshots" / ("b" * 40)).exists()

    async def exercise() -> None:
        async with NativeCell(
            tmp_path / "cell",
            python=PYTHON,
            product_root=ROOT,
            model_cache=frozen_cache,
        ) as cell:
            copied = cell.root / "cache" / "huggingface" / "hub" / "models--BAAI--bge-base-en-v1.5"
            assert (copied / "refs" / "main").read_text(encoding="utf-8") == revision
            assert (copied / "snapshots" / revision / "config.json").is_file()
            assert not (copied / "snapshots" / revision / "config.json").is_symlink()
            assert not (copied / "snapshots" / ("b" * 40)).exists()

    asyncio.run(exercise())

    clip_cache = tmp_path / "clip-model"
    clip_revision = "c" * 40
    clip_snapshot = clip_cache / "snapshots" / clip_revision / "0_CLIPModel"
    (clip_cache / "refs").mkdir(parents=True)
    (clip_cache / "blobs").mkdir()
    clip_snapshot.mkdir(parents=True)
    (clip_cache / "refs" / "main").write_text(clip_revision, encoding="ascii")
    (clip_snapshot / "config.json").write_text("{}", encoding="utf-8")
    (clip_snapshot / "model.safetensors").write_bytes(b"clip")
    frozen_clip = tmp_path / "frozen-clip"
    copy_model_cache(clip_cache, frozen_clip)
    assert (frozen_clip / "snapshots" / clip_revision / "0_CLIPModel" / "config.json").is_file()


def test_native_cell_refuses_semantic_without_model_or_escaped_blob(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="model_cache"):
        NativeCell(tmp_path / "semantic", python=PYTHON, product_root=ROOT, profile="semantic")
    with pytest.raises(ValueError, match="clip_model_cache"):
        NativeCell(
            tmp_path / "semantic-with-bge",
            python=PYTHON,
            product_root=ROOT,
            profile="semantic",
            model_cache=tmp_path / "declared-model",
        )
    declared = NativeCell(
        tmp_path / "declared",
        python=PYTHON,
        product_root=ROOT,
        profile="semantic",
        model_cache=tmp_path / "declared-model",
        clip_model_cache=tmp_path / "declared-clip-model",
    )
    assert _profile_settings("semantic") == lme_profile().settings
    assert "EXOMEM_DISABLE_CLIP" not in declared._environment()

    model_cache = tmp_path / "bad-model"
    revision = "a" * 40
    snapshot = model_cache / "snapshots" / revision
    (model_cache / "refs").mkdir(parents=True)
    (model_cache / "blobs").mkdir()
    snapshot.mkdir(parents=True)
    (model_cache / "refs" / "main").write_text(revision, encoding="utf-8")
    outside = tmp_path / "outside-config"
    outside.write_text("{}", encoding="utf-8")
    (snapshot / "config.json").symlink_to(outside)
    (snapshot / "model.safetensors").write_bytes(b"safetensors")

    async def enter() -> None:
        async with NativeCell(
            tmp_path / "escaped",
            python=PYTHON,
            product_root=ROOT,
            model_cache=model_cache,
        ):
            raise AssertionError("escaped model symlink was accepted")

    with pytest.raises(ValueError, match="model blob"):
        asyncio.run(enter())

    escaped_cache = tmp_path / "escaped-blobs-model"
    escaped_snapshot = escaped_cache / "snapshots" / revision
    (escaped_cache / "refs").mkdir(parents=True)
    escaped_snapshot.mkdir(parents=True)
    (escaped_cache / "refs" / "main").write_text(revision, encoding="ascii")
    outside_blobs = tmp_path / "outside-blobs"
    outside_blobs.mkdir()
    (outside_blobs / "config").write_text("{}", encoding="utf-8")
    (outside_blobs / "weights").write_bytes(b"weights")
    (escaped_cache / "blobs").symlink_to(outside_blobs, target_is_directory=True)
    (escaped_snapshot / "config.json").symlink_to(Path("../../blobs/config"))
    (escaped_snapshot / "model.safetensors").symlink_to(Path("../../blobs/weights"))
    with pytest.raises(ValueError, match="blob roots"):
        copy_model_cache(escaped_cache, tmp_path / "escaped-blobs-copy")
