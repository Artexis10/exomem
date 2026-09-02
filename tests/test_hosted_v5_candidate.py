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


#: The twenty behaviours the hosted-agent-surface scenario names, each against
#: the contribution case that actually carries it. Written out rather than
#: implied, because "its evidence accepts X and rejects Y" is only checkable if
#: every X and Y has a name on this side of the fixture too.
#:
#: Four labels are carried as a constraint *inside* another case rather than by
#: a standalone case of their own -- a forbidden write id, a forbidden claim, a
#: decision order. Those are marked, because "covered" and "has its own case"
#: are different claims and the second one is not true for them.
SCENARIO_POSITIVES: dict[str, str] = {
    "stable preference": "stable-preference-entity-facet",
    "recurring routine": "recurring-routine-compiled-observation",
    "historical baseline": "historical-baseline-records",
    "durable affiliation": "durable-affiliation-entity-facet",
    "exact adopted-artifact": "direct-source-adoption",
    "stable recurring-identity": "generic-promotion",
    "existing-Entity hydration": "existing-identity-hydration",
    "reviewed curation": "reviewed-one-step-apply",
}
SCENARIO_NEGATIVES: dict[str, str] = {
    "fleeting": "fleeting-preference-quiet",
    "one-off": "one-off-routine-quiet",
    "incidental": "frequency-matched-twin",
    "trivial": "stable-low-reuse-trivia-quiet",
    "tentative": "tentative-unresolved-affiliation-quiet",
    "unselected-draft": "unselected-drafts-stay-ephemeral",
    # Carried as `forbidden_write_file_ids` on the adoption traces: the sibling
    # variant of the same artifact must not be written.
    "wrong-variant": "direct-evidence-adoption",
    "false-save": "no-handle-handoff-is-honest",
    "ambiguous-identity auto-selection": "ambiguous-identity-stop",
    # Carried as the fixture's `decision_order` "hydrate-one-match-before-
    # duplicate" and as this case's expected route.
    "duplicate-Entity": "existing-identity-hydration",
    # Carried as the conservatively-mutating fail-closed path: a curation write
    # whose action is omitted is refused rather than assumed confirmed.
    "unconfirmed-curation": "omitted-action-fails-closed",
    "false-terminal": "reported-remote-reference-is-not-byte-proof",
}
#: Every other case the four contributions declare. Listed so that no case can
#: sit in the frozen fixture unaccounted for -- the check below is a partition,
#: not a subset.
NON_SCENARIO_CASES: dict[str, str] = {
    "durable affiliation routed to an accepted relation": "durable-affiliation-accepted-relation",
    "executed method, positive": "executed-method-positive",
    "executed method, failure": "executed-method-failure",
    "executed method, parameter boundary": "executed-method-parameter-boundary",
    "executed method, unreusable one-off": "executed-method-unreusable-one-off",
    "receipt-linked reported delivery": "receipt-linked-reported-delivery",
    "open registry family metadata": "open-registry-family-metadata",
    "curation work-item read": "explicit-work-item-read",
    "curation sealed-plan preview": "sealed-plan-preview",
    "curation plan proposal": "agent-authored-plan-proposal",
    "curation approved-plan resume": "approved-plan-resume",
    "curation compensation proposal": "compensation-plan-proposal",
    "curation unknown action fails closed": "unknown-action-fails-closed",
}


def test_every_named_scenario_behaviour_maps_to_a_real_case_and_back() -> None:
    """The scenario's twenty behaviours, traceable in both directions.

    Forward: every named behaviour resolves to a case id that exists in the
    frozen fixture. Backward: every case id in the frozen fixture is claimed by
    some label -- one of the twenty, or one of the additional behaviours the
    contributions carry beyond the scenario. A case nobody claims is a case
    nobody is reading, and the promotion gate would still demand a trace for it.
    """
    frozen = hosted_plugins.check_behavior_fixture(REPO_ROOT, candidate=V5)
    covered = {
        case for declared in frozen["families"].values() for case in declared["case_ids"]
    }

    assert len(SCENARIO_POSITIVES) == 8
    assert len(SCENARIO_NEGATIVES) == 12

    labelled = {**SCENARIO_POSITIVES, **SCENARIO_NEGATIVES, **NON_SCENARIO_CASES}
    missing = {label: case for label, case in labelled.items() if case not in covered}
    assert not missing, f"labels naming a case that does not exist: {missing}"

    unclaimed = covered - set(labelled.values())
    assert not unclaimed, f"cases no label claims: {sorted(unclaimed)}"


def test_the_scenario_positives_and_negatives_are_paired_across_all_four_families() -> None:
    """Each family has to contribute to both halves, not just to the easy one."""
    frozen = hosted_plugins.check_behavior_fixture(REPO_ROOT, candidate=V5)
    family_of = {
        case: family
        for family, declared in frozen["families"].items()
        for case in declared["case_ids"]
    }

    assert set(hosted_plugins.CONTRIBUTION_INPUTS) == {
        family_of[case] for case in SCENARIO_POSITIVES.values()
    }
    assert set(hosted_plugins.CONTRIBUTION_INPUTS) == {
        family_of[case] for case in SCENARIO_NEGATIVES.values()
    }


def test_a_contribution_case_list_the_owner_never_mapped_is_refused(tmp_path: Path) -> None:
    """Fail closed on a shape the owner has not read.

    A sibling lane adding `negative_cases` beside `cases` would be dropped from
    the coverage set silently, and the promotion gate would keep passing while
    demanding traces for fewer cases than the contribution declares.
    """
    root = copy_release_tree(tmp_path / "repo")
    source = root / hosted_plugins.CONTRIBUTION_ROOT / "recurring_entity_lifecycle.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    payload["negative_cases"] = [{"id": "a-case-the-owner-never-mapped"}]
    source.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="negative_cases"):
        hosted_plugins.combined_behavior_fixture(root)
    with pytest.raises(ValueError, match="negative_cases"):
        hosted_plugins.check(root, platform="claude", candidate=V5)


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


def test_pre_promotion_rebuild_of_an_unpromoted_candidate_is_allowed(tmp_path: Path) -> None:
    """Before promotion a failed doctrine can leave and the candidate rebuild.

    "Pre-lock" in the design means before the candidate's identity is published,
    which is promotion -- the lock files are regenerated by every render, so a
    rebuild replaces them rather than being blocked by them. This deletes them
    first anyway, to say plainly that nothing about a rendered lock stands in
    the way of a rebuild that no promotion record has bound yet.
    """
    root = copy_release_tree(tmp_path / "repo")
    core = root / "plugins/hosted/candidates" / V5 / "skills/exomem/SKILL.md"
    before = hosted_plugins.compatibility_manifest(root, candidate=V5)["compatibility_sha256"]
    generated = root / "plugins/hosted/generated/candidates" / V5
    for platform in hosted_plugins.PLATFORMS:
        for suffix in (".lock.json", ".zip", ".zip.lock.json"):
            (generated / f"{platform}{suffix}").unlink()

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


def test_a_failed_contribution_can_be_re_frozen_and_the_candidate_rebuilt(
    tmp_path: Path,
) -> None:
    """The pre-promotion rollback the design describes, run end to end.

    "Before the first v5 lock, a failed doctrine can be removed from the
    candidate and the combined candidate rebuilt." That is three steps -- change
    the contribution, re-freeze, render -- and the middle one is a deliberate
    act with its own subcommand, because a render that re-froze on its own would
    turn a sibling lane's edit into a new release identity unasked.
    """
    root = copy_release_tree(tmp_path / "repo")
    before = hosted_plugins.behavior_fixture_sha256(root, candidate=V5)
    source = root / hosted_plugins.CONTRIBUTION_ROOT / "governed_curation.json"
    payload = json.loads(source.read_text(encoding="utf-8"))
    removed = payload["cases"].pop()
    source.write_text(json.dumps(payload), encoding="utf-8")

    # Until the owner re-freezes, the candidate is stale and refuses to render,
    # and the refusal says what to run.
    with pytest.raises(ValueError, match="freeze-fixture --candidate"):
        hosted_plugins.render(
            root,
            platform="all",
            openai_app_id=hosted_plugins.REGISTERED_OPENAI_APP_ID,
            candidate=V5,
        )

    hosted_plugins.freeze_behavior_fixture(root, candidate=V5)
    hosted_plugins.render(
        root, platform="all", openai_app_id=hosted_plugins.REGISTERED_OPENAI_APP_ID, candidate=V5
    )
    hosted_plugins.check(root, platform="all", candidate=V5)

    after = hosted_plugins.behavior_fixture_sha256(root, candidate=V5)
    assert after != before
    rebuilt = json.loads(
        (root / "plugins/hosted/candidates" / V5 / hosted_plugins.COMBINED_FIXTURE_NAME).read_text(
            encoding="utf-8"
        )
    )
    assert removed["id"] not in rebuilt["families"]["governed-curation"]["case_ids"]
    for platform in hosted_plugins.PLATFORMS:
        lock = json.loads(
            (
                root / "plugins/hosted/generated/candidates" / V5 / f"{platform}.lock.json"
            ).read_text(encoding="utf-8")
        )
        assert lock["behavior_fixture_sha256"] == after


def test_the_freeze_tool_writes_exactly_the_committed_serialisation(tmp_path: Path) -> None:
    """Re-freezing an unchanged candidate must not move a single byte."""
    root = copy_release_tree(tmp_path / "repo")
    frozen = root / "plugins/hosted/candidates" / V5 / hosted_plugins.COMBINED_FIXTURE_NAME
    before = frozen.read_bytes()

    hosted_plugins.freeze_behavior_fixture(root, candidate=V5)

    assert frozen.read_bytes() == before
    assert before.endswith(b"\n")


def test_a_demote_and_re_promote_cycle_cannot_correct_a_promoted_candidate(
    tmp_path: Path,
) -> None:
    """The rollback contract's real adversary: the operator with a demote button.

    `demote` moves a live record to `failed`, and `promote` accepts `failed` as
    a starting state -- so demote, edit the candidate, render, promote again and
    a corrected release ships under the promoted name, with the first live
    identity nowhere on disk. Asserting that a v6 directory does not exist
    proves nothing about that; this runs the cycle.

    A candidate name's first live identity is sticky. Re-promoting the *same*
    identity after a demote stays allowed, because that is a rollback forward,
    not a correction.
    """
    root = copy_release_tree(tmp_path / "repo")
    for platform in hosted_plugins.PLATFORMS:
        _seed_pending_promotion(root, platform)
    expectation = v5_expectation(root)

    def promote(state: str) -> None:
        hosted_plugins.promote(
            root,
            "claude",
            v5_signed_evidence(root, expectation),
            trusted_key_id="operator-key",
            trusted_secret="operator-secret",
            expected_state=state,
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=V5
            ),
            candidate=V5,
            records_expectation=expectation,
        )

    def demote() -> None:
        hosted_plugins.demote(
            root,
            "claude",
            "client-regression",
            expected_state="live",
            expected_record_sha256=hosted_plugins.promotion_record_sha256(
                root, "claude", candidate=V5
            ),
            candidate=V5,
        )

    def record() -> dict:
        return json.loads(
            hosted_plugins.promotion_record(root, "claude", candidate=V5).read_text(
                encoding="utf-8"
            )
        )

    promote("pending")
    first_identity = record()["compatibility_sha256"]

    # Demoting is legitimate: selection moves away from v5.
    demote()
    assert record()["state"] == "failed"
    # ...but the identity it was promoted with survives the demotion.
    assert first_identity in json.dumps(record())

    # Re-promoting the same bytes is a rollback forward, and stays allowed.
    promote("failed")
    assert record()["state"] == "live"
    assert record()["compatibility_sha256"] == first_identity

    # Now the correction attempt: demote, edit, render, promote.
    demote()
    core = root / "plugins/hosted/candidates" / V5 / "skills/exomem/SKILL.md"
    core.write_text(
        core.read_text(encoding="utf-8") + "\nA post-promotion correction.\n", encoding="utf-8"
    )
    hosted_plugins.render(
        root, platform="all", openai_app_id=hosted_plugins.REGISTERED_OPENAI_APP_ID, candidate=V5
    )
    assert (
        hosted_plugins.compatibility_manifest(root, candidate=V5)["compatibility_sha256"]
        != first_identity
    )

    with pytest.raises(ValueError, match="HOSTED_PROMOTED_IDENTITY_IMMUTABLE"):
        promote("failed")

    # The refusal has to leave the first identity intact and the record unlive.
    assert record()["state"] == "failed"
    assert first_identity in json.dumps(record())


def test_the_sticky_identity_rule_covers_every_candidate_name(tmp_path: Path) -> None:
    """Not a v5 rule. The canonical spec says historical candidates are immutable.

    v2 is the oldest candidate with its own promotion record, so it is the one
    that shows the rule is keyed on the candidate name rather than on v5.
    """
    root = copy_release_tree(tmp_path / "repo")
    candidate = hosted_plugins.LIFECYCLE_CANDIDATE
    record_path = hosted_plugins.promotion_record(root, "claude", candidate=candidate)
    live = {
        "schema_version": 1,
        "platform": "claude",
        "candidate": candidate,
        "minimum_records_reader_version": 2,
        "state": "live",
        "package_lock": {"artifact_sha256": "a" * 64},
        "compatibility_sha256": "b" * 64,
    }
    record_path.write_bytes(hosted_plugins._canonical_json(live) + b"\n")

    hosted_plugins.demote(
        root,
        "claude",
        "client-regression",
        expected_state="live",
        expected_record_sha256=hosted_plugins.promotion_record_sha256(
            root, "claude", candidate=candidate
        ),
        candidate=candidate,
    )
    demoted = json.loads(record_path.read_text(encoding="utf-8"))

    assert demoted["state"] == "failed"
    assert demoted["prior_live_identities"] == [
        {"compatibility_sha256": "b" * 64, "package_lock": {"artifact_sha256": "a" * 64}}
    ]
