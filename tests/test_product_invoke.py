"""The shared product-command invocation seam (`exomem.product_invoke`).

The CLI and the terminal UI both reach the unified registry through this seam;
these tests pin the semantics the two surfaces must share: Param coercion,
vault resolution including the pre-init scan allowances, `source_schema`
injection, the ambient surface+principal binding — owned by the seam itself so
a worker-thread invocation can never run unbound (an unbound principal fails
silently closed at the egress boundary) — and structured `OpError` failures.
"""

from __future__ import annotations

import concurrent.futures
from pathlib import Path

import pytest

from exomem import cli_ops, product_invoke, writer_lease


@pytest.fixture(autouse=True)
def _isolated_writer_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_WRITER_LEASE_STATE_DIR", str(tmp_path / "writer-lease-state"))
    writer_lease.reset_managers_for_tests()
    yield
    writer_lease.reset_managers_for_tests()


def test_read_from_worker_thread_is_owner_visible(vault: Path):
    # The binding lives INSIDE the seam: a bare worker thread (no ContextVars
    # inherited) must still see owner-level results, not egress-scrubbed
    # emptiness.
    def call():
        return product_invoke.invoke_product("browse_memory", {"mode": "list"})

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(call).result(timeout=60)
    assert isinstance(result, dict)
    entries = result.get("entries")
    assert entries, f"expected non-empty vault listing, got {result!r}"


def test_unknown_command_is_structured(vault: Path):
    with pytest.raises(cli_ops.OpError) as excinfo:
        product_invoke.invoke_product("definitely_not_a_command", {})
    assert excinfo.value.code == "UNKNOWN_OP"


def test_unknown_param_is_structured(vault: Path):
    with pytest.raises(cli_ops.OpError) as excinfo:
        product_invoke.invoke_product("ask_memory", {"bogus_param": 1})
    assert excinfo.value.code == "UNKNOWN_PARAM"


def test_process_media_operation_precheck(vault: Path):
    with pytest.raises(cli_ops.OpError) as excinfo:
        product_invoke.invoke_product("process_media", {"operation": "explode"})
    assert excinfo.value.code == "INVALID_MEDIA_OPERATION"


def test_needs_schema_command_gets_schema_injected(vault: Path):
    result = product_invoke.invoke_product(
        "capture_source",
        {
            "content": "a seam test thought about progressive disclosure",
            "title": "Seam Test Source",
            "source_type": "other",
        },
    )
    assert isinstance(result, dict)
    created = [
        p
        for p in (vault / "Knowledge Base").rglob("*.md")
        if "seam-test-source" in p.name.lower()
    ]
    assert created, "capture_source should have written a source file"


def test_explicit_uninitialized_root_allows_scan_only(tmp_path: Path):
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    (plain / "note.md").write_text("# hello\n", encoding="utf-8")
    before = sorted(p.relative_to(plain) for p in plain.rglob("*"))

    result = product_invoke.invoke_product(
        "adopt_vault", {"mode": "scan-only"}, vault_root=plain
    )
    assert isinstance(result, dict)

    after = sorted(p.relative_to(plain) for p in plain.rglob("*"))
    assert before == after, "scan-only must not create or modify anything"


def test_explicit_uninitialized_root_refuses_other_ops(tmp_path: Path):
    plain = tmp_path / "plain-folder"
    plain.mkdir()
    with pytest.raises(RuntimeError):
        product_invoke.invoke_product("ask_memory", {"query": "x"}, vault_root=plain)


def test_env_uninitialized_root_allows_browse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    plain = tmp_path / "plain-env-folder"
    plain.mkdir()
    monkeypatch.setenv("EXOMEM_VAULT_PATH", str(plain))
    result = product_invoke.invoke_product("browse_memory", {"mode": "list"})
    assert isinstance(result, dict)
    with pytest.raises(RuntimeError):
        product_invoke.invoke_product("ask_memory", {"query": "x"})


def test_coerce_accepts_native_types(vault: Path):
    result = product_invoke.invoke_product(
        "ask_memory", {"query": "progressive disclosure", "limit": 3, "graph": False}
    )
    assert isinstance(result, (list, dict))


def test_tier2_opt_out_hides_commands(vault: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("EXOMEM_DISABLE_TIER2", "1")
    with pytest.raises(cli_ops.OpError) as excinfo:
        product_invoke.invoke_product("govern_memory", {"operation": "status"})
    assert excinfo.value.code == "UNKNOWN_OP"
