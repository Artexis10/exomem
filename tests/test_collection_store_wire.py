"""File/store facade parity: hashes explicitly re-derived by §3 are normalized."""

import json

import pytest

from exomem.cli_ops import OpError
from exomem.collection_store import connection
from exomem.plan_memory import plan_memory
from exomem.record_memory import record_memory
from test_collection_store_writer import CID, KEY, manifest_path, manifest_text


@pytest.mark.parametrize("profile", ["records", "planning"])
def test_public_facade_store_create_append_update_and_codes(tmp_path, monkeypatch, profile):
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    from exomem.collection_store.preview import preview_store

    # Advisory carriers are outside the terminal collection receipt contract.
    monkeypatch.setattr("exomem.records._capture_sweep_carrier", lambda *args, **kwargs: None)
    monkeypatch.setattr("exomem.records._due_state_carrier", lambda *args, **kwargs: None)
    call = record_memory if profile == "records" else plan_memory
    file_root = tmp_path / "files"
    store_root = tmp_path / "store"
    for root in (file_root, store_root):
        (root / "Knowledge Base").mkdir(parents=True)
        (root / "Knowledge Base/log.md").write_text("# Log\n")
    # Items need the initial source directory in file mode; scaffold parity
    # also exercises that branch of the common creation arguments.
    with connection.open_writer(
        tmp_path / "collections.sqlite", lease_check=lambda: True
    ) as handle:
        args = dict(
            manifest_path=manifest_path(profile), manifest_text=manifest_text(profile), why="create"
        )
        file_receipt = call(file_root, action="create", **args)
        with preview_store(store_root, handle):
            store_receipt = call(store_root, action="create", **args)
        assert normalized(file_receipt) == normalized(store_receipt)
        action = "append" if profile == "records" else "add"
        key_field = "item_key" if profile == "records" else "plan_id"
        args = dict(
            collection=manifest_path(profile),
            item={"title": "One"},
            **{key_field: KEY},
            why="capture",
        )
        file_receipt = call(file_root, action=action, **args)
        with preview_store(store_root, handle):
            store_receipt = call(store_root, action=action, **args)
        assert normalized(file_receipt) == normalized(store_receipt)
        for root, receipt in ((file_root, file_receipt), (store_root, store_receipt)):
            args = dict(
                collection=manifest_path(profile),
                **{key_field: KEY},
                changes={"title": "Two"},
                why="correct",
                expected_container_hash=receipt["after_container_hash"],
                expected_item_version="0" * 64,
            )
            if root == store_root:
                with preview_store(root, handle), pytest.raises(OpError) as error:
                    call(root, action="update", **args)
                store_code = error.value.code
            else:
                with pytest.raises(OpError) as error:
                    call(root, action="update", **args)
                file_code = error.value.code
        assert file_code == store_code
        args["expected_item_version"] = store_receipt["after_item_hash"]
        with preview_store(store_root, handle):
            store_updated = call(store_root, action="update", **args)
        args["expected_container_hash"] = file_receipt["after_container_hash"]
        args["expected_item_version"] = file_receipt["after_item_hash"]
        file_updated = call(file_root, action="update", **args)
        assert normalized(file_updated) == normalized(store_updated)


def normalized(value):
    changed_hashes = {
        "before_item_hash",
        "after_item_hash",
        "before_container_hash",
        "after_container_hash",
        "hash",
    }
    identities = {"audit_correlation", "transition_id", "held_at", "committed_at"}
    if isinstance(value, dict):
        return {
            k: "<hash>"
            if k in changed_hashes and v is not None
            else "<identity>"
            if k in identities and v is not None
            else normalized(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def test_revision_hash_parity_obeys_the_ruled_normalization(tmp_path, monkeypatch):
    """No unruled normalization of lifecycle payload or manifest hashes."""
    monkeypatch.setenv("EXOMEM_COLLECTION_STORE_PREVIEW", "1")
    from exomem.collection_store.preview import preview_store
    from exomem.collection_store.writer import CollectionWriter
    from exomem import record_governance

    roots = [tmp_path / "files", tmp_path / "store"]
    for root in roots:
        (root / "Knowledge Base").mkdir(parents=True)
        (root / "Knowledge Base/log.md").write_text("# Log\n")
    path = manifest_path()
    text = manifest_text()
    proposed = text.replace("title: Work", "title: Revised")
    record_memory(roots[0], "create", manifest_path=path, manifest_text=text, why="create")
    file_inspect = record_governance.inspect_collection(roots[0], path)
    with connection.open_writer(
        tmp_path / "collections.sqlite", lease_check=lambda: True
    ) as handle:
        writer = CollectionWriter(roots[1], handle)
        store_created = writer.create_collection(path, text, why="create")
        # File mode's container includes the newly created source directory.
        file_revision = record_memory(
            roots[0],
            "revise",
            collection=path,
            manifest_text=proposed,
            expected_manifest_hash=file_inspect["lifecycle_guards"]["expected_manifest_hash"],
            expected_container_hash=file_inspect["lifecycle_guards"]["expected_container_hash"],
            why="revise",
        )
        with preview_store(roots[1], handle):
            store_revision = record_memory(
                roots[1],
                "revise",
                collection=path,
                manifest_text=proposed,
                expected_manifest_hash=writer._collection(CID)[1].manifest_version.hash,
                expected_container_hash=store_created["after_container_hash"],
                why="revise",
            )
        differences = {
            key: {"files": file_revision[key], "store": store_revision[key]}
            for key in file_revision
            if normalized(file_revision)[key] != normalized(store_revision)[key]
        }
        assert json.dumps(normalized(file_revision), sort_keys=True) == json.dumps(
            normalized(store_revision), sort_keys=True
        ), differences
