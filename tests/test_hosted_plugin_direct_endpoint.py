from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from exomem import hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECT_ENDPOINT = "https://exomem-direct.substratesystems.io/api/exomem/mcp/v1"
LEGACY_ENDPOINT = "https://substratesystems.io/api/exomem/mcp/v1"
FIXTURE_OPENAI_APP_ID = "plugin_asdk_app_releaseinput123"


def _direct_candidate_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(REPO_ROOT / "plugins" / "hosted", root / "plugins" / "hosted")
    shutil.copytree(
        REPO_ROOT / "tests" / "fixtures" / "hosted_v5_contributions",
        root / "tests" / "fixtures" / "hosted_v5_contributions",
    )
    source = root / "plugins/hosted/candidates/hosted-alpha-agent-v4-command-binding-v1"
    target = root / "plugins/hosted/candidates/hosted-alpha-agent-v4-direct-v1"
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target)
    definition_path = target / "definition.json"
    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    definition.update({"endpoint": DIRECT_ENDPOINT, "version": "0.4.2"})
    definition_path.write_text(json.dumps(definition), encoding="utf-8")
    return root


def test_direct_candidate_is_self_contained_and_binds_the_selected_resource(
    tmp_path: Path,
) -> None:
    root = _direct_candidate_root(tmp_path)

    files = hosted_plugins.candidate_files(
        root,
        candidate=hosted_plugins.DIRECT_CANDIDATE,
        platform="all",
        openai_app_id=FIXTURE_OPENAI_APP_ID,
    )
    compatibility = json.loads(files["compatibility.json"])
    claude_mcp = json.loads(files["claude/.mcp.json"])
    openai_mcp = json.loads(files["openai/.mcp.json"])
    claude_lock = json.loads(files["claude.lock.json"])
    openai_lock = json.loads(files["openai.lock.json"])

    assert hosted_plugins.DIRECT_CANDIDATE in hosted_plugins.SELF_CONTAINED_CANDIDATES
    assert hosted_plugins.DIRECT_CANDIDATE in hosted_plugins.RECORDS_CANDIDATES
    assert compatibility["features"] == ["agent-command-binding-v1"]
    assert compatibility["endpoint"] == DIRECT_ENDPOINT
    assert compatibility["oauth_discovery"]["resource"] == DIRECT_ENDPOINT
    assert compatibility["oauth_discovery"]["protected_resource_metadata"] == (
        "https://exomem-direct.substratesystems.io/"
        ".well-known/oauth-protected-resource/api/exomem/mcp/v1"
    )
    assert compatibility["oauth_discovery"]["issuer"] == "https://substratesystems.io/api/exomem/oauth"
    assert claude_mcp["mcpServers"]["exomem"]["url"] == DIRECT_ENDPOINT
    assert openai_mcp["mcp_servers"]["exomem"]["url"] == DIRECT_ENDPOINT
    assert claude_lock["endpoint"] == openai_lock["endpoint"] == DIRECT_ENDPOINT
    assert claude_lock["minimum_records_reader_version"] == 2
    assert claude_lock["selection_cases_sha256"] == openai_lock["selection_cases_sha256"]


def test_definition_file_defaults_remain_bound_to_the_legacy_resource(tmp_path: Path) -> None:
    root = _direct_candidate_root(tmp_path)
    path = root / "plugins/hosted/candidates/hosted-alpha-agent-v4-direct-v1/definition.json"

    with pytest.raises(ValueError, match="production endpoint"):
        hosted_plugins.load_definition_file(
            path, expected_profile=hosted_plugins.CANDIDATE_PROFILES[hosted_plugins.DIRECT_CANDIDATE]
        )


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://exomem-direct.substratesystems.io/api/exomem/mcp/v1",
        "https://exomem-direct.substratesystems.io:443/api/exomem/mcp/v1",
        "https://user@exomem-direct.substratesystems.io/api/exomem/mcp/v1",
        "https://exomem-direct.substratesystems.io/api/exomem/mcp/v1?x=1",
        "https://exomem-direct.substratesystems.io/api/exomem/mcp/v1?",
        "https://exomem-direct.substratesystems.io/api/exomem/mcp/v1#fragment",
        "https://exomem-direct.substratesystems.io/api/exomem/mcp/v1#",
        "https://exomem-direct.substratesystems.io/api/exomem/mcp/v1/",
        "https://127.0.0.1/api/exomem/mcp/v1",
        "https://exomem-direct.substratesystems.io%2f/api/exomem/mcp/v1",
        "https://EXOMEM-DIRECT.SUBSTRATESYSTEMS.IO/api/exomem/mcp/v1",
        "https://exomem-direct.substratesystems.io/api/exomem/mcp/%76%31",
        "https://exomem-direct.substratesystems.io/api/exomem/mcp/v1\n",
        "https://" + ".".join(["a" * 63] * 4) + ".test/api/exomem/mcp/v1",
    ],
)
def test_direct_candidate_refuses_noncanonical_resource_endpoints(tmp_path: Path, endpoint: str) -> None:
    root = _direct_candidate_root(tmp_path)
    path = root / "plugins/hosted/candidates/hosted-alpha-agent-v4-direct-v1/definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["endpoint"] = endpoint
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match="direct Hosted endpoint"):
        hosted_plugins.load_definition(root, candidate=hosted_plugins.DIRECT_CANDIDATE)


def test_openai_validation_refuses_a_self_consistent_package_for_another_resource(
    tmp_path: Path,
) -> None:
    root = _direct_candidate_root(tmp_path)
    files = hosted_plugins.candidate_files(
        root,
        candidate=hosted_plugins.DIRECT_CANDIDATE,
        platform="openai",
        openai_app_id=FIXTURE_OPENAI_APP_ID,
    )
    generated = tmp_path / "generated"
    for relative, contents in files.items():
        target = generated / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)

    with pytest.raises(ValueError, match="expected endpoint"):
        hosted_plugins.validate_openai_candidate(
            generated / "openai", expected_endpoint=LEGACY_ENDPOINT
        )


def test_direct_definition_drift_does_not_change_existing_candidate_bytes(tmp_path: Path) -> None:
    root = _direct_candidate_root(tmp_path)
    before = {
        candidate: hosted_plugins.candidate_files(
            root,
            candidate=candidate,
            platform="all",
            openai_app_id=FIXTURE_OPENAI_APP_ID,
        )
        for candidate in hosted_plugins.CANDIDATE_PROFILES
        if candidate != hosted_plugins.DIRECT_CANDIDATE
    }
    path = root / "plugins/hosted/candidates/hosted-alpha-agent-v4-direct-v1/definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["version"] = "0.4.3"
    path.write_text(json.dumps(definition), encoding="utf-8")

    after = {
        candidate: hosted_plugins.candidate_files(
            root,
            candidate=candidate,
            platform="all",
            openai_app_id=FIXTURE_OPENAI_APP_ID,
        )
        for candidate in hosted_plugins.CANDIDATE_PROFILES
        if candidate != hosted_plugins.DIRECT_CANDIDATE
    }

    assert after == before


def test_direct_definition_drift_invalidates_generated_artifacts(tmp_path: Path) -> None:
    root = _direct_candidate_root(tmp_path)
    hosted_plugins.render(
        root,
        candidate=hosted_plugins.DIRECT_CANDIDATE,
        platform="all",
        openai_app_id=FIXTURE_OPENAI_APP_ID,
    )
    path = root / "plugins/hosted/candidates/hosted-alpha-agent-v4-direct-v1/definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["version"] = "0.4.3"
    path.write_text(json.dumps(definition), encoding="utf-8")

    with pytest.raises(ValueError, match="generated artifacts are stale"):
        hosted_plugins.check(
            root,
            candidate=hosted_plugins.DIRECT_CANDIDATE,
            platform="all",
            openai_app_id=FIXTURE_OPENAI_APP_ID,
        )


def test_direct_candidate_accepts_a_canonical_selected_resource_and_moves_identity(
    tmp_path: Path,
) -> None:
    root = _direct_candidate_root(tmp_path)
    before = hosted_plugins.compatibility_manifest(root, candidate=hosted_plugins.DIRECT_CANDIDATE)
    path = root / "plugins/hosted/candidates/hosted-alpha-agent-v4-direct-v1/definition.json"
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["endpoint"] = "https://direct.example.test/api/exomem/mcp/v1"
    path.write_text(json.dumps(definition), encoding="utf-8")

    selected = hosted_plugins.load_definition(root, candidate=hosted_plugins.DIRECT_CANDIDATE)
    after = hosted_plugins.compatibility_manifest(root, candidate=hosted_plugins.DIRECT_CANDIDATE)

    assert selected.endpoint == "https://direct.example.test/api/exomem/mcp/v1"
    assert after["compatibility_sha256"] != before["compatibility_sha256"]
    assert after["oauth_discovery"]["protected_resource_metadata"] == (
        "https://direct.example.test/.well-known/oauth-protected-resource/api/exomem/mcp/v1"
    )
    with pytest.raises(ValueError, match="generated artifacts are stale"):
        hosted_plugins.check(root, candidate=hosted_plugins.DIRECT_CANDIDATE, platform="all")
