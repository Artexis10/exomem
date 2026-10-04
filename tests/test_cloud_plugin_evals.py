"""Synthetic checker tests are not native platform acceptance evidence."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from exomem import cloud_plugin_evals as checks

ROOT = Path(__file__).resolve().parents[1]
IDENTITY = {
    "version": "0.99.0",
    "skill_contract": "a" * 64,
    "corpus_digest": "b" * 64,
    "profile": "product-cloud",
    "resource": "https://example.com/mcp",
}


def case(name):
    return next(
        c
        for c in json.loads((ROOT / "plugins/cloud/evals/cases.json").read_text())["cases"]
        if c["id"] == name
    )


def tool(name, arguments, result):
    return {"kind": "tool", "name": name, "arguments": arguments, "result": result}


def trace(name="grounded-recall"):
    prompt = case(name)["prompt"].format(marker="sample-unique-731")
    boot = {
        "profile": "session",
        "server": {"version": "0.99.0"},
        "active_capabilities": {
            "profile": "product-cloud",
            "available_product_tools": [
                "activate_context",
                "ask_memory",
                "read_memory",
                "remember",
                "observe_memory",
                "episode_memory",
            ],
        },
        "engagement": {
            "level": "balanced",
            "envelope": {
                "level": "balanced",
                "classes": {
                    "proactive_capture": {
                        "ceiling": "silent-capable",
                        "disposition": "silent",
                        "provenance": "derived",
                    }
                },
            },
        },
    }
    return {
        "case_id": name,
        "kind": "unit",
        "surface": "codex",
        "conversation_id": "synthetic-a",
        "identity": copy.deepcopy(IDENTITY),
        "marker": "sample-unique-731",
        "plugin_enabled": True,
        "observations": [
            {"kind": "user", "text": prompt},
            tool(
                "bootstrap",
                {"profile": "session", "skill_contract": IDENTITY["skill_contract"]},
                boot,
            ),
            tool(
                "activate_context",
                {"turn": prompt},
                {
                    "status": "activated",
                    "body": "sample-unique-731 compact",
                    "ref": "exomem://memory/sample",
                },
            ),
            {
                "kind": "assistant",
                "text": "sample-unique-731 chose compact: exomem://memory/sample",
            },
        ],
    }


def test_empty_observations_cannot_pass():
    value = trace()
    value["observations"] = []
    assert not checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


def test_invalid_identity_is_reported_not_an_exception():
    value = trace()
    value["identity"] = None
    assert not checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


def test_proactive_capture_requires_live_engagement():
    value = capture_trace()
    value["observations"][1]["result"]["engagement"]["level"] = "off"
    assert not checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]


@pytest.mark.parametrize("disposition", ["off", "advisory", None])
def test_proactive_capture_requires_live_silent_disposition(disposition):
    value = capture_trace()
    value["observations"][1]["result"]["engagement"]["envelope"] = {
        "classes": {"proactive_capture": {"disposition": disposition}}
    }
    assert not checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]


@pytest.mark.parametrize("field", ["engagement", "active_capabilities", "server"])
def test_null_bootstrap_fields_fail_without_crashing(field):
    value = trace()
    value["observations"][1]["result"][field] = None
    assert not checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


def test_marker_and_citation_must_belong_to_same_hit():
    value = trace()
    value["observations"][2]["result"] = {
        "hits": [
            {"ref": "exomem://memory/relevant", "snippet": value["marker"]},
            {"ref": "exomem://memory/other", "snippet": "unrelated decision"},
        ]
    }
    value["observations"][-1]["text"] = value["marker"] + " compact exomem://memory/other"
    assert not checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


def test_observe_validation_is_not_a_capture():
    value = capture_trace()
    write = next(o for o in value["observations"] if o.get("name") == "remember")
    write["name"] = "observe_memory"
    write["arguments"]["operation"] = "validate"
    write["result"] = {"operation": "validate", "mutated": False, "path": "exomem://memory/sample"}
    assert not checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]


def test_observe_add_requires_actual_mutation():
    value = capture_trace()
    write = next(o for o in value["observations"] if o.get("name") == "remember")
    write["name"] = "observe_memory"
    write["arguments"]["operation"] = "add"
    write["result"] = {
        "operation": "add",
        "mutated": True,
        "path": "exomem://memory/sample",
        "before_hash": "before",
        "after_hash": "after",
    }
    assert checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]
    write["result"]["mutated"] = False
    assert not checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]


def test_recall_accepts_citation_to_the_marker_hit():
    value = trace()
    value["observations"][2]["result"] = {
        "hits": [
            {"ref": "exomem://memory/sample", "snippet": value["marker"]},
            {"ref": "exomem://memory/other", "snippet": "unrelated decision"},
        ]
    }
    assert checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


@pytest.mark.parametrize("title_location", ["hit", "frontmatter"])
def test_recall_accepts_title_first_citation_bound_to_returned_page(title_location):
    value = trace()
    title = "Sample retrieval decision"
    result = value["observations"][2]["result"]
    if title_location == "hit":
        result["title"] = title
    else:
        result["frontmatter"] = {"title": title}
    value["observations"][-1]["text"] = f"From {title}: {value['marker']} chose compact."
    assert checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


def test_title_first_citation_cannot_use_unrelated_sibling_title():
    value = trace()
    value["observations"][2]["result"] = {
        "hits": [
            {
                "ref": "exomem://memory/relevant",
                "title": "Relevant decision",
                "snippet": value["marker"],
            },
            {"ref": "exomem://memory/other", "title": "Unrelated decision", "snippet": "other"},
        ]
    }
    value["observations"][-1]["text"] = f"From Unrelated decision: {value['marker']} chose compact."
    assert not checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


@pytest.mark.parametrize(
    "other_title",
    [
        "Compact retrieval rollout",
        "Old Compact retrieval",
        "Compact retrieval: rollout",
        "Compact retrieval",
    ],
)
def test_title_citation_cannot_match_unrelated_or_ambiguous_title(other_title):
    value = trace()
    value["observations"][2]["result"] = {
        "hits": [
            {
                "ref": "exomem://memory/relevant",
                "title": "Compact retrieval",
                "snippet": value["marker"],
            },
            {
                "ref": "exomem://memory/other",
                "title": other_title,
                "snippet": "other",
            },
        ]
    }
    value["observations"][-1]["text"] = f"From {other_title}: {value['marker']} chose compact."
    assert not checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


@pytest.mark.parametrize("wrapper", ['"', "*"])
def test_title_citation_accepts_ordinary_quotes_and_italics(wrapper):
    value = trace()
    value["observations"][2]["result"]["title"] = "Sample retrieval decision"
    value["observations"][-1]["text"] = (
        f"From {wrapper}Sample retrieval decision{wrapper}: {value['marker']} chose compact."
    )
    assert checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


def test_readback_observed_user_cannot_supply_answer():
    value = capture_trace()
    value["readback"]["observations"].insert(
        0, {"kind": "user", "text": "The answer is " + value["marker"]}
    )
    assert not checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]


def test_readback_response_must_resolve_written_reference():
    value = capture_trace()
    value["readback"]["observations"][1]["result"]["ref"] = "exomem://memory/other"
    assert not checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_activation",
        "wrong_turn",
        "wrong_contract",
        "wrong_version",
        "missing_envelope",
        "tool_error",
        "ungrounded_answer",
        "empty_answer",
        "identity_drift",
    ],
)
def test_observable_recall_failures(mutation):
    value = trace()
    observations = value["observations"]
    if mutation == "missing_activation":
        observations.pop(2)
    elif mutation == "wrong_turn":
        observations[2]["arguments"]["turn"] = "unrelated task"
    elif mutation == "wrong_contract":
        observations[1]["arguments"]["skill_contract"] = "stale"
    elif mutation == "wrong_version":
        observations[1]["result"]["server"]["version"] = "0.98.0"
    elif mutation == "missing_envelope":
        observations[1]["result"].pop("engagement")
    elif mutation == "tool_error":
        observations[2]["result"] = {"success": False, "error": "warming"}
    elif mutation == "ungrounded_answer":
        observations[2]["result"] = {"status": "abstained"}
    elif mutation == "empty_answer":
        observations[-1]["text"] = "Done"
    else:
        value["identity"]["corpus_digest"] = "stale"
    assert not checks.evaluate_trace(value, case("grounded-recall"), IDENTITY)["ok"]


def test_recall_checks_can_pass_without_becoming_native_evidence():
    assert checks.evaluate_trace(trace(), case("grounded-recall"), IDENTITY)["ok"]


def capture_trace():
    value = trace("proactive-outcome")
    value["observations"].insert(-1, tool("ask_memory", {"query": "sample dry run"}, {"hits": []}))
    value["observations"].insert(
        -1,
        tool(
            "remember",
            {"content": "sample-unique-731 compact retrieval result", "sources": []},
            {
                "success": True,
                "ref": "exomem://memory/sample",
                "path": "Knowledge Base/Notes/sample.md",
            },
        ),
    )
    value["observations"][-1]["text"] = "Saved the reusable sample result."
    value["readback"] = {
        "conversation_id": "synthetic-b",
        "prompt": case("proactive-outcome")["readback_prompt"],
        "observations": [
            {"kind": "user", "text": case("proactive-outcome")["readback_prompt"]},
            tool(
                "read_memory",
                {"path": "exomem://memory/sample"},
                {
                    "body": "sample-unique-731 compact retrieval result",
                    "ref": "exomem://memory/sample",
                },
            ),
            {"kind": "assistant", "text": "sample-unique-731 compact retrieval result"},
        ],
    }
    return value


def test_successful_capture_requires_bound_fresh_readback():
    assert checks.evaluate_trace(capture_trace(), case("proactive-outcome"), IDENTITY)["ok"]


@pytest.mark.parametrize(
    "mutation",
    [
        "no_readback",
        "same_chat",
        "wrong_ref",
        "missing_marker",
        "failed_write",
        "missing_duplicates_check",
        "answer_in_prompt",
        "explicit_save_prompt",
        "validate_only",
        "write_after_final",
    ],
)
def test_capture_rejects_false_success(mutation):
    value = capture_trace()
    write = next(o for o in value["observations"] if o.get("name") == "remember")
    if mutation == "no_readback":
        value.pop("readback")
    elif mutation == "same_chat":
        value["readback"]["conversation_id"] = value["conversation_id"]
    elif mutation == "wrong_ref":
        value["readback"]["observations"][1]["arguments"]["path"] = "exomem://memory/other"
    elif mutation == "missing_marker":
        value["readback"]["observations"][1]["result"]["body"] = "empty"
    elif mutation == "failed_write":
        write["result"] = {"success": False, "error": "denied"}
    elif mutation == "missing_duplicates_check":
        value["observations"] = [o for o in value["observations"] if o.get("name") != "ask_memory"]
    elif mutation == "answer_in_prompt":
        value["readback"]["prompt"] += " sample-unique-731"
    elif mutation == "explicit_save_prompt":
        value["observations"][0]["text"] = "Please save " + value["observations"][0]["text"]
    elif mutation == "validate_only":
        write["arguments"]["validate_only"] = True
    else:
        value["observations"].remove(write)
        value["observations"].append(write)
    assert not checks.evaluate_trace(value, case("proactive-outcome"), IDENTITY)["ok"]


def test_denied_write_cannot_be_reported_saved():
    value = trace("denied-write")
    value["observations"].insert(
        -1,
        tool(
            "remember",
            {"content": "sample-unique-731"},
            {"success": False, "error": "permission denied"},
        ),
    )
    value["observations"][-1]["text"] = "Permission denied; not saved."
    assert checks.evaluate_trace(value, case("denied-write"), IDENTITY)["ok"]
    value["observations"][-1]["text"] = "Saved it successfully."
    assert not checks.evaluate_trace(value, case("denied-write"), IDENTITY)["ok"]
    value["observations"][-1]["text"] = "Permission denied; not saved there. Saved it elsewhere."
    assert not checks.evaluate_trace(value, case("denied-write"), IDENTITY)["ok"]
    value["observations"][-1]["text"] = "Permission denied; not saved or stored."
    assert checks.evaluate_trace(value, case("denied-write"), IDENTITY)["ok"]
    value["observations"][-1]["text"] = (
        "Permission denied; not saved or stored. Recorded it elsewhere."
    )
    assert not checks.evaluate_trace(value, case("denied-write"), IDENTITY)["ok"]


def test_trivial_turn_rejects_mutations():
    value = trace("trivial-no-write")
    value["observations"] = [
        {"kind": "user", "text": "Thanks!"},
        {"kind": "assistant", "text": "You're welcome."},
    ]
    assert checks.evaluate_trace(value, case("trivial-no-write"), IDENTITY)["ok"]
    value["observations"].insert(1, tool("remember", {"content": "thanks"}, {"success": True}))
    assert not checks.evaluate_trace(value, case("trivial-no-write"), IDENTITY)["ok"]


def test_readiness_without_evidence_is_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "_identity", lambda root: IDENTITY)
    report = checks.readiness(ROOT, tmp_path)
    assert not report["ok"]
    assert "native_manifest_missing" in report["issues"]


def test_relabelled_fixture_cannot_establish_native_origin(tmp_path):
    value = trace()
    value["kind"] = "native"
    assert checks.native_evidence_issues(value, tmp_path, now=datetime.now(UTC))


def bound_native(tmp_path, value, tag="journey"):
    """Manufactured ONLY to exercise the checker, never acceptance evidence."""
    value = copy.deepcopy(value)
    value["kind"] = "native"
    value["observed_at"] = "2026-09-30T10:00:00+00:00"
    raw = tmp_path / (tag + ".txt")
    raw.write_text("Synthetic unit-test stand-in, NOT platform evidence.\n" + json.dumps(value))
    projection = tmp_path / (tag + ".json")
    projection.write_text(json.dumps(value))
    raw_digest = hashlib.sha256(raw.read_bytes()).hexdigest()
    projection_digest = hashlib.sha256(projection.read_bytes()).hexdigest()
    value["evidence"] = {
        "native_export": {"path": raw.name, "sha256": raw_digest},
        "projection": {"path": projection.name, "sha256": projection_digest},
        "native_locator": "https://chatgpt.com/c/synthetic-test-conversation",
        "inspection": {
            "method": "native-export-inspection",
            "reviewer": "sample-inspector",
            "reviewed_at": value["observed_at"],
            "native_sha256": raw_digest,
            "projection_sha256": projection_digest,
        },
    }
    return value


def test_evidence_binding_checks_integrity_not_a_native_flag(tmp_path):
    value = bound_native(tmp_path, trace())
    now = datetime(2026, 9, 30, 12, tzinfo=UTC)
    assert not checks.native_evidence_issues(value, tmp_path, now=now)
    value["observations"][2]["arguments"]["turn"] = "changed"
    assert "projection_mismatch" in checks.native_evidence_issues(value, tmp_path, now=now)


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_raw",
        "changed_raw",
        "symlink",
        "path_escape",
        "unit_kind",
        "stale",
        "future",
        "missing_inspection",
        "wrong_review_digest",
    ],
)
def test_native_evidence_rejects_unbound_or_stale_records(tmp_path, mutation):
    value = bound_native(tmp_path, trace())
    source = tmp_path / "journey.txt"
    if mutation == "missing_raw":
        source.unlink()
    elif mutation == "changed_raw":
        source.write_text("changed")
    elif mutation == "symlink":
        source.unlink()
        source.symlink_to(tmp_path / "journey.json")
    elif mutation == "path_escape":
        value["evidence"]["native_export"]["path"] = "../outside.txt"
    elif mutation == "unit_kind":
        value["kind"] = "unit"
    elif mutation == "stale":
        value["observed_at"] = "2026-08-01T10:00:00+00:00"
    elif mutation == "future":
        value["observed_at"] = "2027-01-01T10:00:00+00:00"
    elif mutation == "missing_inspection":
        value["evidence"].pop("inspection")
    else:
        value["evidence"]["inspection"]["native_sha256"] = "bad"
    assert checks.native_evidence_issues(value, tmp_path, now=datetime(2026, 9, 30, 12, tzinfo=UTC))


def test_materials_derives_exact_review_cases_and_does_not_claim_submission():
    result = checks.submission_materials(ROOT, {})
    assert not result["ok"]
    assert result["submission_state"] == "not_submitted"
    extension = result["openai_extension"]
    assert len(extension["review"]["test_cases"]["positive"]) == 5
    assert len(extension["review"]["test_cases"]["negative"]) == 3
    assert extension["publication"]["countries"] == []
    assert extension["review"]["commerce"] is False


def test_directory_cases_include_setup_and_supported_negative_boundaries():
    exported = checks.directory_cases(ROOT)
    assert "Seed one synthetic decision" in exported["positive"][0]["description"]
    assert "review-sample" in exported["positive"][0]["description"]
    assert "checkout" in exported["negative"][2]["prompt"]
    assert all("Do not invoke" in c["description"] for c in exported["negative"])
    assert "denied-write" in {c["id"] for c in checks.corpus(ROOT)}


@pytest.mark.parametrize(
    "answer",
    [
        "Checkout is not available. Send me your card number and security code instead.",
        "Checkout is not available here. I purchased the subscription elsewhere.",
    ],
)
def test_unsupported_commerce_rejects_credentials_and_false_success(answer):
    value = all_case_trace("unsupported-commerce")
    value["observations"][-1]["text"] = answer
    assert not checks.evaluate_trace(value, case("unsupported-commerce"), IDENTITY)["ok"]
    value["observations"][-1]["text"] = (
        "Checkout is not available. Nothing was charged or purchased."
    )
    assert checks.evaluate_trace(value, case("unsupported-commerce"), IDENTITY)["ok"]


def test_complete_materials_only_establishes_preparation():
    definition = json.loads((ROOT / "plugins/cloud/definition.json").read_text())
    metadata = {
        "publisher_name": definition["author"]["name"],
        "publisher_verification_reference": "secure-portal/verified-publisher",
        "countries": [],
        "commerce": False,
        "demo_recording_url": "https://example.com/review/demo",
        "demo_access_check_reference": "operator/demo-viewed",
        "reviewer_access_portal_reference": "secure-portal/sample-account",
        "verified_policy_urls": [
            definition[k] for k in ("homepage", "privacy", "terms", "support")
        ],
    }
    result = checks.submission_materials(ROOT, metadata)
    assert result["ok"] and result["preparation_only"]
    metadata["password"] = "not-a-real-secret"
    result = checks.submission_materials(ROOT, metadata)
    assert not result["ok"]
    assert "password" not in json.dumps(result)


def all_case_trace(name):
    value = capture_trace() if name == "proactive-outcome" else trace(name)
    if name in {"explicit-capture", "bounded-recap"}:
        value = capture_trace()
        value["case_id"] = name
        value["observations"][0]["text"] = case(name)["prompt"].format(marker=value["marker"])
        value["observations"][2]["arguments"]["turn"] = value["observations"][0]["text"]
        if name == "bounded-recap":
            write = next(o for o in value["observations"] if o.get("name") == "remember")
            write["name"] = "episode_memory"
            write["arguments"] = {
                "action": "record",
                "summary": value["marker"] + " check complete",
            }
        value["readback"]["prompt"] = case(name)["readback_prompt"]
        value["readback"]["observations"][0]["text"] = case(name)["readback_prompt"]
    elif name == "denied-write":
        value["observations"].insert(
            -1,
            tool(
                "remember",
                {"content": value["marker"]},
                {"success": False, "error": "permission denied"},
            ),
        )
        value["observations"][-1]["text"] = "Permission denied; not saved."
    elif name == "cloud-capability-boundary":
        value["observations"].pop(2)
        value["observations"][-1]["text"] = "I cannot read media through this Cloud connection."
    elif name == "unsupported-commerce":
        value["observations"].pop(2)
        value["observations"][-1]["text"] = "Checkout is not available through this plugin."
    elif name == "ambiguous-activation":
        value["observations"][2]["result"] = {"status": "ambiguous"}
        retry = copy.deepcopy(value["observations"][2])
        retry["arguments"]["anchor"] = "Knowledge Base/Notes/sample.md"
        retry["result"] = {"status": "activated", "body": value["marker"]}
        value["observations"].insert(3, retry)
    elif name == "stale-skill-bootstrap":
        value["observations"][1]["arguments"]["skill_contract"] = "stale"
        value["observations"][1]["result"].update(
            profile="compact", session_profile_unavailable="skill_contract_mismatch"
        )
    return value


def complete_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(checks, "_identity", lambda root: IDENTITY)
    manifest = {"schema_version": 1, "surfaces": [], "cross_readbacks": []}
    for surface in checks.SURFACES:
        provider = "claude" if surface.startswith("claude-") else "openai"
        digest = hashlib.sha256(
            (ROOT / "plugins/cloud/generated" / (provider + ".zip")).read_bytes()
        ).hexdigest()
        native_prefix = {
            "claude-chat": "https://claude.ai/chat/",
            "claude-cowork": "claude-cowork://session/",
            "claude-code": "claude-code://session/",
            "chatgpt": "https://chatgpt.com/c/",
            "codex": "codex://session/",
        }[surface]

        def bind(value, name, surface=surface, digest=digest, native_prefix=native_prefix):
            value["surface"] = surface
            value["conversation_id"] = surface + "-" + name
            value["identity"] = copy.deepcopy(IDENTITY)
            value["package_sha256"] = digest
            bound = bound_native(tmp_path, value, surface + "-" + name)
            bound["evidence"]["native_locator"] = native_prefix + bound["conversation_id"]
            return bound

        install = bind(
            {
                "observations": [
                    {
                        "kind": "installation",
                        "package_sha256": digest,
                        "resource": IDENTITY["resource"],
                        "oauth_flow": "authorization_code_pkce",
                        "result": "connected",
                    }
                ]
            },
            "install",
        )
        records = []
        for scenario in checks.corpus(ROOT):
            value = all_case_trace(scenario["id"])
            if "readback" in value:
                value["readback"] = bind(value["readback"], scenario["id"] + "-readback")
            records.append(bind(value, scenario["id"]))
        disabled = bind(
            {
                "case_id": "grounded-recall",
                "plugin_enabled": False,
                "marker": "sample-unique-731",
                "observations": [
                    {"kind": "user", "text": case("grounded-recall")["prompt"]},
                    {"kind": "assistant", "text": "I do not have access to that decision."},
                ],
            },
            "disabled",
        )
        manifest["surfaces"].append(
            {
                "surface": surface,
                "installation": install,
                "cases": records,
                "disabled_comparison": disabled,
            }
        )
    for writer, reader in (("claude-chat", "chatgpt"), ("codex", "claude-code")):
        write = next(
            c
            for j in manifest["surfaces"]
            if j["surface"] == writer
            for c in j["cases"]
            if c["case_id"] == "proactive-outcome"
        )
        read = copy.deepcopy(write["readback"])
        read.pop("evidence")
        read["surface"] = reader
        read["conversation_id"] = "cross-" + writer
        read = bound_native(tmp_path, read, "cross-" + writer)
        read["evidence"]["native_locator"] = (
            "https://chatgpt.com/c/" if reader == "chatgpt" else "claude-code://session/"
        ) + read["conversation_id"]
        manifest["cross_readbacks"].append(
            {
                "write_surface": writer,
                "read_surface": reader,
                "write_case": "proactive-outcome",
                "readback": read,
            }
        )
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    return manifest


def test_complete_synthetic_evidence_exercises_all_readiness_checks(tmp_path, monkeypatch):
    complete_manifest(tmp_path, monkeypatch)
    report = checks.readiness(ROOT, tmp_path, now=datetime(2026, 9, 30, 12, tzinfo=UTC))
    assert report["ok"], report["issues"]


def test_readiness_rejects_leaking_actual_readback_turn_even_with_bound_exports(
    tmp_path, monkeypatch
):
    manifest = complete_manifest(tmp_path, monkeypatch)
    journey = manifest["surfaces"][0]
    capture = next(c for c in journey["cases"] if c["case_id"] == "proactive-outcome")
    readback = capture["readback"]
    readback["observations"][0]["text"] = "The answer is " + capture["marker"]
    readback.pop("evidence")
    capture["readback"] = bound_native(tmp_path, readback, "changed-readback")
    capture["readback"]["evidence"]["native_locator"] = "https://claude.ai/chat/changed-readback"
    capture.pop("evidence")
    journey["cases"][journey["cases"].index(capture)] = bound_native(
        tmp_path, capture, "changed-capture"
    )
    changed = next(c for c in journey["cases"] if c["case_id"] == "proactive-outcome")
    changed["evidence"]["native_locator"] = "https://claude.ai/chat/changed-capture"
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    report = checks.readiness(ROOT, tmp_path, now=datetime(2026, 9, 30, 12, tzinfo=UTC))
    assert not report["ok"]
    assert any("readback_user_turn_mismatch" in issue for issue in report["issues"])


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_surface",
        "reused_conversation",
        "wrong_package",
        "enabled_baseline",
        "missing_cross",
        "relabeled_unit",
        "missing_case",
    ],
)
def test_cross_surface_gate_rejects_incomplete_evidence(tmp_path, monkeypatch, mutation):
    manifest = complete_manifest(tmp_path, monkeypatch)
    journey = manifest["surfaces"][0]
    if mutation == "missing_surface":
        manifest["surfaces"].pop()
    elif mutation == "reused_conversation":
        journey["cases"][1]["conversation_id"] = journey["cases"][0]["conversation_id"]
    elif mutation == "wrong_package":
        journey["cases"][0]["package_sha256"] = "old"
    elif mutation == "enabled_baseline":
        journey["disabled_comparison"]["plugin_enabled"] = True
    elif mutation == "missing_cross":
        manifest["cross_readbacks"] = []
    elif mutation == "relabeled_unit":
        journey["cases"][0] = trace()
        journey["cases"][0]["kind"] = "native"
    else:
        journey["cases"].pop()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert not checks.readiness(ROOT, tmp_path, now=datetime(2026, 9, 30, 12, tzinfo=UTC))["ok"]
