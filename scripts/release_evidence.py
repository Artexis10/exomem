#!/usr/bin/env python3
"""Reuse complete tested-tree evidence and merge release PRs after checked preflights.

A refused preflight delays the release until its source and proof agree. It
prevents a stale-source release; it adds no manual approval requirement.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

# These names and events are the repository's closed Actions/release protocol.
WORKFLOW = ".github/workflows/ci.yml"
AUTOMATION = ".github/workflows/release-evidence-automation.yml"
RELEASE_BRANCH = "release-please--branches--main"
FULL_EVENTS = ("workflow_dispatch", "schedule")
INTENT_ARTIFACT = "release-intent"
INTENT_FILE = "release-intent.json"
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
    release = event_name == "pull_request" and pr.get("head", {}).get("ref") == RELEASE_BRANCH
    return {"full": event_name in FULL_EVENTS, "release": release}


def event_payload() -> dict:
    path = os.environ.get("GITHUB_EVENT_PATH")
    return json.loads(Path(path).read_text()) if path else {}


def command(*args: str, env: dict | None = None) -> bytes:
    return subprocess.check_output(args, env=env)


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


def select_evidence(source: str, repository: str) -> dict | None:
    workflow_id = api("actions/workflows/ci.yml")["id"]
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
    if api("commits/main")["sha"] != base:
        raise ValueError("main moved from the expected source; refresh the release PR and evidence")
    pr = api(f"pulls/{number}")
    if (
        pr["state"] != "open"
        or pr["base"]["ref"] != "main"
        or pr["base"]["sha"] != base
        or pr["head"]["ref"] != RELEASE_BRANCH
        or pr["head"]["repo"]["full_name"] != os.environ["GH_REPO"]
        or (head is not None and pr["head"]["sha"] != head)
    ):
        raise ValueError("release PR source or reviewed head changed")
    return pr


def check_release_files(number: int) -> None:
    files = [
        file for page in api(f"pulls/{number}/files?per_page=100", pages=True) for file in page
    ]
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="release-files-") as temporary:
        listing = Path(temporary) / "files.jsonl"
        listing.write_text("".join(json.dumps(file) + "\n" for file in files))
        command(
            sys.executable,
            str(root / "scripts/check_release_pr_files.py"),
            str(root / "release-please-config.json"),
            str(listing),
        )


def require_evidence(source: str, repository: str) -> dict:
    selected = select_evidence(source, repository)
    if selected is None:
        raise ValueError(
            f"no complete Full evidence for {source}; dispatch ci.yml on main and retry"
        )
    return selected


def refresh_checks(repository: str) -> None:
    owner = repository.split("/", 1)[0]
    for pr in api(f"pulls?state=open&head={owner}:{RELEASE_BRANCH}"):
        source = pr["base"]["sha"]
        if api("commits/main")["sha"] != source or select_evidence(source, repository) is None:
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


def merge_release(number: int, base: str, head: str, repository: str, token: str = "") -> None:
    """Merge the reviewed head after the final checks, evidence and source preflight."""
    command("gh", "pr", "checks", str(number), "--required")
    print(json.dumps(require_evidence(base, repository)))
    release_pr(number, base, head)
    print(
        "GitHub pins the reviewed head; a base update after this final read can still race the merge."
    )
    merge = ("gh", "pr", "merge", str(number), "--squash", "--match-head-commit", head)
    # A merge made with the workflow token starts no workflow, so Release Please would not tag it.
    command(*merge, env={**os.environ, "GH_TOKEN": token} if token else None)


def intent_record(pr: dict) -> dict:
    return {
        "pr": pr["number"],
        "expected_base": pr["base"]["sha"],
        "expected_head": pr["head"]["sha"],
    }


def intended(pr: dict, repository: str) -> bool:
    """True when a release intent dispatched on main named exactly this PR, source and head."""
    workflow_runs = api(
        f"actions/workflows/{Path(AUTOMATION).name}/runs"
        "?event=workflow_dispatch&branch=main&status=success&per_page=30"
    )["workflow_runs"]
    for run in workflow_runs:
        if (
            run.get("path") != AUTOMATION
            or (run.get("head_repository") or {}).get("full_name") != repository
        ):
            continue
        for artifact in api(f"actions/runs/{run['id']}/artifacts?name={INTENT_ARTIFACT}")[
            "artifacts"
        ]:
            if artifact["expired"]:
                continue
            archive = command(
                "gh", "api", f"repos/{repository}/actions/artifacts/{artifact['id']}/zip"
            )
            try:
                with zipfile.ZipFile(io.BytesIO(archive)) as files:
                    named = json.loads(files.read(INTENT_FILE))
            except (KeyError, ValueError, zipfile.BadZipFile) as error:
                print(
                    f"artifact {artifact['id']} holds no readable release intent: {error}",
                    file=sys.stderr,
                )
                continue
            if named == intent_record(pr):
                return True
    return False


def required_checks(number: int) -> list[dict]:
    # gh exits non-zero while a check is pending or failed; the JSON still lists every check.
    result = subprocess.run(
        ("gh", "pr", "checks", str(number), "--required", "--json", "name,bucket"),
        capture_output=True,
        check=False,
    )
    return json.loads(result.stdout)


def land(repository: str, intent_path: Path | None, token: str) -> None:
    """Merge the open release PR when a release intent named its exact source and head."""
    owner = repository.split("/", 1)[0]
    for pr in api(f"pulls?state=open&head={owner}:{RELEASE_BRANCH}"):
        named = (
            json.loads(intent_path.read_text()) == intent_record(pr)
            if intent_path
            else intended(pr, repository)
        )
        if not named:
            print(
                f"no release intent names release PR #{pr['number']} at {pr['head']['sha']}; "
                "the release session merges it"
            )
            continue
        for _ in range(CHECK_POLLS):
            time.sleep(CHECK_POLL_SECONDS)
            # gh's documented bucket values: pass, fail, pending, skipping, cancel.
            checks = required_checks(pr["number"])
            if any(check["bucket"] == "pending" for check in checks):
                continue
            failed = [check["name"] for check in checks if check["bucket"] in ("fail", "cancel")]
            if failed:
                raise ValueError(
                    f"required checks failed ({', '.join(failed)}); release PR #{pr['number']} "
                    "was not merged"
                )
            break
        else:
            raise ValueError(
                f"required checks were still pending after {CHECK_POLLS * CHECK_POLL_SECONDS // 60} "
                f"minutes; release PR #{pr['number']} was not merged"
            )
        base, head = pr["base"]["sha"], pr["head"]["sha"]
        release_pr(pr["number"], base, head)
        check_release_files(pr["number"])
        merge_release(pr["number"], base, head, repository, token)
        print(f"merged release PR #{pr['number']} at {head}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    # The command names are this helper's closed CLI protocol.
    parser.add_argument(
        "command", choices=("scope", "record", "select", "intent", "refresh", "merge", "land")
    )
    parser.add_argument("--source")
    parser.add_argument("--pr", type=int)
    parser.add_argument("--expected-base")
    parser.add_argument("--expected-head")
    parser.add_argument("--run-id", type=int)
    parser.add_argument("--attempt", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--intent", type=Path)
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
    elif args.command in ("merge", "intent"):
        if (
            not args.pr
            or not args.expected_base
            or (args.command == "merge" and not args.expected_head)
        ):
            parser.error(
                "merge/intent require --pr and --expected-base; merge also requires --expected-head"
            )
        pr = release_pr(args.pr, args.expected_base, args.expected_head)
        check_release_files(args.pr)
        if args.command == "intent":
            if args.expected_head and args.output:
                # The named head binds the intent: a later head or source never matches it.
                args.output.write_text(json.dumps(intent_record(pr)) + "\n")
            selected = select_evidence(args.expected_base, repository)
            if selected:
                print(json.dumps(selected))
                refresh_checks(repository)
                if os.environ.get("GITHUB_OUTPUT"):
                    with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                        output.write("reused=true\n")
            elif any(
                trusted_full(run, repository, api("actions/workflows/ci.yml")["id"])
                and run["head_sha"] == args.expected_base
                and run["head_branch"] == "main"
                and run["status"] in ("queued", "in_progress")
                for run in runs(args.expected_base)
            ):
                print("Full evidence is already in flight")
            else:
                release_pr(args.pr, args.expected_base)
                command("gh", "workflow", "run", "ci.yml", "--ref", "main")
                print(f"dispatched Full evidence on main for {args.expected_base}")
        else:
            merge_release(args.pr, args.expected_base, args.expected_head, repository)
    elif args.command == "land":
        token = os.environ.get("RELEASE_MERGE_TOKEN", "")
        if not token:
            # Without the release App token the merge would start no Release Please run and no tag.
            parser.error("land requires RELEASE_MERGE_TOKEN, the release App installation token")
        land(repository, args.intent, token)
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
