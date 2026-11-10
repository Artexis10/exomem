#!/usr/bin/env python3
"""The cloud-deploy workflow's logic (automate-cloud-releases D1, D2, D3).

After a release, set its cell image as Substrate's `cell_image`, but only when:

1. the image's attestation verifies as this repository's release workflow on
   the release tag;
2. the rollout is not paused;
3. the newest successful platform Deployment carries the release's chart and
   controller build inputs (the platform gate);
4. no barrier entry at or below the release is uncrossed.

Then poll Substrate's bounded rollout view until the canary runs the target.

The repository's Actions logs are public, so this prints only codes and counts:
never a cell ID, a tenant ID, a URL or a secret. It reads only the bounded
rollout view, and it never sends anything but `cellImage` and `release`, so it
cannot clear a pause or pin one cell's image.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
GATE_FILE = Path(__file__).with_name("cloud_deploy_gate.json")
BARRIER_FILE = ROOT / "infra/contracts/upgrade-barriers-v1.json"
CELL_IMAGE_REPOSITORY = "ghcr.io/artexis10/exomem"
RELEASE_WORKFLOW = ".github/workflows/release-please.yml"
PLATFORM_ENVIRONMENT = "cloud-platform"
ROLLOUT_PATH = "/api/exomem/admin/cloud-release/rollout"
RELEASE_PATH = "/api/exomem/admin/cloud-release"
ALERT_JOB = "cloud-deploy"
PLATFORM_NEEDED_ALERT = "cloud-release-platform-needed"
BARRIER_ALERT = "cloud-release-barrier"
# Substrate's two answers that say the release is not newer than the fleet: nothing to do.
NOT_NEWER = ("CELL_IMAGE_SEEN", "CELL_RELEASE_OLDER")
HTTP_TIMEOUT = 30

Transport = Callable[[str, str, dict[str, str], bytes | None], tuple[int, bytes]]


class Stop(Exception):
    """The run ends here. `ok` says whether it succeeded; `code` is all the log says."""

    def __init__(self, code: str, *, ok: bool, detail: str = "") -> None:
        super().__init__(code)
        self.code, self.ok, self.detail = code, ok, detail


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def urllib_transport(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def parse_release(tag: str) -> tuple[int, int, int] | None:
    # A release tag's shape, as the release workflow publishes it (vMAJOR.MINOR.PATCH).
    parts = tag.removeprefix("v").split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


@dataclass(frozen=True)
class Settings:
    repository: str
    github_api: str
    github_token: str
    substrate_url: str
    deploy_token: str
    alert_url: str
    poll_interval: float
    poll_timeout: float

    @classmethod
    def from_env(cls, env: dict[str, str]) -> Settings:
        return cls(
            repository=env["GITHUB_REPOSITORY"],
            github_api=env.get("GITHUB_API_URL", "https://api.github.com"),
            github_token=env["GH_TOKEN"],
            substrate_url=env.get("EXOMEM_SUBSTRATE_URL", "").rstrip("/"),
            deploy_token=env.get("EXOMEM_DEPLOY_TOKEN", ""),
            alert_url=env.get("EXOMEM_CLOUD_DEPLOY_ALERT_URL", ""),
            poll_interval=float(env.get("CLOUD_DEPLOY_POLL_INTERVAL_SECONDS") or 30),
            poll_timeout=float(env.get("CLOUD_DEPLOY_POLL_TIMEOUT_SECONDS") or 3600),
        )


class GitHub:
    def __init__(self, settings: Settings, transport: Transport) -> None:
        self.settings, self.transport = settings, transport

    def _get(self, path: str) -> Any:
        status, body = self.transport("GET", f"{self.settings.github_api}{path}", {
            "Authorization": f"Bearer {self.settings.github_token}",
            "Accept": "application/vnd.github+json",
        }, None)
        if status != 200:
            raise Stop("GITHUB_API_FAILED", ok=False, detail=str(status))
        return json.loads(body)

    def latest_release(self) -> str:
        release = self._get(f"/repos/{self.settings.repository}/releases/latest")
        tag = release.get("tag_name") if isinstance(release, dict) else None
        if not isinstance(tag, str) or parse_release(tag) is None:
            raise Stop("RELEASE_NOT_FOUND", ok=False)
        return tag

    def platform_deployments(self) -> list[dict]:
        """Each successful platform Deployment, newest first: its ref and its payload.
        A Deployment's state is its newest status, which GitHub lists first."""

        repository = self.settings.repository
        found = []
        for page in range(1, 11):
            deployments = self._get(
                f"/repos/{repository}/deployments?environment={PLATFORM_ENVIRONMENT}&per_page=100&page={page}")
            for deployment in deployments:
                statuses = self._get(f"/repos/{repository}/deployments/{deployment['id']}/statuses?per_page=1")
                if statuses and statuses[0].get("state") == "success":
                    payload = deployment.get("payload")
                    if isinstance(payload, str):
                        payload = json.loads(payload or "{}")
                    found.append({"ref": deployment.get("ref"), "payload": payload if isinstance(payload, dict) else {}})
            if len(deployments) < 100:
                break
        return found


class Substrate:
    def __init__(self, settings: Settings, transport: Transport) -> None:
        self.settings, self.transport = settings, transport

    def _call(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        headers = {"Authorization": f"Bearer {self.settings.deploy_token}", "Accept": "application/json"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body, separators=(",", ":")).encode()
        try:
            status, raw = self.transport(method, f"{self.settings.substrate_url}{path}", headers, data)
        except OSError as error:
            raise Stop("SUBSTRATE_UNREACHABLE", ok=False) from error
        try:
            answer = json.loads(raw) if raw else {}
        except ValueError:
            answer = {}
        return status, answer if isinstance(answer, dict) else {}

    @staticmethod
    def _error(status: int, answer: dict) -> str:
        error = answer.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        return code if isinstance(code, str) and code.isascii() and code.replace("_", "").isalnum() else f"HTTP_{status}"

    def rollout(self) -> dict:
        status, answer = self._call("GET", ROLLOUT_PATH)
        view = answer.get("view")
        if status != 200 or not isinstance(view, dict):
            raise Stop(f"SUBSTRATE_{self._error(status, answer)}", ok=False)
        return view

    def set_image(self, image: str, release: str) -> str | None:
        """None when Substrate set it, else Substrate's error code."""

        status, answer = self._call("PUT", RELEASE_PATH, {"cellImage": image, "release": release})
        return None if status == 200 and answer.get("success") is True else self._error(status, answer)


def send_alert(settings: Settings, transport: Transport, alert: str, active: bool, now: datetime) -> None:
    """One transition to Substrate's alert receiver, through the cloud-deploy capability.
    A delivery failure is printed as a code and does not change the run's outcome."""

    if not settings.alert_url:
        return
    parsed = urlsplit(settings.alert_url)
    if parsed.scheme != "https" or not parsed.hostname:
        print("cloud-deploy: ALERT_URL_INVALID")
        return
    transition = {"job": ALERT_JOB, "alert": alert, "active": active}
    transition_id = hashlib.sha256(json.dumps({**transition, "observed_at": now.isoformat()},
                                              separators=(",", ":"), sort_keys=True).encode()).hexdigest()
    body = json.dumps({"schema_version": 1, "transition_id": transition_id, **transition},
                      separators=(",", ":"), sort_keys=True).encode()
    try:
        status, _ = transport("POST", settings.alert_url, {
            "Content-Type": "application/json", "Accept": "application/json",
            "X-Exomem-Alert-Transition": transition_id}, body)
    except OSError:
        status = 0
    if not 200 <= status < 300:
        print(f"cloud-deploy: ALERT_DELIVERY_FAILED {alert}")


def load_gate(path: Path = GATE_FILE) -> tuple[list[str], list[str]]:
    gate = json.loads(path.read_text(encoding="utf-8"))
    return list(gate["include"]), list(gate["exclude"])


def platform_inputs(paths: list[str], include: list[str], exclude: list[str]) -> list[str]:
    """The changed paths that are chart or controller build inputs. The globs are the
    repository's own layout, a closed set it fixes; `*` matches across `/` (fnmatch)."""

    return [path for path in paths
            if any(fnmatch.fnmatchcase(path, glob) for glob in include)
            and not any(fnmatch.fnmatchcase(path, glob) for glob in exclude)]


def git_changed_paths(base: str, head: str) -> list[str]:
    result = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", "--no-renames", base, head],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Stop("PLATFORM_DIFF_FAILED", ok=False)
    return [line for line in result.stdout.splitlines() if line]


def uncrossed_barriers(release: str, deployments: list[dict], barriers: dict) -> list[str]:
    """Entries at or below the release that no successful platform Deployment crossed."""

    target = parse_release(release)
    if target is None:
        raise Stop("RELEASE_NOT_FOUND", ok=False)
    crossed: set[str] = set()
    for deployment in deployments:
        listed = deployment["payload"].get("barriers_crossed")
        if isinstance(listed, list):
            crossed.update(entry for entry in listed if isinstance(entry, str))
    # An entry whose version cannot be read stops the run rather than being crossed unseen.
    return [entry["id"] for entry in barriers["barriers"]
            if ((version := parse_release(entry["version"])) is None or version <= target)
            and entry["id"] not in crossed]


def verify_attestation(repository: str, image: str, tag: str) -> bool:
    """`gh attestation verify`: the signer is this repository's release workflow, run on the tag."""

    result = subprocess.run(
        ["gh", "attestation", "verify", f"oci://{image}", "--repo", repository,
         "--signer-workflow", f"{repository}/{RELEASE_WORKFLOW}", "--source-ref", f"refs/tags/{tag}"],
        capture_output=True, text=True, check=False)
    return result.returncode == 0


def registry_digest(tag: str) -> str | None:
    """The cell image index that the release published as `<version>-cloud`."""

    sys.path.insert(0, str(Path(__file__).parent))
    from release_recovery import image_digest

    return image_digest(f"{CELL_IMAGE_REPOSITORY}:{tag.removeprefix('v')}-cloud")


@dataclass
class Tools:
    """What a run reads and calls. Tests replace each with recorded answers."""

    transport: Transport = urllib_transport
    digest: Callable[[str], str | None] = registry_digest
    attest: Callable[[str, str, str], bool] = verify_attestation
    changed_paths: Callable[[str, str], list[str]] = git_changed_paths
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def deploy(settings: Settings, tools: Tools, *, gate: tuple[list[str], list[str]], barriers: dict) -> None:
    if not settings.deploy_token or not settings.substrate_url:
        raise Stop("CLOUD_DEPLOY_NOT_CONFIGURED", ok=True)
    if urlsplit(settings.substrate_url).scheme != "https":
        raise Stop("SUBSTRATE_URL_NOT_HTTPS", ok=False)
    github = GitHub(settings, tools.transport)
    substrate = Substrate(settings, tools.transport)

    # D1 steps 1-2: the newest release's cell image, and its attestation.
    tag = github.latest_release()
    digest = tools.digest(tag)
    if not digest:
        raise Stop("CELL_IMAGE_NOT_PUBLISHED", ok=False)
    image = f"{CELL_IMAGE_REPOSITORY}@{digest}"
    if not tools.attest(settings.repository, image, tag):
        raise Stop("ATTESTATION_UNVERIFIED", ok=False)

    # D1 step 3: already the fleet's image, by digest or by release tag, before any
    # gate, so a release the fleet runs never raises a platform alert.
    view = substrate.rollout()
    if view.get("cell_image") == image or view.get("release") == tag:
        raise Stop("CELL_IMAGE_CURRENT", ok=True)
    # D1 step 4: never resume a paused fleet; only a person clears the pause.
    if view.get("paused") is True:
        raise Stop("ROLLOUT_PAUSED", ok=False, detail=_code(view.get("error_code")))

    # D2: the platform gate.
    deployments = github.platform_deployments()
    include, exclude = gate
    changed = (platform_inputs(tools.changed_paths(deployments[0]["ref"], tag), include, exclude)
               if deployments and isinstance(deployments[0]["ref"], str) else None)
    if changed is None or changed:
        send_alert(settings, tools.transport, PLATFORM_NEEDED_ALERT, True, tools.now())
        raise Stop(PLATFORM_NEEDED_ALERT, ok=False, detail="NO_PLATFORM_DEPLOYMENT" if changed is None
                   else f"{len(changed)} platform input(s) changed")
    send_alert(settings, tools.transport, PLATFORM_NEEDED_ALERT, False, tools.now())

    # D3: every barrier entry at or below the release must be crossed.
    uncrossed = uncrossed_barriers(tag, deployments, barriers)
    if uncrossed:
        send_alert(settings, tools.transport, BARRIER_ALERT, True, tools.now())
        raise Stop(BARRIER_ALERT, ok=False, detail=" ".join(uncrossed))
    send_alert(settings, tools.transport, BARRIER_ALERT, False, tools.now())

    # D1 step 6: set the image with its verified tag, and nothing else.
    refused = substrate.set_image(image, tag)
    if refused in NOT_NEWER:
        raise Stop(refused, ok=True)
    if refused is not None:
        raise Stop(f"SUBSTRATE_{refused}", ok=False)
    print("cloud-deploy: CELL_IMAGE_SET")

    # D1 step 7: wait until the canary runs the target and is Ready.
    deadline = tools.monotonic() + settings.poll_timeout
    while True:
        view = substrate.rollout()
        if view.get("paused") is True:
            raise Stop("ROLLOUT_PAUSED", ok=False, detail=_code(view.get("error_code")))
        if view.get("canary_on_target") is True:
            raise Stop("CANARY_ON_TARGET", ok=True)
        if tools.monotonic() >= deadline:
            raise Stop("CANARY_WAIT_TIMEOUT", ok=False)
        tools.sleep(settings.poll_interval)


def _code(value: object) -> str:
    """A pause's error code as the log may print it: upper-case letters, digits and underscores."""

    return value if isinstance(value, str) and value.isascii() and value.replace("_", "").isalnum() else "UNKNOWN"


def main(env: dict[str, str] | None = None, tools: Tools | None = None) -> int:
    settings = Settings.from_env(dict(os.environ) if env is None else env)
    try:
        deploy(settings, tools or Tools(), gate=load_gate(),
               barriers=json.loads(BARRIER_FILE.read_text(encoding="utf-8")))
    except Stop as stop:
        print(f"cloud-deploy: {stop.code}" + (f" {stop.detail}" if stop.detail else ""))
        return 0 if stop.ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
