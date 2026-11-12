#!/usr/bin/env python3
"""One command for a Cloud platform upgrade (automate-cloud-releases D7).

`platform_upgrade.py <tag>` runs in an admin session with the operator's
presence. Its machine review runs to completion before anything changes:

1. export the tag into a private checkout and build the chart dependencies;
2. render the chart against the schema;
3. diff the live manifest against the new render and print the changed
   objects, so the operator can stop a diff that surprises;
4. run a server-side dry run;
5. run the release's `cellctl` schema check against the control database;
6. compare the secret contracts of the live release and the tag — a change
   with no barrier entry stops the command;
7. list the `platform` barriers between the live release and the tag, each
   of which needs a `--crossed <id>`.

Then it applies the namespaces, runs `helm upgrade --wait` (`--atomic`
unless a crossed barrier is `one-way`), waits for `cellctl` and the gateway,
records the platform Deployment, and dispatches `cloud-deploy`.

The live release's tag comes from the Helm release description this command
writes on every upgrade (`platform-v<tag>`); an older platform needs
`--from <tag>` once. The platform Deployment record is never the source,
so a wrong record cannot mislead the command.

Every step's files and renders stay in `$EXOMEM_UPGRADE_DIR`, a private
directory outside the repository, because a render holds the private values.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
BARRIER_FILE = ROOT / "infra/contracts/upgrade-barriers-v1.json"
RELEASE = "exomem-platform"
PLATFORM_NAMESPACE = "exomem-platform"
CLOUD_NAMESPACE = "exomem-cloud"
PLATFORM_ENVIRONMENT = "cloud-platform"
DEPLOY_WORKFLOW = "cloud-deploy.yml"
DEFAULT_REPOSITORY = "Artexis10/exomem"
RELEASE_DESCRIPTION_PREFIX = "platform-"
# The files a release may not change without an operator-step barrier entry:
# the active secret registry, its selection and its destinations.
SECRET_CONTRACT_PATHS = (
    "infra/contracts/active-secret-registry-v1.json",
    "infra/contracts/active-secret-selection-v1.json",
    "infra/contracts/secret-destinations-v1.json",
    "infra/contracts/active-secret-registries",
)
# kubectl wait's bound for cellctl and the gateway after a successful upgrade.
VERIFY_TIMEOUT = "180s"
SCHEMA_DSN_ENV = "EXOMEM_CELLCTL_DSN"


class Stop(Exception):
    """The command ends here, before or at the step it names."""

    def __init__(self, step: str, message: str) -> None:
        super().__init__(message)
        self.step, self.message = step, message


@dataclass(frozen=True)
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


Runner = Callable[[list[str], Path | None, str | None], Completed]


def subprocess_runner(argv: list[str], cwd: Path | None = None, stdin: str | None = None) -> Completed:
    result = subprocess.run(argv, cwd=cwd, input=stdin, capture_output=True, text=True, check=False)
    return Completed(result.returncode, result.stdout, result.stderr)


@dataclass(frozen=True)
class Settings:
    values: Path
    upgrade_dir: Path
    repository: str
    timeout: str

    @classmethod
    def from_env(cls, env: dict[str, str], timeout: str) -> Settings:
        missing = [name for name in ("EXOMEM_PLATFORM_VALUES", "EXOMEM_UPGRADE_DIR") if not env.get(name)]
        if missing:
            raise Stop("settings", f"set {', '.join(missing)} (see docs/runbooks/cloud-platform-deploy.md)")
        return cls(
            values=Path(env["EXOMEM_PLATFORM_VALUES"]),
            upgrade_dir=Path(env["EXOMEM_UPGRADE_DIR"]),
            repository=env.get("EXOMEM_REPOSITORY", DEFAULT_REPOSITORY),
            timeout=timeout,
        )


def parse_release(tag: str) -> tuple[int, int, int] | None:
    parts = tag.removeprefix("v").split(".")
    if len(parts) != 3 or not all(part.isdecimal() for part in parts):
        return None
    return int(parts[0]), int(parts[1]), int(parts[2])


def run(runner: Runner, step: str, argv: list[str], *, cwd: Path | None = None,
        stdin: str | None = None) -> Completed:
    result = runner(argv, cwd, stdin)
    if result.returncode != 0:
        raise Stop(step, f"`{' '.join(argv[:3])} ...` failed: {result.stderr.strip() or result.stdout.strip()}")
    return result


def export_tag(runner: Runner, tag: str, destination: Path) -> None:
    """A clean checkout of the tag under the private upgrade directory."""
    if runner(["git", "rev-parse", "--verify", f"{tag}^{{commit}}"], ROOT, None).returncode != 0:
        run(runner, "checkout", ["git", "fetch", "origin", "tag", tag], cwd=ROOT)
        if runner(["git", "rev-parse", "--verify", f"{tag}^{{commit}}"], ROOT, None).returncode != 0:
            raise Stop("checkout", f"no such tag {tag}")
    # Only this command's own export lives under the upgrade directory.
    if destination.exists():
        shutil.rmtree(destination)
        runner(["git", "worktree", "prune"], ROOT, None)
    run(runner, "checkout",
        ["git", "worktree", "add", "--detach", str(destination), tag], cwd=ROOT)


def helm_chart(checkout: Path) -> Path:
    return checkout / "infra/helm/platform"


def helm_args(settings: Settings, checkout: Path) -> list[str]:
    return [RELEASE, str(helm_chart(checkout)), "--namespace", PLATFORM_NAMESPACE,
            "--values", str(settings.values)]


def render(runner: Runner, step: str, settings: Settings, checkout: Path,
           extra: list[str]) -> str:
    return run(runner, step, ["helm", "template", *helm_args(settings, checkout), *extra]).stdout


def manifest_objects(manifest: str) -> dict[tuple[str, str, str], dict]:
    """Every rendered object by (kind, namespace, name)."""
    objects = {}
    for document in yaml.safe_load_all(manifest):
        if not isinstance(document, dict) or not document.get("kind"):
            continue
        meta = document.get("metadata") or {}
        objects[(document["kind"], str(meta.get("namespace") or ""), str(meta.get("name") or ""))] = document
    return objects


def pod_template(document: dict) -> dict | None:
    spec = document.get("spec") or {}
    if document.get("kind") == "CronJob":
        spec = ((spec.get("jobTemplate") or {}).get("spec") or {})
    template = ((spec.get("template") or {}).get("spec"))
    return template if isinstance(template, dict) else None


def diff_manifests(live: str, new: str) -> list[str]:
    """The added, removed and changed objects, and every workload whose pod
    template changed, for the operator's eyes."""
    before, after = manifest_objects(live), manifest_objects(new)
    lines = []
    for key in sorted(before.keys() | after.keys()):
        kind, namespace, name = key
        label = f"{kind} {namespace + '/' if namespace else ''}{name}"
        if key not in before:
            lines.append(f"added: {label}")
        elif key not in after:
            lines.append(f"removed: {label}")
        elif before[key] != after[key]:
            if pod_template(before[key]) is not None and pod_template(before[key]) != pod_template(after[key]):
                lines.append(f"changed (pod template): {label}")
            else:
                lines.append(f"changed: {label}")
    return lines


def live_release_tag(runner: Runner) -> str | None | bool:
    """The tag this command last upgraded to, from the release description.

    None when no live release exists, False when one does but its description
    does not name the tag -- a platform this command has not upgraded yet."""
    result = runner(["helm", "status", RELEASE, "--namespace", PLATFORM_NAMESPACE, "-o", "json"], None, None)
    if result.returncode != 0:
        return None
    try:
        description = (json.loads(result.stdout).get("info") or {}).get("description") or ""
    except ValueError:
        return False
    match = re.fullmatch(rf"{RELEASE_DESCRIPTION_PREFIX}(v?\d+\.\d+\.\d+)", description.strip())
    return match.group(1) if match else False


def schema_check(runner: Runner, env: dict[str, str], checkout: Path) -> str:
    """The release's own column check when the tag carries it (4.1)."""
    if not (checkout / "infra/cellctl/src/cellctl/schema_check.py").exists():
        return "skipped: the release predates cellctl's schema check"
    if not env.get(SCHEMA_DSN_ENV):
        raise Stop("schema-check", f"{SCHEMA_DSN_ENV} is not set; the check needs the control database DSN")
    run(runner, "schema-check",
        ["uv", "run", "--frozen", "python", "-m", "cellctl.schema_check", "--dsn-env", SCHEMA_DSN_ENV],
        cwd=checkout / "infra/cellctl")
    return "the release's schema check passed"


def secret_contract_changed(runner: Runner, live_tag: str, tag: str) -> list[str]:
    changed = []
    for path in SECRET_CONTRACT_PATHS:
        diff = runner(["git", "diff", "--quiet", live_tag, tag, "--", path], ROOT, None)
        if diff.returncode == 1:
            changed.append(path)
        elif diff.returncode != 0:
            raise Stop("secret-contract", f"cannot compare {path} between {live_tag} and {tag}")
    return changed


def barriers_between(barriers: list[dict], live: tuple[int, int, int] | None,
                     tag: tuple[int, int, int]) -> list[dict]:
    """`platform` entries after the live release and at or below the tag."""
    entries = []
    for entry in barriers:
        if entry.get("scope") != "platform":
            continue
        version = parse_release(str(entry.get("version", "")))
        if version is None or version > tag or (live is not None and version <= live):
            continue
        entries.append(entry)
    return entries


def all_crossed_up_to(barriers: list[dict], tag: tuple[int, int, int]) -> list[str]:
    return sorted(entry["id"] for entry in barriers
                  if (v := parse_release(str(entry.get("version", "")))) is not None and v <= tag)


def gh_api(runner: Runner, step: str, argv: list[str]) -> str:
    return run(runner, step, argv).stdout


def newest_platform_deployment(runner: Runner, repository: str) -> dict | None:
    """The newest successful platform Deployment's payload, or None."""
    deployments = json.loads(gh_api(
        runner, "deployment-record",
        ["gh", "api", f"repos/{repository}/deployments?environment={PLATFORM_ENVIRONMENT}&per_page=20"]))
    for deployment in deployments:
        statuses = json.loads(gh_api(
            runner, "deployment-record",
            ["gh", "api", f"repos/{repository}/deployments/{deployment['id']}/statuses?per_page=1"]))
        if statuses and statuses[0].get("state") == "success":
            detail = json.loads(gh_api(
                runner, "deployment-record",
                ["gh", "api", f"repos/{repository}/deployments/{deployment['id']}"]))
            payload = detail.get("payload") or {}
            if isinstance(payload, str):
                payload = json.loads(payload or "{}")
            return payload if isinstance(payload, dict) else {}
    if len(deployments) >= 20:
        # More unsuccessful records than one read covers: an older success may
        # exist, so the record fails visibly rather than reading it as absent.
        raise Stop("deployment-record", "more than 20 platform Deployments without a success")
    return None


def record_deployment(runner: Runner, settings: Settings, tag: str,
                      crossed: list[str], state: str) -> None:
    body = json.dumps({
        "ref": tag, "environment": PLATFORM_ENVIRONMENT,
        "auto_merge": False, "required_contexts": [],
        "payload": {"barriers_crossed": crossed},
        "description": "Platform upgrade",
    })
    result = run(runner, "deployment-record",
                 ["gh", "api", f"repos/{settings.repository}/deployments", "--input", "-"],
                 stdin=body)
    deployment_id = json.loads(result.stdout)["id"]
    gh_api(runner, "deployment-record",
           ["gh", "api", f"repos/{settings.repository}/deployments/{deployment_id}/statuses",
            "-f", f"state={state}"])


def platform_upgrade(argv: list[str], env: dict[str, str], runner: Runner) -> int:
    try:
        return _platform_upgrade(argv, env, runner)
    except Stop as error:
        print(f"{error.step}: {error.message}", file=sys.stderr)
        return 1


def _platform_upgrade(argv: list[str], env: dict[str, str], runner: Runner) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("tag", help="release tag to upgrade to, for example v0.175.0")
    parser.add_argument("--crossed", action="append", default=[], metavar="ID",
                        help="a platform barrier this upgrade crosses after its runbook step")
    parser.add_argument("--from", dest="from_tag", metavar="TAG",
                        help="the release the platform runs, when the release description does not say")
    parser.add_argument("--timeout", default="10m", help="helm upgrade --timeout (default 10m)")
    args = parser.parse_args(argv)

    if parse_release(args.tag) is None:
        raise Stop("settings", f"{args.tag}: not a release tag")
    if args.from_tag and parse_release(args.from_tag) is None:
        raise Stop("settings", f"--from {args.from_tag}: not a release tag")
    settings = Settings.from_env(env, args.timeout)
    settings.upgrade_dir.mkdir(parents=True, exist_ok=True)
    checkout = settings.upgrade_dir / "checkout"

    # --- The machine review: every failure below stops before any change. ---
    print("review: exporting the tag and building the chart")
    export_tag(runner, args.tag, checkout)
    run(runner, "dependencies",
        ["uv", "run", "--frozen", "python", "infra/scripts/helm_dependencies.py", "infra/helm/platform"],
        cwd=checkout)

    print("review: rendering the release")
    new_manifest = render(runner, "render", settings, checkout, ["--no-hooks"])
    (settings.upgrade_dir / "new.yaml").write_text(new_manifest, encoding="utf-8")

    print("review: diffing against the live release")
    described = live_release_tag(runner)
    live_tag = args.from_tag or (described if isinstance(described, str) else None)
    if described is False and not args.from_tag:
        raise Stop("from", "a live release exists but its description names no tag; "
                           "pass --from <tag> (this command writes it on every upgrade)")
    if isinstance(described, str) and args.from_tag and described != args.from_tag:
        print(f"  note: the release description says {described}; --from says {args.from_tag}")
    if live_tag is None:
        print("  no live release found; first install")
    manifest = runner(["helm", "get", "manifest", RELEASE, "--namespace", PLATFORM_NAMESPACE], None, None)
    if manifest.returncode == 0:
        (settings.upgrade_dir / "live.yaml").write_text(manifest.stdout, encoding="utf-8")
        for line in diff_manifests(manifest.stdout, new_manifest):
            print(f"  {line}")
    elif live_tag is not None:
        raise Stop("review", f"live release is {live_tag} but `helm get manifest` failed")

    print("review: server-side dry run")
    dry_run = run(runner, "dry-run",
                  ["helm", "upgrade", "--install", *helm_args(settings, checkout), "--dry-run=server"])
    (settings.upgrade_dir / "dry-run.yaml").write_text(dry_run.stdout, encoding="utf-8")

    print(f"review: schema check — {schema_check(runner, env, checkout)}")

    live_version = parse_release(live_tag) if live_tag else None
    target_version = parse_release(args.tag)
    assert target_version is not None  # parse_args already refuses a bad tag
    barriers = json.loads((checkout / "infra/contracts/upgrade-barriers-v1.json").read_text())["barriers"]
    if live_tag:
        changed = secret_contract_changed(runner, live_tag, args.tag)
        due = barriers_between(barriers, live_version, target_version)
        if changed and not due:
            raise Stop("secret-contract",
                       f"secret contract changed between {live_tag} and {args.tag} "
                       f"({', '.join(changed)}) with no platform barrier entry")
    else:
        due = []

    if due:
        print("review: platform barriers between the live release and the tag:")
        for entry in due:
            print(f"  {entry['id']} ({entry['kind']}): {entry.get('summary', '')}")
    crossed = set(args.crossed)
    missing = [entry["id"] for entry in due if entry["id"] not in crossed]
    if missing:
        raise Stop("barriers", f"uncrossed platform barriers: {', '.join(missing)} — "
                               "do each runbook step, then pass --crossed <id>")
    extra = crossed - {entry["id"] for entry in due}
    if extra:
        raise Stop("barriers", f"--crossed names no barrier between the live release and the tag: "
                               f"{', '.join(sorted(extra))}")
    one_way = any(entry["kind"] == "one-way" for entry in due)

    # --- The upgrade. ---
    print("upgrade: applying the namespaces")
    namespaces = render(runner, "upgrade", settings, checkout, ["--show-only", "templates/namespaces.yaml"])
    run(runner, "upgrade", ["kubectl", "apply", "-f", "-"], stdin=namespaces)

    upgrade_argv = ["helm", "upgrade", "--install", *helm_args(settings, checkout),
                    "--wait", "--timeout", settings.timeout,
                    "--description", f"{RELEASE_DESCRIPTION_PREFIX}{args.tag}"]
    post_renderer = checkout / "infra/helm/platform/reservation-post-renderer.py"
    if post_renderer.exists():
        upgrade_argv += ["--post-renderer", str(post_renderer)]
    if not one_way:
        upgrade_argv.append("--atomic")
    print(f"upgrade: {' '.join(upgrade_argv[:3])} ... ({'no --atomic: a one-way barrier was crossed' if one_way else '--atomic'})")
    try:
        run(runner, "upgrade", upgrade_argv)
        print("verify: waiting for cellctl and the gateway")
        run(runner, "verify",
            ["kubectl", "--namespace", CLOUD_NAMESPACE, "wait", "--for=condition=Available",
             "deployment/cellctl", "deployment/exomem-cloud-gateway", "--timeout", VERIFY_TIMEOUT])
    except Stop as error:
        print(f"{error.step}: {error.message}")
        try:
            previous = newest_platform_deployment(runner, settings.repository)
            record_deployment(runner, settings, args.tag,
                              list((previous or {}).get("barriers_crossed", [])), "failure")
            print(f"recorded a failed platform Deployment for {args.tag}")
        except Stop as record_error:
            print(f"the failure record itself failed: {record_error.message}")
        return 1

    # --- Record and dispatch. ---
    previous = newest_platform_deployment(runner, settings.repository)
    if previous is None:
        crossed_ids = all_crossed_up_to(barriers, target_version)
    else:
        crossed_ids = sorted(set(previous.get("barriers_crossed", [])) | {entry["id"] for entry in due})
    record_deployment(runner, settings, args.tag, crossed_ids, "success")
    print(f"recorded the platform Deployment for {args.tag}")

    run(runner, "dispatch",
        ["gh", "workflow", "run", DEPLOY_WORKFLOW, "--ref", "main", "-R", settings.repository])
    print(f"dispatched {DEPLOY_WORKFLOW} on main")
    return 0


def main() -> int:
    try:
        os.umask(0o077)
        return platform_upgrade(sys.argv[1:], dict(os.environ), subprocess_runner)
    except Stop as error:
        print(f"{error.step}: {error.message}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
