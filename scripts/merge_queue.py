"""Test queued pull requests together on main's full CI run before they land.

GitHub's merge queue is available only to organization-owned repositories, so
this script is the repository's queue. Pull request CI runs only the changed
tests and the guard modules, and a change can still break a test it never
touched. The queue runs every shard on the exact tree main will have, before
main has it.

A pull request joins the queue when auto-merge is enabled on it and its
`required CI gate` passed. The `merge queue` commit status on its head is the
queue's whole state, and main's branch protection requires it:

1. `advance` (merge-queue.yml) squashes the queued pull requests onto main in
   the order auto-merge was enabled, pushes the result as a `merge-queue/`
   branch, and dispatches ci.yml on it with `queue` naming the members.
2. ci.yml runs the jobs that a push to main runs. Its `merge queue verdict` job
   calls `verdict`. On success, each member's status names the tested tree, and
   auto-merge lands the members. A failed batch sends each member to a retest
   alone, so one breaking change does not hold the others. A member fails only
   when it also fails alone.
3. `tested` runs in a push to main. It reports whether the merged pull
   request's queue run passed on the same tree, so main's run can skip the core
   and harness shards that would repeat those tests on the same files.

Usage: merge_queue.py advance | verdict | tested
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ci_change_scope import docs_only
# The candidates' branch family. Nothing protects it, so the workflow token can push it.
from release_evidence import MERGE_QUEUE_PREFIX as BRANCH_PREFIX

#: The commit status that holds the queue's state; main's branch protection requires it.
CONTEXT = "merge queue"
#: ci.yml's aggregate check. The queue takes only pull requests whose own CI passed.
PR_GATE = "required CI gate"
#: The workflow token cannot push a commit that changes workflow files (GitHub refuses it).
WORKFLOWS = ".github/workflows/"
#: A batch's cap. A larger batch saves runs only when every member passes.
BATCH_LIMIT = 8
#: A queue run takes about 25 minutes. A testing status older than this lost its run.
TESTING_TIMEOUT = timedelta(minutes=120)
#: Auto-merge lands a passed member within seconds; the next batch waits for it to land.
MERGE_WAIT = timedelta(minutes=15)

# Each status description starts with one of these words, so the next step can read it.
TESTING, RETEST, PASSED, FAILED = "testing", "retest alone", "passed", "failed"


def run(*args: str, input: str | None = None) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, input=input).stdout


def git(*args: str) -> str:
    return run("git", *args).strip()


def repository() -> str:
    return os.environ["GITHUB_REPOSITORY"]


def api(path: str, *fields: str, method: str = "GET") -> object:
    flags = [arg for field in fields for arg in ("-f", field)]
    out = run("gh", "api", "-X", method, f"repos/{repository()}/{path}", *flags)
    return json.loads(out) if out.strip() else None


def set_status(sha: str, state: str, description: str, target_url: str = "") -> None:
    fields = [f"state={state}", f"context={CONTEXT}", f"description={description[:140]}"]
    if target_url:
        fields.append(f"target_url={target_url}")
    api(f"statuses/{sha}", *fields, method="POST")


@dataclass(frozen=True)
class Status:
    state: str  # GitHub's commit status states: pending, success, failure, error
    description: str
    created: datetime


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    head: str
    draft: bool
    auto_merge: datetime | None
    gate_passed: bool
    queue: Status | None


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_OPEN_PULLS = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, baseRefName: "main", first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title isDraft headRefOid
        autoMergeRequest { enabledAt }
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name conclusion }
          ... on StatusContext { context state description createdAt }
        } } } } } }
      }
    }
  }
}
"""


def open_pulls() -> list[PullRequest]:
    owner, name = repository().split("/")
    pulls, cursor = [], None
    while True:
        args = ["gh", "api", "graphql", "-f", f"query={_OPEN_PULLS}", "-f", f"owner={owner}", "-f", f"name={name}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        page = json.loads(run(*args))["data"]["repository"]["pullRequests"]
        for node in page["nodes"]:
            commits = node["commits"]["nodes"]
            rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
            contexts = rollup["contexts"]["nodes"] if rollup else []
            gate = any(c.get("name") == PR_GATE and c.get("conclusion") == "SUCCESS" for c in contexts)
            queue = next(
                (
                    Status(c["state"].lower(), c.get("description") or "", _time(c["createdAt"]))
                    for c in contexts
                    if c.get("context") == CONTEXT
                ),
                None,
            )
            auto = node["autoMergeRequest"]
            pulls.append(
                PullRequest(
                    number=node["number"],
                    title=node["title"],
                    head=node["headRefOid"],
                    draft=node["isDraft"],
                    auto_merge=_time(auto["enabledAt"]) if auto else None,
                    gate_passed=gate,
                    queue=queue,
                )
            )
        if not page["pageInfo"]["hasNextPage"]:
            return pulls
        cursor = page["pageInfo"]["endCursor"]


@dataclass(frozen=True)
class Plan:
    wait: str = ""  # why nothing starts now; empty when the plan has work
    kind: str = ""  # "batch", or "retest" for one member that failed in a batch
    members: tuple[PullRequest, ...] = ()


def plan(pulls: list[PullRequest], now: datetime, requeued: int | None = None) -> Plan:
    """What the queue does next, from the pull requests' state alone.

    Main moves only between batches, so a queue run always tests the tree main
    gets. `requeued` is the pull request whose auto-merge was just enabled
    again: enabling it again after a failure asks for a fresh test.
    """
    for pull in pulls:
        status = pull.queue
        if status and status.state == "pending" and status.description.startswith(TESTING):
            if now - status.created < TESTING_TIMEOUT:
                return Plan(wait=f"#{pull.number} is in a queue run")
    for pull in pulls:
        status = pull.queue
        if pull.auto_merge and status and status.state == "success" and now - status.created < MERGE_WAIT:
            return Plan(wait=f"#{pull.number} passed and is landing")

    def waiting(pull: PullRequest) -> bool:
        if pull.draft or pull.auto_merge is None or not pull.gate_passed:
            return False
        status = pull.queue
        if status is None or status.state == "pending":
            # A pending status here is a retest or a testing status whose run was lost.
            return True
        return status.state in ("failure", "error") and pull.number == requeued

    queued = sorted((pull for pull in pulls if waiting(pull)), key=lambda pull: pull.auto_merge)
    retests = [p for p in queued if p.queue and p.queue.state == "pending" and p.queue.description.startswith(RETEST)]
    if retests:
        return Plan(kind="retest", members=(retests[0],))
    if not queued:
        return Plan(wait="no pull request is queued")
    return Plan(kind="batch", members=tuple(queued[:BATCH_LIMIT]))


def changed_paths(base: str, head: str) -> list[str]:
    merge_base = git("merge-base", base, head)
    return [p for p in git("diff", "--name-only", "--no-renames", "-z", merge_base, head).split("\0") if p]


def squash(tip: str, main: str, head: str, message: str) -> str | None:
    """The commit that applies `head`'s change onto `tip`, as a squash merge would, or None on a conflict.

    The change is `head` against its merge base with main, the diff GitHub's
    squash merge applies, so the candidate's tree is the tree main gets.
    """
    merge_base = git("merge-base", main, head)
    result = subprocess.run(
        ["git", "merge-tree", "--write-tree", f"--merge-base={merge_base}", tip, head],
        capture_output=True,
        text=True,
    )
    if result.returncode == 1:
        return None
    result.check_returncode()
    tree = result.stdout.splitlines()[0]
    return git("commit-tree", tree, "-p", tip, "-m", message)


def advance() -> int:
    event = json.loads(open(os.environ["GITHUB_EVENT_PATH"]).read()) if os.environ.get("GITHUB_EVENT_PATH") else {}
    requeued = None
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request_target" and event.get("action") == "auto_merge_enabled":
        requeued = event["pull_request"]["number"]
    now = datetime.now(timezone.utc)
    decision = plan(open_pulls(), now, requeued)
    if decision.wait:
        print(f"Nothing to start: {decision.wait}.")
        return 0

    git("fetch", "--no-tags", "origin", "main")
    main = git("rev-parse", "FETCH_HEAD")
    for pull in decision.members:
        git("fetch", "--no-tags", "origin", f"+refs/pull/{pull.number}/head:refs/queue/{pull.number}")
    testable, bypassed = [], False
    for pull in decision.members:
        head = git("rev-parse", f"refs/queue/{pull.number}")
        if head != pull.head:
            print(f"#{pull.number} moved while the queue read it; the next run takes it.")
            continue
        if squash(main, main, head, "conflict check") is None:
            set_status(head, "failure", f"{FAILED}: conflicts with main; rebase onto main")
            print(f"#{pull.number} conflicts with main.")
            continue
        paths = changed_paths(main, head)
        bypass = ""
        if docs_only(paths):
            # Only docs-marked tests read these paths, and the pull request's own CI ran them.
            bypass = "a docs-only change"
        elif any(path.startswith(WORKFLOWS) for path in paths):
            # GitHub refuses the workflow token a push that changes workflows; main's
            # push run still runs every shard on a tree the queue did not test.
            bypass = "it changes workflows, which the queue cannot push; main's push run tests it"
        if bypass:
            set_status(head, "success", f"{PASSED} without a queue run: {bypass}")
            print(f"#{pull.number} passed without a queue run: {bypass}.")
            bypassed = True
        else:
            testable.append(pull)
    if bypassed:
        # Main moves only between batches: the next run starts the batch after these land.
        return 0

    tip, members = main, []
    for pull in testable:
        message = f"Merge queue: #{pull.number} {pull.title}\n\nHead: {pull.head}"
        commit = squash(tip, main, pull.head, message)
        if commit is None:
            print(f"#{pull.number} conflicts with a pull request ahead of it; the next batch takes it.")
            continue
        tip = commit
        members.append(pull)
    if not members:
        print("No queued pull request could join this batch.")
        return 0

    branch = f"{BRANCH_PREFIX}{main[:12]}-{os.environ.get('GITHUB_RUN_ID', 'local')}"
    git("push", "origin", f"{tip}:refs/heads/{branch}")
    queue = " ".join([decision.kind, *(f"{pull.number}@{pull.head}" for pull in members)])
    run("gh", "workflow", "run", "ci.yml", "--repo", repository(), "--ref", branch, "-f", f"queue={queue}")
    others = " ".join(f"#{pull.number}" for pull in members)
    label = "alone" if len(members) == 1 else f"with {others}"
    url = f"https://github.com/{repository()}/actions/workflows/ci.yml?query=branch%3A{branch}"
    for pull in members:
        set_status(pull.head, "pending", f"{TESTING} on main {main[:7]} {label}", url)
    print(f"Testing {others} on {branch}.")
    return 0


def parse_queue(queue: str) -> tuple[str, list[tuple[int, str]]]:
    kind, *tokens = queue.split()
    if kind not in ("batch", "retest") or not tokens:
        raise ValueError(f"unexpected queue input: {queue!r}")
    members = []
    for token in tokens:
        number, head = token.split("@")
        members.append((int(number), head))
    return kind, members


def verdicts(kind: str, members: list[tuple[int, str]], result: str, tree: str, base: str) -> list[tuple[str, str, str]]:
    """The (head, state, description) each member gets from a finished queue run.

    A batch that fails sends every member to a retest alone, so a flaky test or
    one breaking change never fails the others. A member fails only after it
    fails once more alone. A cancelled run tests its members again.
    """
    if result == "success":
        return [(head, "success", f"{PASSED} on main {base[:7]}; tree {tree}") for _, head in members]
    if result == "cancelled":
        return [(head, "pending", f"{RETEST}: the queue run was cancelled") for _, head in members]
    if kind == "retest":
        return [(head, "failure", f"{FAILED} alone after failing in a batch") for _, head in members]
    return [(head, "pending", f"{RETEST}: the batch failed") for _, head in members]


def verdict() -> int:
    kind, members = parse_queue(os.environ["QUEUE"])
    sha = os.environ["GITHUB_SHA"]
    tree = api(f"git/commits/{sha}")["tree"]["sha"]
    branch = os.environ["GITHUB_REF_NAME"]
    base = branch.removeprefix(BRANCH_PREFIX).split("-", 1)[0]
    url = f"{os.environ['GITHUB_SERVER_URL']}/{repository()}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    for head, state, description in verdicts(kind, members, os.environ["RESULT"], tree, base):
        set_status(head, state, description, url)
    run("gh", "workflow", "run", "merge-queue.yml", "--repo", repository(), "--ref", "main")
    try:
        api(f"git/refs/heads/{branch}", method="DELETE")
    except subprocess.CalledProcessError as error:
        # A leftover candidate branch costs nothing and protects nothing.
        print(f"Could not delete {branch}: {error.stderr}", file=sys.stderr)
    return 0


def tested() -> int:
    """Write `tested=true` when the queue passed this push's merged pull request on this exact tree."""
    sha = os.environ["GITHUB_SHA"]
    found = False
    try:
        tree = api(f"git/commits/{sha}")["tree"]["sha"]
        for pull in api(f"commits/{sha}/pulls"):
            statuses = api(f"commits/{pull['head']['sha']}/statuses")
            latest = next((s for s in statuses if s["context"] == CONTEXT), None)
            if latest and latest["state"] == "success" and latest["description"].endswith(f"tree {tree}"):
                found = True
    except (subprocess.CalledProcessError, KeyError, TypeError) as error:
        # Unknown means untested: the shards run.
        print(f"Could not read the queue's result: {error}", file=sys.stderr)
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        output.write(f"tested={'true' if found else 'false'}\n")
    print("The queue passed this tree." if found else "The queue did not pass this tree; the shards run.")
    return 0


if __name__ == "__main__":
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    sys.exit({"advance": advance, "verdict": verdict, "tested": tested}[command]())
