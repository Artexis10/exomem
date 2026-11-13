"""Release cuts retain source, immutable origins and the next native history boundary."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import release_cuts as cuts  # noqa: E402


def git(root, *args):
    return subprocess.check_output(("git", *args), cwd=root, text=True).strip()


def commit(root, message):
    git(root, "add", ".")
    git(root, "commit", "--quiet", "-m", message)
    return git(root, "rev-parse", "HEAD")


def repository(root):
    git(root, "init", "--quiet", "-b", "main")
    for key, value in (
        ("user.name", "Test"),
        ("user.email", "test@example.invalid"),
        ("commit.gpgsign", "false"),
        ("core.hooksPath", "/dev/null"),
    ):
        git(root, "config", key, value)


@pytest.mark.parametrize("change", ["edit", "delete", "replace"])
def test_accepted_origin_cannot_be_changed_through_a_later_source_pr(tmp_path, monkeypatch, change):
    """Normal main commits must not rewrite the publisher's accepted origin decision."""
    repository(tmp_path)
    path = tmp_path / ".github/release-cuts/v1.0.0.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"source": "first"}')
    commit(tmp_path, "chore: record origin")
    if change in ("delete", "replace"):
        path.unlink()
        commit(tmp_path, "chore: remove origin")
    if change != "delete":
        path.write_text('{"source": "second"}')
        commit(tmp_path, "chore: change origin")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ValueError, match="write-once"):
        cuts.origin_at("main", "v1.0.0")


@pytest.mark.parametrize("change", ["parent", "tree"])
def test_native_tagging_rejects_an_unreviewed_squash_outcome(monkeypatch, change):
    source, head, release, tree = "a" * 40, "b" * 40, "c" * 40, "d" * 40

    def api(path, **kwargs):
        return {
            "parents": [{"sha": "e" * 40 if change == "parent" else source}],
            "commit": {
                "tree": {"sha": tree if path.endswith(head) or change != "tree" else "f" * 40}
            },
        }

    monkeypatch.setattr(cuts.evidence, "api", api)
    monkeypatch.setattr(cuts.evidence, "optional_api", api)
    with pytest.raises(ValueError, match="sole parent|tree differs"):
        cuts.verify_squash(source, head, release)


def test_wrong_committed_plugin_version_cannot_tag_a_release(tmp_path):
    """A structurally identical plugin manifest can still install the wrong release."""
    (tmp_path / "pyproject.toml").write_text('[project]\nversion="1.2.0"\n')
    plugin = tmp_path / "plugins/claude-code/.claude-plugin/plugin.json"
    plugin.parent.mkdir(parents=True)
    plugin.write_text('{"version":"1.1.0"}')
    with pytest.raises(ValueError, match="selected release version"):
        cuts.check_packages(tmp_path, "1.2.0")


def native(root, branch, releases, recording, *, pull_requests=None):
    result = subprocess.run(
        ("node", str(ROOT / "tests/fixtures/release/pinned_release_please.cjs")),
        input=json.dumps(
            {
                "root": str(root),
                "branch": branch,
                "releases": releases,
                "recording": str(recording),
                "pullRequests": pull_requests or [],
                "operation": "releases" if pull_requests else "proposal",
            }
        ),
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def refresh_lock(root, env, *arguments):
    subprocess.run(
        ("uv", "lock", *arguments), cwd=root, env=env, capture_output=True, check=True, timeout=180
    )
    project = tomllib.loads((root / "pyproject.toml").read_text())["project"]
    path = root / "uv.lock"
    content = path.read_text()
    assignment = f'name = "{project["name"]}"\nversion = "{project["version"]}"'
    # uv rewrites comments; a valid source PR must preserve the repository's root version marker.
    assert content.count(assignment) == 1
    path.write_text(content.replace(assignment, assignment + " # x-release-please-version", 1))


def generate(root, runtime):
    env = {**os.environ, "PYTHONPATH": str(root / "src"), "UV_PROJECT_ENVIRONMENT": str(runtime)}
    for args in (
        ("uv", "run", "--frozen", "python", "scripts/cloud-plugin.py", "build"),
        (
            "uv",
            "run",
            "--frozen",
            "exomem",
            "package-skills",
            "--plugin-root",
            "plugins/claude-code",
        ),
    ):
        subprocess.run(args, cwd=root, env=env, capture_output=True, check=True, timeout=180)


@pytest.mark.skipif(
    not os.environ.get("RELEASE_PLEASE_NODE_MODULES"),
    reason="explicit pinned Release Please integration proof",
)
@pytest.mark.timeout(300)
def test_two_native_cuts_keep_intervening_source_dependencies_and_generated_instructions(
    tmp_path, monkeypatch
):
    """A native second release must include a change main received while the first source qualified."""
    root = tmp_path / "repository"
    root.mkdir()
    archive = subprocess.check_output(("git", "archive", "HEAD"), cwd=ROOT)
    subprocess.run(("tar", "-xf", "-", "-C", str(root)), input=archive, check=True)
    repository(root)
    git(root, "remote", "add", "origin", str(root))
    monkeypatch.setenv("NODE_PATH", os.environ["RELEASE_PLEASE_NODE_MODULES"])
    baseline = commit(root, "chore: initial released source")
    initial_version = json.loads((root / ".release-please-manifest.json").read_text())["."]
    git(root, "tag", f"v{initial_version}")
    releases = [{"tag": f"v{initial_version}", "sha": baseline}]
    (root / "selected.txt").write_text("selected behavior\n")
    source = commit(root, "feat: selected behavior")
    branch = cuts.evidence.cut_branch(source)
    git(root, "branch", branch, source)
    # C's Full receipt is deliberately unavailable while ordinary main advances.
    (root / "intervening.txt").write_text("intervening behavior\n")
    pyproject = root / "pyproject.toml"
    pyproject.write_text(pyproject.read_text().replace('"httpx>=0.27"', '"httpx>=0.28"'))
    env = {**os.environ, "UV_PROJECT_ENVIRONMENT": str(tmp_path / "runtime")}
    refresh_lock(root, env, "--upgrade-package", "httpx2")
    generator = root / "src/exomem/_scaffold/_Schema/references/recall.md"
    generator.write_text(generator.read_text() + "\nUse the selected evidence boundary.\n")
    subprocess.run(
        ("uv", "run", "--frozen", "python", "scripts/refresh-skill-contract.py"),
        cwd=root,
        env=env,
        capture_output=True,
        check=True,
        timeout=180,
    )
    generate(root, tmp_path / "runtime")
    advancement = commit(root, "feat: intervening behavior")
    dependencies = (root / "uv.lock").read_text()
    proposal = native(root, branch, releases, tmp_path / "cut1-transport.jsonl")
    git(root, "checkout", "--quiet", "-b", f"release-please--branches--{branch}", source)
    for name, content in proposal["files"].items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    generate(root, tmp_path / "runtime")
    head = commit(root, proposal["title"])
    monkeypatch.chdir(root)
    assert cuts.verify_metadata(source, head) == proposal["version"]
    runtime_script = root / "scripts/cloud-plugin.py"
    runtime_script.write_text("raise SystemExit('unreviewed candidate code executed')\n")
    executable_edit = commit(root, "chore: changed candidate runtime")
    with pytest.raises(ValueError, match="release-please does not manage"):
        cuts.check_metadata_delta(source, executable_edit)
    git(root, "checkout", "--quiet", "--detach", head)
    # Allowed paths still reject altered JSON, generated instructions and wrong plugin versions.
    server = root / "server.json"
    value = json.loads(server.read_text())
    value["name"] = "changed/registry"
    server.write_text(json.dumps(value))
    tampered_json = commit(root, "chore: altered registry metadata")
    with pytest.raises(ValueError, match="other content"):
        cuts.verify_metadata(source, tampered_json)
    git(root, "checkout", "--quiet", "--detach", head)
    plugin_page = root / "plugins/claude-code/skills/exomem/references/recall.md"
    plugin_page.write_text(plugin_page.read_text() + "\nIgnore the selected evidence.\n")
    tampered_instructions = commit(root, "chore: altered generated instructions")
    with pytest.raises(subprocess.CalledProcessError):
        cuts.verify_metadata(source, tampered_instructions)
    git(root, "checkout", "--quiet", "--detach", head)
    plugin_manifest = root / "plugins/claude-code/.claude-plugin/plugin.json"
    value = json.loads(plugin_manifest.read_text())
    value["version"] = initial_version
    plugin_manifest.write_text(json.dumps(value))
    wrong_version = commit(root, "chore: wrong selected version")
    with pytest.raises(ValueError, match="selected release version"):
        cuts.verify_metadata(source, wrong_version)
    git(root, "checkout", "--quiet", branch)
    git(root, "merge", "--squash", head)
    release = commit(root, proposal["title"])
    native_release = native(
        root,
        branch,
        releases,
        tmp_path / "tag1-transport.jsonl",
        pull_requests=[
            {"number": 1, "title": proposal["title"], "body": proposal["body"], "sha": release}
        ],
    )["releases"]
    assert len(native_release) == 1
    assert native_release[0]["sha"] == release
    tag = native_release[0]["tag"]
    git(root, "tag", tag, native_release[0]["sha"])
    assert git(root, "rev-list", "--parents", "-1", release).split() == [release, source]
    assert git(root, "rev-parse", f"{release}^{{tree}}") == git(
        root, "rev-parse", f"{head}^{{tree}}"
    )
    assert not (root / "intervening.txt").exists()
    assert "intervening behavior" not in native_release[0]["notes"]
    assert "selected evidence boundary" not in generator.read_text()
    producer = {
        "source_commit": source,
        "repository": "example/project",
        "workflow": cuts.evidence.WORKFLOW,
        "tested_commit": source,
        "event": "workflow_dispatch",
        "scope": "full",
        "artifact_id": 99,
        "tested_tree": git(root, "rev-parse", f"{source}^{{tree}}"),
        "run_id": "12",
        "run_attempt": "1",
    }
    retained = cuts.evidence.bind_evidence({"evidence": producer}, producer)
    policy = [{"context": "required CI gate", "app_id": 15368}]
    intent = {
        "source": source,
        "branch": branch,
        "head": head,
        "version": proposal["version"],
        "policy": policy,
        "evidence": retained,
        "fallback_requested": True,
        "predecessor": {
            "tag": f"v{initial_version}",
            "version": initial_version,
            "source": baseline,
            "reconciliation_commit": baseline,
        },
    }
    merged_pr = {
        "number": 1,
        "state": "closed",
        "merged": True,
        "merge_commit_sha": release,
        "base": {"ref": branch, "sha": source},
        "head": {
            "ref": f"release-please--branches--{branch}",
            "sha": head,
            "repo": {"full_name": "example/project"},
        },
        "body": cuts.INTENT_START + json.dumps(intent) + cuts.INTENT_END,
    }

    reconciliation_prs = []
    expired = {"value": False}
    unrelated = {"source": None}
    policy = [{"context": "required CI gate", "app_id": 15368}]

    def api(path, **kwargs):
        if path.startswith("releases/tags/"):
            return {"body": native_release[0]["notes"]}
        if path == "branches/main":
            return {"protection": {"required_status_checks": {"checks": policy}}}
        if path == "actions/workflows/ci.yml":
            return {"id": 8}
        if path.startswith("actions/workflows/ci.yml/runs?"):
            return [{"workflow_runs": []}]
        if path.startswith("commits/") and path.endswith("/pulls"):
            return []
        if path.startswith("actions/runs/"):
            assert not expired["value"], "accepted origin used expired temporary evidence"
            return {
                "id": 12,
                "run_attempt": 1,
                "workflow_id": 8,
                "path": cuts.evidence.WORKFLOW,
                "event": "workflow_dispatch",
                "conclusion": "success",
                "repository": {"full_name": "example/project"},
                "head_repository": {"full_name": "example/project"},
            }
        if "/check-runs?" in path:
            return [
                {
                    "check_runs": [
                        {
                            "name": "required CI gate",
                            "app": {"id": 15368},
                            "status": "completed",
                            "conclusion": "success",
                        }
                    ]
                }
            ]
        if path.endswith("/status?per_page=100"):
            return [{"statuses": []}]
        if path.startswith("pulls?state=all"):
            return [merged_pr]
        if path == "pulls/1":
            return merged_pr
        if path.startswith("pulls?state=open&base=main"):
            return [pr for pr in reconciliation_prs if not pr["merged"]]
        if path.startswith("pulls/") and path != "pulls/1":
            pr = next(pr for pr in reconciliation_prs if str(pr["number"]) == path.split("/")[1])
            if not pr["merged"] and unrelated["source"] is None:
                (root / "during-checks.txt").write_text("ordinary source during metadata CI\n")
                unrelated["source"] = commit(root, "feat: ordinary source during metadata checks")
            if not pr["merged"]:
                pr["head"]["sha"] = git(root, "rev-parse", f"refs/heads/release-reconcile/{source}")
            return pr
        if path.startswith("pulls?state=open"):
            return [[]]
        if path == "releases/latest":
            return {"tag_name": tag}
        if path.startswith("commits/"):
            sha = path.split("/", 1)[1]
            return {
                "sha": sha,
                "parents": [
                    {"sha": parent}
                    for parent in git(root, "show", "-s", "--format=%P", sha).split()
                ],
                "commit": {"tree": {"sha": git(root, "rev-parse", f"{sha}^{{tree}}")}},
            }
        raise AssertionError(path)

    monkeypatch.setenv("GH_REPO", "example/project")
    monkeypatch.setattr(cuts.evidence, "api", api)

    def optional_api(path):
        if path.startswith("commits/"):
            return api(path)
        ref = path.removeprefix("git/ref/heads/")
        exists = (
            subprocess.run(
                ("git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"), cwd=root
            ).returncode
            == 0
        )
        return {"object": {"sha": git(root, "rev-parse", ref)}} if exists else None

    monkeypatch.setattr(cuts.evidence, "optional_api", optional_api)
    monkeypatch.setattr(cuts.evidence, "read_receipt", lambda run, repository: producer)
    resumed = cuts.prepare(source)
    record = cuts.qualify(resumed)
    assert resumed["source"] == source and resumed["merged"] is True
    assert record["release"] == git(root, "rev-parse", tag) == release
    assert record["evidence"] == producer and record["version"] == proposal["version"]
    git(root, "checkout", "--quiet", "main")
    original_command = cuts.evidence.command
    raced = {"value": False, "parent": None, "squash": None, "interrupted": False}

    def write_api(method, path, value):
        if method == "POST" and path == "git/refs":
            git(root, "update-ref", value["ref"], value["sha"])
            return {}
        assert method == "POST" and path == "pulls"
        pr = {
            "number": len(reconciliation_prs) + 2,
            "merged": False,
            "state": "open",
            "head": {"sha": git(root, "rev-parse", f"refs/heads/release-reconcile/{source}")},
            "title": value["title"],
        }
        reconciliation_prs.append(pr)
        return pr

    def command(*args, **kwargs):
        if args[:3] != ("gh", "pr", "merge"):
            assert args[0] != "gh", "outbound write escaped the fixture"
            return original_command(*args, **kwargs)
        pr = next(pr for pr in reconciliation_prs if str(pr["number"]) == args[3])
        if not raced["value"]:
            # This source arrives after prospective validation, inside the merge transport.
            page = root / "src/exomem/_scaffold/_Schema/references/late-release/usage.md"
            page.parent.mkdir(parents=True)
            page.write_text("Use exomem@<exomem-release> with the selected source.\n")
            pyproject.write_text(pyproject.read_text().replace('"httpx>=0.28"', '"httpx>=0.28.1"'))
            refresh_lock(root, env, "--offline")
            subprocess.run(
                ("uv", "run", "--frozen", "python", "scripts/refresh-skill-contract.py"),
                cwd=root,
                env=env,
                capture_output=True,
                check=True,
                timeout=180,
            )
            generate(root, tmp_path / "runtime")
            raced["parent"] = commit(root, "feat: late generator input")
        parent = git(root, "rev-parse", "main")
        merged_tree = git(
            root, "merge-tree", "--write-tree", parent, pr["head"]["sha"]
        ).splitlines()[0]
        merged = git(root, "commit-tree", merged_tree, "-p", parent, "-m", pr["title"])
        # This is the disposable fixture's own main and all inputs are committed.
        git(root, "reset", "--hard", merged)
        pr.update(merged=True, state="closed", merge_commit_sha=merged)
        if not raced["value"]:
            raced.update(value=True, squash=merged)
        return b""

    original_trusted_main = cuts.trusted_main

    def trusted_main():
        if raced["value"] and not raced["interrupted"]:
            # The controller reached its repair pass only after the actual-parent projection differed.
            raced["interrupted"] = True
            raise ValueError("simulated interruption after origin acceptance")
        return original_trusted_main()

    monkeypatch.setattr(cuts, "trusted_main", trusted_main)
    monkeypatch.setattr(cuts, "write_api", write_api)
    monkeypatch.setattr(cuts.evidence, "command", command)
    with pytest.raises(ValueError, match="interruption after origin acceptance"):
        cuts.reconcile(record)
    assert cuts.origin_at("main", tag) == record
    late_plugin = root / "plugins/claude-code/skills/exomem/references/late-release/usage.md"
    assert initial_version in late_plugin.read_text()
    expired["value"] = True
    original_select = cuts.evidence.select_evidence
    # A green receipt from an older package contract cannot bless this stale source.
    monkeypatch.setattr(cuts.evidence, "select_evidence", lambda source, repo: {"run_id": "11"})
    with pytest.raises(ValueError, match="completed preceding reconciliation"):
        cuts.prepare(raced["squash"])
    monkeypatch.setattr(cuts.evidence, "select_evidence", original_select)
    cuts.reconcile(record)
    reconciled = git(root, "rev-parse", "main")
    assert reconciled != raced["squash"] and proposal["version"] in late_plugin.read_text()
    assert len(reconciliation_prs) == 2
    assert (root / "during-checks.txt").exists()
    assert git(root, "log", "--format=%H", "main", "--", cuts.origin_path(tag)).splitlines() == [
        raced["squash"]
    ]
    assert cuts.origin_at("main", tag) == record
    cuts.verify_tag(tag, reconciled)
    assert (root / "intervening.txt").read_text() == "intervening behavior\n"
    assert "httpx>=0.28.1" in pyproject.read_text()

    previous_lock = tomllib.loads(dependencies)
    current_lock = tomllib.loads((root / "uv.lock").read_text())
    assert [
        (package["name"], package["version"])
        for package in current_lock["package"]
        if package["name"] != "exomem"
    ] == [
        (package["name"], package["version"])
        for package in previous_lock["package"]
        if package["name"] != "exomem"
    ]
    assert (
        "selected evidence boundary"
        in (root / "plugins/claude-code/skills/exomem/references/recall.md").read_text()
    )
    assert (
        json.loads((root / "release-please-config.json").read_text())["last-release-sha"] == source
    )
    assert (
        subprocess.run(("git", "merge-base", "--is-ancestor", release, "main"), cwd=root).returncode
        == 1
    )
    cuts.evidence.validate_predecessor(
        reconciled,
        proposal["version"],
        {"version": proposal["version"], "reconciliation_commit": reconciled},
    )
    second_cut = cuts.prepare(reconciled)
    second_branch = second_cut["branch"]
    assert second_cut["source"] == reconciled and second_cut["propose"] is True
    original_projection = cuts.reconciliation_tree
    monkeypatch.setattr(
        cuts.evidence,
        "select_evidence",
        lambda source, repo: {
            "source_commit": reconciled,
            "tested_tree": git(root, "rev-parse", f"{reconciled}^{{tree}}"),
            "package_checks": cuts.evidence.PACKAGE_CHECKS,
        },
    )
    monkeypatch.setattr(
        cuts,
        "reconciliation_tree",
        lambda *args: pytest.fail(
            "current authentic package proof should replace local projection"
        ),
    )
    assert cuts.prepare(reconciled)["source"] == reconciled
    monkeypatch.setattr(cuts, "reconciliation_tree", original_projection)
    monkeypatch.setattr(cuts.evidence, "select_evidence", original_select)
    releases.insert(0, {"tag": tag, "sha": release, "notes": native_release[0]["notes"]})
    second = native(root, second_branch, releases, tmp_path / "cut2-transport.jsonl")
    assert second["body"].count("intervening behavior") == 1
    assert "selected behavior" not in second["body"]
    assert second["version"] != proposal["version"]
    git(root, "checkout", "--quiet", "-b", f"release-please--branches--{second_branch}", reconciled)
    for name, content in second["files"].items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(content)
    generate(root, tmp_path / "runtime")
    second_head = commit(root, second["title"])
    git(root, "checkout", "--quiet", second_branch)
    git(root, "merge", "--squash", second_head)
    second_release = commit(root, second["title"])
    second_native = native(
        root,
        second_branch,
        releases,
        tmp_path / "tag2-transport.jsonl",
        pull_requests=[
            {"number": 2, "title": second["title"], "body": second["body"], "sha": second_release}
        ],
    )["releases"]
    assert len(second_native) == 1 and second_native[0]["sha"] == second_release
    assert second_native[0]["notes"].count("intervening behavior") == 1
    assert "selected behavior" not in second_native[0]["notes"]
    git(root, "tag", second_native[0]["tag"], second_native[0]["sha"])
    assert (root / "intervening.txt").exists()
    assert "selected evidence boundary" in plugin_page.read_text()
    git(root, "checkout", "--quiet", "main")
    second_producer = {
        **producer,
        "source_commit": reconciled,
        "tested_commit": reconciled,
        "tested_tree": git(root, "rev-parse", f"{reconciled}^{{tree}}"),
        "run_id": "13",
    }
    second_record = cuts.make_origin(
        {"merge_commit_sha": second_release},
        {
            "source": reconciled,
            "branch": second_branch,
            "head": second_head,
            "version": second["version"],
            "evidence": second_producer,
            "policy": policy,
            "predecessor": {
                "tag": tag,
                "version": record["version"],
                "source": source,
                "reconciliation_commit": reconciled,
            },
        },
    )
    cuts.reconcile_files(root, second_record, second_native[0]["notes"])
    generate(root, tmp_path / "runtime")
    final_main = commit(root, "chore: reconcile later published metadata")
    current_notes = (root / "CHANGELOG.md").read_text()
    cuts.reconcile(record)
    assert git(root, "rev-parse", "main") == final_main
    assert (
        json.loads((root / ".release-please-manifest.json").read_text())["."] == second["version"]
    )
    assert (
        json.loads((root / "release-please-config.json").read_text())["last-release-sha"]
        == reconciled
    )
    assert (root / "CHANGELOG.md").read_text() == current_notes
    print(
        json.dumps(
            {
                "first_source": source,
                "main_advancement": advancement,
                "first_head": head,
                "first_release": release,
                "tag": tag,
                "first_version": proposal["version"],
                "second_version": second["version"],
                "second_release": second_release,
                "reconciliation": reconciled,
                "raced_reconciliation": raced["squash"],
                "unrelated_during_checks": unrelated["source"],
                "repair_prs": len(reconciliation_prs),
                "producer": retained,
                "intervening_notes_count": second["body"].count("intervening behavior"),
                "transport": [
                    "cut1-transport.jsonl",
                    "tag1-transport.jsonl",
                    "cut2-transport.jsonl",
                    "tag2-transport.jsonl",
                ],
            }
        )
    )


def test_duplicate_intents_dispatch_at_most_one_full_fallback(monkeypatch):
    """A controller interruption after dispatch must not spend another Full run on retry."""
    source, head = "a" * 40, "b" * 40
    cut = {
        "source": source,
        "branch": cuts.evidence.cut_branch(source),
        "policy": [{"context": "required CI gate", "app_id": 15368}],
    }
    pr = {"number": 1, "merged": False, "head": {"sha": head}, "body": ""}
    monkeypatch.setenv("GH_REPO", "example/project")
    monkeypatch.setenv("GH_TOKEN", "fixture")
    monkeypatch.setattr(cuts, "release_pull", lambda branch: pr)
    monkeypatch.setattr(
        cuts,
        "save_intent",
        lambda current, intent: current.update(
            body=cuts.INTENT_START + json.dumps(intent) + cuts.INTENT_END
        ),
    )
    monkeypatch.setattr(cuts.evidence, "select_evidence", lambda source, repo: None)
    monkeypatch.setattr(cuts.evidence, "full_inflight", lambda source, repository: False)
    monkeypatch.setattr(cuts, "POLLS", 1)
    monkeypatch.setattr(cuts.time, "sleep", lambda seconds: None)
    dispatches = []
    monkeypatch.setattr(cuts.evidence, "command", lambda *args, **kwargs: dispatches.append(args))
    for _ in range(2):
        with pytest.raises(ValueError, match="Full evidence is pending"):
            cuts.qualify(cut, evidence_only=True)
    assert len(dispatches) == 1
    assert dispatches[0][-1] == cut["branch"]


def test_new_squash_graph_waits_for_github_visibility_without_rewriting_it(monkeypatch):
    """A merge result can be visible on the PR before the commit read catches up."""
    source, head, release, tree = "a" * 40, "b" * 40, "c" * 40, "d" * 40
    replies = iter([None, {"parents": [{"sha": source}], "commit": {"tree": {"sha": tree}}}])
    monkeypatch.setattr(cuts.evidence, "optional_api", lambda path: next(replies))
    monkeypatch.setattr(cuts.evidence, "api", lambda path: {"commit": {"tree": {"sha": tree}}})
    monkeypatch.setattr(cuts.time, "sleep", lambda seconds: None)
    assert cuts.verify_squash(source, head, release)["parents"] == [{"sha": source}]


def test_failed_required_check_stops_a_cut_before_metadata_execution_or_merge(monkeypatch):
    """A failed configured App check must stop the isolated cut's external merge."""
    source, head = "a" * 40, "b" * 40
    cut = {"source": source, "branch": cuts.evidence.cut_branch(source)}
    intent = {
        **cut,
        "head": head,
        "policy": [{"context": "required CI gate", "app_id": 15368}],
        "evidence": {"run_id": "12"},
    }
    pr = {
        "number": 1,
        "merged": False,
        "head": {"sha": head},
        "body": cuts.INTENT_START + json.dumps(intent) + cuts.INTENT_END,
    }
    monkeypatch.setattr(cuts, "release_pull", lambda branch: pr)
    monkeypatch.setattr(cuts.evidence, "release_pr", lambda *args: pr)
    monkeypatch.setattr(cuts.evidence, "main_check_policy", lambda: intent["policy"])
    monkeypatch.setattr(
        cuts.evidence,
        "api",
        lambda path, **kwargs: (
            [
                {
                    "check_runs": [
                        {
                            "name": "required CI gate",
                            "app": {"id": 15368},
                            "status": "completed",
                            "conclusion": "failure",
                        }
                    ]
                }
            ]
            if "check-runs" in path
            else [{"statuses": []}]
        ),
    )
    writes = []
    monkeypatch.setattr(cuts.evidence, "command", lambda *args, **kwargs: writes.append(args))
    with pytest.raises(ValueError, match="required checks failed: required CI gate"):
        cuts.qualify(cut)
    assert writes == []


@pytest.mark.parametrize(
    "package_checks",
    [None, cuts.evidence.PACKAGE_CHECKS],
    ids=["old-full-requires-projection", "current-full-reuses-proof"],
)
def test_prepare_reuses_only_the_current_full_package_contract(monkeypatch, package_checks):
    """A generic green Full must not admit a source before prior metadata repair."""
    source, previous_source, main = "b" * 40, "a" * 40, "c" * 40
    monkeypatch.setenv("GH_REPO", "example/project")
    monkeypatch.setattr(cuts, "trusted_main", lambda: main)
    monkeypatch.setattr(cuts, "git", lambda *args, **kwargs: "d" * 40)
    monkeypatch.setattr(cuts, "release_pull", lambda branch: None)
    monkeypatch.setattr(
        cuts, "origin_at", lambda main, tag: {"version": "1.0.0", "source": previous_source}
    )
    monkeypatch.setattr(cuts, "verify_origin", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cuts,
        "read_json",
        lambda ref, path: (
            {".": "1.0.0"}
            if path.endswith("manifest.json")
            else {"last-release-sha": previous_source}
        ),
    )
    monkeypatch.setattr(cuts.evidence, "validate_predecessor", lambda *args: None)
    monkeypatch.setattr(
        cuts.evidence,
        "api",
        lambda path, **kwargs: [[]] if path.startswith("pulls?") else {"tag_name": "v1.0.0"},
    )
    monkeypatch.setattr(cuts.evidence, "optional_api", lambda path: None)
    monkeypatch.setattr(
        cuts.evidence,
        "main_check_policy",
        lambda: [{"context": "required CI gate", "app_id": 15368}],
    )
    monkeypatch.setattr(
        cuts.evidence, "select_evidence", lambda *args: {"package_checks": package_checks}
    )

    def unavailable_projection(*args):
        raise ValueError("local package projection unavailable")

    monkeypatch.setattr(cuts, "reconciliation_tree", unavailable_projection)
    writes = []
    monkeypatch.setattr(cuts, "write_api", lambda *args: writes.append(args))
    if package_checks is None:
        with pytest.raises(ValueError, match="local package projection unavailable"):
            cuts.prepare(source)
        assert writes == []
    else:
        assert cuts.prepare(source)["source"] == source
        assert len(writes) == 1
