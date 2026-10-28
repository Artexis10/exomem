"""Exercise the CI package boundary without privilege, network, or package writes."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def install_on_fake_runner(tmp_path):
    commands = tmp_path / "bin"
    commands.mkdir()
    trace = tmp_path / "commands.jsonl"
    shim = commands / "fake-command"
    shim.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
name, args = Path(sys.argv[0]).name, sys.argv[1:]
with Path(os.environ["COMMAND_TRACE"]).open("a") as stream:
    stream.write(json.dumps([name, *args]) + "\\n")
state = Path(os.environ["PACKAGE_STATE"])
if name == "sudo":
    os.execvp(args[0], args)
if name == "timeout":
    os.execvp(args[2], args[2:])
if name == "dpkg-query":
    print("installed" if (state / "installed").exists() else "not-installed")
elif name == "apt-get":
    if "update" in args:
        sys.exit(int(os.environ.get("UPDATE_STATUS", "0")))
    if "--download-only" in args:
        status = int(os.environ.get("DOWNLOAD_STATUS", "0"))
        if status == 0:
            (state / "downloaded").touch()
        sys.exit(status)
    if "--no-download" in args:
        if not (state / "downloaded").exists():
            sys.exit(98)
        status = int(os.environ.get("INSTALL_STATUS", "0"))
        if status == 0:
            (state / "installed").touch()
        sys.exit(status)
    # A combined acquisition/configuration call recreates the deadline defect.
    sys.exit(99)
elif name != "sleep":
    sys.exit(97)
''')
    shim.chmod(0o755)
    # This is the helper's closed process boundary; no privileged command runs.
    for name in ("sudo", "timeout", "dpkg-query", "apt-get", "sleep"):
        (commands / name).symlink_to(shim)

    def run(**statuses):
        environment = {**os.environ, **statuses, "PATH": f"{commands}:{os.environ['PATH']}",
                       "COMMAND_TRACE": str(trace), "PACKAGE_STATE": str(tmp_path)}
        result = subprocess.run(["bash", str(ROOT / "scripts/ci-apt-install.sh"), "age"],
                                env=environment, capture_output=True, text=True, check=False)
        calls = [json.loads(line) for line in trace.read_text().splitlines()]
        return result, calls

    return run


def test_package_acquisition_has_its_own_deadline_before_configuration(install_on_fake_runner):
    """A nearly exhausted download allowance must leave a fresh dpkg allowance."""
    result, calls = install_on_fake_runner()
    assert result.returncode == 0, result.stderr
    installs = [call for call in calls if call[0] == "apt-get" and "install" in call]
    assert len(installs) == 2
    assert "--download-only" in installs[0]
    assert "--no-download" in installs[1]
    bounded_installs = [call for call in calls if call[0] == "timeout" and "install" in call]
    assert len(bounded_installs) == 2
    assert all(int(call[2]) > 0 for call in bounded_installs)


@pytest.mark.parametrize("failed_phase", ["download", "install"])
def test_a_failed_package_phase_never_replays_configuration(install_on_fake_runner, failed_phase):
    """A failed fetch cannot install; an interrupted install has an uncertain outcome."""
    result, calls = install_on_fake_runner(**{f"{failed_phase.upper()}_STATUS": "124"})
    assert result.returncode == 124
    configurations = [call for call in calls if call[0] == "apt-get" and "--no-download" in call]
    assert len(configurations) == (failed_phase == "install")


def test_every_ci_caller_allows_the_helpers_complete_deadline(install_on_fake_runner):
    """A caller must allow download, configuration, retry backoffs, and kill grace."""
    result, calls = install_on_fake_runner(UPDATE_STATUS="124")
    assert result.returncode == 0
    seconds = sum(int(call[2]) + int(call[1].partition("=")[2])
                  for call in calls if call[0] == "timeout")
    seconds += sum(int(call[1]) for call in calls if call[0] == "sleep")
    callers = []
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text())
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                # This fixed executable path identifies the repository's helper protocol.
                if "scripts/ci-apt-install.sh" in step.get("run", ""):
                    callers.append((path.name, step["name"]))
                    assert step["timeout-minutes"] * 60 > seconds, (path.name, step["name"], seconds)
                    assert step["timeout-minutes"] < job["timeout-minutes"]
    assert callers
