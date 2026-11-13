#!/usr/bin/env python3
"""Select, qualify and reconcile one fixed source through the main controller.

A conflict stops one cut. Main remains available for ordinary delivery.
Pinned Release Please owns proposals, versions, notes and tags.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

import release_evidence as evidence
from check_release_pr_files import _extra_files, unreleased_changes, version_updates

# These names are the controller's closed GitHub protocol, not product vocabulary.
ORIGIN_DIRECTORY = ".github/release-cuts"
INTENT_START, INTENT_END = "<!-- release-cut-intent\n", "\n-->"
POLLS, POLL_SECONDS = 360, 20


def git(*args: str, cwd: Path | None = None) -> str:
    return evidence.command("git", *args, cwd=cwd).decode().strip()


def write_api(method: str, path: str, value: dict) -> dict:
    response = subprocess.run(
        ("gh", "api", "--method", method, f"repos/{os.environ['GH_REPO']}/{path}", "--input", "-"),
        input=json.dumps(value),
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    return json.loads(response.stdout) if response.stdout.strip() else {}


def read_json(ref: str, path: str) -> dict:
    return json.loads(git("show", f"{ref}:{path}"))


def trusted_main() -> str:
    git("fetch", "--quiet", "--no-tags", "origin", "main")
    return git("rev-parse", "FETCH_HEAD")


def origin_path(tag: str) -> str:
    # Release tags use the publisher's fixed SemVer format, with no path components.
    if not tag.startswith("v") or any(
        char not in "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ.-"
        for char in tag[1:]
    ):
        raise ValueError("release tag must use v followed by a SemVer version")
    return f"{ORIGIN_DIRECTORY}/{tag}.json"


def origin_at(main: str, tag: str) -> dict | None:
    path = origin_path(tag)
    history = git("log", "--format=%H", main, "--", path).splitlines()
    present = bool(git("ls-tree", "--name-only", main, "--", path))
    # A trusted record is write-once across all source PRs, including delete/recreate attempts.
    if len(history) > 1 or (history and not present):
        raise ValueError("write-once release origin changed or was deleted after acceptance")
    return read_json(main, path) if present else None


def release_pull(branch: str) -> dict | None:
    pulls = evidence.api(f"pulls?state=all&base={quote(branch, safe='')}&per_page=100")
    matching = [
        pr
        for pr in pulls
        if pr["head"]["ref"] == f"release-please--branches--{branch}"
        and pr["head"]["repo"]["full_name"] == os.environ["GH_REPO"]
    ]
    if len(matching) > 1:
        raise ValueError("cut has more than one release proposal; resolve the conflicting PR")
    return evidence.api(f"pulls/{matching[0]['number']}") if matching else None


def read_intent(pr: dict) -> dict | None:
    body = pr.get("body") or ""
    if INTENT_START not in body:
        return None
    return json.loads(body.split(INTENT_START, 1)[1].split(INTENT_END, 1)[0])


def save_intent(pr: dict, intent: dict) -> None:
    current = evidence.api(f"pulls/{pr['number']}")
    if current["head"]["sha"] != intent["head"]:
        raise ValueError("release head changed after cut selection")
    body = (current.get("body") or "").split(INTENT_START, 1)[0].rstrip()
    write_api(
        "PATCH",
        f"pulls/{pr['number']}",
        {
            "body": body + "\n\n" + INTENT_START + json.dumps(intent, sort_keys=True) + INTENT_END,
        },
    )


def predecessor(source: str, main: str) -> tuple[dict, dict | None]:
    """C's immutable manifest names its predecessor, including after an in-flight tag exists."""
    version = read_json(source, ".release-please-manifest.json")["."]
    tag = "v" + version
    previous_origin = origin_at(main, tag)
    if previous_origin:
        verify_origin(previous_origin, main, require_evidence=False)
        reconciliation = git(
            "log", "--diff-filter=A", "--format=%H", "-1", main, "--", origin_path(tag)
        )
        previous = {
            "tag": tag,
            "version": version,
            "source": previous_origin["source"],
            "reconciliation_commit": reconciliation,
        }
    else:
        git("fetch", "--quiet", "origin", f"refs/tags/{tag}")
        anchor = git("rev-parse", "FETCH_HEAD^{commit}")
        git("merge-base", "--is-ancestor", anchor, main)
        previous = {
            "tag": tag,
            "version": version,
            "source": anchor,
            "reconciliation_commit": anchor,
        }
    evidence.validate_predecessor(source, version, previous)
    if (
        previous_origin
        and read_json(source, "release-please-config.json").get("last-release-sha")
        != previous["source"]
    ):
        raise ValueError("source does not retain the preceding released source boundary")
    return previous, previous_origin


def accepted_origin(pr: dict | None, source: str, main: str) -> dict | None:
    if not pr or not pr.get("merged"):
        return None
    release = pr["merge_commit_sha"]
    git("fetch", "--quiet", "origin", release)
    version = read_json(release, ".release-please-manifest.json")["."]
    record = origin_at(main, "v" + version)
    if record is not None:
        if record["source"] != source or record["release"] != release:
            raise ValueError("accepted release origin conflicts with the resumed cut")
        verify_origin(record, main, require_evidence=False)
    return record


def prepare(source: str) -> dict:
    branch = evidence.cut_branch(source)
    main = trusted_main()
    git("merge-base", "--is-ancestor", source, main)
    pr = release_pull(branch)
    accepted = accepted_origin(pr, source, main)
    if accepted is not None:
        return {
            "source": source,
            "branch": branch,
            "predecessor": accepted["predecessor"],
            "policy": accepted["required_checks"],
            "propose": False,
            "merged": True,
        }
    intent = read_intent(pr) if pr else None
    if intent and (intent["source"] != source or intent["branch"] != branch):
        raise ValueError("existing intent conflicts with the selected cut")
    for page in evidence.api("pulls?state=open&per_page=100", pages=True):
        for other in page:
            if other["base"]["ref"].startswith("release-cuts/") and other["base"]["ref"] != branch:
                raise ValueError(
                    f"finish cut PR #{other['number']} before selecting another source"
                )
    previous, previous_origin = predecessor(source, main)
    if pr is None and evidence.api("releases/latest")["tag_name"] != previous["tag"]:
        raise ValueError("source must contain the preceding reconciliation and published version")
    if previous_origin and intent is None:
        selected = evidence.select_evidence(source, os.environ["GH_REPO"])
        if selected is None or selected.get("package_checks") != evidence.PACKAGE_CHECKS:
            # Avoid Full on an unfinished repair. A wrong rejection costs this cut one local check.
            with reconciliation_tree(source, previous_origin, "") as (_, expected):
                if expected != git("rev-parse", f"{source}^{{tree}}"):
                    raise ValueError("source must contain the completed preceding reconciliation")
    ref = evidence.optional_api(f"git/ref/heads/{branch}")
    if ref is None:
        if pr:
            raise ValueError("cut branch is missing; restore its original identity before retrying")
        write_api("POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": source})
    elif ref["object"]["sha"] != source:
        if not pr or not pr.get("merged") or pr["merge_commit_sha"] != ref["object"]["sha"]:
            raise ValueError("cut branch changed from its selected source")
        verify_squash(source, pr["head"]["sha"], pr["merge_commit_sha"])
    return {
        "source": source,
        "branch": branch,
        "predecessor": previous,
        "policy": evidence.main_check_policy(),
        "propose": pr is None,
        "merged": bool(pr and pr.get("merged")),
    }


def visible_commit(sha: str) -> dict:
    for _ in range(12):
        commit = evidence.optional_api(f"commits/{sha}")
        if commit is not None:
            return commit
        time.sleep(5)
    raise ValueError("release graph is pending; retry the same cut")


def verify_squash(source: str, head: str, release: str) -> dict:
    commit = visible_commit(release)
    reviewed = evidence.api(f"commits/{head}")["commit"]["tree"]["sha"]
    if [parent["sha"] for parent in commit["parents"]] != [source]:
        raise ValueError("release squash must have the selected source as its sole parent")
    if commit["commit"]["tree"]["sha"] != reviewed:
        raise ValueError("release squash tree differs from the checked release head")
    return commit


def check_metadata_delta(source: str, head: str) -> str:
    git("fetch", "--quiet", "origin", source, head)
    config = read_json(source, "release-please-config.json")
    paths = git("diff", "--name-only", source, head).splitlines()
    files = []
    for path in paths:
        file = {
            "filename": path,
            "patch": git("diff", "--no-ext-diff", "--unified=3", source, head, "--", path),
        }
        # The validator consumes patch lines without the Git file headers.
        file["patch"] = "\n".join(
            line for line in file["patch"].splitlines() if not line.startswith(("--- ", "+++ "))
        )
        if path == ".release-please-manifest.json" or path in {
            entry["path"] for entry in _extra_files(config)
        }:
            file.update(before=git("show", f"{source}:{path}"), after=git("show", f"{head}:{path}"))
        files.append(file)
    problems = unreleased_changes(config, files)
    if problems:
        raise ValueError("release metadata changed other content: " + "; ".join(problems))
    version = read_json(head, ".release-please-manifest.json")["."]
    return version


def verify_metadata(source: str, head: str) -> str:
    version = check_metadata_delta(source, head)
    with tempfile.TemporaryDirectory(prefix="release-metadata-") as temporary:
        root = Path(temporary)
        subprocess.run(
            ("tar", "-xf", "-", "-C", str(root)),
            input=evidence.command("git", "archive", head),
            check=True,
            timeout=60,
        )
        check_packages(root, version)
    return version


def proposal_head(cut: dict) -> str:
    pr = release_pull(cut["branch"])
    if not pr:
        raise ValueError("Release Please has not created this cut proposal")
    head = pr["head"]["sha"]
    evidence.release_pr(pr["number"], cut["source"], head)
    check_metadata_delta(cut["source"], head)
    return head


def push_generated(cut: dict, candidate: Path) -> None:
    head = cut["proposal_head"]
    branch = f"release-please--branches--{cut['branch']}"
    if git("rev-parse", "HEAD", cwd=candidate) != head:
        raise ValueError("generated package checkout differs from the permitted proposal head")
    if subprocess.run(("git", "diff", "--quiet"), cwd=candidate, timeout=30).returncode == 0:
        return
    git("add", "--", "plugins/cloud/generated", "plugins/claude-code", cwd=candidate)
    git(
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "-m",
        "chore: regenerate packages for the selected version",
        cwd=candidate,
    )
    git(
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "push",
        f"--force-with-lease=refs/heads/{branch}:{head}",
        "origin",
        f"HEAD:refs/heads/{branch}",
        cwd=candidate,
    )


def child_environment(root: Path) -> dict:
    # Candidate code receives package inputs, never controller credentials.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GH_TOKEN", "GITHUB_TOKEN", "RELEASE_MERGE_TOKEN", "RELEASE_ACTIONS_TOKEN")
    }
    environment.update(
        PYTHONPATH=str(root / "src"),
        UV_PROJECT_ENVIRONMENT=str(root / ".venv"),
        XDG_STATE_HOME=str(root / ".test-state"),
    )
    return environment


def check_packages(root: Path, version: str) -> None:
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    plugin = json.loads((root / "plugins/claude-code/.claude-plugin/plugin.json").read_text())
    if project["version"] != version or plugin["version"] != version:
        raise ValueError("package and committed plugin must use the selected release version")
    environment = child_environment(root)
    for args in (
        ("uv", "lock", "--check"),
        ("uv", "run", "--frozen", "python", "scripts/cloud-plugin.py", "check"),
        (
            "uv",
            "run",
            "--frozen",
            "python",
            "-m",
            "pytest",
            "-q",
            "tests/test_plugin_sync.py::test_committed_plugin_matches_the_packaged_sources",
            "tests/test_plugin_sync.py::test_manifest_matches_apart_from_the_version",
        ),
    ):
        evidence.command(*args, cwd=root, env=environment)


def authenticate_evidence(source: str, producer: dict) -> dict:
    run = evidence.api(f"actions/runs/{producer['run_id']}/attempts/{producer['run_attempt']}")
    if (
        not evidence.trusted_full(
            run, os.environ["GH_REPO"], evidence.api("actions/workflows/ci.yml")["id"]
        )
        or run["status"] != "completed"
        or run["conclusion"] != "success"
    ):
        raise ValueError("bound Full producer did not succeed")
    receipt = evidence.read_receipt(run, os.environ["GH_REPO"])
    if receipt is None:
        raise ValueError("bound Full producer evidence is pending or unavailable")
    authenticated = {**receipt, "source_commit": source}
    if authenticated["tested_tree"] != evidence.api(f"commits/{source}")["commit"]["tree"]["sha"]:
        raise ValueError("bound Full producer tested a different source tree")
    evidence.bind_evidence({"evidence": producer}, authenticated)
    return authenticated


def qualify(cut: dict, *, evidence_only: bool = False) -> dict:
    source, branch = cut["source"], evidence.cut_branch(cut["source"])
    if cut["branch"] != branch:
        raise ValueError("cut branch differs from its selected source")
    pr = release_pull(branch)
    if not pr:
        raise ValueError("Release Please has not created the cut proposal; retry this source")
    main = trusted_main()
    accepted = accepted_origin(pr, source, main)
    if accepted is not None:
        return accepted
    checkpoint = read_intent(pr)
    if checkpoint is None and pr.get("merged"):
        raise ValueError("merged cut lacks its retained Full producer pointer")
    head = pr["head"]["sha"]
    if (
        pr["base"]["ref"] != branch
        or pr["head"]["ref"] != f"release-please--branches--{branch}"
        or pr["head"]["repo"]["full_name"] != os.environ["GH_REPO"]
        or (checkpoint and (checkpoint["source"] != source or checkpoint["head"] != head))
    ):
        raise ValueError("release source or head changed after cut selection")
    git("merge-base", "--is-ancestor", source, main)
    previous, _ = predecessor(source, main)
    version = check_metadata_delta(source, head)
    if checkpoint and checkpoint.get("version") not in (None, version):
        raise ValueError("checkpoint version differs from the checked manifest")
    intent = {
        **cut,
        "head": head,
        "version": version,
        "predecessor": previous,
        "policy": evidence.main_check_policy(),
        "fallback_requested": bool(checkpoint and checkpoint.get("fallback_requested")),
    }
    if checkpoint and checkpoint.get("evidence"):
        intent["evidence"] = checkpoint["evidence"]
    if not pr.get("merged"):
        evidence.release_pr(pr["number"], source, head)
        save_intent(pr, intent)
    if intent.get("evidence"):
        intent["evidence"] = authenticate_evidence(source, intent["evidence"])
    else:
        if pr.get("merged"):
            raise ValueError("merged cut lacks its retained Full producer pointer")
        for _ in range(POLLS):
            selected = evidence.select_evidence(source, os.environ["GH_REPO"])
            if selected:
                intent["evidence"] = authenticate_evidence(source, selected)
                save_intent(pr, intent)
                break
            inflight = evidence.full_inflight(source, os.environ["GH_REPO"])
            if not inflight and not intent["fallback_requested"]:
                # Persist before dispatch: a retry must never create a second Full fallback.
                intent["fallback_requested"] = True
                save_intent(pr, intent)
                evidence.command(
                    "gh",
                    "workflow",
                    "run",
                    "ci.yml",
                    "--ref",
                    branch,
                    env={
                        **os.environ,
                        "GH_TOKEN": os.environ.get("RELEASE_ACTIONS_TOKEN", os.environ["GH_TOKEN"]),
                    },
                )
            time.sleep(POLL_SECONDS)
        else:
            raise ValueError(
                "Full evidence is pending; retry the same source after its producer completes"
            )
    if evidence_only and not pr.get("merged"):
        return {**cut, "predecessor": previous, "policy": intent["policy"]}
    for _ in range(evidence.CHECK_POLLS):
        if not pr.get("merged"):
            evidence.release_pr(pr["number"], source, head)
        intent["policy"] = evidence.main_check_policy()
        checks = evidence.inherited_checks(head, intent["policy"])
        if checks is not None:
            if any(check["bucket"] != "pass" for check in checks):
                raise ValueError(
                    "required checks failed: "
                    + ", ".join(check["name"] for check in checks if check["bucket"] != "pass")
                )
            break
        time.sleep(evidence.CHECK_POLL_SECONDS)
    else:
        raise ValueError("required checks are pending; retry after configured App checks report")
    if verify_metadata(source, head) != version:
        raise ValueError("checked release manifest version changed")
    if pr.get("merged"):
        return make_origin(pr, intent)
    save_intent(pr, intent)
    evidence.release_pr(pr["number"], source, head)
    evidence.command(
        "gh", "pr", "merge", str(pr["number"]), "--squash", "--match-head-commit", head
    )
    for _ in range(12):
        pr = evidence.api(f"pulls/{pr['number']}")
        if pr.get("merged"):
            return make_origin(pr, intent)
        time.sleep(5)
    raise ValueError("squash outcome is pending; retry this cut before tagging")


def make_origin(pr: dict, intent: dict) -> dict:
    source, head, release = intent["source"], intent["head"], pr["merge_commit_sha"]
    commit = verify_squash(source, head, release)
    for _ in range(12):
        ref = evidence.optional_api(f"git/ref/heads/{intent['branch']}")
        if ref is not None and ref["object"]["sha"] == release:
            break
        if ref is not None and ref["object"]["sha"] != source:
            raise ValueError("cut branch changed from the verified squash outcome")
        time.sleep(5)
    else:
        raise ValueError("cut squash ref is pending; retry this cut before tagging")
    if not intent.get("evidence") or not intent.get("version"):
        raise ValueError("cut intent lacks its checked version or Full producer")
    tree = commit["commit"]["tree"]["sha"]
    return {
        "schema": 1,
        "tag": "v" + intent["version"],
        "version": intent["version"],
        "branch": intent["branch"],
        "source": source,
        "source_tree": evidence.api(f"commits/{source}")["commit"]["tree"]["sha"],
        "head": head,
        "head_tree": tree,
        "release": release,
        "release_tree": tree,
        "evidence": intent["evidence"],
        "required_checks": intent["policy"],
        "predecessor": intent["predecessor"],
    }


def reconcile_files(root: Path, record: dict, notes: str) -> None:
    path = root / origin_path(record["tag"])
    if path.exists():
        if json.loads(path.read_text()) != record:
            raise ValueError("write-once release origin already has different content")
        return
    config_path = root / "release-please-config.json"
    config = json.loads(config_path.read_text())
    managed = {".release-please-manifest.json"} | {entry["path"] for entry in _extra_files(config)}
    before = {name: (root / name).read_text() for name in managed}
    for name, content in version_updates(config, before, record["version"]).items():
        (root / name).write_text(content)
    config["last-release-sha"] = record["source"]
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    result = subprocess.run(
        ("node", str(Path(__file__).with_name("release_changelog.cjs"))),
        input=json.dumps(
            {
                "version": record["version"],
                "notes": notes,
                "content": (root / "CHANGELOG.md").read_text(),
            }
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=60,
    )
    (root / "CHANGELOG.md").write_text(result.stdout)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")


@contextmanager
def reconciliation_tree(base: str, record: dict, notes: str):
    """Project current metadata and maintained generators from one exact main tree."""
    branch = f"release-reconcile/{record['source']}"
    with tempfile.TemporaryDirectory(prefix="release-reconcile-") as temporary:
        root = Path(temporary)
        evidence.command("git", "clone", "--quiet", "--shared", ".", str(root))
        git("checkout", "--quiet", "-B", branch, base, cwd=root)
        path = root / origin_path(record["tag"])
        if path.exists():
            version = read_json(base, ".release-please-manifest.json")["."]
            cursor = read_json(base, "release-please-config.json").get("last-release-sha")
            if version == record["version"]:
                if cursor != record["source"]:
                    raise ValueError("accepted cut's version and source boundary disagree")
            else:
                newer = origin_at(base, "v" + version)
                if newer is None or newer["source"] != cursor:
                    raise ValueError("current metadata has no accepted newer release origin")
                verify_origin(newer, base, require_evidence=False)
                git("merge-base", "--is-ancestor", record["source"], newer["source"])
        # Equal accepted origins preserve later versions and notes; generation still runs below.
        reconcile_files(root, record, notes)
        environment = child_environment(root)
        evidence.command(
            "uv",
            "run",
            "--frozen",
            "python",
            "scripts/cloud-plugin.py",
            "build",
            cwd=root,
            env=environment,
        )
        evidence.command(
            "uv",
            "run",
            "--frozen",
            "exomem",
            "package-skills",
            "--plugin-root",
            "plugins/claude-code",
            cwd=root,
            env=environment,
        )
        check_packages(root, json.loads((root / ".release-please-manifest.json").read_text())["."])
        git(
            "add",
            "--",
            "CHANGELOG.md",
            "release-please-config.json",
            origin_path(record["tag"]),
            ".release-please-manifest.json",
            *[
                entry["path"]
                for entry in _extra_files(
                    json.loads((root / "release-please-config.json").read_text())
                )
            ],
            "plugins/cloud/generated",
            "plugins/claude-code",
            cwd=root,
        )
        yield root, git("write-tree", cwd=root)


def push_reconciliation(root: Path, record: dict) -> str:
    branch = f"release-reconcile/{record['source']}"
    git(
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "user.name=github-actions[bot]",
        "-c",
        "user.email=41898282+github-actions[bot]@users.noreply.github.com",
        "commit",
        "-m",
        f"chore: reconcile published metadata for {record['tag']}",
        cwd=root,
    )
    remote = git("remote", "get-url", "origin")
    git("remote", "set-url", "origin", remote, cwd=root)
    existing = evidence.optional_api(f"git/ref/heads/{branch}")
    lease = (
        f"--force-with-lease=refs/heads/{branch}:{existing['object']['sha'] if existing else ''}"
    )
    git(
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
        "push",
        lease,
        "origin",
        f"HEAD:refs/heads/{branch}",
        cwd=root,
    )
    return git("rev-parse", "HEAD", cwd=root)


def prospective_tree(base: str, head: str) -> str | None:
    git("fetch", "--quiet", "origin", head)
    result = subprocess.run(
        ("git", "merge-tree", "--write-tree", base, head),
        text=True,
        capture_output=True,
        timeout=60,
    )
    return result.stdout.splitlines()[0] if result.returncode == 0 else None


def reconcile(record: dict) -> None:
    """Refresh only mismatched projections and repair a raced squash before publication."""
    tag, source = record["tag"], record["source"]
    notes = evidence.api(f"releases/tags/{tag}")["body"]
    branch = f"release-reconcile/{source}"
    verified = False
    projections = {}

    def projection(base: str) -> str:
        if base not in projections:
            with reconciliation_tree(base, record, notes) as (_, tree):
                projections[base] = tree
        return projections[base]

    for _ in range(6):
        main = trusted_main()
        accepted = origin_at(main, tag)
        if accepted is not None and accepted != record:
            raise ValueError("trusted main already accepted a conflicting release origin")
        if not verified:
            verify_origin(record, main, require_evidence=accepted is None)
            verified = True
        prs = evidence.api(
            f"pulls?state=open&base=main&head={quote(os.environ['GH_REPO'].split('/')[0] + ':' + branch, safe='')}"
        )
        with reconciliation_tree(main, record, notes) as (root, tree):
            projections[main] = tree
            if accepted is not None and tree == git("rev-parse", f"{main}^{{tree}}"):
                return
            existing = evidence.api(f"pulls/{prs[0]['number']}") if prs else None
            if existing is None or prospective_tree(main, existing["head"]["sha"]) != tree:
                push_reconciliation(root, record)
        pr = (
            prs[0]
            if prs
            else write_api(
                "POST",
                "pulls",
                {
                    "base": "main",
                    "head": branch,
                    "title": f"chore: reconcile published metadata for {tag}",
                    "body": "Preserve current source and dependency selections while recording the checked release origin and next history boundary.",
                },
            )
        )
        for _ in range(evidence.CHECK_POLLS):
            current = evidence.api(f"pulls/{pr['number']}")
            checks = evidence.inherited_checks(current["head"]["sha"], evidence.main_check_policy())
            if checks is None:
                time.sleep(evidence.CHECK_POLL_SECONDS)
                continue
            if any(check["bucket"] != "pass" for check in checks):
                raise ValueError(
                    "reconciliation required checks failed; repair or refresh this cut"
                )
            break
        else:
            raise ValueError("reconciliation is pending; retry after its required checks finish")
        latest = trusted_main()
        expected = projection(latest)
        if prospective_tree(latest, current["head"]["sha"]) != expected:
            # Main can move freely. Only a conflict or changed generated tree needs fresh CI.
            continue
        evidence.command(
            "gh",
            "pr",
            "merge",
            str(pr["number"]),
            "--squash",
            "--match-head-commit",
            current["head"]["sha"],
        )
        for _ in range(12):
            merged = evidence.api(f"pulls/{pr['number']}")
            if merged.get("merged"):
                break
            time.sleep(5)
        else:
            raise ValueError("reconciliation squash is pending; retry the same cut")
        outcome = visible_commit(merged["merge_commit_sha"])
        if len(outcome["parents"]) != 1:
            raise ValueError("reconciliation squash must have exactly one main parent")
        parent = outcome["parents"][0]["sha"]
        git("fetch", "--quiet", "origin", parent)
        if outcome["commit"]["tree"]["sha"] == projection(parent):
            return
        # A base race can accept the origin before new generator inputs were repackaged.
        # The next pass repairs current main through a PR, keeping the accepted origin unchanged.
    raise ValueError("reconciliation refresh is pending after bounded retries; resume the same cut")


def verify_origin(record: dict, main: str, *, require_evidence: bool) -> None:
    """Check immutable graph and source binding; accepted records survive artifact expiry."""
    source, head, release = record["source"], record["head"], record["release"]
    if record["branch"] != evidence.cut_branch(source) or record["tag"] != "v" + record["version"]:
        raise ValueError("release origin identity conflicts")
    git("fetch", "--quiet", "origin", f"refs/tags/{record['tag']}:refs/tags/{record['tag']}")
    git("fetch", "--quiet", "origin", source, head, release)
    tag_commit = git("rev-parse", f"refs/tags/{record['tag']}^{{commit}}")
    if tag_commit != release:
        raise ValueError("immutable release tag differs from its origin")
    git("merge-base", "--is-ancestor", source, main)
    if (
        subprocess.run(("git", "merge-base", "--is-ancestor", release, main), timeout=30).returncode
        == 0
    ):
        raise ValueError("isolated release commit must remain outside main ancestry")
    commit = verify_squash(source, head, release)
    if (
        commit["commit"]["tree"]["sha"] != record["release_tree"]
        or record["head_tree"] != record["release_tree"]
    ):
        raise ValueError("release origin tree conflicts")
    source_tree = evidence.api(f"commits/{source}")["commit"]["tree"]["sha"]
    if source_tree != record["source_tree"] or record["evidence"]["tested_tree"] != source_tree:
        raise ValueError("release origin source or Full tree conflicts")
    if read_json(release, ".release-please-manifest.json")["."] != record["version"]:
        raise ValueError("release origin version differs from its manifest")
    if not record["required_checks"] or record["evidence"]["source_commit"] != source:
        raise ValueError("release origin lacks required checks or matching Full evidence")
    evidence.validate_predecessor(
        source, read_json(source, ".release-please-manifest.json")["."], record["predecessor"]
    )
    if require_evidence:
        authenticate_evidence(source, record["evidence"])
        if verify_metadata(source, head) != record["version"]:
            raise ValueError("checked release version conflicts with the origin")


def verify_tag(tag: str, main: str) -> None:
    release = git("rev-parse", f"refs/tags/{tag}^{{commit}}")
    for _ in range(12):
        record = origin_at(main, tag)
        if record is not None:
            break
        legacy = subprocess.run(("git", "merge-base", "--is-ancestor", release, main), timeout=30)
        if legacy.returncode == 0:
            return
        if legacy.returncode != 1:
            raise ValueError("release ancestry is unavailable")
        time.sleep(5)
        main = trusted_main()
    else:
        raise ValueError("isolated release origin is pending on trusted main; retry this cut")
    path = origin_path(tag)
    introduction = git("log", "--diff-filter=A", "--format=%H", "-1", main, "--", path)
    if read_json(introduction, path) != record:
        raise ValueError("write-once release origin changed after acceptance")
    verify_origin(record, main, require_evidence=False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "operation",
        choices=(
            "prepare",
            "check-head",
            "push-generated",
            "evidence",
            "qualify",
            "publish",
            "reconcile",
            "verify-tag",
        ),
    )
    parser.add_argument("--source")
    parser.add_argument("--state", type=Path, default=Path("release-cut.json"))
    parser.add_argument("--tag")
    parser.add_argument("--candidate", type=Path)
    parser.add_argument("--trusted-main")
    args = parser.parse_args()
    os.environ["GH_REPO"] = os.environ.get("GH_REPO") or os.environ["GITHUB_REPOSITORY"]
    if args.operation == "prepare":
        if not args.source:
            parser.error("prepare requires --source")
        state = prepare(args.source)
        args.state.write_text(json.dumps(state, indent=2) + "\n")
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            for key in ("branch", "propose", "merged"):
                output.write(f"{key}={str(state[key]).lower()}\n")
    elif args.operation == "check-head":
        cut = json.loads(args.state.read_text())
        cut["proposal_head"] = proposal_head(cut)
        args.state.write_text(json.dumps(cut, indent=2) + "\n")
        with open(os.environ["GITHUB_OUTPUT"], "a") as output:
            output.write(f"head={cut['proposal_head']}\n")
    elif args.operation == "push-generated":
        if args.candidate is None:
            parser.error("push-generated requires --candidate")
        push_generated(json.loads(args.state.read_text()), args.candidate)
    elif args.operation in ("evidence", "qualify", "publish"):
        record = qualify(
            json.loads(args.state.read_text()), evidence_only=args.operation == "evidence"
        )
        args.state.write_text(json.dumps(record, indent=2) + "\n")
        if args.operation == "publish":
            print(
                evidence.command(
                    "node", str(Path(__file__).with_name("release_publish.cjs")), str(args.state)
                )
                .decode()
                .strip()
            )
    elif args.operation == "reconcile":
        record = json.loads(args.state.read_text())
        reconcile(record)
        # Observe the accepted record on fetched main before any publisher dispatch.
        for _ in range(12):
            accepted = origin_at(trusted_main(), record["tag"])
            if accepted is not None:
                if accepted != record:
                    raise ValueError("accepted release origin conflicts")
                evidence.command(
                    "gh",
                    "workflow",
                    "run",
                    "release-please.yml",
                    "--ref",
                    record["tag"],
                    "-f",
                    f"tag={record['tag']}",
                    env={
                        **os.environ,
                        "GH_TOKEN": os.environ.get("RELEASE_ACTIONS_TOKEN", os.environ["GH_TOKEN"]),
                    },
                )
                return
            time.sleep(5)
        raise ValueError("accepted origin is pending on main; retry this cut before publication")
    else:
        if not args.tag or not args.trusted_main:
            parser.error("verify-tag requires --tag and --trusted-main")
        verify_tag(args.tag, args.trusted_main)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, subprocess.CalledProcessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
