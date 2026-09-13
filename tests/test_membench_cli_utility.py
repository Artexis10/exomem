"""membench CLI `utility` subcommand: no spend without --paid."""
from __future__ import annotations

from membench.cli import main


def test_utility_help_lists_paid_flag(capsys):
    with __import__("pytest").raises(SystemExit) as excinfo:
        main(["utility", "run", "--help"])
    assert excinfo.value.code == 0
    assert "--paid" in capsys.readouterr().out


def test_utility_without_paid_refuses_before_any_spend(tmp_path):
    code = main([
        "utility", "run",
        "--output", str(tmp_path / "run"),
        "--seed", "1",
        "--product-root", str(tmp_path / "product"),
        "--python", str(tmp_path / "product" / "fake-python"),
        "--tokenizer-path", str(tmp_path / "tok"),
        "--approval-token", "x",
    ])
    assert code != 0
    assert not (tmp_path / "run").exists()


def test_utility_run_requires_product_root_and_python(tmp_path):
    with __import__("pytest").raises(SystemExit) as excinfo:
        main(["utility", "run", "--output", str(tmp_path / "run"), "--seed", "1"])
    assert excinfo.value.code != 0
    assert not (tmp_path / "run").exists()


import json

import pytest


@pytest.fixture(autouse=True)
def _forbid_backend_construction(monkeypatch):
    """Any metered backend construction in these paths is a spend risk."""
    import lme.metered as metered

    def refuse(*args, **kwargs):
        raise AssertionError("CLI constructed a metered backend")

    monkeypatch.setattr(metered.MeteredOpenAIBackend, "__init__", refuse)
    yield


def test_help_never_constructs_a_backend_or_writes_output(tmp_path, capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["utility", "run", "--help"])
    assert excinfo.value.code == 0
    assert not any(tmp_path.iterdir())


def test_missing_paid_flag_never_constructs_a_backend(tmp_path):
    code = main([
        "utility", "run", "--output", str(tmp_path / "run"), "--seed", "1",
        "--product-root", str(tmp_path), "--python", str(tmp_path / "python"),
        "--tokenizer-path", str(tmp_path / "tok"), "--approval-token", "operator-approved",
    ])
    assert code == 2
    assert not (tmp_path / "run").exists()


def test_cli_exposes_no_unsafe_overrides(capsys):
    with pytest.raises(SystemExit):
        main(["utility", "run", "--help"])
    text = capsys.readouterr().out
    for unsafe in ("--api-key", "--backend", "--identity", "--skip-gate", "--no-gate",
                   "--family", "--cell-factory", "--variants"):
        assert unsafe not in text, unsafe


def test_approval_token_help_states_it_is_not_a_credential(capsys):
    with pytest.raises(SystemExit):
        main(["utility", "run", "--help"])
    text = capsys.readouterr().out.lower()
    assert "approval" in text and "never an api key" in text


def test_reader_subcommand_refuses_a_pending_family(tmp_path, capsys):
    from pathlib import Path

    from tests.test_membench_utility_report import PENDING_UTILITY_REVISION, _make_run

    run_dir, _product = _make_run(tmp_path, contract_revision=PENDING_UTILITY_REVISION)
    repo_root = Path(__file__).resolve().parents[1]
    code = main(["utility", "read", "--run", str(run_dir), "--product-root", str(repo_root)])
    assert code == 2
    assert "pending" in capsys.readouterr().err.lower()


def test_run_subcommand_refuses_real_pending_family_without_a_traceback(tmp_path, capsys, monkeypatch):
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    import membench.utility.runner as runner
    from protocol.contracts import derive_preregistration_identity
    from tests.test_membench_utility_report import PENDING_UTILITY_REVISION

    pending = derive_preregistration_identity(root, contract_revision=PENDING_UTILITY_REVISION)
    monkeypatch.setattr(runner, "_default_identity_provider", lambda product_root: pending)
    code = main(["utility", "run", "--output", str(tmp_path / "run"), "--seed", "11",
                 "--product-root", str(root), "--python", sys.executable,
                 "--tokenizer-path", str(root / "pyproject.toml"), "--paid",
                 "--approval-token", "instrument-test"])
    assert code == 2
    assert "pending" in capsys.readouterr().err.lower()
    assert not (tmp_path / "run").exists()


def test_reader_subcommand_reports_recomputed_outcomes(tmp_path, capsys, monkeypatch):

    import membench.utility.runner as runner
    from tests.test_membench_utility_report import _make_run

    run_dir, product_root = _make_run(tmp_path)
    monkeypatch.setattr(runner, "_default_identity_validator", lambda identity, root: None)
    monkeypatch.setattr(runner, "_default_report_family_gate", lambda identity, families: None)
    monkeypatch.setattr(runner, "_default_fixture_gate", lambda family_id, root: None)
    code = main(["utility", "read", "--run", str(run_dir), "--product-root", str(product_root),
                 "--allow-synthetic"])
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["synthetic"] is True
    assert payload["scores"]["helpful_history"]["pair"]["attempted"] == 1
