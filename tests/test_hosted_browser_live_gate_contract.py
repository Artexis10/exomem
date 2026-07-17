from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]
BROWSER_ROOT = ROOT / "tests" / "browser"
LIVE_SPEC = BROWSER_ROOT / "live-transfer.spec.mjs"


def test_live_transfer_browser_gate_has_an_explicit_opt_in_entrypoint() -> None:
    package = json.loads((BROWSER_ROOT / "package.json").read_text(encoding="utf-8"))

    assert package["scripts"]["test:hosted-live"] == (
        "playwright test live-transfer.spec.mjs --project=hosted-live-chromium"
    )
    assert LIVE_SPEC.is_file()


def test_live_transfer_browser_gate_cannot_substitute_mocked_routes() -> None:
    spec = LIVE_SPEC.read_text(encoding="utf-8")

    assert "EXOMEM_LIVE_ENABLED" in spec
    assert "EXOMEM_LIVE_STORAGE_STATE" in spec
    assert "EXOMEM_LIVE_DOWNLOAD_PATH" in spec
    assert "page.route(" not in spec
    assert "route.fulfill(" not in spec
    assert ".example.test" not in spec


def test_live_transfer_browser_gate_names_every_openspec_scenario() -> None:
    spec = LIVE_SPEC.read_text(encoding="utf-8")
    required_markers = {
        "canonical CORS preflight",
        "exact transfer headers and methods",
        "90 MiB upload streams through the real edge",
        "large download streams through the real edge",
        "aborted upload requires a fresh ticket",
        "successful ticket replay is rejected",
        "grant path and operation alteration is rejected",
        "hostile origin is denied",
    }

    assert all(marker in spec for marker in required_markers)
    assert "90 * 1024 * 1024" in spec


def test_live_transfer_owner_storage_state_stays_outside_the_repository() -> None:
    spec = LIVE_SPEC.read_text(encoding="utf-8")
    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    assert "*.storage-state.json" in ignore
    assert "lstatSync" in spec
    assert "realpathSync" in spec
    assert "REPO_ROOT" in spec
    assert "storage state must be outside the repository" in spec
    assert "0o077" in spec


def test_live_transfer_ticket_and_browser_probe_are_strict() -> None:
    spec = LIVE_SPEC.read_text(encoding="utf-8")

    assert "expectedOperation" in spec
    assert "transferUrl.username" in spec
    assert "transferUrl.search" in spec
    assert "transferUrl.hash" in spec
    assert "expectedTransferPath" in spec
    assert "safeHeaders" in spec
    assert spec.count("page.waitForResponse") >= 3
    assert "urlHasNoForbiddenComponents" in spec
    assert "expect(transferUrl.username)" not in spec
    assert 'response.headers.get("access-control-allow-origin")' not in spec
    assert 'response.headers.get("access-control-expose-headers")' not in spec
    assert "mutateDownloadPathClaim" in spec
    assert "isExactUploadProof" in spec
    assert "isExactGrantRejection" in spec
    assert "expect(result.body).toEqual" not in spec
    assert "LARGE_TRANSFER_RESPONSE_TIMEOUT_MS" in spec
    assert "MIN_LARGE_DOWNLOAD_BYTES" in spec
    assert "access-control-expose-headers" in spec
