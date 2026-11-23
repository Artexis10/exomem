"""Test queued pull requests together on main's full CI run before they land.

GitHub's merge queue is available only to organization-owned repositories, so
this script is the repository's queue. Pull request CI runs only the changed
tests and the guard modules, and a change can still break a test it never
touched. The queue runs every shard on the exact tree main will have, before
main has it.

A pull request joins the queue when auto-merge is enabled on it and its own
required checks passed. The `merge queue` commit status on its head is the
queue's whole state, and main's branch protection requires it:

1. `advance` (merge-queue.yml) squashes the queued pull requests onto main in
   the order auto-merge was enabled, pushes the result as a `merge-queue/`
   branch, and dispatches ci.yml on it with `queue` naming the members.
2. ci.yml runs the jobs that a push to main runs. Its `merge queue verdict` job
   calls `verdict`. A failure first reruns the failed jobs once, so a flaky test
   fails nothing. On success, each member's status names the tested tree, and
   auto-merge lands the members. A batch that still fails sends each member to
   a retest alone, so one breaking change holds only itself.
3. `tested` runs in a push to main. It reports whether the merged pull
   request's queue run passed on the same tree, so main's run can skip the core
   and harness shards that would repeat those tests on the same files.

A pass that has not landed within MERGE_WAIT goes back into the queue, so it
never lands later on a tree that no run tested. A release reconcile
pull request passes without a queue run: it carries release metadata that the
release cut itself checks, and a cut must not wait on the queue.

Usage: merge_queue.py advance | verdict | tested
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ci_change_scope import docs_only
from release_evidence import MERGE_QUEUE_CONTEXT as CONTEXT
from release_evidence import MERGE_QUEUE_PREFIX as BRANCH_PREFIX
from release_evidence import RECONCILE_PREFIX

#: The pull request's own required checks (ci.yml's gate and pr-title.yml's title check).
#: The queue tests only a pull request that can land once the queue passes it.
PR_GATE = "required CI gate"
TITLE_CHECK = "Conventional Commit title"
#: GitHub refuses the workflow token a push that changes workflow files, and tests
#: read those files, so such a pull request passes without a queue run and main's
#: push run tests it.
WORKFLOWS = ".github/workflows/"
#: A batch's cap. A failed batch costs one retest per member.
BATCH_LIMIT = 4
#: A queue run takes about 25 minutes. A testing status older than this lost its run.
TESTING_TIMEOUT = timedelta(minutes=120)
#: Auto-merge lands a passed member within seconds; the next batch waits for it to land.
MERGE_WAIT = timedelta(minutes=15)

# Status descriptions are this script's own protocol: each starts with one of these
# tokens, which the next step reads back. A passed status ends with the tested tree.
TESTING, RETEST, QUEUED, PASSED, FAILED = "testing", "retest alone", "queued again", "passed", "failed"
RERUN = "rerunning failed jobs"


def run(*args: str, env: dict | None = None) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, env=env).stdout


def git(*args: str, env: dict | None = None) -> str:
    return run("git", *args, env=env).strip()


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
    state: str  # GitHub's commit status states, lowercase: pending, success, failure, error
    description: str
    created: datetime
    target_url: str = ""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    head: str
    branch: str
    draft: bool
    fork: bool
    auto_merge: datetime | None
    checks_passed: bool
    queue: Status | None


def _time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_OPEN_PULLS = """
query($owner: String!, $name: String!, $cursor: String) {
  repository(owner: $owner, name: $name) {
    ref(qualifiedName: "refs/heads/main") { target { oid } }
    pullRequests(states: OPEN, baseRefName: "main", first: 50, after: $cursor) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title isDraft isCrossRepository headRefOid headRefName
        autoMergeRequest { enabledAt }
        commits(last: 1) { nodes { commit { oid statusCheckRollup { contexts(first: 100) { nodes {
          __typename
          ... on CheckRun { name conclusion }
          ... on StatusContext { context state description createdAt targetUrl }
        } } } } } }
      }
    }
  }
}
"""


def open_pulls() -> tuple[str, list[PullRequest]]:
    """Main's head and every open pull request into main."""
    owner, name = repository().split("/")
    pulls, cursor, main = [], None, ""
    while True:
        args = ["gh", "api", "graphql", "-f", f"query={_OPEN_PULLS}", "-f", f"owner={owner}", "-f", f"name={name}"]
        if cursor:
            args += ["-f", f"cursor={cursor}"]
        data = json.loads(run(*args))["data"]["repository"]
        main = data["ref"]["target"]["oid"]
        page = data["pullRequests"]
        for node in page["nodes"]:
            commits = node["commits"]["nodes"]
            # Statuses are written to the head, so read them only from the head.
            current = commits and commits[0]["commit"]["oid"] == node["headRefOid"]
            rollup = commits[0]["commit"]["statusCheckRollup"] if current else None
            contexts = rollup["contexts"]["nodes"] if rollup else []
            passed = {c.get("name") for c in contexts if c.get("conclusion") == "SUCCESS"}
            statuses = [c for c in contexts if c.get("context") == CONTEXT]
            queue = None
            # An EXPECTED placeholder only says the status is missing.
            if statuses and statuses[0]["state"] != "EXPECTED":
                c = statuses[0]
                queue = Status(c["state"].lower(), c.get("description") or "", _time(c["createdAt"]), c.get("targetUrl") or "")
            auto = node["autoMergeRequest"]
            pulls.append(
                PullRequest(
                    number=node["number"],
                    title=node["title"],
                    head=node["headRefOid"],
                    branch=node["headRefName"],
                    draft=node["isDraft"],
                    fork=node["isCrossRepository"],
                    auto_merge=_time(auto["enabledAt"]) if auto else None,
                    checks_passed={PR_GATE, TITLE_CHECK} <= passed,
                    queue=queue,
                )
            )
        if not page["pageInfo"]["hasNextPage"]:
            return main, pulls
        cursor = page["pageInfo"]["endCursor"]


def _is(status: Status | None, state: str, token: str) -> bool:
    return status is not None and status.state == state and status.description.startswith(token)


def stale_passes(pulls: list[PullRequest], now: datetime) -> list[PullRequest]:
    """Open pull requests whose pass did not land in time.

    Such a pass would let the pull request land later, during another batch or
    on a newer main, on a tree no queue run tested.
    """
    return [
        pull
        for pull in pulls
        if not pull.branch.startswith(RECONCILE_PREFIX)
        and _is(pull.queue, "success", PASSED)
        and now - pull.queue.created >= MERGE_WAIT
    ]


def forks(pulls: list[PullRequest]) -> list[PullRequest]:
    """Fork pull requests asking to land that have not been told why they cannot."""
    return [pull for pull in pulls if pull.fork and pull.auto_merge and not _is(pull.queue, "failure", FAILED)]


def reconciles(pulls: list[PullRequest]) -> list[PullRequest]:
    """Release reconcile pull requests that still need their pass."""
    return [
        pull
        for pull in pulls
        if pull.branch.startswith(RECONCILE_PREFIX)
        and not pull.fork
        and pull.checks_passed
        and not _is(pull.queue, "success", PASSED)
    ]


@dataclass(frozen=True)
class Plan:
    wait: str = ""  # why nothing starts now; empty when the plan has work
    kind: str = ""  # "batch", or "retest" for one member of a failed batch
    members: tuple[PullRequest, ...] = ()


def plan(pulls: list[PullRequest], now: datetime) -> Plan:
    """What the queue does next, from the pull requests' state alone.

    Main moves only between batches, so a queue run always tests the tree main
    gets. Enabling auto-merge again after a failure asks for a fresh test.
    """
    for pull in pulls:
        if _is(pull.queue, "pending", TESTING) and now - pull.queue.created < TESTING_TIMEOUT:
            return Plan(wait=f"#{pull.number} is in a queue run")
    for pull in pulls:
        if pull.auto_merge and _is(pull.queue, "success", PASSED) and now - pull.queue.created < MERGE_WAIT:
            return Plan(wait=f"#{pull.number} passed and is landing")

    def waiting(pull: PullRequest) -> bool:
        if pull.draft or pull.auto_merge is None or not pull.checks_passed:
            return False
        # A fork's code would run with this repository's write token before it lands.
        if pull.fork or pull.branch.startswith(RECONCILE_PREFIX):
            return False
        status = pull.queue
        if status is None or status.state == "pending":
            # Pending here is a retest, a requeue, or a testing status whose run was lost.
            return True
        # Auto-merge enabled after the failure asks for a fresh test.
        return status.state in ("failure", "error") and pull.auto_merge > status.created

    queued = sorted((pull for pull in pulls if waiting(pull)), key=lambda pull: pull.auto_merge)
    retests = [pull for pull in queued if _is(pull.queue, "pending", RETEST)]
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
    return git("commit-tree", result.stdout.splitlines()[0], "-p", tip, "-m", message)


def run_id(url: str) -> str:
    # verdict() sets the target URL to the run's page, which ends with its id.
    return url.rstrip("/").rsplit("/", 1)[-1]


def rerun_failed_jobs(pulls: list[PullRequest]) -> None:
    """Rerun the failed jobs of each queue run whose verdict asked for it, once its run has finished."""
    pending = {}
    for pull in pulls:
        if _is(pull.queue, "pending", TESTING) and pull.queue.description.endswith(RERUN):
            pending.setdefault(pull.queue.target_url, []).append(pull)
    for url, members in pending.items():
        identifier = run_id(url)
        try:
            # The verdict job asks for the rerun as the run's last step; the run finishes seconds later.
            for _ in range(30):
                if api(f"actions/runs/{identifier}")["status"] == "completed":
                    break
                time.sleep(10)
            run("gh", "run", "rerun", identifier, "--failed", "--repo", repository())
        except (subprocess.CalledProcessError, KeyError, ValueError) as error:
            # The testing status times out and queues the members again; the rest of the queue moves on.
            print(f"Could not rerun run {identifier}: {getattr(error, 'stderr', error)}", file=sys.stderr)
            continue
        for pull in members:
            description = pull.queue.description.removesuffix(RERUN).rstrip(": ")
            set_status(pull.head, "pending", description, url)
        print(f"Reran the failed jobs of run {identifier}.")


def wake() -> None:
    """Start another controller run, which reads the statuses this one just wrote."""
    run("gh", "workflow", "run", "merge-queue.yml", "--repo", repository(), "--ref", "main")


def unbuildable(pull: PullRequest, error: subprocess.CalledProcessError) -> None:
    """Fail one pull request the queue cannot build, so it never stops the others."""
    reason = (error.stderr or "").strip().splitlines()[-1:] or [f"git exited {error.returncode}"]
    set_status(pull.head, "failure", f"{FAILED}: the queue could not build it: {reason[0]}")
    print(f"#{pull.number} could not be built: {reason[0]}", file=sys.stderr)


def advance() -> int:
    now = datetime.now(timezone.utc)
    main, pulls = open_pulls()

    rerun_failed_jobs(pulls)
    for pull in reconciles(pulls):
        set_status(pull.head, "success", f"{PASSED} on main {main[:7]} without a queue run: release metadata that the cut checks")
        print(f"#{pull.number} passed without a queue run: a release reconcile.")
    for pull in stale_passes(pulls, now):
        set_status(pull.head, "pending", f"{QUEUED}: it passed but did not land in time")
        print(f"#{pull.number} is queued again: it passed but did not land in time.")
    for pull in forks(pulls):
        set_status(pull.head, "failure", f"{FAILED}: a fork cannot join the queue; push the branch to this repository")
        print(f"#{pull.number} is a fork and cannot join the queue.")
    if stale_passes(pulls, now) or reconciles(pulls):
        wake()
        return 0

    decision = plan(pulls, now)
    if decision.wait:
        print(f"Nothing to start: {decision.wait}.")
        return 0

    git("fetch", "--no-tags", "origin", main)
    testable, bypassed = [], False
    for pull in decision.members:
        try:
            git("fetch", "--no-tags", "origin", f"+refs/pull/{pull.number}/head:refs/queue/{pull.number}")
            head = git("rev-parse", f"refs/queue/{pull.number}")
            if head != pull.head:
                print(f"#{pull.number} moved while the queue read it; the next run takes it.")
                continue
            if squash(main, main, head, "conflict check") is None:
                set_status(head, "failure", f"{FAILED}: conflicts with main; rebase onto main")
                print(f"#{pull.number} conflicts with main.")
                continue
            paths = changed_paths(main, head)
        except subprocess.CalledProcessError as error:
            unbuildable(pull, error)
            continue
        bypass = ""
        if docs_only(paths):
            # Only docs-marked tests read these paths, and the pull request's own CI ran them.
            bypass = "a docs-only change"
        elif any(path.startswith(WORKFLOWS) for path in paths):
            bypass = "it changes workflows, which the queue cannot push; main's push run tests it"
        if bypass:
            set_status(head, "success", f"{PASSED} on main {main[:7]} without a queue run: {bypass}")
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
        try:
            commit = squash(tip, main, pull.head, message)
        except subprocess.CalledProcessError as error:
            unbuildable(pull, error)
            continue
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


def verdicts(
    kind: str, members: list[tuple[int, str]], result: str, attempt: int, tree: str, base: str, main: str
) -> list[tuple[str, str, str]]:
    """The (head, state, description) each member gets from a finished queue run attempt.

    A pass holds only while main is still the base the run tested: otherwise the
    members would land on a tree no run tested, so they are tested again. A
    first failure reruns the failed jobs, so a flaky test fails nothing. A batch
    that fails again sends every member to a retest alone, so one breaking
    change holds only itself. A pull request that fails again alone has failed.
    """
    if result == "success" and not main.startswith(base):
        return [(head, "pending", f"{QUEUED}: main moved during the queue run") for _, head in members]
    if result == "success":
        return [(head, "success", f"{PASSED} on main {base[:7]}; tree {tree}") for _, head in members]
    if result == "cancelled":
        return [(head, "pending", f"{QUEUED}: the queue run was cancelled") for _, head in members]
    label = "alone" if len(members) == 1 else "with " + " ".join(f"#{number}" for number, _ in members)
    if attempt == 1:
        return [(head, "pending", f"{TESTING} on main {base[:7]} {label}: {RERUN}") for _, head in members]
    if kind == "batch" and len(members) > 1:
        return [(head, "pending", f"{RETEST}: the batch failed twice") for _, head in members]
    return [(head, "failure", f"{FAILED}: the queue run failed twice") for _, head in members]


def verdict() -> int:
    branch = os.environ["GITHUB_REF_NAME"]
    if not branch.startswith(BRANCH_PREFIX):
        raise ValueError(f"a verdict reports only a queue run on a {BRANCH_PREFIX} branch, not {branch}")
    kind, members = parse_queue(os.environ["QUEUE"])
    sha = os.environ["GITHUB_SHA"]
    tree = api(f"git/commits/{sha}")["tree"]["sha"]
    base = branch.removeprefix(BRANCH_PREFIX).split("-", 1)[0]
    attempt = int(os.environ["GITHUB_RUN_ATTEMPT"])
    url = f"{os.environ['GITHUB_SERVER_URL']}/{repository()}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
    main = api("git/ref/heads/main")["object"]["sha"]
    outcome = verdicts(kind, members, os.environ["RESULT"], attempt, tree, base, main)
    for head, state, description in outcome:
        # Only a member the controller put into this kind of run can get its verdict.
        statuses = api(f"commits/{head}/statuses")
        latest = next((status for status in statuses if status["context"] == CONTEXT), None)
        if not (latest and latest["state"] == "pending" and latest["description"].startswith(TESTING)):
            print(f"{head} is not in a queue run; its status stays as it is.", file=sys.stderr)
            continue
        set_status(head, state, description, url)
    wake()
    if all(not description.endswith(RERUN) for _, _, description in outcome):
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
