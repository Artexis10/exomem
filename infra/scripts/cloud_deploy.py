#!/usr/bin/env python3
"""The cloud-deploy workflow's logic (automate-cloud-releases D1, D2, D3).

After a release, set its cell image as Substrate's `cell_image`, but only when:

1. the image's attestation verifies as the release workflow, on the release tag,
   of the repository that built the image (named by the image's own repository);
2. the rollout is not paused;
3. the newest successful platform Deployment carries the release's chart and
   controller build inputs (the platform gate);
4. no barrier entry at or below the release is uncrossed.

Then poll Substrate's bounded rollout view until the canary runs the target.

Once it has read the release and the rollout view, and before any later stop, every run
also reports two alerts: the fleet has run an older release for too long, and cells are
pinned to an image other than the fleet's. A run that ends on a failing stop with no alert
of its own sends `cloud-deploy-failing`. The workflow runs daily, so a missed dispatch, an
unowned platform upgrade or a pinned cell is reported every day.

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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
GATE_FILE = Path(__file__).with_name("cloud_deploy_gate.json")
BARRIER_FILE = ROOT / "infra/contracts/upgrade-barriers-v1.json"
# Cell image repository (newest first) -> the repository whose release workflow built it.
# The set is closed because it lists the repositories that ever built these images; an
# image outside it is refused.
CELL_IMAGE_BUILDERS = {
    "ghcr.io/substrate-systems/evermatter": "substrate-systems/evermatter",
    "ghcr.io/artexis10/exomem": "Artexis10/exomem",
}
RELEASE_WORKFLOW = ".github/workflows/release-please.yml"
PLATFORM_ENVIRONMENT = "cloud-platform"
ROLLOUT_PATH = "/api/exomem/admin/cloud-release/rollout"
RELEASE_PATH = "/api/exomem/admin/cloud-release"
ALERT_JOB = "cloud-deploy"
PLATFORM_NEEDED_ALERT = "cloud-release-platform-needed"
BARRIER_ALERT = "cloud-release-barrier"
RELEASE_BEHIND_ALERT = "cloud-release-behind"
CELL_PINNED_ALERT = "cloud-cell-pinned"
FAILING_ALERT = "cloud-deploy-failing"
# A release gets two days for its rollout and any platform upgrade it needs before the fleet counts as behind.
RELEASE_BEHIND_DEFAULT_HOURS = 48
# The failing stops that name their cause in an alert of their own, so `cloud-deploy-failing` stays quiet for them.
PAUSED_STOP = "ROLLOUT_PAUSED"
# cellctl raises cloud-rollout-paused for a pause, so the pause stop counts as having its own alert.
OWN_ALERT_STOPS = (PLATFORM_NEEDED_ALERT, BARRIER_ALERT, PAUSED_STOP)
# The off switch: a run without the deploy token does nothing, and sends no alert.
NOT_CONFIGURED = "CLOUD_DEPLOY_NOT_CONFIGURED"
# Substrate's two answers that say the release is not newer than the fleet: nothing to do.
NOT_NEWER = ("CELL_IMAGE_SEEN", "CELL_RELEASE_OLDER")
HTTP_TIMEOUT = 30
# The newest platform Deployments one run reads to find the newest success. Each failed
# upgrade adds one unsuccessful Deployment; this many in a row needs a person anyway.
PLATFORM_SCAN_LIMIT = 20
# The newest releases one run reads to find the oldest one the fleet lacks (GitHub's page maximum).
RELEASE_SCAN_LIMIT = 100

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
class Release:
    tag: str
    published_at: datetime | None  # None: GitHub gave a time that is missing, unparseable or without a zone


def release_behind_hours(env: dict[str, str]) -> int:
    """CLOUD_DEPLOY_RELEASE_BEHIND_HOURS as a positive whole number. Like the poll settings, a bad value
    stops the run at start with a traceback, and an unset one takes the default."""

    hours = int(env.get("CLOUD_DEPLOY_RELEASE_BEHIND_HOURS") or RELEASE_BEHIND_DEFAULT_HOURS)
    if hours < 1:
        raise ValueError("CLOUD_DEPLOY_RELEASE_BEHIND_HOURS must be a positive whole number")
    try:
        timedelta(hours=hours)
    except OverflowError:
        raise ValueError("CLOUD_DEPLOY_RELEASE_BEHIND_HOURS is too large") from None
    return hours


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
    release_behind_after: timedelta

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
            release_behind_after=timedelta(hours=release_behind_hours(env)),
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
        try:
            return json.loads(body)
        except ValueError as error:
            raise Stop("GITHUB_API_FAILED", ok=False, detail="not JSON") from error

    def latest_release(self) -> str:
        release = self._get(f"/repos/{self.settings.repository}/releases/latest")
        tag = release.get("tag_name") if isinstance(release, dict) else None
        if not isinstance(tag, str) or parse_release(tag) is None:
            raise Stop("RELEASE_NOT_FOUND", ok=False)
        return tag

    def first_release_after(self, fleet: tuple[int, int, int]) -> Release | None:
        """The oldest published release newer than `fleet`, or None when the newest releases
        hold none. One read, newest first. When the fleet is older than every release on the
        page, the oldest one on it stands in, so the age it gives is a lower bound."""

        releases = self._get(f"/repos/{self.settings.repository}/releases?per_page={RELEASE_SCAN_LIMIT}")
        newer = []
        for release in releases if isinstance(releases, list) else []:
            if not isinstance(release, dict) or release.get("draft") or release.get("prerelease"):
                continue  # `releases/latest` skips both, so the fleet is never behind one
            tag = release.get("tag_name")
            version = parse_release(tag) if isinstance(tag, str) else None
            if version is not None and version > fleet:
                newer.append((version, release))
        if not newer:
            return None
        _, oldest = min(newer, key=lambda found: found[0])
        return Release(oldest["tag_name"], self._published_at(oldest.get("published_at")))

    @staticmethod
    def _published_at(value: object) -> datetime | None:
        try:
            published = datetime.fromisoformat(value) if isinstance(value, str) else None
        except ValueError:
            return None
        return published.astimezone(UTC) if published and published.tzinfo else None

    def newest_platform_deployment(self) -> dict | None:
        """The newest successful platform Deployment's ref and payload, or None when the
        environment has none. GitHub lists Deployments newest first, and a Deployment's
        state is its newest status, which GitHub also lists first. The read stops at the
        first success, so a run costs a few calls however many upgrades were recorded."""

        repository = self.settings.repository
        deployments = self._get(f"/repos/{repository}/deployments?environment={PLATFORM_ENVIRONMENT}"
                                f"&per_page={PLATFORM_SCAN_LIMIT}")
        if not isinstance(deployments, list):
            raise Stop("PLATFORM_DEPLOYMENT_UNREADABLE", ok=False)
        for deployment in deployments:
            statuses = self._get(f"/repos/{repository}/deployments/{deployment['id']}/statuses?per_page=1")
            if not (statuses and statuses[0].get("state") == "success"):
                continue
            payload = deployment.get("payload")
            try:
                if isinstance(payload, str):
                    payload = json.loads(payload or "{}")
            except ValueError:
                raise Stop("PLATFORM_DEPLOYMENT_UNREADABLE", ok=False) from None
            return {"ref": deployment.get("ref"), "payload": payload if isinstance(payload, dict) else {}}
        if len(deployments) >= PLATFORM_SCAN_LIMIT:
            # More unsuccessful Deployments than one read covers: an older success may
            # exist, so the run fails visibly rather than reading the platform as absent.
            raise Stop("PLATFORM_DEPLOYMENT_SCAN_LIMIT", ok=False, detail=str(PLATFORM_SCAN_LIMIT))
        return None


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
    # -z: without it git quotes a non-ASCII path, which then matches no platform glob.
    result = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", "--no-renames", "-z", base, head],
                            capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise Stop("PLATFORM_DIFF_FAILED", ok=False)
    return [name for name in result.stdout.split("\0") if name]


def uncrossed_barriers(release: str, deployment: dict, barriers: dict) -> list[str]:
    """Entries at or below the release that the newest successful platform Deployment
    does not list. Each Deployment lists every entry crossed so far (the runbook carries
    the previous list forward), so the newest one is the whole record."""

    target = parse_release(release)
    if target is None:
        raise Stop("RELEASE_NOT_FOUND", ok=False)
    listed = deployment["payload"].get("barriers_crossed")
    crossed = {entry for entry in listed if isinstance(entry, str)} if isinstance(listed, list) else set()
    # An entry whose version cannot be read stops the run rather than being crossed unseen.
    return [entry["id"] for entry in barriers["barriers"]
            if ((version := parse_release(entry["version"])) is None or version <= target)
            and entry["id"] not in crossed]


def attestation_repository(image: str) -> str:
    """The repository that built `image` (`<repository>@<digest>`), from its image repository."""

    builder = CELL_IMAGE_BUILDERS.get(image.partition("@")[0])
    if builder is None:
        raise Stop("CELL_IMAGE_REPOSITORY_UNKNOWN", ok=False)
    return builder


def verify_attestation(image: str, tag: str) -> bool:
    """`gh attestation verify`: the signer is the builder's release workflow, run on the tag."""

    repository = attestation_repository(image)
    result = subprocess.run(
        ["gh", "attestation", "verify", f"oci://{image}", "--repo", repository,
         "--signer-workflow", f"{repository}/{RELEASE_WORKFLOW}", "--source-ref", f"refs/tags/{tag}"],
        capture_output=True, text=True, check=False)
    return result.returncode == 0


def registry_image(tag: str) -> str | None:
    """The cell image index (`<repository>@<digest>`) that the release published as `<version>-cloud`,
    under the newest repository that holds it; a release cut before the move is in the old one."""

    sys.path.insert(0, str(Path(__file__).parent))
    from release_recovery import image_digest

    for repository in CELL_IMAGE_BUILDERS:
        digest = image_digest(f"{repository}:{tag.removeprefix('v')}-cloud")
        if digest:
            return f"{repository}@{digest}"
    return None


@dataclass
class Tools:
    """What a run reads and calls. Tests replace each with recorded answers."""

    transport: Transport = urllib_transport
    image: Callable[[str], str | None] = registry_image
    attest: Callable[[str, str], bool] = verify_attestation
    changed_paths: Callable[[str, str], list[str]] = git_changed_paths
    sleep: Callable[[float], None] = time.sleep
    monotonic: Callable[[], float] = time.monotonic
    now: Callable[[], datetime] = lambda: datetime.now(UTC)


def report_release_behind(settings: Settings, tools: Tools, github: GitHub, tag: str, image: str, view: dict) -> bool:
    """Send `cloud-release-behind`: active when the fleet has run an older release for longer than
    `settings.release_behind_after`, counted from the publish time of the oldest release newer than
    the fleet's. The newest release's own age would say nothing: releases come every few hours. A fleet on
    the newest release (by digest or by tag, as D1 step 4 reads it), ahead of it, or on a tag
    that is not a release is not behind. An unknown fleet release, or a publish time that
    cannot be read, sends nothing and prints its code. The log shows the hours only. Returns whether
    it reported the fleet as behind."""

    now = tools.now()
    fleet = view.get("release")
    behind = False
    if view.get("cell_image") != image and fleet != tag:
        if fleet is None:
            print("cloud-deploy: RELEASE_BEHIND_UNKNOWN")
            return False
        fleet_version = parse_release(fleet) if isinstance(fleet, str) else None
        newest = parse_release(tag)
        if fleet_version is not None and newest is not None and fleet_version < newest:
            try:
                first_missed = github.first_release_after(fleet_version)
            except (Stop, OSError):
                # The list read only feeds this alert, so its failure must not stop the rollout.
                first_missed = None
            if first_missed is None:
                print("cloud-deploy: RELEASE_BEHIND_UNKNOWN")
                return False
            if first_missed.published_at is None:
                print("cloud-deploy: RELEASE_TIME_UNREADABLE")
                return False
            age = now - first_missed.published_at
            behind = age > settings.release_behind_after
            if behind:
                print(f"cloud-deploy: RELEASE_BEHIND hours={int(age.total_seconds() // 3600)}")
    send_alert(settings, tools.transport, RELEASE_BEHIND_ALERT, behind, now)
    return behind


def report_pinned_cells(settings: Settings, tools: Tools, view: dict) -> None:
    """Send `cloud-cell-pinned`: active when the view counts a cell pinned off the fleet's image.
    A count that is absent (Substrate predates the field) or not a non-negative integer is
    unknown: it sends nothing, and never reads as zero."""

    cells = view.get("cells")
    pinned = cells.get("pinned_off_fleet") if isinstance(cells, dict) else None
    if not isinstance(pinned, int) or isinstance(pinned, bool) or pinned < 0:
        print("cloud-deploy: PINNED_UNKNOWN")
        return
    send_alert(settings, tools.transport, CELL_PINNED_ALERT, pinned > 0, tools.now())
    if pinned:
        print(f"cloud-deploy: CELLS_PINNED {pinned}")


def report_run_outcome(settings: Settings, tools: Tools, stop: Stop) -> None:
    """Send `cloud-deploy-failing`: active when the run ended on a failing stop, resolved when it ended
    ok. A stop with an alert of its own (OWN_ALERT_STOPS) resolves it, since that alert names the cause."""

    failing = not stop.ok and stop.code not in OWN_ALERT_STOPS
    send_alert(settings, tools.transport, FAILING_ALERT, failing, tools.now())


def deploy(settings: Settings, tools: Tools, *, gate: tuple[list[str], list[str]], barriers: dict) -> None:
    if not settings.deploy_token or not settings.substrate_url:
        raise Stop(NOT_CONFIGURED, ok=True)
    if urlsplit(settings.substrate_url).scheme != "https":
        raise Stop("SUBSTRATE_URL_NOT_HTTPS", ok=False)
    github = GitHub(settings, tools.transport)
    substrate = Substrate(settings, tools.transport)

    # D1 steps 1-2: the newest release's cell image, and its attestation.
    tag = github.latest_release()
    image = tools.image(tag)
    if not image:
        raise Stop("CELL_IMAGE_NOT_PUBLISHED", ok=False)
    if not tools.attest(image, tag):
        raise Stop("ATTESTATION_UNVERIFIED", ok=False)

    # Both lag alerts go out as soon as the view is read, so every stop below reports them.
    view = substrate.rollout()
    reported_behind = report_release_behind(settings, tools, github, tag, image, view)
    report_pinned_cells(settings, tools, view)

    # D1 step 3: never resume a paused fleet; only a person clears the pause. This comes
    # first, so a re-run for the release the rollout paused on fails instead of passing.
    refuse_paused(view)
    # D1 step 4: already the fleet's image, by digest or by release tag, before any
    # gate, so a release the fleet runs never raises a platform alert.
    if view.get("cell_image") == image or view.get("release") == tag:
        raise Stop("CELL_IMAGE_CURRENT", ok=True)

    # D2: the platform gate.
    deployment = github.newest_platform_deployment()
    include, exclude = gate
    ref = deployment["ref"] if deployment else None
    if deployment is None or not isinstance(ref, str):
        send_alert(settings, tools.transport, PLATFORM_NEEDED_ALERT, True, tools.now())
        raise Stop(PLATFORM_NEEDED_ALERT, ok=False, detail="NO_PLATFORM_DEPLOYMENT")
    changed = platform_inputs(tools.changed_paths(ref, tag), include, exclude)
    if changed:
        send_alert(settings, tools.transport, PLATFORM_NEEDED_ALERT, True, tools.now())
        raise Stop(PLATFORM_NEEDED_ALERT, ok=False, detail=f"{len(changed)} platform input(s) changed")
    send_alert(settings, tools.transport, PLATFORM_NEEDED_ALERT, False, tools.now())

    # D3: every barrier entry at or below the release must be crossed.
    uncrossed = uncrossed_barriers(tag, deployment, barriers)
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
    # The fleet now targets the newest release, so the lag this run reported is over.
    if reported_behind:
        send_alert(settings, tools.transport, RELEASE_BEHIND_ALERT, False, tools.now())

    # D1 step 7: wait until the canary runs the target and is Ready.
    deadline = tools.monotonic() + settings.poll_timeout
    while True:
        view = substrate.rollout()
        refuse_paused(view)
        if view.get("canary_on_target") is True:
            raise Stop("CANARY_ON_TARGET", ok=True)
        if tools.monotonic() >= deadline:
            raise Stop("CANARY_WAIT_TIMEOUT", ok=False)
        tools.sleep(settings.poll_interval)


def refuse_paused(view: dict) -> None:
    """Stop with the pause's code, and nothing else, when the rollout view says it is paused."""

    if view.get("paused") is True:
        raise Stop(PAUSED_STOP, ok=False, detail=_code(view.get("error_code")))


def finish(stop: Stop) -> int:
    """Print the single line a stopped run shows, a code and the detail it chose to show, and return its exit code."""

    print(f"cloud-deploy: {stop.code}" + (f" {stop.detail}" if stop.detail else ""))
    return 0 if stop.ok else 1


def _code(value: object) -> str:
    """A pause's error code as the log may print it: upper-case letters, digits and underscores."""

    return value if isinstance(value, str) and value.isascii() and value.replace("_", "").isalnum() else "UNKNOWN"


def main(env: dict[str, str] | None = None, tools: Tools | None = None) -> int:
    settings = Settings.from_env(dict(os.environ) if env is None else env)
    tools = tools or Tools()
    try:
        deploy(settings, tools, gate=load_gate(), barriers=json.loads(BARRIER_FILE.read_text(encoding="utf-8")))
    except Stop as stop:
        if stop.code != NOT_CONFIGURED:
            report_run_outcome(settings, tools, stop)
        return finish(stop)
    return 0


if __name__ == "__main__":
    sys.exit(main())
