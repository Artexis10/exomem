#!/usr/bin/env python3
"""Record authentic Full evidence, select matching trees and refresh release checks."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import zipfile
from pathlib import Path

# These names and events are the repository's closed Actions/release protocol.
WORKFLOW = ".github/workflows/ci.yml"
AUTOMATION = ".github/workflows/release-evidence-automation.yml"
RELEASE_BRANCH = "release-please--branches--main"
FULL_EVENTS = ("workflow_dispatch", "schedule")
# Full's required package-build job checks lock, Cloud output and committed plugin content/version.
PACKAGE_CHECKS = "release-packages-v1"
# Release PR checks take about 2 minutes after a re-run; 20 minutes bounds a stuck one.
CHECK_POLLS, CHECK_POLL_SECONDS = 60, 20


def ci_scope(event_name: str, event: dict) -> dict[str, bool]:
    """One policy owns release-PR targeting and the scheduled/manual Full scope."""
    pr = event.get("pull_request", {})
    # GitHub's PR schema fixes these types; a malformed payload cannot name the release PR.
    if event_name == "pull_request" and (
        not isinstance(pr, dict) or not isinstance(pr.get("head"), dict)
    ):
        raise ValueError("pull request scope requires the GitHub PR schema")
    base = pr.get("base", {}).get("ref", "")
    head = pr.get("head", {}).get("ref", "")
    release = event_name == "pull_request" and (
        head == RELEASE_BRANCH
        or (base.startswith("release-cuts/") and head == f"release-please--branches--{base}")
    )
    return {"full": event_name in FULL_EVENTS, "release": release}


def event_payload() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    return json.loads(Path(path).read_text()) if path else {}


def command(*args: str, env: dict | None = None, cwd: Path | None = None) -> bytes:
    return subprocess.check_output(args, env=env, cwd=cwd, timeout=180)


def api(path: str, *, pages: bool = False):
    args = ["gh", "api", f"repos/{os.environ['GH_REPO']}/{path}"]
    if pages:
        args.extend(("--paginate", "--slurp"))
    return json.loads(command(*args))


def runs(commit: str) -> list[dict]:
    return [
        run
        for page in api(f"actions/workflows/ci.yml/runs?head_sha={commit}&per_page=100", pages=True)
        for run in page["workflow_runs"]
    ]


def trusted_full(run: dict, repository: str, workflow_id: int) -> bool:
    return (
        run.get("event") in FULL_EVENTS
        and run.get("path") == WORKFLOW
        and run.get("workflow_id") == workflow_id
        and (run.get("repository") or {}).get("full_name") == repository
        and (run.get("head_repository") or {}).get("full_name") == repository
    )


def legacy_workflow(commit: str) -> bool:
    """Old source has no receipt writer; new missing receipts must use full fallback."""
    root = api(f"git/trees/{commit}")["tree"]
    scripts = next((entry for entry in root if entry["path"] == "scripts"), None)
    return scripts is None or not any(
        entry["path"] == Path(__file__).name for entry in api(f"git/trees/{scripts['sha']}")["tree"]
    )


def read_receipt(run: dict, repository: str) -> dict | None:
    run_id, latest_attempt = str(run["id"]), int(run["run_attempt"])
    prefix = f"full-ci-evidence-{run_id}-"
    artifacts = [
        artifact
        for page in api(f"actions/runs/{run_id}/artifacts?per_page=100", pages=True)
        for artifact in page["artifacts"]
        if artifact["name"].startswith(prefix)
    ]
    if not artifacts:
        return None
    for artifact in reversed(artifacts):
        try:
            # Artifact names encode the issuing attempt, not the successful retry.
            attempt = int(artifact["name"].removeprefix(prefix))
            identity = artifact.get("workflow_run") or {}
            if (
                artifact["expired"]
                or not 1 <= attempt <= latest_attempt
                or identity.get("id") != run["id"]
                or identity.get("head_sha") != run["head_sha"]
            ):
                raise ValueError("Full evidence artifact identity is unavailable or mismatched")
            issuer = api(f"actions/runs/{run_id}/attempts/{attempt}")
            if (
                issuer["id"] != run["id"]
                or issuer["run_attempt"] != attempt
                or issuer["head_sha"] != run["head_sha"]
                or not trusted_full(issuer, repository, run["workflow_id"])
            ):
                raise ValueError("Full evidence issuing attempt differs")
            gates = [
                job
                for page in api(
                    f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=30", pages=True
                )
                for job in page["jobs"]
                if job["name"] == "required CI gate" and job["conclusion"] == "success"
            ]
            if (
                len(gates) != 1
                or gates[0]["started_at"] < issuer["run_started_at"]
                or not gates[0]["started_at"] <= artifact["created_at"] <= gates[0]["completed_at"]
            ):
                raise ValueError("Full receipt has no successful issuing gate")
            archive = command(
                "gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"
            )
            if (
                artifact.get("digest")
                and artifact["digest"] != "sha256:" + hashlib.sha256(archive).hexdigest()
            ):
                raise ValueError("Full evidence archive digest differs")
            with zipfile.ZipFile(io.BytesIO(archive)) as files:
                receipt = json.loads(files.read("full-ci-evidence.json"))
            if not isinstance(receipt, dict):
                raise ValueError("Full evidence receipt must be an object")
            expected = {
                "repository": repository,
                "workflow": WORKFLOW,
                "run_id": run_id,
                "run_attempt": str(attempt),
                "event": run["event"],
                "scope": "full",
            }
            if any(receipt.get(key) != value for key, value in expected.items()):
                raise ValueError("Full evidence receipt does not match its producer")
            tested = api(f"commits/{receipt['tested_commit']}")
            if tested["commit"]["tree"]["sha"] != receipt.get("tested_tree"):
                raise ValueError("Full receipt tree differs from its tested commit")
            if receipt["tested_commit"] != run["head_sha"]:
                raise ValueError("Full receipt commit differs from its producer")
            return {
                **receipt,
                "artifact_id": artifact["id"],
                "successful_attempt": str(latest_attempt),
            }
        except (
            subprocess.CalledProcessError,
            ValueError,
            KeyError,
            TypeError,
            zipfile.BadZipFile,
        ) as error:
            print(
                f"artifact {artifact['id']} cannot supply Full evidence: {error}", file=sys.stderr
            )
    raise ValueError("no authentic Full receipt remains for this run")


def evidence_sources(source: str, repository: str) -> tuple[str, set[str]]:
    commit = api(f"commits/{source}")
    tree = commit["commit"]["tree"]["sha"]
    candidates = {source}
    for pr in api(f"commits/{source}/pulls"):
        if (
            pr.get("merged_at")
            and pr.get("merge_commit_sha") == source
            and pr["base"]["ref"] == "main"
            and (pr["head"].get("repo") or {}).get("full_name") == repository
        ):
            candidates.add(pr["head"]["sha"])
    return tree, candidates


def full_inflight(source: str, repository: str) -> bool:
    """A matching source-PR producer also prevents a duplicate fallback after squash."""
    workflow_id = api("actions/workflows/ci.yml")["id"]
    tree, candidates = evidence_sources(source, repository)
    for candidate in candidates:
        if candidate != source and api(f"commits/{candidate}")["commit"]["tree"]["sha"] != tree:
            continue
        if any(
            trusted_full(run, repository, workflow_id)
            and run["head_sha"] == candidate
            and run["status"] in ("queued", "in_progress")
            for run in runs(candidate)
        ):
            return True
    return False


def select_evidence(source: str, repository: str) -> dict | None:
    workflow_id = api("actions/workflows/ci.yml")["id"]
    tree, candidates = evidence_sources(source, repository)
    for candidate in sorted(candidates):
        for run in runs(candidate):
            if run.get("head_sha") != candidate or not trusted_full(run, repository, workflow_id):
                continue
            if run.get("status") != "completed" or run.get("conclusion") != "success":
                continue
            try:
                exact = api(f"actions/runs/{run['id']}/attempts/{run['run_attempt']}")
                if (
                    not trusted_full(exact, repository, workflow_id)
                    or exact.get("status") != "completed"
                    or exact.get("conclusion") != "success"
                    or any(
                        exact.get(key) != run.get(key) for key in ("id", "run_attempt", "head_sha")
                    )
                ):
                    continue
                receipt = read_receipt(exact, repository)
                if receipt is not None:
                    if receipt.get("tested_tree") == tree:
                        return {**receipt, "source_commit": source}
                elif (
                    exact["event"] in FULL_EVENTS
                    and candidate == source
                    and exact["head_branch"] == "main"
                    and legacy_workflow(source)
                ):
                    return {
                        "repository": repository,
                        "workflow": WORKFLOW,
                        "run_id": str(run["id"]),
                        "run_attempt": str(run["run_attempt"]),
                        "event": run["event"],
                        "scope": "full",
                        "tested_commit": source,
                        "tested_tree": tree,
                        "source_commit": source,
                        "contract": "legacy-exact-main",
                    }
            except (
                subprocess.CalledProcessError,
                ValueError,
                KeyError,
                zipfile.BadZipFile,
            ) as error:
                print(f"run {run['id']} cannot supply Full evidence: {error}", file=sys.stderr)
    return None


def release_pr(number: int, base: str, head: str | None = None) -> dict:
    pr = api(f"pulls/{number}")
    if (
        pr["state"] != "open"
        or pr["base"]["ref"] != cut_branch(base)
        or pr["base"]["sha"] != base
        or pr["head"]["ref"] != f"release-please--branches--{cut_branch(base)}"
        or pr["head"]["repo"]["full_name"] != os.environ["GH_REPO"]
        or (head is not None and pr["head"]["sha"] != head)
    ):
        raise ValueError("release PR source or reviewed head changed")
    return pr


def cut_branch(source: str) -> str:
    """The full Git object name is the cut's immutable, collision-free identity."""
    # Git's SHA-1 wire format is fixed by this repository's object format.
    if len(source) != 40 or any(char not in "0123456789abcdef" for char in source):
        raise ValueError("select a full lowercase source commit SHA")
    return f"release-cuts/{source}"


def optional_api(path: str):
    result = subprocess.run(
        ("gh", "api", f"repos/{os.environ['GH_REPO']}/{path}"),
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        # gh's documented HTTP diagnostic distinguishes absence from denied or failed reads.
        if "HTTP 404" in result.stderr.decode(errors="replace"):
            return None
        raise ValueError(result.stderr.decode(errors="replace").strip())
    return json.loads(result.stdout)


def main_check_policy() -> list[dict]:
    """The public branch endpoint exposes classic required contexts and App identities."""
    protection = api("branches/main").get("protection", {})
    checks = protection.get("required_status_checks", {}).get("checks", [])
    policy = [{"context": check["context"], "app_id": check.get("app_id")} for check in checks]
    if not policy:
        raise ValueError("main has no readable required-check policy; no cut can land")
    return sorted(policy, key=lambda check: (check["context"], check.get("app_id") or -1))


def inherited_checks(head: str, policy: list[dict]) -> list[dict] | None:
    """An absent context or foreign App is pending, never an empty-list success."""
    if not policy:
        raise ValueError("required-check policy is empty")
    checks = [
        check
        for page in api(f"commits/{head}/check-runs?per_page=100", pages=True)
        for check in page["check_runs"]
    ]
    statuses = [
        status
        for page in api(f"commits/{head}/status?per_page=100", pages=True)
        for status in page["statuses"]
    ]
    result = []
    for required in policy:
        matches = [
            check
            for check in checks
            if check["name"] == required["context"]
            and (
                required.get("app_id") in (None, -1)
                or check.get("app", {}).get("id") == required["app_id"]
            )
        ]
        if matches:
            latest = max(matches, key=lambda check: check.get("id", 0))
            if latest["status"] != "completed":
                return None
            bucket = "pass" if latest["conclusion"] == "success" else "fail"
        else:
            matching_statuses = [
                status
                for status in statuses
                if status["context"] == required["context"] and required.get("app_id") in (None, -1)
            ]
            if not matching_statuses:
                return None
            latest = max(matching_statuses, key=lambda status: status.get("id", 0))
            if latest["state"] == "pending":
                return None
            bucket = "pass" if latest["state"] == "success" else "fail"
        result.append({"name": required["context"], "bucket": bucket})
    return result


def bind_evidence(intent: dict, selected: dict) -> dict:
    previous = intent.get("evidence")
    # Producer identity stays fixed even if GitHub later reports another successful retry.
    identity = (
        "repository",
        "workflow",
        "run_id",
        "run_attempt",
        "tested_commit",
        "tested_tree",
        "source_commit",
        "artifact_id",
        "contract",
        "package_checks",
    )
    if previous is not None and any(previous.get(key) != selected.get(key) for key in identity):
        raise ValueError("the cut already names a different Full producer")
    return previous if previous is not None else selected


def validate_predecessor(source: str, version: str, previous: dict) -> None:
    try:
        if version != previous["version"]:
            raise ValueError("version differs")
        command("git", "merge-base", "--is-ancestor", previous["reconciliation_commit"], source)
    except (ValueError, subprocess.CalledProcessError) as error:
        raise ValueError("source must contain the preceding reconciliation and version") from error


def require_evidence(source: str, repository: str) -> dict:
    selected = select_evidence(source, repository)
    if selected is None:
        raise ValueError(f"no complete Full evidence for {source}; resume its selected release cut")
    return selected


def refresh_checks(repository: str) -> None:
    for page in api("pulls?state=open&per_page=100", pages=True):
        for pr in page:
            source = pr["base"]["sha"]
            branch = pr["base"]["ref"]
            if pr["head"]["ref"] != f"release-please--branches--{branch}":
                continue
            if branch != cut_branch(source) and branch != "main":
                continue
            if select_evidence(source, repository) is None:
                continue
            for run in runs(pr["head"]["sha"]):
                if (
                    run["event"] == "pull_request"
                    and run["head_sha"] == pr["head"]["sha"]
                    and run["status"] == "completed"
                    and run["conclusion"] == "failure"
                ):
                    command("gh", "run", "rerun", str(run["id"]), "--failed")
                    print(f"re-ran failed release checks from run {run['id']}")
                    break


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # The command names are this helper's closed CLI protocol.
    parser.add_argument("command", choices=("scope", "record", "select", "refresh"))
    parser.add_argument("--source")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repository = os.environ.get("GH_REPO") or os.environ["GITHUB_REPOSITORY"]
    os.environ["GH_REPO"] = repository
    if args.command == "scope":
        selected = ci_scope(os.environ["GITHUB_EVENT_NAME"], event_payload())
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for key, value in selected.items():
                output.write(f"{key}={str(value).lower()}\n")
    elif args.command == "record":
        event = event_payload()
        if not ci_scope(os.environ["GITHUB_EVENT_NAME"], event)["full"] or args.output is None:
            parser.error("record requires Full scope and --output")
        receipt = {
            "repository": repository,
            "workflow": WORKFLOW,
            "run_id": os.environ["GITHUB_RUN_ID"],
            "run_attempt": os.environ["GITHUB_RUN_ATTEMPT"],
            "event": os.environ["GITHUB_EVENT_NAME"],
            "scope": "full",
            "package_checks": PACKAGE_CHECKS,
            "tested_commit": command("git", "rev-parse", "HEAD").decode().strip(),
            "tested_tree": command("git", "rev-parse", "HEAD^{tree}").decode().strip(),
        }
        if receipt["tested_commit"] != os.environ["GITHUB_SHA"]:
            raise ValueError("actual checkout differs from GITHUB_SHA")
        args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    elif args.command == "select":
        if not args.source:
            parser.error("select requires --source")
        print(json.dumps(require_evidence(args.source, repository)))
    else:
        if not args.run_id or not args.attempt:
            parser.error("refresh requires --run-id and --attempt")
        for _ in range(40):
            run = api(f"actions/runs/{args.run_id}/attempts/{args.attempt}")
            if run["status"] == "completed":
                break
            time.sleep(15)
        else:
            # A silent success here would let a release PR wait for checks that never re-run.
            raise ValueError(
                f"Full run {args.run_id} attempt {args.attempt} had not completed "
                f"(status {run['status']}) when the poll ended; release checks were not re-run"
            )
        if run["conclusion"] != "success":
            raise ValueError(
                f"Full run {args.run_id} attempt {args.attempt} concluded {run['conclusion']}; "
                "no evidence was recorded and release checks were not re-run"
            )
        if not trusted_full(run, repository, api("actions/workflows/ci.yml")["id"]):
            raise ValueError(
                f"Full run {args.run_id} attempt {args.attempt} is not a trusted Full run of "
                f"{WORKFLOW}; release checks were not re-run"
            )
        refresh_checks(repository)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
