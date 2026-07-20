"""Node-evaluated unit tests for the Adoption Studio UI model and route (state.v2).

Mirrors ``tests/test_studio_ui_model.py``: the pure browser modules are exercised
through ``node --input-type=module --eval`` so Node never becomes a runtime
dependency (skipped when Node is absent). These tests define the contract for
``adoption-model.v1.js`` and the ``state.v2.js`` route serialization.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
MODEL = ROOT / "src/exomem/studio/adoption-model.v1.js"
STATE = ROOT / "src/exomem/studio/state.v2.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="Node is not installed")


def _node(source: str) -> dict:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", source],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def test_tristate_selection_math_with_nested_override_and_junk_default_off() -> None:
    source = f"""
      import {{initialSelection, toggleFolder, overrideFile, folderState,
               selectionCounts, selectionPayload}} from {MODEL.as_uri()!r};
      const tree = [
        {{path: 'Docs'}}, {{path: 'Docs/Notes'}}, {{path: 'Archive'}},
      ];
      const inventory = {{rows: [
        {{path: 'Docs/Notes/a.md', eligible: true}},
        {{path: 'Docs/Notes/b.md', eligible: true}},
        {{path: 'Archive/c.md', eligible: true}},
        {{path: 'Archive/img.png', eligible: false}},
        {{path: 'Docs/tmp.md~', eligible: false, junk: true}},
      ], junk_count: 1}};

      let sel = initialSelection(tree, {{}});
      const base = {{
        counts: selectionCounts(inventory, sel),
        docs: folderState(sel, tree, 'Docs'),
      }};

      // Turn Archive off entirely.
      sel = toggleFolder(sel, 'Archive', false);
      const archiveOff = {{
        counts: selectionCounts(inventory, sel),
        archive: folderState(sel, tree, 'Archive'),
        docs: folderState(sel, tree, 'Docs'),
      }};

      // Re-include a single file under the off folder (nested override → mixed).
      sel = overrideFile(sel, 'Archive/c.md', true);
      const overridden = {{
        counts: selectionCounts(inventory, sel),
        archive: folderState(sel, tree, 'Archive'),
        payload: selectionPayload(sel, ['Docs', 'Archive']),
      }};

      // An OFF file override under a default-on folder rides `exclude`.
      const fileOff = selectionPayload(
        overrideFile(initialSelection(tree, {{}}), 'Docs/Notes/b.md', false),
        ['Docs', 'Archive']);

      // A child folder toggled off makes the parent tri-state mixed.
      const childMixed = folderState(
        toggleFolder(initialSelection(tree, {{}}), 'Docs/Notes', false), tree, 'Docs');

      console.log(JSON.stringify({{base, archiveOff, overridden, childMixed, fileOff}}));
    """

    result = _node(source)

    assert result["base"]["counts"]["selectedNotes"] == 3
    assert result["base"]["counts"]["selectableNotes"] == 3
    assert result["base"]["counts"]["junkIncluded"] == 0
    assert result["base"]["docs"] == "checked"

    assert result["archiveOff"]["counts"]["selectedNotes"] == 2
    assert result["archiveOff"]["archive"] == "unchecked"
    assert result["archiveOff"]["docs"] == "checked"

    assert result["overridden"]["counts"]["selectedNotes"] == 3
    assert result["overridden"]["archive"] == "mixed"
    # Untouched default-on roots become explicit includes; the explicit
    # Archive-off rule stays an exclude; the ON file override is add-only.
    assert result["overridden"]["payload"] == {
        "include": ["Docs"],
        "exclude": ["Archive"],
        "overrides": ["Archive/c.md"],
        "include_junk": False,
    }

    assert result["fileOff"] == {
        "include": ["Archive", "Docs"],
        "exclude": ["Docs/Notes/b.md"],
        "overrides": [],
        "include_junk": False,
    }

    assert result["childMixed"] == "mixed"


def test_junk_inclusion_and_payload_toggle() -> None:
    source = f"""
      import {{initialSelection, selectionCounts, selectionPayload}}
        from {MODEL.as_uri()!r};
      const inventory = {{rows: [
        {{path: 'a.md', eligible: true}},
        {{path: 'x.md~', eligible: false, junk: true}},
        {{path: 'y.md~', eligible: false, junk: true}},
      ], junk_count: 2}};
      let sel = initialSelection([], {{}});
      const off = selectionCounts(inventory, sel);
      sel = {{...sel, includeJunk: true}};
      const on = selectionCounts(inventory, sel);
      console.log(JSON.stringify({{off, on, payload: selectionPayload(sel)}}));
    """

    result = _node(source)

    assert result["off"]["junkAvailable"] == 2
    assert result["off"]["junkIncluded"] == 0
    assert result["on"]["junkIncluded"] == 2
    assert result["payload"]["include_junk"] is True


def test_plan_bullets_sums_reconcile_and_count_line_matches_contract() -> None:
    source = f"""
      import {{planBullets, countLine}} from {MODEL.as_uri()!r};
      const plan = planBullets({{copy: 1204, skip_unsupported: 1213, skip_junk: 14}});
      console.log(JSON.stringify({{
        plan,
        line: countLine(200, 1532, 0),
        lineOmitted: countLine(200, 1532, 1332),
      }}));
    """

    result = _node(source)

    assert result["plan"]["total"] == 2431
    assert result["plan"]["total"] == (
        result["plan"]["copy"] + result["plan"]["unsupported"] + result["plan"]["junk"]
    )
    assert result["plan"]["bullets"][0] == "1204 text notes will be copied in"
    assert result["plan"]["bullets"][-1] == (
        "0 files will be changed, moved, or deleted — always"
    )
    assert result["line"] == "Showing 200 of 1532"
    assert result["lineOmitted"] == "Showing 200 of 1532 · 1332 not shown"


def test_phase_gating_legal_step_and_phase_screen_including_unknown() -> None:
    source = f"""
      import {{legalStep, phaseScreen}} from {MODEL.as_uri()!r};
      const r = (phase) => ({{phase}});
      console.log(JSON.stringify({{
        legal: {{
          selectingChoose: legalStep('selecting', 'choose'),
          selectingIllegal: legalStep('selecting', 'organize'),
          plannedDefault: legalStep('planned', 'start'),
          plannedChoose: legalStep('planned', 'choose'),
          appliedOrganize: legalStep('applied', 'organize'),
          appliedIllegal: legalStep('applied', 'choose'),
          partialSuggestions: legalStep('partial', 'suggestions'),
          failed: legalStep('failed', 'choose'),
          cancelled: legalStep('cancelled', 'preview'),
          applying: legalStep('applying', 'preview'),
        }},
        screen: {{
          none: phaseScreen(null, 'start'),
          selectingFindings: phaseScreen(r('selecting'), 'findings'),
          selectingChoose: phaseScreen(r('selecting'), 'choose'),
          plannedPreview: phaseScreen(r('planned'), 'preview'),
          applying: phaseScreen(r('applying'), 'preview'),
          appliedResult: phaseScreen(r('applied'), 'start'),
          appliedHandoff: phaseScreen(r('applied'), 'organize'),
          partialResult: phaseScreen(r('partial'), 'start'),
          doneCard: phaseScreen(r('done'), 'start'),
          doneQuestion: phaseScreen(r('done'), 'question'),
          cancelled: phaseScreen(r('cancelled'), 'start'),
          failed: phaseScreen(r('failed'), 'start'),
          unknown: phaseScreen(r('teleporting'), 'start'),
        }},
      }}));
    """

    result = _node(source)

    assert result["legal"] == {
        "selectingChoose": "choose",
        "selectingIllegal": "findings",
        "plannedDefault": "preview",
        "plannedChoose": "choose",
        "appliedOrganize": "organize",
        "appliedIllegal": "start",
        "partialSuggestions": "suggestions",
        "failed": "start",
        "cancelled": "start",
        "applying": "start",
    }
    assert result["screen"] == {
        "none": "start",
        "selectingFindings": "findings",
        "selectingChoose": "choose",
        "plannedPreview": "preview",
        "applying": "applying",
        "appliedResult": "result",
        "appliedHandoff": "handoff",
        "partialResult": "result",
        "doneCard": "done",
        "doneQuestion": "question",
        "cancelled": "cancelled",
        "failed": "failed",
        "unknown": "unknown",
    }


def test_failure_groups_map_codes_and_keep_server_reason_on_unknown() -> None:
    source = f"""
      import {{failureGroups}} from {MODEL.as_uri()!r};
      const groups = failureGroups([
        {{path: 'a.png', code: 'UNSUPPORTED_IMPORT_TYPE', reason: 'binary'}},
        {{path: 'b.md', code: 'NOT_FOUND', reason: 'gone'}},
        {{path: 'c.md', code: 'UNSUPPORTED_IMPORT_TYPE', reason: 'binary'}},
        {{path: 'd.md', code: 'DISK_ON_FIRE', reason: 'disk full'}},
      ]);
      console.log(JSON.stringify({{groups}}));
    """

    result = _node(source)
    groups = {g["code"]: g for g in result["groups"]}

    assert groups["UNSUPPORTED_IMPORT_TYPE"]["reason"] == (
        "This kind of file isn't supported yet."
    )
    assert groups["UNSUPPORTED_IMPORT_TYPE"]["paths"] == ["a.png", "c.md"]
    assert groups["NOT_FOUND"]["reason"] == (
        "This file moved or was removed after the scan."
    )
    assert groups["DISK_ON_FIRE"]["reason"] == "Couldn't be copied: disk full"
    assert [g["code"] for g in result["groups"]] == [
        "UNSUPPORTED_IMPORT_TYPE",
        "NOT_FOUND",
        "DISK_ON_FIRE",
    ]


def test_stale_notice_and_poll_delay_and_chips() -> None:
    source = f"""
      import {{staleNotice, pollDelay, suggestionChips}} from {MODEL.as_uri()!r};
      console.log(JSON.stringify({{
        stale: staleNotice({{changed_count: 3}}),
        staleOne: staleNotice({{changed_count: 1}}),
        poll: {{
          scanFast: pollDelay('scanning', 0),
          scanSlow: pollDelay('scanning', 60000),
          applyFast: pollDelay('applying', 59999),
          proposalsFast: pollDelay('awaiting_proposals', 0),
          proposalsSlow: pollDelay('awaiting_proposals', 120000),
        }},
        chips: suggestionChips(
          [{{name: 'Meeting notes'}}, {{name: 'Recipes'}}],
          [{{path: 'Documents/Notes'}}],
        ),
      }}));
    """

    result = _node(source)

    assert result["stale"] == (
        "Your folder changed since we looked (3 files changed or moved). "
        "Let's re-check so this plan stays accurate. Nothing has been copied yet."
    )
    assert "1 file changed or moved" in result["staleOne"]
    assert result["poll"] == {
        "scanFast": 1500,
        "scanSlow": 4000,
        "applyFast": 1500,
        "proposalsFast": 5000,
        "proposalsSlow": 15000,
    }
    assert result["chips"][0] == {
        "label": "Find my notes on Meeting notes",
        "query": "Meeting notes",
    }
    assert result["chips"][1]["query"] == "Recipes"


def test_state_v2_roundtrips_adopt_params_and_keeps_legacy_urls_byte_identical() -> None:
    # Legacy review URL: view=review is default → byte-identical query string.
    legacy = f"""
      global.window = {{
        location: {{pathname: '/studio/', search: '?mode=activation&state=all&category=relation_debt&ref=exomem%3A%2F%2Freview%2Fstable&panel=evolution'}},
        history: {{pushState: (_s, _t, target) => global.target = target}},
      }};
      const {{readRoute, writeRoute}} = await import({STATE.as_uri()!r});
      const route = readRoute();
      writeRoute(route);
      console.log(JSON.stringify({{route, target: global.target}}));
    """

    legacy_result = _node(legacy)
    assert legacy_result["route"]["mode"] == "activation"
    assert legacy_result["route"]["view"] == "review"
    assert legacy_result["route"]["run"] == ""
    assert legacy_result["route"]["astep"] == "start"
    assert legacy_result["target"] == (
        "/studio/?mode=activation&state=all&category=relation_debt"
        "&ref=exomem%3A%2F%2Freview%2Fstable&panel=evolution"
    )

    # Default review route serializes to a bare path (byte-identical to v1).
    default = f"""
      global.window = {{
        location: {{pathname: '/studio/', search: ''}},
        history: {{pushState: (_s, _t, target) => global.target = target}},
      }};
      const {{readRoute, writeRoute}} = await import({STATE.as_uri()!r});
      writeRoute(readRoute());
      console.log(JSON.stringify({{target: global.target}}));
    """
    assert _node(default)["target"] == "/studio/"

    # Adopt route round-trips view/run/astep.
    adopt = f"""
      global.window = {{
        location: {{pathname: '/studio/', search: '?view=adopt&run=adr-20260714-ab12cd34&astep=choose'}},
        history: {{pushState: (_s, _t, target) => global.target = target}},
      }};
      const {{readRoute, writeRoute}} = await import({STATE.as_uri()!r});
      const route = readRoute();
      writeRoute(route);
      console.log(JSON.stringify({{route, target: global.target}}));
    """
    adopt_result = _node(adopt)
    assert adopt_result["route"]["view"] == "adopt"
    assert adopt_result["route"]["run"] == "adr-20260714-ab12cd34"
    assert adopt_result["route"]["astep"] == "choose"
    assert adopt_result["target"] == (
        "/studio/?view=adopt&run=adr-20260714-ab12cd34&astep=choose"
    )

    # Adopt default astep (start) is omitted from the URL.
    adopt_default = f"""
      global.window = {{
        location: {{pathname: '/studio/', search: '?view=adopt'}},
        history: {{pushState: (_s, _t, target) => global.target = target}},
      }};
      const {{readRoute, writeRoute}} = await import({STATE.as_uri()!r});
      writeRoute(readRoute());
      console.log(JSON.stringify({{target: global.target}}));
    """
    assert _node(adopt_default)["target"] == "/studio/?view=adopt"


def test_contract_findings_view_lines_consequence_and_invalid_flag() -> None:
    source = f"""
      import {{contractFindingsView}} from {MODEL.as_uri()!r};
      const reviewable = contractFindingsView({{
        status: 'proposed',
        reviewed_none_required: true,
        contract_findings: [
          {{code: 'RELATION_DISPOSITION_MISSING', severity: 'error',
            detail: 'This note builds on others but names no typed relation yet.'}},
        ],
      }});
      const invalid = contractFindingsView({{
        status: 'invalid',
        reviewed_none_required: false,
        contract_findings: [
          {{code: 'CONTRACT_BLOCKED', severity: 'error',
            detail: 'The write contract blocks this content.'}},
        ],
      }});
      const clean = contractFindingsView({{status: 'proposed', contract_findings: []}});
      const empty = contractFindingsView(null);
      console.log(JSON.stringify({{reviewable, invalid, clean, empty}}));
    """

    result = _node(source)

    # A reviewable gap: findings show, the reviewed-none consequence is stated,
    # and approval stays enabled (the server refuses only truly invalid ones).
    assert result["reviewable"]["approveDisabled"] is False
    assert result["reviewable"]["hasFindings"] is True
    assert result["reviewable"]["findings"][0]["severity"] == "error"
    assert result["reviewable"]["findings"][0]["text"] == (
        "This note builds on others but names no typed relation yet."
    )
    assert result["reviewable"]["consequence"] == (
        "Approving records this as reviewed with no typed relation yet — "
        "it will come back for relation review."
    )

    # An invalid proposal: findings show, but approval is disabled and there is
    # no reviewed-none consequence to state (nothing can be approved).
    assert result["invalid"]["approveDisabled"] is True
    assert result["invalid"]["hasFindings"] is True
    assert result["invalid"]["findings"][0]["text"] == (
        "The write contract blocks this content."
    )
    assert result["invalid"]["consequence"] == ""

    # No findings → nothing to render; a missing context is inert.
    assert result["clean"]["hasFindings"] is False
    assert result["clean"]["consequence"] == ""
    assert result["clean"]["approveDisabled"] is False
    assert result["empty"]["findings"] == []
    assert result["empty"]["hasFindings"] is False
    assert result["empty"]["approveDisabled"] is False


def test_contract_findings_view_invalid_fallback_to_generic_findings() -> None:
    source = f"""
      import {{contractFindingsView}} from {MODEL.as_uri()!r};
      // Invalid + no contract_findings, but generic findings explain the block.
      const withGeneric = contractFindingsView({{
        status: 'invalid',
        contract_findings: [],
        findings: [
          {{code: 'VALIDATION_FAILED', path: 'content',
            detail: 'This proposal was blocked before it could be applied.'}},
        ],
      }});
      // Invalid + nothing at all → one fixed generic explanation line.
      const bothEmpty = contractFindingsView({{
        status: 'invalid', contract_findings: [], findings: [],
      }});
      // A generic finding carrying only a code (no detail) shows the code text.
      const codeOnly = contractFindingsView({{
        status: 'invalid', contract_findings: [],
        findings: [{{code: 'PROPOSAL_INVALID', path: 'x'}}],
      }});
      // A reviewable (non-invalid) proposal with empty contract_findings must NOT
      // pull in generic findings — there is nothing to render.
      const reviewableEmpty = contractFindingsView({{
        status: 'proposed', contract_findings: [],
        findings: [{{code: 'X', detail: 'should not show'}}],
      }});
      console.log(JSON.stringify({{withGeneric, bothEmpty, codeOnly, reviewableEmpty}}));
    """

    result = _node(source)

    # Invalid with empty contract_findings falls back to the generic findings.
    assert result["withGeneric"]["approveDisabled"] is True
    assert result["withGeneric"]["hasFindings"] is True
    assert result["withGeneric"]["findings"][0]["text"] == (
        "This proposal was blocked before it could be applied."
    )

    # Both empty → one fixed generic explanation, approval still disabled.
    assert result["bothEmpty"]["approveDisabled"] is True
    assert result["bothEmpty"]["hasFindings"] is True
    assert result["bothEmpty"]["findings"] == [
        {"code": "", "severity": "", "text": "This suggestion can't be applied as written."}
    ]

    # detail-less generic finding falls back to its code as the line text.
    assert result["codeOnly"]["findings"][0]["text"] == "PROPOSAL_INVALID"

    # Non-invalid proposals never fall back to generic findings.
    assert result["reviewableEmpty"]["hasFindings"] is False
    assert result["reviewableEmpty"]["findings"] == []


def test_junk_count_present_zero_map_is_authoritative() -> None:
    source = f"""
      import {{junkCount}} from {MODEL.as_uri()!r};
      console.log(JSON.stringify({{
        presentZero: junkCount({{junk_counts: {{conflict: 0}}}}, 5),
        presentNonZero: junkCount({{junk_counts: {{conflict: 2, dot_trash: 1}}}}, 5),
        emptyMap: junkCount({{junk_counts: {{}}}}, 5),
        absentMap: junkCount({{}}, 4),
        noSummary: junkCount(null, 3),
      }}));
    """

    result = _node(source)

    # A present junk_counts map is authoritative even when it sums to zero;
    # the old `sum || fallback` wrongly fell back to the flagged-row count here.
    assert result["presentZero"] == 0
    assert result["presentNonZero"] == 3
    assert result["emptyMap"] == 0
    # Only an ABSENT map falls back to counting junk-flagged inventory rows.
    assert result["absentMap"] == 4
    assert result["noSummary"] == 3


def test_selection_rules_round_trip_after_resume() -> None:
    source = f"""
      import {{initialSelection, toggleFolder, overrideFile, selectionPayload,
               selectionFromRules}} from {MODEL.as_uri()!r};
      const inventoryPaths = [
        'Docs/Notes/a.md', 'Docs/Notes/b.md', 'Archive/c.md', 'Archive/img.png',
      ];
      let sel = initialSelection([], {{}});
      sel = toggleFolder(sel, 'Archive', false);
      sel = overrideFile(sel, 'Archive/c.md', true);
      sel = overrideFile(sel, 'Docs/Notes/b.md', false);
      const payload = selectionPayload(sel, ['Docs', 'Archive']);
      const revived = selectionFromRules(payload, inventoryPaths);
      const payload2 = selectionPayload(revived, ['Docs', 'Archive']);
      console.log(JSON.stringify({{payload, payload2, revived,
        missing: selectionFromRules(null, inventoryPaths)}}));
    """
    result = _node(source)
    assert result["payload2"] == result["payload"]
    assert result["revived"]["files"] == {"Archive/c.md": True, "Docs/Notes/b.md": False}
    assert result["revived"]["folders"]["Archive"] is False
    assert result["missing"] is None
