"""Governance policy loader: strict-YAML parse, fingerprint, empty fast path.

Mirrors `test_access.py`'s coverage of `access._load_config`/`policy_fingerprint`
(same fingerprint-then-recompile shape), plus the kernel-specific conflicted-copy
refusal (design decision D3).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from exomem.governance import policy


def _write(vault: Path, kind: str, name: str, text: str) -> Path:
    p = vault / "Knowledge Base" / "_Governance" / kind / f"{name}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


_SCOPE_A = (
    "governance_version: 1\n"
    "id: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
    "name: client-confidential\n"
    "paths: [\"Projects/AcmeCo/**\"]\n"
)

_RULE_A = (
    "governance_version: 1\n"
    "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
    "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
    "audience: external\n"
    "ceiling: 2\n"
)


def test_empty_dir_yields_empty_singleton(vault: Path) -> None:
    (vault / "Knowledge Base" / "_Governance").mkdir(parents=True, exist_ok=True)
    pol = policy.load(vault)
    assert pol is policy.EMPTY_POLICY
    assert pol.empty is True
    assert pol.fingerprint == "missing"


def test_missing_dir_yields_empty_singleton(vault: Path) -> None:
    pol = policy.load(vault)
    assert pol is policy.EMPTY_POLICY
    assert pol.empty is True


def test_valid_scope_and_rule_compile_clean(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    pol = policy.load(vault)
    assert pol.empty is False
    assert not pol.findings
    assert set(pol.scopes) == {"01ARZ3NDEKTSV4RRFFQ69G5FAV"}
    scope = pol.scopes["01ARZ3NDEKTSV4RRFFQ69G5FAV"]
    assert scope.name == "client-confidential"
    assert scope.paths == ("Projects/AcmeCo/**",)
    assert len(pol.rules) == 1
    rule = pol.rules[0]
    assert rule.audience == "external"
    assert rule.ceiling == 2
    assert rule.scope_ids == ("01ARZ3NDEKTSV4RRFFQ69G5FAV",)


def test_unknown_field_is_a_compile_error_and_keeps_last_good(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    _write(vault, "rules", "acmeco-external", _RULE_A)
    good = policy.load(vault)
    assert good.empty is False

    bad_rule = _RULE_A + "unexpected_field: true\n"
    _write(vault, "rules", "acmeco-external", bad_rule)
    refused = policy.load(vault)

    assert refused.scopes == good.scopes
    assert refused.rules == good.rules
    assert any(f["code"] == "unknown_field" for f in refused.findings)
    assert any(f["severity"] == "error" for f in refused.findings)


def test_bad_ceiling_is_a_compile_error(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    bad_rule = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\n"
        "ceiling: 9\n"
    )
    _write(vault, "rules", "acmeco-external", bad_rule)
    pol = policy.load(vault)
    assert any(f["code"] == "invalid_ceiling" for f in pol.findings)
    assert not pol.rules


def test_cold_start_compile_error_is_blocked_not_open(vault: Path) -> None:
    """No prior good compile exists (this is the FIRST load ever for this
    vault): a compile-error refusal must not be conflated with `EMPTY_POLICY`
    (which every caller treats as "no governance, fully open"). A cold-start
    refusal is the distinct `.blocked` fail-closed floor."""
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    bad_rule = (
        "governance_version: 1\n"
        "id: 01ARZ3NDEKTSV4RRFFQ69G5FB0\n"
        "scope_ids: [\"01ARZ3NDEKTSV4RRFFQ69G5FAV\"]\n"
        "audience: external\n"
        "ceiling: 9\n"  # plain typo — out of range
    )
    _write(vault, "rules", "acmeco-external", bad_rule)

    pol = policy.load(vault)  # first-ever load for this vault: no prior good

    assert pol.empty is False
    assert pol.blocked is True
    assert pol.fingerprint == "blocked"
    assert not pol.scopes
    assert not pol.rules
    assert any(f["code"] == "invalid_ceiling" for f in pol.findings)


def test_cold_start_conflicted_copy_is_blocked_not_open(vault: Path) -> None:
    """Same cold-start distinction, for the conflicted-copy refusal path."""
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    conflict = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (conflicted copy 2026-01-01).yaml"
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")

    pol = policy.load(vault)  # first-ever load: the conflict is there from the start

    assert pol.empty is False
    assert pol.blocked is True
    assert not pol.scopes
    assert any(f["code"] == "conflicted_copy" for f in pol.findings)


def test_unrecognized_file_is_a_warning_not_a_refusal(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    stray = vault / "Knowledge Base" / "_Governance" / "notes.txt"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text("not a policy document", encoding="utf-8")
    pol = policy.load(vault)
    assert pol.empty is False
    assert set(pol.scopes) == {"01ARZ3NDEKTSV4RRFFQ69G5FAV"}
    assert any(f["code"] == "unknown_file" and f["severity"] == "warning" for f in pol.findings)


def test_fingerprint_stable_across_reparse_and_changes_on_timestamp_preserving_replace(
    vault: Path,
) -> None:
    """Fingerprint analogue of `test_access.py:111`: content-hash based, so a
    same-mtime replacement (bytes changed, mtime explicitly restored) still
    invalidates — a bare stat comparison alone would miss it."""
    path = _write(vault, "scopes", "acmeco", _SCOPE_A)
    first = policy.load(vault)
    again = policy.load(vault)
    assert first.fingerprint == again.fingerprint
    assert first is again  # unchanged signature -> cache hit, same object

    old_mtime = path.stat().st_mtime
    path.write_text(_SCOPE_A + "tags: [\"extra\"]\n", encoding="utf-8")
    os.utime(path, (old_mtime, old_mtime))

    changed = policy.load(vault)
    assert changed.fingerprint != first.fingerprint
    assert changed.scopes["01ARZ3NDEKTSV4RRFFQ69G5FAV"].tags == ("extra",)


def test_conflicted_copy_sibling_refuses_compile(vault: Path) -> None:
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    good = policy.load(vault)
    assert good.empty is False

    conflict = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (conflicted copy 2026-01-01).yaml"
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")

    refused = policy.load(vault)
    assert refused.scopes == good.scopes
    assert any(f["code"] == "conflicted_copy" for f in refused.findings)
    # A prior good compile exists: it stays in effect (D3) — NOT the cold-start
    # `.blocked` floor, since there's a real policy to fall back on.
    assert refused.empty is False
    assert refused.blocked is False
    assert refused.fingerprint == good.fingerprint

    # Resolving the conflict (deleting the sibling) lets the next load recompile clean.
    conflict.unlink()
    time.sleep(0.01)
    recovered = policy.load(vault)
    assert recovered.empty is False
    assert not recovered.findings


def test_conflicted_copy_marker_is_case_insensitive(vault: Path) -> None:
    """A capital-C 'Conflicted copy' must be refused as a conflict, not
    silently parsed as a second scope document (which would misreport as
    `duplicate_id` instead of the real problem)."""
    _write(vault, "scopes", "acmeco", _SCOPE_A)
    conflict = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (Conflicted copy 2026-01-01).yaml"
    )
    conflict.write_text(_SCOPE_A, encoding="utf-8")

    pol = policy.load(vault)  # cold start: both files present from the first load

    assert pol.blocked is True
    assert any(f["code"] == "conflicted_copy" for f in pol.findings)
    assert not any(f["code"] == "duplicate_id" for f in pol.findings)


def test_dir_of_only_conflicted_copies_is_blocked_not_empty(vault: Path) -> None:
    """When every file in `_Governance/` matches the conflicted-copy marker,
    `_iter_policy_files` recognizes none of them — but that must surface as
    the fail-closed `.blocked` state (with a `conflicted_copy` finding), not
    silently resolve to `EMPTY_POLICY` as if no policy existed at all."""
    stray = (
        vault
        / "Knowledge Base"
        / "_Governance"
        / "scopes"
        / "acmeco (conflicted copy 2026-01-01).yaml"
    )
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_text(_SCOPE_A, encoding="utf-8")

    pol = policy.load(vault)

    assert pol.empty is False
    assert pol.blocked is True
    assert any(f["code"] == "conflicted_copy" for f in pol.findings)


# --------------------------------------------------------------------------
# DEFECT A — a scope that cannot select non-markdown items
# --------------------------------------------------------------------------


def test_scope_without_a_path_selector_warns_that_media_is_uncovered(
    vault: Path,
) -> None:
    """A tag/type/class scope cannot cover a sidecar-less binary.

    `board-call.mp4` beside a `tags: [confidential]` note comes back at
    DISCLOSURE_MAX while the tagged `.md` is withheld, because a binary has no
    frontmatter to match and no sidecar to borrow it from. The SEMANTICS are
    deliberately unchanged — changing them would make an unreadable file
    guess — but a control that fails closed everywhere else should not leave
    an authoring foot-gun silent, so the compile says so.
    """
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "confidential.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Confidential\ntags: ["confidential"]\n',
        encoding="utf-8",
    )
    pol = policy.load(vault)
    media_findings = [
        f for f in pol.findings if f.get("code") == "SCOPE_CANNOT_SELECT_MEDIA"
    ]
    assert media_findings, f"no media-coverage finding in {pol.findings}"
    assert media_findings[0]["severity"] == "warning"
    assert "paths" in media_findings[0]["detail"]
    # A warning must not refuse the compile.
    assert pol.blocked is False
    assert "01ARZ3NDEKTSV4RRFFQ69G5FAV" in pol.scopes


def test_scope_with_a_path_selector_emits_no_media_warning(vault: Path) -> None:
    gov = vault / "Knowledge Base" / "_Governance"
    (gov / "scopes").mkdir(parents=True, exist_ok=True)
    (gov / "scopes" / "patterns.yaml").write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FAV\n"
        'name: Patterns\npaths: ["Notes/Patterns/**"]\ntags: ["confidential"]\n',
        encoding="utf-8",
    )
    pol = policy.load(vault)
    assert not [
        f for f in pol.findings if f.get("code") == "SCOPE_CANNOT_SELECT_MEDIA"
    ]
