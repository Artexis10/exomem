"""The one v5 candidate: owned skills, one combined fixture, bound everywhere.

v5 carries the durable-baseline, generated-artifact adoption, recurring-entity
lifecycle and governed-curation doctrine. Two properties make it a release
rather than a directory of files:

* it owns every skill it ships, including the core skill, so the doctrine could
  be written without moving a byte v1-v4 resolve; and
* one canonicalised snapshot of the four generic contribution inputs is frozen
  in its candidate tree, and that file's digest is bound into compatibility,
  both package locks, both archive locks and promotion evidence -- so drift in
  the fixture, in an input, or in an owned skill invalidates verification
  instead of quietly shipping evidence that describes a different candidate.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from test_hosted_plugin_promotion import _sign_evidence, digest, signed_evidence

from exomem import commands, hosted_plugins

REPO_ROOT = Path(__file__).resolve().parents[1]
V5 = hosted_plugins.BASELINE_CANDIDATE
GENERATED = REPO_ROOT / "plugins/hosted/generated/candidates" / V5
CANDIDATE = REPO_ROOT / "plugins/hosted/candidates" / V5


def copy_release_tree(destination: Path) -> Path:
    """Copy everything v5 verification reads: the plugin tree and the inputs."""
    shutil.copytree(
        REPO_ROOT / "plugins" / "hosted",
        destination / "plugins" / "hosted",
        ignore=shutil.ignore_patterns("tmp*", ".exomem-hosted-render-*", "*.promotion.lock"),
    )
    shutil.copytree(
        REPO_ROOT / hosted_plugins.CONTRIBUTION_ROOT,
        destination / hosted_plugins.CONTRIBUTION_ROOT,
    )
    return destination


def _seed_pending_promotion(root: Path, platform: str) -> None:
    path = hosted_plugins.promotion_record(root, platform, candidate=V5)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        hosted_plugins._canonical_json(
            {
                "candidate": V5,
                "minimum_records_reader_version": 2,
                "platform": platform,
                "schema_version": 1,
                "state": "pending",
            }
        )
        + b"\n"
    )


def v5_expectation(root: Path) -> dict[str, object]:
    cases = json.loads(
        (root / "plugins/hosted/candidates" / V5 / "selection-cases.json").read_text(
            encoding="utf-8"
        )
    )
    return {
        "deployment_sha256": digest("deployed-v5"),
        "vault_purpose": "records-live-acceptance",
        "reset_epoch": "reset-v5",
        "principal_hmac_sha256": digest("principal-v5"),
        "audience_hmac_sha256": digest("audience-v5"),
        "client_contracts": {
            client: [
                contract["client"],
                contract["client_version"],
                contract["model_version"],
                contract["system_contract_version"],
            ]
            for client, contract in cases["client_contracts"].items()
        },
        "graph_proof_digest": digest("graph-v5"),
        "prompt_cases": {case["id"]: case["prompt_sha256"] for case in cases["cases"]},
        "selection_cases_sha256": hosted_plugins._sha256(hosted_plugins._canonical_json(cases)),
    }


def v5_signed_evidence(
    root: Path, expectation: dict[str, object], *, platform: str = "claude"
) -> dict[str, object]:
    evidence = signed_evidence(root, platform=platform)
    compatibility = hosted_plugins.compatibility_manifest(root, candidate=V5)
    definition = hosted_plugins.load_definition(root, candidate=V5)
    generated = root / "plugins/hosted/generated/candidates" / V5
    lock = json.loads((generated / f"{platform}.lock.json").read_text(encoding="utf-8"))
    archive_lock = json.loads(
        (generated / f"{platform}.zip.lock.json").read_text(encoding="utf-8")
    )
    cases = json.loads(
        (root / "plugins/hosted/candidates" / V5 / "selection-cases.json").read_text(
            encoding="utf-8"
        )
    )
    fixture = json.loads(
        (root / "plugins/hosted/candidates" / V5 / hosted_plugins.COMBINED_FIXTURE_NAME).read_text(
            encoding="utf-8"
        )
    )
    now = datetime.now(UTC)
    actions = [
        {"action": action, "outcome": outcome}
        for action, outcome in (
            ("describe", "completed"),
            ("validate", "completed"),
            ("create", "committed"),
            ("inspect", "completed"),
            ("query", "completed"),
            ("append", "committed"),
            ("update", "committed"),
            ("revise", "committed"),
            ("rebaseline", "committed"),
        )
    ]
    evidence.update(
        {
            "plugin_version": definition.version,
            "profile": definition.profile,
            "compatibility_sha256": compatibility["compatibility_sha256"],
            "schema_contract_sha256": compatibility["schema_contract_sha256"],
            "command_surface_sha256": compatibility["command_surface_sha256"],
            "package_artifact_sha256": lock["artifact_sha256"],
            "archive_sha256": archive_lock["archive_sha256"],
            "behavior_fixture_sha256": compatibility["behavior_fixture_sha256"],
            "behavior_traces": {
                family: {case: True for case in declared["case_ids"]}
                for family, declared in fixture["families"].items()
            },
            "records_acceptance": {
                "schema_version": 1,
                "deployment": {"sha256": expectation["deployment_sha256"]},
                "release": {
                    "package": "exomem",
                    "version": definition.version,
                    "profile": V5,
                    "minimum_records_reader_version": 2,
                },
                "surface": {"mcp_digest": compatibility["schema_contract_sha256"]},
                "run": {
                    "nonce": "records-v5-run-20260902",
                    "timestamp": now.isoformat(timespec="seconds").replace("+00:00", "Z"),
                    "expires_at": (now + timedelta(hours=1))
                    .isoformat(timespec="seconds")
                    .replace("+00:00", "Z"),
                },
                "vault": {
                    "purpose": expectation["vault_purpose"],
                    "reset_epoch": expectation["reset_epoch"],
                },
                "identity": {
                    "principal_hmac_sha256": expectation["principal_hmac_sha256"],
                    "audience_hmac_sha256": expectation["audience_hmac_sha256"],
                },
                "client_contracts": {
                    client: dict(contract)
                    for client, contract in cases["client_contracts"].items()
                },
                "actions": actions,
                "mutations": [
                    {
                        "action": action,
                        "request_id": f"request-{action}",
                        "receipt_id": f"receipt-{action}",
                        "terminal_outcome": "committed",
                        "before_readback_sha256": digest(f"before-{action}"),
                        "after_readback_sha256": digest(f"after-{action}"),
                    }
                    for action in ("create", "append", "update", "revise", "rebaseline")
                ],
                "restart": {"outcome": "completed", "readback_sha256": digest("restart-v5")},
                "prompt_cases": [
                    {
                        "id": case["id"],
                        "sha256": case["prompt_sha256"],
                        "client": case["client"],
                        "action": "append" if case["expected"] == "append" else "proposal",
                        "outcome": "committed" if case["expected"] == "append" else "completed",
                        "mutation": case["expected"] == "append",
                    }
                    for case in cases["cases"]
                ],
                "graph_availability": {"proof_digest": expectation["graph_proof_digest"]},
            },
        }
    )
    _sign_evidence(evidence)
    return evidence


# --------------------------------------------------------------------------
# Owned skills
# --------------------------------------------------------------------------


def test_v5_owns_every_skill_it_ships_including_the_core_skill() -> None:
    paths = hosted_plugins._skill_paths(REPO_ROOT, V5)
    names = tuple(path.parent.name for path in paths)

    assert set(names) == set(hosted_plugins.SKILL_NAMES) | {
        "exomem-records",
        "exomem-supersede",
    }
    assert all(path.is_relative_to(CANDIDATE) for path in paths), (
        "a v5 skill still resolves the shared tree"
    )
    assert all(path.is_file() for path in paths)


def test_editing_a_shared_skill_cannot_move_v5_but_still_moves_v1(tmp_path: Path) -> None:
    """The whole reason for a self-contained candidate, stated as a test."""
    root = copy_release_tree(tmp_path / "repo")
    before_v5 = hosted_plugins._skills_digest(root, V5)
    before_v1 = hosted_plugins._skills_digest(root, hosted_plugins.DEFAULT_CANDIDATE)

    shared = root / "plugins/hosted/skills/exomem/SKILL.md"
    shared.write_text(shared.read_text(encoding="utf-8") + "\nDrift.\n", encoding="utf-8")

    assert hosted_plugins._skills_digest(root, V5) == before_v5
    assert hosted_plugins._skills_digest(root, hosted_plugins.DEFAULT_CANDIDATE) != before_v1


def test_v5_preserves_v4_command_order_and_still_withholds_transfer_artifact() -> None:
    v4 = commands.PRODUCT_SURFACE_PROFILES[commands.HOSTED_ALPHA_AGENT_V4_PROFILE]
    v5 = commands.PRODUCT_SURFACE_PROFILES[commands.HOSTED_ALPHA_AGENT_V5_PROFILE]

    assert v5.command_names == v4.command_names
    assert "transfer_artifact" not in v5.command_names
    assert "transfer_artifact" in commands.HOSTED_SURFACE_EXCLUSIONS


# --------------------------------------------------------------------------
# The combined fixture and its digest
# --------------------------------------------------------------------------


def test_the_frozen_fixture_is_the_canonicalisation_of_all_four_inputs() -> None:
    frozen = hosted_plugins.check_behavior_fixture(REPO_ROOT, candidate=V5)

    assert set(frozen["contributions"]) == set(hosted_plugins.CONTRIBUTION_INPUTS)
    assert frozen["profile"] == commands.HOSTED_ALPHA_AGENT_V5_PROFILE
    for family, filename in hosted_plugins.CONTRIBUTION_INPUTS.items():
        source = json.loads(
            (REPO_ROOT / hosted_plugins.CONTRIBUTION_ROOT / filename).read_text(encoding="utf-8")
        )
        # Canonicalisation normalises whitespace and key order only. Case order
        # is content, so it survives untouched.
        assert frozen["contributions"][family]["family"] == source["family"]
        assert frozen["families"][family]["source"].endswith(filename)


def test_the_fixture_covers_the_positive_and_negative_case_of_every_family() -> None:
    frozen = hosted_plugins.check_behavior_fixture(REPO_ROOT, candidate=V5)
    covered = {
        case for declared in frozen["families"].values() for case in declared["case_ids"]
    }

    positives = {
        "stable-preference-entity-facet",
        "recurring-routine-compiled-observation",
        "historical-baseline-records",
        "durable-affiliation-entity-facet",
        "direct-source-adoption",
        "direct-evidence-adoption",
        "existing-identity-hydration",
        "reviewed-one-step-apply",
    }
    negatives = {
        "fleeting-preference-quiet",
        "one-off-routine-quiet",
        "stable-low-reuse-trivia-quiet",
        "tentative-unresolved-affiliation-quiet",
        "unselected-drafts-stay-ephemeral",
        "no-handle-handoff-is-honest",
        "reported-remote-reference-is-not-byte-proof",
        "ambiguous-identity-stop",
        "frequency-matched-twin",
        "unknown-action-fails-closed",
        "omitted-action-fails-closed",
        "executed-method-unreusable-one-off",
    }
    assert positives <= covered
    assert negatives <= covered


def test_the_combined_digest_is_bound_in_every_evidence_surface() -> None:
    expected = hosted_plugins.behavior_fixture_sha256(REPO_ROOT, candidate=V5)
    assert expected == hashlib.sha256(
        (CANDIDATE / hosted_plugins.COMBINED_FIXTURE_NAME).read_bytes()
    ).hexdigest()

    compatibility = json.loads((GENERATED / "compatibility.json").read_text(encoding="utf-8"))
    assert compatibility["behavior_fixture_sha256"] == expected

    for platform in hosted_plugins.PLATFORMS:
        lock = json.loads((GENERATED / f"{platform}.lock.json").read_text(encoding="utf-8"))
        archive_lock = json.loads(
            (GENERATED / f"{platform}.zip.lock.json").read_text(encoding="utf-8")
        )
        assert lock["behavior_fixture_sha256"] == expected
        assert archive_lock["behavior_fixture_sha256"] == expected


def test_historical_candidates_bind_no_fixture_digest() -> None:
    """The binding is v5's. Adding it to v1-v4 would move their identities."""
    for candidate in ("hosted-alpha-agent-v2", "hosted-alpha-agent-v3", "hosted-alpha-agent-v4"):
        compatibility = hosted_plugins.compatibility_manifest(REPO_ROOT, candidate=candidate)
        assert "behavior_fixture_sha256" not in compatibility


def test_archive_binds_the_combined_digest(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    output = hosted_plugins.archive(root, tmp_path / "dist", platform="claude", candidate=V5)
    lock = json.loads((output / "claude.zip.lock.json").read_text(encoding="utf-8"))

    assert lock["behavior_fixture_sha256"] == hosted_plugins.behavior_fixture_sha256(
        root, candidate=V5
    )


# --------------------------------------------------------------------------
# Drift invalidates package verification
# --------------------------------------------------------------------------


def test_fixture_drift_invalidates_package_verification(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    hosted_plugins.check(root, platform="claude", candidate=V5)

    frozen = root / "plugins/hosted/candidates" / V5 / hosted_plugins.COMBINED_FIXTURE_NAME
    payload = json.loads(frozen.read_text(encoding="utf-8"))
    payload["contributions"]["governed-curation"]["actions"].append("drifted")
    frozen.write_bytes(hosted_plugins._canonical_json(payload) + b"\n")

    with pytest.raises(ValueError, match="behavior fixture is stale"):
        hosted_plugins.check(root, platform="claude", candidate=V5)


def test_a_whitespace_only_fixture_edit_still_invalidates_the_package(tmp_path: Path) -> None:
    """The frozen file is the artifact, so its bytes are what the digest covers."""
    root = copy_release_tree(tmp_path / "repo")
    frozen = root / "plugins/hosted/candidates" / V5 / hosted_plugins.COMBINED_FIXTURE_NAME
    frozen.write_bytes(frozen.read_bytes() + b"\n")

    with pytest.raises(ValueError, match="stale"):
        hosted_plugins.check(root, platform="claude", candidate=V5)


def test_contribution_input_drift_invalidates_package_verification(tmp_path: Path) -> None:
    """A sibling lane editing its own input must not silently restate v5."""
    root = copy_release_tree(tmp_path / "repo")
    source = root / hosted_plugins.CONTRIBUTION_ROOT / "personal_baselines.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["cases"][0]["id"] = "renamed-case"
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="behavior fixture is stale"):
        hosted_plugins.check(root, platform="claude", candidate=V5)


def test_core_skill_drift_invalidates_package_verification(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    core = root / "plugins/hosted/candidates" / V5 / "skills/exomem/SKILL.md"
    core.write_text(core.read_text(encoding="utf-8") + "\nDrifted doctrine.\n", encoding="utf-8")

    with pytest.raises(ValueError, match="stale"):
        hosted_plugins.check(root, platform="claude", candidate=V5)


# --------------------------------------------------------------------------
# Promotion
# --------------------------------------------------------------------------


def test_promotion_binds_the_combined_fixture_digest(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    _seed_pending_promotion(root, "claude")
    expectation = v5_expectation(root)
    evidence = v5_signed_evidence(root, expectation)

    hosted_plugins.promote(
        root,
        "claude",
        evidence,
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="pending",
        expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude", candidate=V5),
        candidate=V5,
        records_expectation=expectation,
    )
    record = json.loads(
        hosted_plugins.promotion_record(root, "claude", candidate=V5).read_text(encoding="utf-8")
    )

    assert record["state"] == "live"
    assert record["package_lock"]["behavior_fixture_sha256"] == (
        hosted_plugins.behavior_fixture_sha256(root, candidate=V5)
    )


def test_promotion_refuses_a_combined_fixture_digest_mismatch(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    _seed_pending_promotion(root, "claude")
    expectation = v5_expectation(root)
    evidence = v5_signed_evidence(root, expectation)
    evidence["behavior_fixture_sha256"] = "0" * 64
    _sign_evidence(evidence)

    with pytest.raises(ValueError, match="compatibility or package identity"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=V5
            ),
            candidate=V5,
            records_expectation=expectation,
        )


@pytest.mark.parametrize("family", sorted(hosted_plugins.CONTRIBUTION_INPUTS))
def test_promotion_refuses_missing_traces_from_any_family(tmp_path: Path, family: str) -> None:
    root = copy_release_tree(tmp_path / "repo")
    _seed_pending_promotion(root, "claude")
    expectation = v5_expectation(root)
    evidence = v5_signed_evidence(root, expectation)
    traces = evidence["behavior_traces"]
    assert isinstance(traces, dict)
    del traces[family]
    _sign_evidence(evidence)

    with pytest.raises(ValueError, match="every behavior family"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=V5
            ),
            candidate=V5,
            records_expectation=expectation,
        )


def test_promotion_refuses_evidence_that_drops_a_negative_case(tmp_path: Path) -> None:
    """Reporting only the wins is the failure mode coverage exists to catch."""
    root = copy_release_tree(tmp_path / "repo")
    _seed_pending_promotion(root, "claude")
    expectation = v5_expectation(root)
    evidence = v5_signed_evidence(root, expectation)
    traces = evidence["behavior_traces"]
    assert isinstance(traces, dict)
    del traces["durable-personal-baselines"]["fleeting-preference-quiet"]
    _sign_evidence(evidence)

    with pytest.raises(ValueError, match="cover every durable-personal-baselines case"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=V5
            ),
            candidate=V5,
            records_expectation=expectation,
        )


def test_promotion_refuses_an_unmet_case(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    _seed_pending_promotion(root, "claude")
    expectation = v5_expectation(root)
    evidence = v5_signed_evidence(root, expectation)
    traces = evidence["behavior_traces"]
    assert isinstance(traces, dict)
    traces["governed-curation"]["unknown-action-fails-closed"] = False
    _sign_evidence(evidence)

    with pytest.raises(ValueError, match="unmet governed-curation case"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=V5
            ),
            candidate=V5,
            records_expectation=expectation,
        )


def test_promotion_refuses_a_wrong_profile(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    _seed_pending_promotion(root, "claude")
    expectation = v5_expectation(root)
    evidence = v5_signed_evidence(root, expectation)
    evidence["profile"] = commands.HOSTED_ALPHA_AGENT_V4_PROFILE
    _sign_evidence(evidence)

    with pytest.raises(ValueError, match="compatibility or package identity"):
        hosted_plugins.promote(
            root,
            "claude",
            evidence,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state="pending",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=V5
            ),
            candidate=V5,
            records_expectation=expectation,
        )


def test_v5_ships_unpromoted_like_v4(tmp_path: Path) -> None:
    """Rendering a candidate does not require a promotion record.

    v4 is rendered, locked and archived with no record under
    `plugins/hosted/promotion/candidates/`, and `check`/`archive` never read
    one. Promotion is a release-time operator act with live client evidence, so
    the lane that mints a candidate stops at the package.
    """
    assert not (REPO_ROOT / "plugins/hosted/promotion/candidates" / V5).exists()
    assert not (
        REPO_ROOT / "plugins/hosted/promotion/candidates/hosted-alpha-agent-v4"
    ).exists()
    hosted_plugins.check(REPO_ROOT, platform="all", candidate=V5)


# --------------------------------------------------------------------------
# Rollback
# --------------------------------------------------------------------------


def test_pre_lock_rebuild_of_an_unpromoted_candidate_is_allowed(tmp_path: Path) -> None:
    """Before promotion a failed doctrine can leave and the candidate rebuild."""
    root = copy_release_tree(tmp_path / "repo")
    core = root / "plugins/hosted/candidates" / V5 / "skills/exomem/SKILL.md"
    before = hosted_plugins.compatibility_manifest(root, candidate=V5)["compatibility_sha256"]

    core.write_text(
        core.read_text(encoding="utf-8").replace(
            "## Governed curation", "## Governed curation (revised)"
        ),
        encoding="utf-8",
    )
    hosted_plugins.render(
        root, platform="all", openai_app_id=hosted_plugins.REGISTERED_OPENAI_APP_ID, candidate=V5
    )
    hosted_plugins.check(root, platform="all", candidate=V5)
    after = hosted_plugins.compatibility_manifest(root, candidate=V5)["compatibility_sha256"]

    assert before != after


def test_a_promoted_candidate_cannot_be_corrected_by_mutation(tmp_path: Path) -> None:
    root = copy_release_tree(tmp_path / "repo")
    for platform in hosted_plugins.PLATFORMS:
        _seed_pending_promotion(root, platform)
    expectation = v5_expectation(root)
    hosted_plugins.promote(
        root,
        "claude",
        v5_signed_evidence(root, expectation),
        trusted_key_id="operator-key",
        trusted_secret="operator-secret",
        expected_state="pending",
        expected_record_sha256=hosted_plugins.promotion_record_sha256(root, "claude", candidate=V5),
        candidate=V5,
        records_expectation=expectation,
    )

    core = root / "plugins/hosted/candidates" / V5 / "skills/exomem/SKILL.md"
    core.write_text(
        core.read_text(encoding="utf-8") + "\nA post-promotion correction.\n", encoding="utf-8"
    )
    hosted_plugins.render(
        root, platform="all", openai_app_id=hosted_plugins.REGISTERED_OPENAI_APP_ID, candidate=V5
    )

    with pytest.raises(ValueError, match="different compatibility or package identity"):
        hosted_plugins.distribution_manifest(
            root,
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            candidate=V5,
            records_expectation=expectation,
        )


def test_a_correction_after_promotion_needs_a_new_candidate() -> None:
    """v6 is the only place a corrected identity can go.

    The registry is append-only in effect: a candidate's profile name is its
    published identity, and every digest a promotion record binds is derived
    from files that candidate owns. Correcting v5 in place changes those
    digests, which is exactly what the previous test refuses -- so the
    correction has to arrive as a candidate the registry does not yet have.
    """
    assert V5 in hosted_plugins.CANDIDATE_PROFILES
    assert "hosted-alpha-agent-v6" not in hosted_plugins.CANDIDATE_PROFILES
    assert hosted_plugins.CANDIDATE_PROFILES[V5] == commands.HOSTED_ALPHA_AGENT_V5_PROFILE
    # Selection may move away from a promoted candidate; nothing removes it.
    assert set(hosted_plugins.CANDIDATE_PROFILES) >= {
        "hosted-alpha-agent-v1",
        "hosted-alpha-agent-v2",
        "hosted-alpha-agent-v3",
        "hosted-alpha-agent-v4",
        V5,
    }
