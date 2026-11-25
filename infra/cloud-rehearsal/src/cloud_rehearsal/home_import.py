"""The Home-import rehearsal (`file-imports-on-the-cell` task 5.1).

A person who sends a folder and closes the tab expects the cell to file it. This mode sends a fixture tree
through the real import Job, asks the cell to file it by a series file, makes no `file` call from any client,
restarts the cell once while it files, and reads the result against the fixture's manifest.

The pass criteria come from the manifest that this module writes, never from the cell's own replies. Each
check compares one cell answer with what the fixture says it must be: the Sources the cell holds (one per
eligible file, with its `collection_path`, `file_modified` and origin fields), the files it listed as left
with their reasons, and the counts, which add up to the files of the tree.

Two facts about this run are recorded in the report instead of being hidden:

* The shipped CLI cannot ask the cell to file without also filing itself. The run therefore uploads with
  `--no-file` (the CLI makes no `file` call and writes no `filing` key) and the harness then writes
  `filing: cell` into the stage's series file in the pod, which is the line a Home upload writes itself. A
  cell start, which the harness causes with one pod deletion, finds that line and registers the stage.
* The restart that proves exactly-once filing is a second pod deletion, made as soon as the filing view shows
  the stage is being filed. When the stage is already `done` by then, the restart proved nothing, and the
  verdict is `incomplete`, never `pass`.

The live run needs a Cloud rehearsal cluster, so the pure parts (the fixture, the manifest and every check)
have their own tests, and the run body is exercised by the `home-import` mode of the `Cloud rehearsal` workflow.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from cellctl.manifests import namespace_name

from .large_import import STAGE_SCRIPT, Agent, exec_json
from .mcp_client import TenantClient
from .native_import import retain_cli_diagnostics, run_cli_import
from .raw_import import copy_vault
from .report import StepFailure, StepRecord
from .shell import run

if TYPE_CHECKING:
    from .scenarios import Context

# Verdicts, worst last. `incomplete` is no failed check with a restart that did not land during filing.
PASS, INCOMPLETE, FAIL = "pass", "incomplete", "fail"
# The origin a command-line upload states about itself, and the label the run passes with `--label`.
ORIGIN_LABEL = "Home rehearsal phone"
ORIGIN_BROWSER = "cli"
# Fixture file times, whole seconds from 2017-07-14T02:40:00Z, one hour apart so each file has its own.
BASE_MTIME = 1_500_000_000
# The reasons the cell and the CLI give, which the manifest expects: the cell's residual codes for a file it
# did not select, and the CLI's own reason for a file it leaves at the source.
CELL_UNSELECTED, CLI_HIDDEN = "unselected", "hidden"
FILING_TIMEOUT_S = 900.0
# A started cell registers a stage at once; a view still `unknown` after this long means it never did.
REGISTER_TIMEOUT_S = 180.0
POLL_SECONDS = 2.0

# Written into the stage's series file in the pod, with the product's own names for the file and the key.
PROMOTE_SCRIPT = """import json, sys
from pathlib import Path
from exomem import import_stage
path = Path('/data/vault') / import_stage.STAGE_DIR / sys.argv[1] / import_stage.SERIES_FILE
document = json.loads(path.read_text())
before = document.get('filing')
document['filing'] = import_stage.FILING_CELL
path.write_text(json.dumps(document))
print(json.dumps({'filing_before': before, 'filing': document['filing'], 'origin': document.get('origin'),
                  'series_file': import_stage.SERIES_FILE}))
"""


@dataclass(frozen=True)
class Member:
    """One file of the fixture tree and what the run must find of it."""

    path: str
    data: bytes
    mtime: int
    # `filed`: one Source. `left_by_cell`: sent, staged, and listed by the cell with `reason`.
    # `left_by_cli`: not sent, and named in the CLI's own left-behind lines with `reason`.
    outcome: str
    reason: str | None = None

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


@dataclass(frozen=True)
class Manifest:
    members: tuple[Member, ...]

    def with_outcome(self, outcome: str) -> tuple[Member, ...]:
        return tuple(member for member in self.members if member.outcome == outcome)

    @property
    def filed(self) -> tuple[Member, ...]:
        return self.with_outcome("filed")

    @property
    def left_by_cell(self) -> tuple[Member, ...]:
        return self.with_outcome("left_by_cell")

    @property
    def left_by_cli(self) -> tuple[Member, ...]:
        return self.with_outcome("left_by_cli")

    def summary(self) -> dict[str, Any]:
        return {"files": len(self.members), "filed": len(self.filed), "left_by_cell": len(self.left_by_cell),
                "left_by_cli": len(self.left_by_cli)}


@dataclass
class HomeImportPlan:
    run_id: str
    workdir: Path
    cli_image: str
    bulk: int = 30  # enough notes that filing lasts long enough to restart the cell inside it

    @property
    def root(self) -> Path:
        return self.workdir / "home-import"


# --- the fixture ---------------------------------------------------------------------------

# A PDF that the cell stores as a Source with its text pending; the bytes are invented.
PDF = b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n%%EOF\n"


def fixture_members(bulk: int) -> tuple[Member, ...]:
    """The tree: loose files, a nested folder, the members of an expanded zip, and the files a Home import meets.

    Each file's bytes are unique, so a file the cell files twice cannot hide as a skipped duplicate. The Latin-1
    text file is not valid UTF-8. The folder `archive-export` is what a person gets after expanding a zip.
    """
    text = {
        "loose.md": "# Loose note\n\nA note at the top of the folder.\n",
        "loose.txt": "A plain text file at the top of the folder.\n",
        "notes/nested/deep.md": "# Deep note\n\nA note two folders down.\n",
        "archive-export/day-1/log.txt": "First day of the expanded archive.\n",
        "archive-export/day-2/log.txt": "Second day of the expanded archive.\n",
        **{f"bulk/note-{index:03d}.md": f"# Bulk note {index}\n\nBody of bulk note {index}.\n" for index in range(1, bulk + 1)},
    }
    members = [Member(path, body.encode(), 0, "filed") for path, body in text.items()]
    members.append(Member("latin1.txt", "café naïve über\n".encode("latin-1"), 0, "filed"))
    members.append(Member("archive-export/scan.pdf", PDF, 0, "filed"))
    members.append(Member("empty.txt", b"", 0, "left_by_cell", CELL_UNSELECTED))
    members.append(Member(".hidden.md", b"# Hidden\n\nA file a person did not mean to send.\n", 0, "left_by_cli", CLI_HIDDEN))
    ordered = sorted(members, key=lambda member: member.path)
    return tuple(
        Member(member.path, member.data, BASE_MTIME + 3600 * index, member.outcome, member.reason)
        for index, member in enumerate(ordered))


def build_fixture(folder: Path, bulk: int) -> Manifest:
    """Write the tree under `folder`, with each file's time, and return the manifest that the checks read."""
    manifest = Manifest(fixture_members(bulk))
    for member in manifest.members:
        target = folder / member.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(member.data)
        os.utime(target, (member.mtime, member.mtime))
    return manifest


def modified_text(epoch: int) -> str:
    """A file time as the Source states it: UTC, to the second."""
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# --- the checks ----------------------------------------------------------------------------


def read_front(text: str) -> dict[str, Any] | None:
    """The front matter of a page, or None for a page with none."""
    if not text.startswith("---\n"):
        return None
    end = text.find("\n---", 4)
    if end < 0:
        return None
    front = yaml.safe_load(text[4:end])
    return front if isinstance(front, dict) else None


def source_fronts(vault: Path) -> dict[str, list[dict[str, Any]]]:
    """Every Source page of a vault copy that records where it came from, by its `collection_path`."""
    found: dict[str, list[dict[str, Any]]] = {}
    sources = vault / "Knowledge Base" / "Sources"
    for page in sorted(sources.rglob("*.md")) if sources.is_dir() else ():
        front = read_front(page.read_text(encoding="utf-8", errors="replace"))
        if front and isinstance(front.get("imported_from"), str):
            found.setdefault(str(front.get("collection_path")), []).append(front)
    return found


def check_sources(manifest: Manifest, fronts: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[str]:
    """Each eligible file has exactly one Source with its path, size, hash, time and origin; no other file has one."""
    problems: list[str] = []
    for member in manifest.filed:
        pages = fronts.get(member.path, [])
        if len(pages) != 1:
            problems.append(f"{member.path}: {len(pages)} Sources, expected one")
            continue
        front = pages[0]
        expected = {"collection_path": member.path, "original_sha256": member.sha256,
                    "original_bytes": len(member.data), "file_modified": modified_text(member.mtime),
                    "origin_label": ORIGIN_LABEL, "upload_browser": ORIGIN_BROWSER}
        for key, value in expected.items():
            if front.get(key) != value:
                problems.append(f"{member.path}: {key} is {front.get(key)!r}, expected {value!r}")
        if "upload_device" in front:
            problems.append(f"{member.path}: upload_device is {front['upload_device']!r}; the CLI states no device")
    expected_paths = {member.path for member in manifest.filed}
    for path in sorted(set(fronts) - expected_paths):
        problems.append(f"{path}: has a Source, but the fixture says it is not eligible")
    return problems


def check_leftovers(manifest: Manifest, listed: Sequence[Mapping[str, Any]], staged: Sequence[str],
                    cli_lines: Sequence[str]) -> list[str]:
    """The files left behind are listed with their reasons, and they are the ones the fixture says."""
    problems: list[str] = []
    by_path = {str(entry.get("path")): entry for entry in listed}
    for member in manifest.left_by_cell:
        entry = by_path.get(member.path)
        if entry is None:
            problems.append(f"{member.path}: the cell does not list it as left")
        elif entry.get("code") != member.reason:
            problems.append(f"{member.path}: the cell lists it with code {entry.get('code')!r}, expected {member.reason!r}")
        if member.path not in staged:
            problems.append(f"{member.path}: not in the stage, which keeps what it left")
    for path in sorted(set(by_path) - {member.path for member in manifest.left_by_cell}):
        problems.append(f"{path}: the cell lists it as left, but the fixture says it was filed")
    for member in manifest.left_by_cli:
        if member.path in staged:
            problems.append(f"{member.path}: reached the stage, but the CLI should have left it behind")
        if not any(line.strip().startswith(f"{member.reason}:") for line in cli_lines):
            problems.append(f"{member.path}: the CLI's output has no left-behind line for reason {member.reason!r}")
    extra = sorted(set(staged) - {member.path for member in manifest.left_by_cell})
    if extra:
        problems.append(f"the stage still holds files that were filed or never sent: {extra[:10]}")
    return problems


def check_counts(manifest: Manifest, view: Mapping[str, Any], listing: Mapping[str, Any]) -> list[str]:
    """The filing view and the stage report agree with the manifest, and filed plus left add up to the tree."""
    problems: list[str] = []
    progress = view.get("progress") or {}
    if view.get("state") != "done":
        problems.append(f"the filing view's state is {view.get('state')!r}, expected 'done'")
    if view.get("stop") is not None:
        problems.append(f"the filing view still names a stop: {view.get('stop')!r}")
    wanted = {"filed": len(manifest.filed), "skipped_duplicates": 0, "failed": 0, "remaining": 0}
    for key, value in wanted.items():
        if progress.get(key) != value:
            problems.append(f"the filing view's {key} is {progress.get(key)!r}, expected {value}")
    windows = (listing.get("import_windows") or {}).get("windows") or []
    unfiled = sum(int((window.get("unfiled") or {}).get("count") or 0) for window in windows)
    if unfiled != len(manifest.left_by_cell):
        problems.append(f"the stage report counts {unfiled} unfiled files, expected {len(manifest.left_by_cell)}")
    filed, total = progress.get("filed"), len(manifest.members)
    if isinstance(filed, int) and filed + unfiled + len(manifest.left_by_cli) != total:
        problems.append(f"{filed} filed, {unfiled} unfiled and {len(manifest.left_by_cli)} left at the source "
                        f"do not add up to the {total} files of the tree")
    if listing.get("adopted_files") not in (None, len(manifest.filed)):
        problems.append(f"the cell says it adopted {listing.get('adopted_files')} files, expected {len(manifest.filed)}")
    return problems


def check_origin(series_origin: Any) -> list[str]:
    """The series file the CLI wrote carries the origin that the run asked for."""
    if not isinstance(series_origin, dict):
        return [f"the series file has no origin, found {series_origin!r}"]
    problems = []
    for key, value in (("label", ORIGIN_LABEL), ("browser_family", ORIGIN_BROWSER)):
        if series_origin.get(key) != value:
            problems.append(f"the series file's origin {key} is {series_origin.get(key)!r}, expected {value!r}")
    return problems


def check_no_client_file_call(call_labels: Sequence[str]) -> list[str]:
    """The harness's own MCP client made no `file` call; it only read the cell's status."""
    return [f"the harness client called {label}, which can file" for label in sorted(set(call_labels))
            if not label.startswith("inspect_memory")]


def verdict(problems: Sequence[str], *, interrupted: bool) -> str:
    """`fail` for any wrong answer, `incomplete` when the restart did not land in filing, otherwise `pass`."""
    if problems:
        return FAIL
    return PASS if interrupted else INCOMPLETE


# --- the run -------------------------------------------------------------------------------


async def read_filing(agent: Agent, import_id: str) -> dict[str, Any]:
    """The cell's filing view of one stage, which reads one record and opens no run."""
    body, _ = await agent.call("inspect_memory", {"operation": "imports.status", "arguments": {
        "path": f"_Imports/{import_id}", "view": "filing"}}, kind="filing")
    return body


async def read_listing(agent: Agent, import_id: str) -> dict[str, Any]:
    """The runs and the stage report of one stage, with each left file and its code."""
    body, _ = await agent.call("inspect_memory", {"operation": "imports.status", "arguments": {
        "path": f"_Imports/{import_id}"}}, kind="listing")
    return body


async def restart_cell(ctx: Context) -> str:
    """Delete the cell's pod, as a node drain does, and wait until a new pod is Ready."""
    pod = ctx.runtime_pod(ctx.a.cell_id)
    if pod is None:
        raise StepFailure("the cell has no runtime pod to restart")
    name, old_uid = pod["metadata"]["name"], pod["metadata"]["uid"]
    ctx.kubectl("delete", "pod", "--namespace", namespace_name(ctx.a.cell_id), name, "--wait=false")
    deadline = time.monotonic() + 300
    while True:
        current = ctx.runtime_pod(ctx.a.cell_id)
        if current and current["metadata"]["uid"] != old_uid and any(
                c["type"] == "Ready" and c["status"] == "True" for c in current["status"].get("conditions", [])):
            return name
        if time.monotonic() > deadline:
            raise StepFailure("the deleted pod was not replaced by a Ready pod within 300 s")
        await asyncio.sleep(1)


def left_behind_lines(log: str) -> list[str]:
    """The CLI's own lines about files it leaves at the source, from its log."""
    lines = log.splitlines()
    for position, line in enumerate(lines):
        if line.strip().startswith("Left behind"):
            tail = []
            for following in lines[position + 1:]:
                if not following.startswith(" "):
                    break
                tail.append(following)
            return tail
    return []


async def send_fixture(ctx: Context, plan: HomeImportPlan, folder: Path, out: dict[str, Any]) -> str:
    """The CLI upload through the real import Job, as a person runs it, with no filing by the CLI."""
    try:
        cli = await run_cli_import(ctx, plan.cli_image, folder, label="home", tenant=ctx.a, windowed=True,
                                   cli_args=["--no-file", "--label", ORIGIN_LABEL])
    finally:
        retain_cli_diagnostics(ctx, "home")
    run(["docker", "rm", "--force", cli.name], check=False, timeout=60)
    log = cli.log_file.read_text(errors="replace")
    out["cli"] = {"result": cli.result, "left_behind_lines": left_behind_lines(log)}
    await ctx.wait_cell(ctx.a.cell_id, lambda cell: bool(cell.get("ready")) and cell.get("observed_state") == "running"
                        and cell.get("hold_kind") is None, timeout=900, description="the imported cell to resume ready")
    ids = cli.result.get("import_ids") or []
    if len(ids) != 1:
        raise StepFailure(f"the fixture should fit one window, the CLI sent {len(ids)}: {ids}")
    return str(ids[0])


async def run_home_import(ctx: Context, plan: HomeImportPlan) -> None:
    """The stage body: write into `report.stages["home_import"]` as it goes, so partial results survive."""
    out = ctx.report.stages["home_import"]
    out.update({"findings": [], "verdict": None,
                "verdict_meaning": "pass means every check held and the restart landed while the cell was filing; "
                                   "incomplete means every check held but the stage was done before the restart"})
    folder = plan.root / "tree"
    folder.mkdir(parents=True, exist_ok=True)
    manifest = build_fixture(folder, plan.bulk)
    out["fixture"] = manifest.summary()
    (plan.root / "manifest.json").write_text(json.dumps(
        [{**asdict(member), "data": None, "sha256": member.sha256} for member in manifest.members], indent=1))

    import_id = await send_fixture(ctx, plan, folder, out)
    out["import_id"] = import_id
    promoted = await asyncio.to_thread(exec_json, ctx, PROMOTE_SCRIPT, import_id)
    out["series_file"] = promoted
    problems = check_origin(promoted.get("origin"))
    if promoted.get("filing_before") is not None:
        problems.append(f"the CLI wrote filing {promoted['filing_before']!r} even with --no-file")

    # The agent below only reads status: it never calls `file`.
    from .scenarios import first_session

    client = ctx.a.client = TenantClient(ctx.resolver, ctx.browser, ctx.a.session, ctx.substrate.redirect_uri)
    await first_session(client, StepRecord(0, "home_import"))
    agent = Agent(client)
    started = time.monotonic()
    out["restarts"] = [{"why": "a cell start finds the series file and registers the stage",
                        "pod": await restart_cell(ctx)}]
    states: list[dict[str, Any]] = []
    interrupted = False
    view: dict[str, Any] = {}
    while time.monotonic() - started < FILING_TIMEOUT_S:
        view = await read_filing(agent, import_id)
        states.append({"at": round(time.monotonic() - started, 1), "state": view.get("state"),
                       "filed": (view.get("progress") or {}).get("filed")})
        if view.get("state") == "done":
            break
        if view.get("state") == "unknown" and time.monotonic() - started > REGISTER_TIMEOUT_S:
            break
        if not interrupted and view.get("state") in ("working", "parked"):
            out["restarts"].append({"why": "the cell restarts while it files", "state_seen_before": view.get("state"),
                                    "pod": await restart_cell(ctx)})
            interrupted = True
        await asyncio.sleep(POLL_SECONDS)
    out["filing_states"] = states[-60:]
    out["restart_during_filing"] = interrupted
    if view.get("state") != "done":
        problems.append(f"the filing view did not reach 'done' in {time.monotonic() - started:.0f} s; "
                        f"last state {view.get('state')!r}")
    out["filing_view"] = view

    listing = await read_listing(agent, import_id)
    out["listing"] = listing
    staged = await asyncio.to_thread(exec_json, ctx, STAGE_SCRIPT, import_id)
    staged_paths = sorted(path for path in staged if path != promoted["series_file"])
    out["staged_after_filing"] = staged_paths
    vault_copy = plan.root / "vault"
    if vault_copy.exists():
        shutil.rmtree(vault_copy)
    await copy_vault(ctx, ctx.a, vault_copy)
    fronts = source_fronts(vault_copy)
    members = [m for window in (listing.get("import_windows") or {}).get("windows") or [] for m in window.get("members") or []]
    cli_lines = out["cli"]["left_behind_lines"]
    problems += check_sources(manifest, fronts)
    problems += check_leftovers(manifest, members, staged_paths, cli_lines)
    problems += check_counts(manifest, view, listing)
    stats = agent.stats()
    out["agent"] = stats
    problems += check_no_client_file_call(list(stats["calls"]))
    out["findings"] = problems
    out["verdict"] = verdict(problems, interrupted=interrupted)
    if out["verdict"] == INCOMPLETE:
        out["findings"].append("the stage was done before the restart, so the exactly-once claim is untested in this run")
