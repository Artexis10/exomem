"""Recoverable prior content for corrected Records/Planning rows (close-memory-loop 3.15).

Each test pins one failure the hashes-only audit chain cannot catch: a lost
prior payload, a retry that invents history, a legacy gap presented as a
complete empty history, history reachable for a withheld row, an unbounded
read, and history lost through export and restore.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from test_episode_records_leaf import COLLECTION, READING, _collection, _container, _entries

from exomem import commands, record_history, registry_history, writer_lease
from exomem import hosted_portability as portability
from exomem.governance.principal import RequestPrincipal, owner_principal, request_scope
from exomem.vault import parse_frontmatter

EXTERNAL = "external"


@pytest.fixture
def owner():
    with request_scope(owner_principal(surface="mcp")):
        yield


def _append(vault: Path, item: dict | None = None) -> str:
    result = commands.op_record_memory(
        vault,
        action="append",
        collection=COLLECTION,
        item=dict(READING if item is None else item),
        expected_container_hash=_container(vault),
        why="Log the vat reading.",
    )
    return result["item_key"]


def _entry(vault: Path, key: str) -> Path:
    for path in _entries(vault):
        frontmatter, _body, _raw = parse_frontmatter(path.read_text(encoding="utf-8"))
        if frontmatter["record_id"] == key:
            return path
    raise AssertionError(key)


def _correct(vault: Path, key: str, temperature: int, why: str, *, version: str | None = None) -> dict:
    return commands.op_record_memory(
        vault,
        action="update",
        collection=COLLECTION,
        item_key=key,
        changes={"temperature_c": temperature},
        expected_container_hash=_container(vault),
        expected_item_version=version or hashlib.sha256(_entry(vault, key).read_bytes()).hexdigest(),
        why=why,
    )


def _history(vault: Path, key: str, **kwargs: object) -> dict:
    return commands.op_record_memory(
        vault, action="history", collection=COLLECTION, item_key=key, **kwargs
    )


def _temperatures(history: dict) -> list[int]:
    return [revision["prior"]["values"]["temperature_c"] for revision in history["revisions"]]


def test_two_corrections_recover_both_prior_payloads_and_reasons(vault: Path, owner) -> None:
    _collection(vault)
    key = _append(vault)
    path = _entry(vault, key)
    _correct(vault, key, 14, "The probe read the wrong vat.")
    _correct(vault, key, 15, "Recalibrated probe; fourteen was low.")

    history = _history(vault, key)

    assert _entry(vault, key) == path  # stable identity: same row, same file
    assert history["status"] == "complete"
    assert history["retained"] == history["returned"] == 2
    assert history["truncated"] is False and history["continuation"] is None
    newest, oldest = history["revisions"]
    assert newest["prior"]["values"] == {**READING, "temperature_c": 14}
    assert newest["why"] == "Recalibrated probe; fourteen was low."
    assert oldest["prior"]["values"] == READING
    assert oldest["why"] == "The probe read the wrong vat."
    assert {revision["binding"] for revision in history["revisions"]} == {None}
    assert "temperature_c: 15" in path.read_text(encoding="utf-8")


def test_a_retry_or_unchanged_update_keeps_no_history(vault: Path, owner) -> None:
    _collection(vault)
    key = _append(vault)
    stale_version = hashlib.sha256(_entry(vault, key).read_bytes()).hexdigest()
    _correct(vault, key, 14, "The probe read the wrong vat.")
    assert _history(vault, key)["retained"] == 1

    with pytest.raises(Exception, match="STALE|changed"):
        _correct(vault, key, 14, "The probe read the wrong vat.", version=stale_version)
    _correct(vault, key, 14, "Same reading confirmed.")

    history = _history(vault, key)
    assert history["retained"] == 1
    assert _temperatures(history) == [41]
    # The unchanged update is accounted for, not reported as lost history.
    assert history["status"] == "complete" and "unavailable" not in history


def test_a_row_corrected_before_retention_reports_unavailable_legacy(vault: Path, owner) -> None:
    _collection(vault)
    key = _append(vault)
    _correct(vault, key, 14, "The probe read the wrong vat.")
    # A vault corrected by a release without retention has no kept entry.
    shutil.rmtree(vault / record_history.history_dir(_collection_id(vault), key), ignore_errors=True)

    legacy = _history(vault, key)
    assert legacy["status"] == "unavailable_legacy"
    assert legacy["unavailable"] == 1
    assert legacy["revisions"] == []

    _correct(vault, key, 15, "Recalibrated probe.")
    partial = _history(vault, key)
    assert partial["status"] == "unavailable_legacy"
    assert partial["unavailable"] == 1
    assert _temperatures(partial) == [14]

    untouched = _append(vault, {**READING, "vat": "east"})
    assert _history(vault, untouched)["status"] == "complete"


def _collection_id(vault: Path) -> str:
    return commands.op_record_memory(vault, action="query", collection=COLLECTION)["collection_id"]


def _withhold(vault: Path, withheld: Path) -> None:
    root = vault / "Knowledge Base" / "_Governance"
    (root / "scopes").mkdir(parents=True, exist_ok=True)
    (root / "rules").mkdir(parents=True, exist_ok=True)
    relative = withheld.relative_to(vault / "Knowledge Base").as_posix()
    for name, scope_id, rule_id, paths, ceiling in (
        ("records", "01ARZ3NDEKTSV4RRFFQ69G5FAV", "01ARZ3NDEKTSV4RRFFQ69G5FB0",
         "Records/**", 6),
        ("withheld", "01ARZ3NDEKTSV4RRFFQ69G5FZZ", "01ARZ3NDEKTSV4RRFFQ69G5FZY",
         relative, 0),
    ):
        (root / "scopes" / f"{name}.yaml").write_text(
            f'governance_version: 1\nid: {scope_id}\nname: {name}\npaths: ["{paths}"]\n',
            encoding="utf-8",
        )
        (root / "rules" / f"{name}.yaml").write_text(
            f'governance_version: 1\nid: {rule_id}\nscope_ids: ["{scope_id}"]\n'
            f"audience: {EXTERNAL}\nceiling: {ceiling}\n",
            encoding="utf-8",
        )


def _refusal(call) -> tuple[str, str]:
    with pytest.raises(Exception) as raised:
        call()
    return type(raised.value).__name__, str(raised.value)


def test_history_is_withheld_exactly_when_its_row_is(vault: Path) -> None:
    with request_scope(owner_principal(surface="mcp")):
        _collection(vault)
        visible = _append(vault)
        hidden = _append(vault, {**READING, "vat": "south"})
        _correct(vault, visible, 14, "Visible correction.")
        _correct(vault, hidden, 14, "Private correction.")
        cid = _collection_id(vault)
        _withhold(vault, _entry(vault, hidden))
        assert _history(vault, hidden)["retained"] == 1
    kept = vault / record_history.history_dir(cid, hidden)
    (kept_file,) = kept.iterdir()
    kept_relative = kept_file.relative_to(vault).as_posix()

    with request_scope(RequestPrincipal(audience_id=EXTERNAL, surface="mcp")):
        released = _history(vault, visible)
        assert _temperatures(released) == [41]
        # Who corrected the row is the owner's business, not another audience's.
        assert [revision["actor"] for revision in released["revisions"]] == [{"surface": "mcp"}]
        missing = _refusal(lambda: _history(vault, "0" * 32))
        assert _refusal(lambda: _history(vault, hidden)) == missing
        # The public dispatcher every surface shares serves no kept file directly.
        absent = record_history.history_root() + "/absent.json"
        for name in ("read_memory", "query_dataset"):
            command = next(c for c in commands.PRODUCT_COMMANDS if c.name == name)
            assert _refusal(
                lambda command=command: writer_lease.invoke_command(command, vault, path=kept_relative)
            ) == _refusal(lambda command=command: writer_lease.invoke_command(command, vault, path=absent))


def test_history_hides_an_origin_whose_input_the_reader_can_no_longer_see(vault: Path) -> None:
    from test_episode_records_leaf import _origin_reading_body
    from test_episode_recovery import _write_source_rule

    from exomem import records

    _collection(vault)
    body = _origin_reading_body(vault)
    _write_source_rule(vault, ceiling=6)
    reader = RequestPrincipal(audience_id="client-a", surface="mcp")
    with request_scope(reader):
        key = _append(vault)
        records.update_record(
            vault, COLLECTION, item_key=key, changes={}, body=body,
            expected_container_hash=_container(vault),
            expected_item_version=hashlib.sha256(_entry(vault, key).read_bytes()).hexdigest(),
            why="Attach the reading's origin.",
        )
        records.update_record(
            vault, COLLECTION, item_key=key, changes={}, body="- [finding] Corrected. ^reading\n",
            expected_container_hash=_container(vault),
            expected_item_version=hashlib.sha256(_entry(vault, key).read_bytes()).hexdigest(),
            why="Drop the origin.",
        )
        assert "exomem-origin" in _history(vault, key)["revisions"][0]["prior"]["body"]
    _write_source_rule(vault, ceiling=0)

    with request_scope(reader):
        history = _history(vault, key)
    (newest, _oldest) = history["revisions"]
    assert "exomem-origin" not in newest["prior"]["body"]
    assert "The vat reading was retained." in newest["prior"]["body"]
    assert history["status"] == "complete"


# Two hundred guarded corrections dominate this case; the bound it pins is the read.
@pytest.mark.timeout(300)
def test_a_deep_history_reads_one_bounded_page_and_reports_truncation(
    vault: Path, owner, monkeypatch: pytest.MonkeyPatch
) -> None:
    _collection(vault)
    key = _append(vault)
    for revision in range(200):
        _correct(vault, key, revision, f"Correction {revision}.")
    opened: list[str] = []
    read_kept = registry_history.read_kept
    monkeypatch.setattr(
        registry_history,
        "read_kept",
        lambda root, relative, name: opened.append(name) or read_kept(root, relative, name),
    )

    first = _history(vault, key)
    assert len(opened) == first["returned"] == record_history.PAGE_DEFAULT
    assert first["retained"] == 200 and first["truncated"] is True
    assert first["status"] == "complete"
    assert _temperatures(first) == list(range(198, 178, -1))
    second = _history(vault, key, limit=record_history.PAGE_MAX, continuation=first["continuation"])
    assert _temperatures(second) == list(range(178, 128, -1))
    assert len(opened) == record_history.PAGE_DEFAULT + record_history.PAGE_MAX

    kept = vault / record_history.history_dir(_collection_id(vault), key)
    per_correction = sum(path.stat().st_size for path in kept.iterdir()) / 200
    assert per_correction < 2048  # one row's payload and attribution, not a file snapshot


def test_export_and_restore_carry_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from test_hosted_restore_candidate import _bootstrap, _request

    from exomem import init as init_module
    from exomem.hosted_restore import restore_candidate

    source = tmp_path / "source"
    init_module.init_vault(source)
    with request_scope(owner_principal(surface="mcp")):
        _collection(source)
        key = _append(source)
        _correct(source, key, 14, "The probe read the wrong vat.")
        _correct(source, key, 15, "Recalibrated probe.")
        before = _history(source, key)
    exported = portability.export_quiesced_vault(
        source,
        tmp_path / "artifacts",
        context=portability.PortabilityContext(
            cell_id="source-cell",
            vault_id="logical-vault",
            operation_id="export-operation",
            created_at="2026-10-05T10:00:00+00:00",
            operator_authorized=True,
            lifecycle_state="quiesced",
            routing_stopped=True,
            active_mutations=0,
            background_writers_stopped=True,
            reads_allowed=True,
        ),
    )

    restore_candidate(_request(tmp_path, exported), bootstrap_security=_bootstrap)

    with request_scope(owner_principal(surface="mcp")):
        after = _history(tmp_path / "target-vault", key)
    assert after["status"] == "complete"
    assert after["revisions"] == before["revisions"]
    assert _temperatures(after) == [14, 41]
