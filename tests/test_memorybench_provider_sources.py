from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest


ROOT = Path("benchmarks/memorybench")
PROVIDERS = ROOT / "providers"
LOCKFILE = ROOT / "LOCKFILE.json"
PATCH = ROOT / "registration.patch"

MEMORYBENCH_PIN = "118209a746d97d0d85e5a7234267f0b6962857e9"
MEMORYBENCH_TREE = "2ee25bdbcb6bfaaecb32f917920c53775a299b37"
BASIC_MEMORY_PIN = "816accaa9befe8281668ba8819eaf74d11ce2385"
BASIC_MEMORY_TREE = "4f0255a31c609cad90dbf3b50e3d14a517e4566e"

REGISTRATION_PATHS = {
    "src/types/provider.ts",
    "src/providers/index.ts",
    "src/utils/config.ts",
}

EXPECTED_PROVIDER_FILES = {
    "providers/_guest_transport.ts",
    "providers/exomem/index.ts",
    "providers/basic-memory/index.ts",
    "providers/basic-memory/sidecar.py",
    "providers/tests/guest_transport.test.ts",
    "providers/tests/basic_memory.test.ts",
    "providers/tests/exomem.test.ts",
    "providers/basic-memory/test_sidecar.py",
}


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def _patch_paths(patch_text: str) -> set[str]:
    return {
        match.group(1)
        for match in re.finditer(r"^diff --git a/(\S+) b/(\S+)$", patch_text, re.MULTILINE)
        if match.group(1) == match.group(2)
    }


def test_guest_provider_source_set_is_complete_and_bounded() -> None:
    missing = sorted(path for path in EXPECTED_PROVIDER_FILES if not (ROOT / path).is_file())
    assert not missing, f"missing §4.4 provider sources/tests: {missing}"

    allowed = {ROOT / path for path in EXPECTED_PROVIDER_FILES}
    actual = {path for path in PROVIDERS.rglob("*") if path.is_file()}
    assert actual == allowed


def test_sidecar_imports_exact_upstream_basic_provider_and_renderer() -> None:
    source = _read("providers/basic-memory/sidecar.py")
    assert (
        "from basic_memory_benchmarks.providers.bm_local import BasicMemoryLocalProvider"
        in source
    )
    assert (
        "from basic_memory_benchmarks.converters.longmemeval_to_corpus import "
        "_render_session_doc" in source
    )
    assert "basic-memory-exomem-provider" not in source
    assert "exomem-provider" not in source


def test_sidecar_exposes_exactly_three_post_routes() -> None:
    source = _read("providers/basic-memory/sidecar.py")
    declared = set(re.findall(r'"(/v1/[a-z-]+)"', source))
    assert declared == {"/v1/ingest", "/v1/search", "/v1/cleanup"}
    assert not re.search(r"/(?:health|ready|readiness|debug|shutdown)\b", source)


def test_basic_source_pins_competitor_configuration_provenance() -> None:
    source = _read("providers/basic-memory/sidecar.py")
    for required in (
        "BasicMemoryLocalProvider",
        "_render_session_doc",
        "search_notes",
        '"page": 1',
        '"search_type": "hybrid"',
        '"output_format": "json"',
        "project info",
        "basic-memory.log",
        "--delete-notes",
        "--local",
    ):
        assert required in source


def test_transport_values_are_labelled_and_runtime_is_sequential() -> None:
    helper = _read("providers/_guest_transport.ts")
    exomem = _read("providers/exomem/index.ts")
    basic = _read("providers/basic-memory/index.ts")
    assert "exomem_authored_transport" in helper + exomem + basic
    assert "latency_publishable" in helper + exomem + basic
    assert "false" in helper + exomem + basic
    for source in (exomem, basic):
        assert re.search(
            r"concurrency\s*=\s*\{[^}]*default:\s*1[^}]*ingest:\s*1"
            r"[^}]*indexing:\s*1[^}]*search:\s*1",
            source,
            flags=re.DOTALL,
        )


def test_provider_results_are_flat_and_do_not_fabricate_scores() -> None:
    basic = _read("providers/basic-memory/index.ts")
    exomem = _read("providers/exomem/index.ts")
    assert 'content: hit.text ?? ""' in basic
    assert "score: hit.score ?? 0" in basic
    assert "metadata:" not in re.search(
        r"return\s+response\.hits\.map\([\s\S]+?\n\s*\}\)", basic
    ).group(0)
    assert "content: data.body" in exomem
    assert "score: 0.0" in exomem
    assert "fabricat" not in exomem.lower()


def test_registration_patch_is_canonical_and_touches_only_three_files() -> None:
    patch = PATCH.read_bytes()
    assert patch
    text = patch.decode("utf-8")
    assert _patch_paths(text) == REGISTRATION_PATHS
    assert "src/providers/supermemory" not in text
    assert "src/orchestrator/" not in text
    assert "package.json" not in text
    assert "bun.lock" not in text
    assert "src/exomem" not in text
    assert b"\r\n" not in patch

    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    registration = lock["registration_overlay"]
    assert registration["path_allowlist"] == sorted(REGISTRATION_PATHS)
    assert registration["patch_sha256"] == hashlib.sha256(patch).hexdigest()


def test_lockfile_pins_memorybench_basic_and_every_overlay_byte() -> None:
    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    assert lock["commit_sha"] == MEMORYBENCH_PIN
    assert lock["tree_sha"] == MEMORYBENCH_TREE
    assert lock["bun_version_pinned"] == "1.3.14"

    basic = lock["basic_memory"]
    assert basic["commit_sha"] == BASIC_MEMORY_PIN
    assert basic["tree_sha"] == BASIC_MEMORY_TREE
    assert basic["root_uv_lock_sha256"] == (
        "9c1de71b903584a42fe1c14765e564194ebc5b5810faad575cc18d4f14e006bc"
    )
    assert basic["benchmark_uv_lock_sha256"] == (
        "04607f61b39649be28c16f9ee742045ea30f7526a6675a86755bdf03d6461911"
    )
    assert basic["benchmark_pyproject_sha256"] == (
        "46d9109eb1f6d050b40c80ddec4629bcfcdeb185d530a277cf8496d157b8522f"
    )
    assert basic["provider_base_sha256"] == (
        "5cf7ef2663b31854d2d43d197f4b9df7efa25d660bd017d7099da01c1c753e86"
    )
    assert basic["provider_bm_local_sha256"] == (
        "bf0c191a2a1971e68031f038b7f9dfb788eeabf8d90024a35c0ba6491e2ac43a"
    )
    assert basic["longmemeval_renderer_sha256"] == (
        "5ab42168eb40bed049448321579986a9c494902fd7d8e67b2f3b4122fbf264a4"
    )
    assert basic["models_sha256"] == (
        "d384693d888f32a1406afbaf09975c0d5070899049c236b3ed8c2d537afeda5f"
    )

    additive = lock["provider_files_sha256"]
    assert set(additive) == {
        "src/providers/_guest_transport.ts",
        "src/providers/exomem/index.ts",
        "src/providers/basic-memory/index.ts",
        "src/providers/basic-memory/sidecar.py",
        "src/providers/__guest_tests__/guest_transport.test.ts",
        "src/providers/__guest_tests__/basic_memory.test.ts",
        "src/providers/__guest_tests__/exomem.test.ts",
        "src/providers/basic-memory/test_sidecar.py",
        "src/cli/commands/competitive-ingest.ts",
    }
    for destination, record in additive.items():
        source = ROOT / record["source"]
        assert source.is_file(), destination
        assert record["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_competitive_ingest_v2_parser_bytes_are_exactly_lockfile_bound() -> None:
    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    record = lock["provider_files_sha256"]["src/cli/commands/competitive-ingest.ts"]
    source = ROOT / record["source"]

    assert record["sha256"] == hashlib.sha256(source.read_bytes()).hexdigest()


def test_registration_preimages_are_the_reviewed_upstream_bytes() -> None:
    lock = json.loads(LOCKFILE.read_text(encoding="utf-8"))
    records = {row["path"]: row for row in lock["registration_overlay"]["files"]}
    assert records["src/types/provider.ts"]["preimage_sha256"] == (
        "5d3691fad4fbcddb2a269542f28ffcb5e2540cd9e8ae0e91f846c55760b1adc0"
    )
    assert records["src/providers/index.ts"]["preimage_sha256"] == (
        "e626aa581c50e178aa0ebdfa4781576bbeefd52dfb087981d192d692563e948a"
    )
    assert records["src/utils/config.ts"]["preimage_sha256"] == (
        "0eed39d1366083b0393816abefc90987ae6731c7d221d4f76ccaacc58dd3561c"
    )
    assert all(re.fullmatch(r"[0-9a-f]{64}", row["postimage_sha256"]) for row in records.values())


@pytest.mark.parametrize(
    ("source_path", "required"),
    [
        ("providers/_guest_transport.ts", ("30_000", "180_000", "70_000", "120_000")),
        ("providers/basic-memory/index.ts", ("receipt", "containerTag", "documentIds")),
        ("providers/exomem/index.ts", ("EXOMEM_VAULT_PATH", "EXOMEM_REST_API_KEY", "hybrid")),
    ],
)
def test_provider_sources_carry_fail_closed_contract_markers(
    source_path: str, required: tuple[str, ...]
) -> None:
    source = _read(source_path)
    for marker in required:
        assert marker in source


def test_sources_contain_no_operator_paths_or_founder_gate_bypass() -> None:
    windows_user_root = "C:" + "\\Users\\"
    for path in ROOT.rglob("*"):
        if path.is_file() and "__pycache__" not in path.parts:
            text = path.read_text(encoding="utf-8", errors="ignore")
            assert "/home/" not in text
            assert windows_user_root not in text

    ledger = Path("openspec/changes/add-competitive-benchmark-programme/tasks.md").read_text(
        encoding="utf-8"
    )
    assert "- [x] 0.7 ⛳ Founder ratified" in ledger
    assert "benchmarks/epistemic/contracts/ratification.v1.json" in ledger
    assert "- [x] 4.4 TS providers" in ledger


def test_guest_transport_exports_attach_only_cleanup_helpers() -> None:
    helper = _read("providers/_guest_transport.ts")
    for name in ("attachBasicMemoryService", "attachExomemService"):
        match = re.search(
            rf"export async function {name}\b(?P<body>[\s\S]+?)(?=\nexport (?:async )?function|\Z)",
            helper,
        )
        assert match, f"missing reviewed attach-only API {name}"
        body = match.group("body")
        assert "readSecureDescriptor" in body
        assert "DescriptorExpectation" in body or "descriptorExpectation" in body
        assert "ensureBasicMemoryService" not in body
        assert "ensureExomemService" not in body
        assert "spawn(" not in body and "spawnSync(" not in body
        assert "acquireLaunchLock" not in body


def test_cleanup_imports_concrete_providers_and_only_attach_transport_apis() -> None:
    cleanup = (ROOT / "cleanup.ts").read_text(encoding="utf-8")
    assert "BasicMemoryProvider" in cleanup
    assert "ExomemProvider" in cleanup
    assert "attachBasicMemoryService" in cleanup
    assert "attachExomemService" in cleanup
    assert re.search(r"\.clear\(\s*(?:target\.)?container_tag", cleanup)
    assert "ensureBasicMemoryService" not in cleanup
    assert "ensureExomemService" not in cleanup
    assert "spawn(" not in cleanup and "spawnSync(" not in cleanup


def test_memorybench_export_default_preflight_calls_the_reviewed_setup_verifier() -> None:
    export = (ROOT / "export.py").read_text(encoding="utf-8")
    assert re.search(r"(?:from\s+benchmarks\.memorybench\.setup\s+import|from\s+\.setup\s+import)[\s\S]*verify_checkout", export)
    assert re.search(r"verify_checkout\(", export)
    assert "materialized" in export
