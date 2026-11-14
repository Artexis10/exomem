"""Explicit packet profiles reach the recording worker and the full plugin server."""

from __future__ import annotations

import json
import subprocess
from functools import partial
from pathlib import Path
from types import SimpleNamespace

import pytest
from import_guidance import recording, runner, session_close
from import_guidance.__main__ import main
from import_guidance.recording import Arm
from import_guidance.scripted_replies import load_scenario

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("packet", [None, "anchors"])
def test_recording_pins_only_the_requested_profile_and_refuses_a_different_cache(
    tmp_path, monkeypatch, packet
):
    """A shell export cannot choose the recording profile, or make a differently pinned cache reusable."""
    worker = tmp_path / "worker.py"
    worker.write_text(
        "import json, os, sys\nfrom pathlib import Path\n"
        "Path(sys.argv[3]).write_text(json.dumps({'shapes': {}, 'seen': "
        "{k: v for k, v in os.environ.items() if k.startswith('EXOMEM_')}}))\n"
    )
    scenario_path = tmp_path / "scenario.json"
    scenario_path.write_text(json.dumps({
        "id": "profile", "title": "Profile probe", "prompt": "A request",
        "replies": {}, "default_reply": {},
        "served_tools": ["recall_context"], "record": {"steps": [{"tool": "recall_context"}]},
    }))
    scenario = load_scenario(scenario_path)
    monkeypatch.setattr(recording, "WORKER", worker)
    monkeypatch.setattr(recording, "checkout_sha", lambda root: "same-product")
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_VAULT_PATH", "/outside/vault")
    out = tmp_path / "run"
    target = out / "shapes" / "test" / "profile.json"
    arm = Arm("test", ROOT)
    assert recording.record(scenario, arm, target, tmp_path / "scratch", activation_packet=packet) == []
    recorded = recording.load_recording(target)
    assert recorded["seen"] == ({"EXOMEM_ACTIVATION_PACKET": packet} if packet else {})
    assert recorded.get("activation_packet") == packet
    assert recording.record(scenario, arm, target, tmp_path / "scratch", activation_packet=packet) == []
    problems = recording.record(scenario, arm, target, tmp_path / "scratch", activation_packet="maps")
    assert problems and "activation_packet" in problems[0] and "delete it" in problems[0]
    assert recording.load_recording(target) == recorded
    monkeypatch.setattr(runner, "load_scenarios", lambda *a: [scenario])
    monkeypatch.setattr(runner, "discover_agent_envelope", lambda: SimpleNamespace(executable=Path("unused"), version="test"))
    stream = "\n".join(json.dumps(event) for event in (
        {"type": "system", "subtype": "init", "mcp_servers": [{"name": "exomem", "status": "connected"}]},
        {"type": "result", "subtype": "success", "result": "An answer"},
    ))
    monkeypatch.setattr(runner, "run_once", partial(runner.run_once, run=lambda *a: subprocess.CompletedProcess([], 0, stream, "")))
    args = ["run", "--product-root", f"test={ROOT}", "--model", "unused", "--out", str(out)]
    assert main(args + (["--activation-packet", packet] if packet else [])) == 0
    assert json.loads((out / "runs.jsonl").read_text()).get("activation_packet") == packet


@pytest.mark.parametrize("packet", [None, "anchors"])
def test_full_plugin_run_pins_the_server_and_records_the_selected_profile(tmp_path, monkeypatch, packet):
    """The full session's server must use the requested profile, while outside settings stay scrubbed."""
    monkeypatch.setenv("EXOMEM_ACTIVATION_PACKET", "maps")
    monkeypatch.setenv("EXOMEM_UNRELATED_SETTING", "outside")
    seed = tmp_path / "seed"
    seed.mkdir()
    case = session_close.Case("probe", "Profile probe", seed, "balanced", ("A request",), tmp_path / "case.json")
    monkeypatch.setattr(session_close, "load_cases", lambda *a, **k: [case])
    monkeypatch.setattr(session_close, "discover_agent_envelope", lambda: SimpleNamespace(executable=Path("unused"), version="test"))

    def seed_vault(case, arm, work, env, ready_graph):
        (work / "vault").mkdir()

    def drive(argv, cwd, env, turns, stream_path, hook_turn=None):
        config = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text())
        server_env = config["mcpServers"]["exomem"]["env"]
        assert server_env.get("EXOMEM_ACTIVATION_PACKET") == packet
        assert "EXOMEM_UNRELATED_SETTING" not in server_env
        stream = json.dumps({"type": "system", "subtype": "init", "mcp_servers": [{"name": "exomem", "status": "connected"}]}) + "\n"
        stream += json.dumps({"type": "result", "subtype": "success", "result": "An answer"}) + "\n"
        stream_path.write_text(stream)
        return SimpleNamespace(stdout=stream, stderr="", returncode=0, stdout_eof=True, stopped=None, finished=1, hook_turn=None)

    monkeypatch.setattr(session_close, "seed_vault", seed_vault)
    monkeypatch.setattr(session_close, "drive_session", drive)
    out = tmp_path / "run"
    args = ["run", "--suite", "session-close", "--product-root", f"test={ROOT}", "--model", "unused", "--out", str(out)]
    assert main(args + (["--activation-packet", packet] if packet else [])) == 0
    row = json.loads((out / "runs.jsonl").read_text())
    assert row.get("activation_packet") == packet
    dry = tmp_path / "dry"
    assert session_close.dry_run(case, Arm("test", ROOT), dry, activation_packet=packet) == []
    server = json.loads((dry / "plugin" / "test-probe" / "mcp.json").read_text())
    assert server["mcpServers"]["exomem"]["env"].get("EXOMEM_ACTIVATION_PACKET") == packet


@pytest.mark.parametrize("suite", ["import", "session-close"])
def test_dry_run_forwards_the_explicit_profile_without_starting_an_actor(tmp_path, monkeypatch, suite):
    """Dry runs must check the same requested profile as scored runs."""
    item = SimpleNamespace(id="probe", record={})
    monkeypatch.setattr(runner, "load_scenarios", lambda *a: [item])
    monkeypatch.setattr(session_close, "load_cases", lambda *a, **k: [item])
    seen = []

    def dry(*args, **kwargs):
        seen.append(kwargs["activation_packet"])
        return ["probe stops before actor preview"]

    monkeypatch.setattr(runner, "dry_run", dry)
    monkeypatch.setattr(session_close, "dry_run", dry)
    assert main(["run", "--suite", suite, "--product-root", f"test={ROOT}", "--model", "unused", "--out", str(tmp_path / "run"), "--dry-run", "--activation-packet", "anchors"]) == 1
    assert seen == ["anchors"]
