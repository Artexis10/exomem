"""Doctor reports the remote owner binding without ever printing the id.

Four states (unset, active, mismatch, malformed) on `env.EXOMEM_OWNER_OAUTH_SUBJECT`,
and a warning when policy rules or grants name the audience the bound remote
sign-in used to have, because those stop applying to it. Synthetic ids only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_governance_egress import SCOPE_ID, _gov_dir, _reset_caches, write_rule, write_scope

from exomem import doctor as doctor_module
from exomem.governance.principal import normalize_audience

BASE_URL = "https://kb.example.com"
ALLOWED_ID = "4242"
OTHER_ID = "7171"
LOGIN = "example-owner"
FORMER_AUDIENCE = normalize_audience(subject=ALLOWED_ID, issuer=BASE_URL)
CHECK_ID = "env.EXOMEM_OWNER_OAUTH_SUBJECT"
FORMER_ID = "governance.remote_owner_former_audience"


@pytest.fixture
def remote_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    monkeypatch.setenv("EXOMEM_BASE_URL", BASE_URL)
    monkeypatch.setenv("GITHUB_CLIENT_ID", "test-client-id")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "test-client-secret")
    monkeypatch.setenv("EXOMEM_GITHUB_USERNAME", LOGIN)
    monkeypatch.setenv("EXOMEM_GITHUB_USER_ID", ALLOWED_ID)
    monkeypatch.setenv("EXOMEM_JWT_SIGNING_KEY", "test-signing-key")
    monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT", raising=False)
    return monkeypatch


def _binding_check(monkeypatch: pytest.MonkeyPatch, value: str | None):
    if value is None:
        monkeypatch.delenv("EXOMEM_OWNER_OAUTH_SUBJECT", raising=False)
    else:
        monkeypatch.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", value)
    checks = {check.id: check for check in doctor_module._check_remote_env()}
    return checks[CHECK_ID]


def _assert_content_free(check) -> None:
    text = f"{check.message} {check.remediation or ''} {check.details or ''}"
    assert ALLOWED_ID not in text
    assert OTHER_ID not in text
    assert LOGIN not in text


def test_unset_binding_passes_and_says_how_to_enable_it(
    remote_env: pytest.MonkeyPatch,
) -> None:
    check = _binding_check(remote_env, None)
    assert check.status == "pass"
    assert "separate non-owner principal" in check.message
    assert "EXOMEM_OWNER_OAUTH_SUBJECT=github:" in (check.remediation or "")
    assert "EXOMEM_GITHUB_USER_ID" in (check.remediation or "")
    _assert_content_free(check)


def test_active_binding_passes_and_names_the_remote_label(
    remote_env: pytest.MonkeyPatch,
) -> None:
    check = _binding_check(remote_env, f"github:{ALLOWED_ID}")
    assert check.status == "pass"
    assert "act as the owner" in check.message
    assert "remote" in check.message
    _assert_content_free(check)


def test_a_binding_for_another_account_fails_as_unreachable(
    remote_env: pytest.MonkeyPatch,
) -> None:
    check = _binding_check(remote_env, f"github:{OTHER_ID}")
    assert check.status == "fail"
    assert "can never apply" in check.message
    _assert_content_free(check)


@pytest.mark.parametrize(
    "value",
    ["GitHub:4242", "github:04242", "github:٤٢٤٢", LOGIN, "user@example.com"],
)
def test_a_malformed_binding_fails_and_is_treated_as_unset(
    remote_env: pytest.MonkeyPatch, value: str
) -> None:
    check = _binding_check(remote_env, value)
    assert check.status == "fail"
    assert "Treated as unset" in check.message
    assert "github:<numeric id>" in check.message
    _assert_content_free(check)


def _name_the_former_audience(vault: Path) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0, audience=FORMER_AUDIENCE)
    grant = _gov_dir(vault) / "grants" / "former.yaml"
    grant.parent.mkdir(parents=True, exist_ok=True)
    grant.write_text(
        "governance_version: 1\nid: 01ARZ3NDEKTSV4RRFFQ69G5FC9\nkind: standing\n"
        f'scope_ids: ["{SCOPE_ID}"]\naudience: {FORMER_AUDIENCE}\nceiling: 3\n',
        encoding="utf-8",
    )
    _reset_caches()


def test_active_binding_warns_about_rules_naming_the_former_audience(
    vault: Path, remote_env: pytest.MonkeyPatch
) -> None:
    _name_the_former_audience(vault)
    remote_env.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{ALLOWED_ID}")
    checks = doctor_module._check_remote_owner_former_audience(vault)
    assert [check.id for check in checks] == [FORMER_ID]
    assert checks[0].status == "warn"
    assert checks[0].message.startswith("2 rules/grants name your former remote audience")
    assert checks[0].details == {"rules": 1, "grants": 1}
    _assert_content_free(checks[0])


def test_unset_binding_previews_the_rules_that_would_stop_applying(
    vault: Path, remote_env: pytest.MonkeyPatch
) -> None:
    """The owner reads this before enabling the binding."""
    _name_the_former_audience(vault)
    checks = doctor_module._check_remote_owner_former_audience(vault)
    assert [check.id for check in checks] == [FORMER_ID]
    assert checks[0].status == "pass"
    assert "would stop applying" in checks[0].message
    _assert_content_free(checks[0])


@pytest.mark.parametrize("value", [f"github:{OTHER_ID}", "github:01"])
def test_no_former_audience_check_for_a_binding_that_cannot_apply(
    vault: Path, remote_env: pytest.MonkeyPatch, value: str
) -> None:
    _name_the_former_audience(vault)
    remote_env.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", value)
    assert doctor_module._check_remote_owner_former_audience(vault) == []


def test_no_former_audience_check_when_no_rule_names_it(
    vault: Path, remote_env: pytest.MonkeyPatch
) -> None:
    write_scope(vault)
    write_rule(vault, ceiling=0, audience="external")
    _reset_caches()
    remote_env.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{ALLOWED_ID}")
    assert doctor_module._check_remote_owner_former_audience(vault) == []
    assert doctor_module._check_remote_owner_former_audience(None) == []


def test_the_remote_profile_runs_both_checks(
    vault: Path, remote_env: pytest.MonkeyPatch
) -> None:
    _name_the_former_audience(vault)
    remote_env.setenv("EXOMEM_OWNER_OAUTH_SUBJECT", f"github:{ALLOWED_ID}")
    checks = {
        check.id: check
        for check in doctor_module.doctor(vault=str(vault), profile="remote").checks
    }
    assert checks[CHECK_ID].status == "pass"
    assert checks[FORMER_ID].status == "warn"
