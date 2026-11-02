"""Install a published exomem release into a new virtual environment for CI.

The compatibility tests and the installed journey run earlier releases. This script
installs one with the dependency versions and hashes from that release's own
uv.lock, so a later upstream release cannot change what those checks run. The
wheel installs from a local file URL with its digest, so its provenance record
names the file and the digest that the journey verifies. It runs on Linux,
macOS and Windows runners.

Usage: ci_install_release.py VERSION VENV WHEEL_SHA256 ENV_NAME
ENV_NAME receives the environment's interpreter path through GITHUB_ENV.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path


def _download(url: str, target: Path) -> None:
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as response:
                target.write_bytes(response.read())
            return
        except OSError:
            if attempt == 3:
                raise
            time.sleep(2**attempt)


def main(version: str, venv: str, sha: str, env_name: str) -> None:
    repository = os.environ.get("GITHUB_REPOSITORY", "Artexis10/exomem")
    # The journey re-reads the wheel that the provenance record names, so the wheel stays in place.
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    work = Path(tempfile.mkdtemp(prefix="exomem-release.", dir=base))

    release = work / "release.json"
    _download(f"https://pypi.org/pypi/exomem/{version}/json", release)
    wheel_url = next(
        entry["url"] for entry in json.loads(release.read_text())["urls"]
        if entry["filename"].endswith(".whl")
    )
    wheel = work / wheel_url.rsplit("/", 1)[-1]
    _download(wheel_url, wheel)
    actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual != sha:
        sys.exit(f"{wheel.name}: SHA-256 {actual} does not match the pinned {sha}")

    project = work / "project"
    project.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        _download(f"https://raw.githubusercontent.com/{repository}/v{version}/{name}", project / name)
    locked = work / "locked.txt"
    # --no-config ignores the tag's uv version pin; the frozen export needs none of its uv settings.
    subprocess.run(
        ["uv", "export", "--quiet", "--no-config", "--frozen", "--no-dev", "--no-emit-project",
         "-o", str(locked)],
        cwd=project, check=True,
    )
    # --require-hashes needs --hash; the #sha256 fragment puts the digest into the provenance record.
    with locked.open("a", encoding="utf-8") as requirements:
        requirements.write(f"exomem @ {wheel.as_uri()}#sha256={sha} --hash=sha256:{sha}\n")

    subprocess.run(["uv", "venv", venv, "--python", "3.13"], check=True)
    python = Path(venv) / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    subprocess.run(
        ["uv", "pip", "install", "--python", str(python), "--require-hashes", "-r", str(locked)],
        check=True,
    )
    with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as github_env:
        github_env.write(f"{env_name}={python}\n")


if __name__ == "__main__":
    if len(sys.argv) != 5:
        sys.exit(__doc__)
    main(*sys.argv[1:])
