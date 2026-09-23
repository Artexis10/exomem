"""D1-T7: `exomem dreamer` writes the operator setting; there is no run-once."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from exomem import __main__ as cli
from exomem import dreamer


def _config() -> dict:
    path = Path(os.environ["EXOMEM_CONFIG_PATH"])
    return json.loads(path.read_text("utf-8")) if path.exists() else {}


@pytest.fixture(autouse=True)
def _clean():
    dreamer.reset_for_tests()
    yield
    dreamer.reset_for_tests()


def test_on_off_pause_resume_write_the_config_key(capsys) -> None:
    Path(os.environ["EXOMEM_CONFIG_PATH"]).write_text(json.dumps({"mode": "quiet"}), "utf-8")
    for command, stored in (
        ("on", "on"),
        ("pause", "paused"),
        ("resume", "on"),
        ("off", "off"),
    ):
        assert cli.main(["dreamer", command]) == 0
        config = _config()
        assert config["dreamer"] == stored
        # Other operator settings in the shared file are preserved.
        assert config["mode"] == "quiet"
        assert dreamer.setting() == stored
    capsys.readouterr()


def test_status_prints_the_worker_status(capsys, tmp_path: Path) -> None:
    assert cli.main(["dreamer", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["setting"] == "off"
    assert payload["state"] == "off"
    vault = tmp_path / "vault"
    vault.mkdir()
    assert cli.main(["dreamer", "status", "--json", "--vault", str(vault)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["sidecar"] is None
    assert str(vault) not in json.dumps(payload)


def test_there_is_no_run_once_subcommand(capsys) -> None:
    with pytest.raises(SystemExit) as exited:
        cli.main(["dreamer", "run-once"])
    assert exited.value.code == 2
    with pytest.raises(SystemExit):
        cli.main(["dreamer", "run"])


def test_the_environment_kill_switch_is_reported(capsys, monkeypatch) -> None:
    assert cli.main(["dreamer", "on"]) == 0
    monkeypatch.setenv("EXOMEM_DREAMER", "off")
    assert cli.main(["dreamer", "status", "--json"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert json.loads(lines[-1])["setting"] == "off"
