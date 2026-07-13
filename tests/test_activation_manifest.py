from __future__ import annotations

import multiprocessing
import shutil
from pathlib import Path

import pytest
import yaml

from exomem import (
    activation,
    activation_manifest,
    adopt,
    audit,
    memory_refs,
    relation_registry,
    semantic_language_registry,
    vault,
)

_ID_A = "00000000-0000-0000-0000-000000000001"
_ID_B = "00000000-0000-0000-0000-000000000002"


def _write_page(
    root: Path,
    rel: str,
    *,
    page_type: str = "insight",
    status: str = "active",
    exomem_id: str | None = None,
    tags: list[str] | None = None,
) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [f"type: {page_type}", f"status: {status}"]
    if exomem_id is not None:
        fields.append(f"exomem_id: {exomem_id}")
    if tags:
        fields.append("tags: [" + ", ".join(tags) + "]")
    path.write_text(
        "---\n" + "\n".join(fields) + f"\n---\n\n# {path.stem}\n\nBody.\n",
        encoding="utf-8",
    )
    return path


def _markdown_snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*.md")
    }


def _ensure_worker(root: str, gate, output) -> None:  # noqa: ANN001
    gate.wait()
    manifest = activation_manifest.ensure_manifest(Path(root))
    output.put(manifest)


def test_nonempty_baseline_captures_only_active_writable_compiled_pages(
    tmp_path: Path,
) -> None:
    kb = tmp_path / "Knowledge Base"
    stable = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/stable.md",
        exomem_id=_ID_A,
    )
    legacy = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Patterns/legacy.md",
        page_type="pattern",
    )
    _write_page(tmp_path, "Knowledge Base/Sources/raw.md", page_type="source")
    _write_page(tmp_path, "Knowledge Base/Evidence/raw.md", page_type="evidence")
    _write_page(tmp_path, "Knowledge Base/Entities/person.md", page_type="entity")
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/archived.md",
        status="archived",
    )
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/hub.md",
        tags=["hub"],
    )
    _write_page(tmp_path, "Knowledge Base/Notes/Insights/day-snapshot.md")
    _write_page(tmp_path, "Knowledge Base/Notes/Insights/system-architecture.md")
    _write_page(tmp_path, "Knowledge Base/Notes/Insights/index.md")
    _write_page(tmp_path, "Knowledge Base/Notes/Insights/log.md")
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/malformed-id.md",
        exomem_id="not-a-uuid",
    )
    _write_page(tmp_path, "Knowledge Base/_Schema/schema-note.md")
    _write_page(tmp_path, "Knowledge Base/Reference/readonly.md")
    _write_page(tmp_path, "Knowledge Base/Private/excluded.md")
    _write_page(tmp_path, "Knowledge Base/Notes/Insights/note.sync-conflict-1.md")
    (kb / "_access.yaml").write_text(
        "readonly:\n  - Reference\nexcluded:\n  - Private\n", encoding="utf-8"
    )
    before_md = _markdown_snapshot(tmp_path)
    before_files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}

    manifest = activation_manifest.ensure_manifest(tmp_path)

    assert manifest.schema_version == 1
    assert manifest.contract_version == 1
    assert [page.path_at_activation for page in manifest.pages] == [
        "Knowledge Base/Notes/Insights/malformed-id.md",
        "Knowledge Base/Notes/Insights/stable.md",
        "Knowledge Base/Notes/Patterns/legacy.md",
    ]
    by_path = {page.path_at_activation: page for page in manifest.pages}
    assert by_path[stable.relative_to(tmp_path).as_posix()].identity_kind == "exomem_id"
    assert by_path[stable.relative_to(tmp_path).as_posix()].identity == _ID_A
    assert by_path[legacy.relative_to(tmp_path).as_posix()].identity_kind == "path_source_hash"
    assert by_path[legacy.relative_to(tmp_path).as_posix()].identity == legacy.relative_to(
        tmp_path
    ).as_posix()
    assert by_path[stable.relative_to(tmp_path).as_posix()].source_hash == vault.content_hash(
        stable.read_text(encoding="utf-8")
    )
    assert len(by_path[stable.relative_to(tmp_path).as_posix()].source_hash) == 64
    assert by_path["Knowledge Base/Notes/Insights/malformed-id.md"].identity_kind == (
        "path_source_hash"
    )
    assert _markdown_snapshot(tmp_path) == before_md
    after_files = {p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file()}
    assert after_files - before_files == {
        "Knowledge Base/_Schema/semantic-activation.yaml"
    }


def test_manifest_is_deterministic_create_once_and_does_not_append_later_pages(
    tmp_path: Path,
) -> None:
    first = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Patterns/z-last.md",
        page_type="pattern",
    )
    second = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/a-first.md",
        exomem_id=_ID_A,
    )

    initial = activation_manifest.ensure_manifest(tmp_path)
    path = activation_manifest.manifest_path(tmp_path)
    first_bytes = path.read_bytes()
    parsed = yaml.safe_load(first_bytes)
    assert list(parsed) == ["schema_version", "contract_version", "pages"]
    assert [item["path_at_activation"] for item in parsed["pages"]] == sorted(
        [first.relative_to(tmp_path).as_posix(), second.relative_to(tmp_path).as_posix()]
    )
    later = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Failures/later.md",
        page_type="failure",
        exomem_id=_ID_B,
    )

    repeated = activation_manifest.ensure_manifest(tmp_path)

    assert repeated == initial
    assert path.read_bytes() == first_bytes
    assert not activation_manifest.is_grandfathered(tmp_path, later, manifest=repeated)


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        ("schema_version: [\n", "ACTIVATION_MANIFEST_INVALID_YAML"),
        (
            "schema_version: 2\ncontract_version: 1\npages: []\n",
            "ACTIVATION_MANIFEST_UNSUPPORTED_SCHEMA",
        ),
        (
            "schema_version: 1\ncontract_version: 2\npages: []\n",
            "ACTIVATION_MANIFEST_UNSUPPORTED_CONTRACT",
        ),
        ("[]\n", "ACTIVATION_MANIFEST_INVALID"),
        (
            "schema_version: true\ncontract_version: 1\npages: []\n",
            "ACTIVATION_MANIFEST_INVALID",
        ),
    ],
)
def test_invalid_existing_manifest_fails_closed_without_replacement(
    tmp_path: Path, raw: str, code: str
) -> None:
    path = activation_manifest.manifest_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(raw, encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(activation_manifest.ActivationManifestError) as exc:
        activation_manifest.ensure_manifest(tmp_path)

    assert exc.value.code == code
    assert exc.value.reason
    assert path.read_bytes() == before


def test_stable_id_survives_move_but_legacy_membership_requires_original_path_and_hash(
    tmp_path: Path,
) -> None:
    stable = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/stable.md",
        exomem_id=_ID_A,
    )
    legacy = _write_page(tmp_path, "Knowledge Base/Notes/Insights/legacy.md")
    manifest = activation_manifest.ensure_manifest(tmp_path)
    assert activation_manifest.is_grandfathered(tmp_path, stable, manifest=manifest)
    assert activation_manifest.is_grandfathered(tmp_path, legacy, manifest=manifest)

    moved_stable = stable.with_name("moved-stable.md")
    moved_legacy = legacy.with_name("moved-legacy.md")
    stable.rename(moved_stable)
    legacy.rename(moved_legacy)
    moved_stable.write_text(
        moved_stable.read_text(encoding="utf-8") + "\nChanged after activation.\n",
        encoding="utf-8",
    )

    assert activation_manifest.is_grandfathered(
        tmp_path, moved_stable, manifest=manifest
    )
    assert not activation_manifest.is_grandfathered(
        tmp_path, moved_legacy, manifest=manifest
    )
    moved_legacy.rename(legacy)
    legacy.write_text(
        legacy.read_text(encoding="utf-8") + "\nChanged after activation.\n",
        encoding="utf-8",
    )
    assert not activation_manifest.is_grandfathered(tmp_path, legacy, manifest=manifest)


def test_duplicate_ids_fall_back_but_sync_conflicts_do_not_create_ambiguity(
    tmp_path: Path,
) -> None:
    canonical = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/canonical.md",
        exomem_id=_ID_A,
    )
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/canonical.sync-conflict-1.md",
        exomem_id=_ID_A,
    )
    duplicate_a = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/duplicate-a.md",
        exomem_id=_ID_B,
    )
    duplicate_b = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/duplicate-b.md",
        exomem_id=_ID_B,
    )

    manifest = activation_manifest.ensure_manifest(tmp_path)
    by_path = {page.path_at_activation: page for page in manifest.pages}

    assert by_path[canonical.relative_to(tmp_path).as_posix()].identity_kind == "exomem_id"
    assert by_path[duplicate_a.relative_to(tmp_path).as_posix()].identity_kind == "path_source_hash"
    assert by_path[duplicate_b.relative_to(tmp_path).as_posix()].identity_kind == "path_source_hash"
    assert not any("sync-conflict" in page.path_at_activation for page in manifest.pages)


def test_later_page_cannot_steal_a_grandfathered_stable_id(tmp_path: Path) -> None:
    original = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/original.md",
        exomem_id=_ID_A,
    )
    manifest = activation_manifest.ensure_manifest(tmp_path)
    assert activation_manifest.is_grandfathered(tmp_path, original, manifest=manifest)

    copied = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/later-copy.md",
        exomem_id=_ID_A,
    )

    assert not activation_manifest.is_grandfathered(tmp_path, copied, manifest=manifest)
    assert not activation_manifest.is_grandfathered(tmp_path, original, manifest=manifest)


def test_concurrent_first_activation_has_one_valid_immutable_winner(tmp_path: Path) -> None:
    _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/existing.md",
        exomem_id=_ID_A,
    )
    context = multiprocessing.get_context("spawn")
    gate = context.Event()
    output = context.Queue()
    processes = [
        context.Process(target=_ensure_worker, args=(str(tmp_path), gate, output))
        for _ in range(3)
    ]
    for process in processes:
        process.start()
    gate.set()
    results = [output.get(timeout=20) for _ in processes]
    for process in processes:
        process.join(timeout=20)
        assert process.exitcode == 0

    path = activation_manifest.manifest_path(tmp_path)
    winner_bytes = path.read_bytes()
    assert all(result == results[0] for result in results)
    assert activation_manifest.load_manifest(tmp_path) == results[0]
    assert activation_manifest.ensure_manifest(tmp_path) == results[0]
    assert path.read_bytes() == winner_bytes


def test_race_loser_returns_winner_installed_between_scan_and_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_page(tmp_path, "Knowledge Base/Notes/Insights/contender.md")
    winner = activation_manifest.ActivationManifest(1, 1, ())
    original_snapshot = activation_manifest._snapshot

    def snapshot_then_install(root: Path) -> activation_manifest.ActivationManifest:
        contender = original_snapshot(root)
        path = activation_manifest.manifest_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        vault.batch_atomic_write(
            [
                vault.PlannedWrite(
                    path=path,
                    content=activation_manifest._serialize(winner),
                )
            ],
            vault_root=root,
        )
        return contender

    monkeypatch.setattr(activation_manifest, "_snapshot", snapshot_then_install)

    assert activation_manifest.ensure_manifest(tmp_path) == winner
    assert activation_manifest.load_manifest(tmp_path) == winner


def test_manifest_and_classification_survive_transfer_without_rebuildable_sidecars(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source-vault"
    page = _write_page(
        source,
        "Knowledge Base/Notes/Insights/stable.md",
        exomem_id=_ID_A,
    )
    legacy = _write_page(
        source,
        "Knowledge Base/Notes/Insights/legacy.md",
    )
    manifest = activation_manifest.ensure_manifest(source)
    sidecar = source / "Knowledge Base/.refs.sqlite"
    sidecar.write_bytes(b"rebuildable")
    before = activation_manifest.manifest_path(source).read_bytes()

    copied = tmp_path / "copied-vault"
    shutil.copytree(source, copied, ignore=shutil.ignore_patterns("*.sqlite"))
    copied_page = copied / page.relative_to(source)
    copied_legacy = copied / legacy.relative_to(source)

    assert not (copied / "Knowledge Base/.refs.sqlite").exists()
    assert activation_manifest.load_manifest(copied) == manifest
    assert activation_manifest.is_grandfathered(copied, copied_page)
    assert activation_manifest.is_grandfathered(copied, copied_legacy)
    assert activation_manifest.ensure_manifest(copied) == manifest
    assert activation_manifest.manifest_path(copied).read_bytes() == before


def test_rebuildable_indexes_and_registry_creation_do_not_move_the_boundary(
    tmp_path: Path,
) -> None:
    page = _write_page(
        tmp_path,
        "Knowledge Base/Notes/Insights/stable.md",
        exomem_id=_ID_A,
    )
    manifest = activation_manifest.ensure_manifest(tmp_path)
    path = activation_manifest.manifest_path(tmp_path)
    before = path.read_bytes()

    refs = memory_refs.ReferenceIndex(tmp_path)
    refs.rebuild_all()
    refs.path.unlink()
    refs.rebuild_all()
    relation_registry.save_registry(tmp_path, relation_registry.empty_proposal())
    semantic_language_registry.save_registry(
        tmp_path, semantic_language_registry.empty_proposal()
    )

    assert activation_manifest.ensure_manifest(tmp_path) == manifest
    assert path.read_bytes() == before
    assert activation_manifest.is_grandfathered(tmp_path, page, manifest=manifest)


def test_read_only_operations_do_not_activate_a_vault(tmp_path: Path) -> None:
    _write_page(tmp_path, "Knowledge Base/Notes/Insights/existing.md")
    path = activation_manifest.manifest_path(tmp_path)

    activation.scan(tmp_path)
    audit.audit(tmp_path, categories=["index_drift"])
    adopt.adopt(tmp_path)

    assert not path.exists()
