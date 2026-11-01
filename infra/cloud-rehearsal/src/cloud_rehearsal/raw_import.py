"""The raw-data import rehearsal (`scale-agent-led-imports` task 5.1).

Messy, generator-made notes and media go into Cloud cells through the shipped CLI and the cell's MCP tools, and
the same journey runs locally. Each key item is compared between the two arms, so the verdict is parity with
local, never a count against a target. The collection and its answer key come from
`benchmarks/collection_ingest/generate.py` by path and profile; the journey is `drive.py`'s `Journey`; the
score is `score.py`'s. A value that could not be read stays unknown; it is never recorded as zero.

Two targets, in one run: tenant A's fresh vault, and tenant B's vault, which first adopts a different small
collection, so the run can show that nothing already there changed or was copied.
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import tarfile
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from cellctl.manifests import namespace_name

from . import images
from . import substrate as substrate_mod
from .large_import import TRANSIENT_ATTEMPTS, import_summary
from .mcp_client import TenantClient
from .native_import import retain_cli_diagnostics, run_cli_import
from .report import StepFailure, StepRecord, innermost
from .shell import CommandFailed, run

BENCH = images.REPO_ROOT / "benchmarks" / "collection_ingest"


def _import_bench() -> tuple[Any, Any, Any]:
    """The benchmark's own modules, imported from their folder so that changes there need no change here.

    The folder is on `sys.path` only while they load, and their top-level names (`score`, `drive`, `control`,
    `generate`) are dropped from `sys.modules` after, so no other rehearsal mode can import one by mistake.
    """
    saved_path, known = list(sys.path), set(sys.modules)
    sys.path.insert(0, str(BENCH))
    try:
        loaded = tuple(importlib.import_module(name) for name in ("drive", "score", "mcp_session"))
    finally:
        sys.path[:] = saved_path
        for name in set(sys.modules) - known:
            origin = getattr(sys.modules[name], "__file__", None)
            if origin and Path(origin).parent == BENCH:
                del sys.modules[name]
    return loaded


drive, score, _session_module = _import_bench()
LedgerSession, classify = _session_module.LedgerSession, _session_module.classify

if TYPE_CHECKING:
    from .scenarios import Context, Tenant

# The corpus the verdict is about, and the different one that the existing vault already holds.
MAIN_COLLECTION = {"profile": "standard", "seed": 7}
EARLIER_COLLECTION = {"profile": "small", "seed": 8}
MAIN_LABEL, EARLIER_LABEL = "Collection", "Earlier collection"
MEDIA_DEADLINE_S = 300.0
LISTED_ITEMS = 100  # a group's report names this many items per state; the counts cover all of them
SCORE_TIMEOUT_S = 3600

# Verdicts, worst last. `incomplete` is any unknown with no loss: a missing read is never a pass.
PASS, INCOMPLETE, FAIL = "pass", "incomplete", "fail"
_SEVERITY = (PASS, INCOMPLETE, FAIL)

VAULT_SCRIPT = """import os, sys, tarfile
root = '/data/vault'
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
    for name in sorted(os.listdir(root)):
        if name != '_Imports':
            archive.add(os.path.join(root, name), arcname=name)
"""


# --- pure decisions ------------------------------------------------------------------------


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def edge_ends(key: Mapping[str, Any]) -> dict[str, dict[str, tuple[str, str]]]:
    """Each distinct link and embed edge of the key, named 'source -> target', with its two key paths."""
    notes = [i for i in key["items"] if i["kind"] == "note"]
    links = {f"{n['path']} -> {lk['target']}": (n["path"], lk["target"])
             for n in notes for lk in n["links"] if lk["resolution"] == "resolvable"}
    embeds = {f"{n['path']} -> {e['attachment']}": (n["path"], e["attachment"]) for n in notes for e in n["embeds"]}
    return {"links": links, "embeds": embeds}


def item_states(key: Mapping[str, Any], per_item: Mapping[str, Any] | None) -> dict[str, dict[str, bool | None]]:
    """Per group, per key item: did this arm keep it? `None` is unknown: the arm did not measure it.

    `per_item` is the `per_item` block of `score.py`'s metrics, or `None` when the arm was not scored.
    For junk, "kept" means kept out: no page came of it.
    """
    notes = [i for i in key["items"] if i["kind"] == "note"]
    atts = [i for i in key["items"] if i["kind"] == "attachment"]
    junk = [i for i in key["items"] if i["kind"] == "junk"]
    pages = (per_item or {}).get("pages_per_item", {})
    junk_pages = (per_item or {}).get("junk_pages", {})
    links = (per_item or {}).get("links_kept", {})
    embeds = (per_item or {}).get("embeds_kept", {})

    def landed(item: Mapping[str, Any]) -> bool | None:
        return pages[item["path"]] >= 1 if item["path"] in pages else None

    def filed_at_most_once(item: Mapping[str, Any]) -> bool | None:
        return pages[item["path"]] <= 1 if item["path"] in pages else None

    ends = edge_ends(key)
    return {
        "notes": {i["path"]: landed(i) for i in notes},
        "attachments": {i["path"]: landed(i) for i in atts},
        "japanese_notes": {i["path"]: landed(i) for i in notes if i["lang"] == "ja"},
        "nfd_names": {i["path"]: landed(i) for i in notes + atts if i["nfd_filename"]},
        # A duplicate is an item that came out as more than one page; an item that did not land is not one.
        "no_duplicate_pages": {i["path"]: filed_at_most_once(i) for i in notes + atts},
        "junk_kept_out": {i["path"]: (junk_pages[i["path"]] == 0 if i["path"] in junk_pages else None) for i in junk},
        "links": {edge: links.get(edge) for edge in sorted(ends["links"])},
        "embeds": {edge: embeds.get(edge) for edge in sorted(ends["embeds"])},
    }


def path_cause(path: str) -> str:
    """Why an item may be left behind, read from the key path's shape: a hidden part, or its extension."""
    parts = path.split("/")
    if any(part.startswith(".") for part in parts):
        return "hidden path"
    suffix = Path(parts[-1]).suffix.lower()
    return f"extension {suffix}" if suffix else "no extension"


def compare_group(cloud: Mapping[str, bool | None], local: Mapping[str, bool | None],
                  cause: Callable[[str], str] = path_cause) -> dict[str, Any]:
    """Item by item. A loss is an item that local kept and Cloud did not; a Cloud-only extra is the reverse.

    `loss_causes` groups the losses by `cause(item)`; a loss counts once, in `loss` and in its cause.
    """
    states: dict[str, list[str]] = {"loss": [], "cloud_extra": [], "unknown": []}
    kept_by_both = missed_by_both = 0
    for item in sorted(cloud.keys() | local.keys()):
        there, here = cloud.get(item), local.get(item)
        if there is None or here is None:
            states["unknown"].append(item)
        elif here and not there:
            states["loss"].append(item)
        elif there and not here:
            states["cloud_extra"].append(item)
        elif there:
            kept_by_both += 1
        else:
            missed_by_both += 1
    by_cause: dict[str, list[str]] = {}
    for item in states["loss"]:
        by_cause.setdefault(cause(item), []).append(item)
    return {
        "items": len(cloud.keys() | local.keys()), "kept_by_both": kept_by_both, "missed_by_both": missed_by_both,
        **{state: {"count": len(names), "listed": names[:LISTED_ITEMS]} for state, names in states.items()},
        "loss_causes": {name: {"count": len(items), "listed": items[:LISTED_ITEMS]} for name, items in sorted(by_cause.items())},
    }


def compare_arms(key: Mapping[str, Any], cloud: Mapping[str, Any] | None,
                 local: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    cloud_states, local_states = item_states(key, cloud), item_states(key, local)
    ends = edge_ends(key)
    cloud_landed = {path for group in ("notes", "attachments") for path, kept in cloud_states[group].items() if kept}

    def edge_cause(group: str) -> Callable[[str], str]:
        # A lost endpoint explains its edges; only an edge between two landed items is a dropped edge.
        return lambda edge: "endpoint missing" if not set(ends[group][edge]) <= cloud_landed else "edge dropped, both landed"

    return {group: compare_group(cloud_states[group], local_states[group], edge_cause(group) if group in ends else path_cause)
            for group in cloud_states}


def arm_gaps(label: str, metrics: Mapping[str, Any] | None, key: Mapping[str, Any], *, stopped_at: str | None,
             converged: bool | None) -> list[str]:
    """Why this arm's reads cannot carry a pass: each reason is a measurement that is partial or absent."""
    if metrics is None:
        return [f"{label}: not scored"]
    gaps = [f"{label}: {tool} read failed {count} times" for tool, count in sorted((metrics.get("measurement_gaps") or {}).items())]
    if stopped_at:
        gaps.append(f"{label}: the journey stopped at {stopped_at}")
    if converged is not True:
        gaps.append(f"{label}: the media queue had {'not converged' if converged is False else 'no convergence reading'}")
    pages = (metrics.get("per_item") or {}).get("pages_per_item", {})
    if not any(pages.get(i["path"], 0) >= 1 for i in key["items"] if i["kind"] == "note"):
        gaps.append(f"{label}: no key note landed, so there is nothing to compare")
    return gaps


@dataclass(frozen=True)
class Snapshot:
    """What a vault's Knowledge Base held at one moment, for the pre-existing page check."""

    files: frozenset[str] = frozenset()  # every file under Knowledge Base, Markdown or not
    sourced: Mapping[str, str] = field(default_factory=dict)  # page path -> SHA-256, for pages with `imported_from`
    identities: Mapping[str, tuple[str, str, str]] = field(default_factory=dict)  # page path -> its source identity


def check_existing_pages(before: Snapshot, after: Snapshot) -> list[str]:
    """What the import did to the pages the vault held before it.

    Pages that came from a source are immutable after capture: each must be unchanged. Every other file only
    has to exist, because adoption rewrites the managed pages (the indexes and the log) on every run. No source
    identity of a pre-existing page may appear on more than one page.
    """
    problems = []
    for path, digest in sorted(before.sourced.items()):
        if path not in after.files:
            problems.append(f"missing: {path}")
        elif after.sourced.get(path) != digest:
            problems.append(f"changed: {path}")
    problems += [f"missing: {path}" for path in sorted(before.files - after.files) if path not in before.sourced]
    pages_by_identity: dict[tuple[str, str, str], list[str]] = {}
    for path, identity in sorted(after.identities.items()):
        pages_by_identity.setdefault(identity, []).append(path)
    for identity in sorted(set(before.identities.values())):
        if len(pages_by_identity.get(identity, [])) > 1:
            problems.append(f"duplicated: {pages_by_identity[identity]}")
    return problems


def target_verdict(groups: Mapping[str, Mapping[str, Any]], page_problems: Sequence[str] | None = (),
                   gaps: Sequence[str] = ()) -> str:
    """`fail` on any loss or any damaged pre-existing page; `incomplete` on any unknown, any gap, or when a
    required page check did not run (`page_problems` is `None`); otherwise `pass`: no loss on the first pass."""
    if any(group["loss"]["count"] for group in groups.values()) or page_problems:
        return FAIL
    if page_problems is None or gaps or any(group["unknown"]["count"] for group in groups.values()):
        return INCOMPLETE
    return PASS


def worst(verdicts: Sequence[str | None]) -> str:
    """The run's verdict: a target that was never judged (`None`) makes it `incomplete` at best."""
    return max((INCOMPLETE if verdict is None else verdict for verdict in verdicts), key=_SEVERITY.index, default=INCOMPLETE)


def imported_from_matches(values: Sequence[str], key_paths: Sequence[str]) -> collections.Counter[str]:
    """For each folder the product wrote in front of an original path in `imported_from`, how many values match."""
    seen: collections.Counter[str] = collections.Counter()
    suffixes = [nfc("/" + path) for path in key_paths]
    for value in values:
        text = nfc(value)
        for suffix in suffixes:
            if text.endswith(suffix):
                seen[text[: -len(suffix)]] += 1
                break
    return seen


def imported_from_prefix(values: Sequence[str], key_paths: Sequence[str]) -> str | None:
    """The folder the product wrote in front of each original path in `imported_from`; `None` if none match.

    A Cloud cell names the staged folder, not the label, so the scorer is told what the data shows.
    """
    seen = imported_from_matches(values, key_paths)
    return seen.most_common(1)[0][0] if seen else None


# --- the hosted arm's transport ------------------------------------------------------------


class CloudSession(LedgerSession):
    """`LedgerSession` over a tenant's OAuth connector. Each call opens its own session, so a cell restart
    between calls costs a retry and nothing more."""

    def __init__(self, client: TenantClient) -> None:
        super().__init__()
        self.client = client

    async def invoke(self, tool: str, args: dict[str, Any]) -> tuple[str, Any, str | None, str | None]:
        last = "no attempt"
        for attempt in range(TRANSIENT_ATTEMPTS):
            try:
                async with self.client.mcp() as session:
                    result = await session.call(tool, args)
            except Exception as error:  # noqa: BLE001 - a transport failure is retried, then reported
                last = f"{type(innermost(error)).__name__}: {str(innermost(error))[:200]}"
                await asyncio.sleep(min(30, 2**attempt))
                continue
            # A structured refusal (MUTATION_WARMING asks for a retry) goes to `LedgerSession.call`, which waits
            # `retry_after_ms` and calls again; only an error with no structured body is a tool error.
            if isinstance(result.structured, dict) and isinstance(result.structured.get("error"), dict):
                return classify(result.structured)
            if result.is_error:
                return "tool_error", None, result.error_code, result.text()[:800]
            return classify(result.structured or None)
        return "transport_error", None, "TRANSPORT", last


# --- the plan and the stage ---------------------------------------------------------------


@dataclass
class RawImportPlan:
    run_id: str
    workdir: Path
    cli_image: str
    python: Path  # an interpreter with fastmcp, the server and its extractors: the local arm, the driver and the scorer run on it

    @property
    def root(self) -> Path:
        return self.workdir / "raw-import"


def generate_collection(plan: RawImportPlan, name: str, spec: Mapping[str, Any]) -> tuple[Path, Path]:
    """Write a collection and, outside it, its answer key, with the generator as it stands."""
    folder, key = plan.root / name, plan.root / f"{name}.key.json"
    run([sys.executable, str(BENCH / "generate.py"), "--seed", str(spec["seed"]), "--profile", spec["profile"],
         "--out", str(folder), "--key", str(key)])
    return folder, key


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def vault_snapshot(vault: Path) -> Snapshot:
    kb = vault / "Knowledge Base"
    files = frozenset(path.relative_to(vault).as_posix() for path in kb.rglob("*") if path.is_file()) if kb.is_dir() else frozenset()
    sourced: dict[str, str] = {}
    identities: dict[str, tuple[str, str, str]] = {}
    for rel, page in score.scan_vault(vault)["pages"].items():
        front = page["front"]
        if isinstance(front.get("imported_from"), str):
            sourced[rel] = sha256_file(vault / rel)
        identity = tuple(front.get(name) for name in ("import_collection", "collection_path", "original_sha256"))
        if all(isinstance(part, str) and part for part in identity):
            identities[rel] = identity  # type: ignore[assignment]
    return Snapshot(files, sourced, identities)


def run_scorer(plan: RawImportPlan, script: str, *arguments: str) -> None:
    """A benchmark script on `plan.python`: the drivers import fastmcp, which the harness's own interpreter lacks."""
    run([str(plan.python), str(BENCH / script), *arguments], timeout=SCORE_TIMEOUT_S)


def score_vault(plan: RawImportPlan, key: Path, log: Path, out: Path) -> dict[str, Any] | None:
    """Run `score.py` on a finished vault. `None` when it did not finish: the arm is then unknown, not zero."""
    try:
        run_scorer(plan, "score.py", "--key", str(key), "--drive-log", str(log), "--out", str(out), "--python", str(plan.python))
    except CommandFailed as error:
        out.with_suffix(".failure.txt").write_text(f"{error}\n{error.stdout[-2000:]}\n{error.stderr[-2000:]}")
        return None
    return json.loads(out.read_text(encoding="utf-8"))


def local_arm(plan: RawImportPlan, collection: Path, key: Path) -> dict[str, Any]:
    """The same journey on a personal server: `drive.py --profile personal`, then `score.py`.

    `drive.py` exits 0 when a journey stops, so the log says whether it stopped and whether the media queue converged.
    """
    work = plan.root / "local"
    log, metrics_path = plan.root / "local.drive.json", plan.root / "local.metrics.json"
    started = time.monotonic()
    out: dict[str, Any] = {"profile": "personal", "metrics": None}  # `metrics` is for the caller; the report keeps the headline
    try:
        run_scorer(plan, "drive.py", "--collection", str(collection), "--workdir", str(work), "--profile", "personal",
                   "--python", str(plan.python), "--server-root", str(images.REPO_ROOT), "--log", str(log),
                   "--label", "raw-import local arm")
        drive_log = json.loads(log.read_text(encoding="utf-8"))
        stopped = [journey["stopped_at"] for journey in drive_log["journeys"] if journey.get("stopped_at")]
        out["stopped_at"] = ", ".join(stopped) or None
        out["converged"] = all(drive_log["media"][phase].get("converged") is True for phase in ("after_first_run", "after_rerun"))
        out["metrics"] = score_vault(plan, key, log, metrics_path)
    except (CommandFailed, OSError, KeyError, ValueError) as error:
        out["failure"] = f"{type(error).__name__}: {error}"[:1500]
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


async def provision_paid_tenant(ctx: Context, tenant: Tenant) -> None:
    """A second paid tenant the way step 2 admits the first."""
    from .scenarios import activate_subscription, redeem_invite

    await redeem_invite(ctx, tenant, await substrate_mod.seed_invite(ctx.substrate, tenant.email, paid=True))
    await activate_subscription(ctx, tenant)
    await ctx.wait_cell(tenant.cell_id, lambda cell: bool(cell.get("ready")) and cell.get("observed_state") == "running"
                        and cell.get("hold_kind") is None, timeout=600, description=f"tenant {tenant.label}'s cell to be running and ready")


async def copy_vault(ctx: Context, tenant: Tenant, destination: Path) -> dict[str, Any]:
    """Copy the cell's vault out, read-only, into a new folder: one streamed tar of the volume, minus the staged imports.

    This costs one pod exec, and the archive goes to a file as it arrives, so a huge file never sits in memory.
    The owner export would add the export flag, an export Job, a versioned download and the archive verifier,
    to read the same files.
    """
    archive = destination.with_suffix(".tar.gz")
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(3):  # the pod may be replaced between attempts, so each one looks it up again
        pod = ctx.runtime_pod(tenant.cell_id)
        if pod is None:
            code = -1
            await asyncio.sleep(5 * (attempt + 1))
            continue
        command = ["docker", "exec", ctx.stack.k3s.container, "kubectl", "exec", "--namespace", namespace_name(tenant.cell_id),
                   pod["metadata"]["name"], "--container", "exomem", "--", "python3", "-c", VAULT_SCRIPT]
        with archive.open("wb") as sink:
            code = subprocess.run(command, stdout=sink, stderr=subprocess.PIPE, check=False, timeout=SCORE_TIMEOUT_S).returncode
        if code == 0:
            break
        await asyncio.sleep(5 * (attempt + 1))
    else:
        raise StepFailure(f"the vault copy of tenant {tenant.label} kept failing (last exit code {code})")
    destination.mkdir()
    with tarfile.open(archive, mode="r:gz") as tar:
        tar.extractall(destination, filter="data")
    result = {"tar_bytes": archive.stat().st_size, "tar_sha256": sha256_file(archive),
              "files": sum(1 for path in destination.rglob("*") if path.is_file())}
    archive.unlink()
    return result


async def import_collection(ctx: Context, plan: RawImportPlan, tenant: Tenant, collection: Path, name: str) -> tuple[str, dict]:
    """The CLI import as a person runs it. Returns the staged folder's vault path and the import's row."""
    try:
        cli = await run_cli_import(ctx, plan.cli_image, collection, label=name, tenant=tenant)
    finally:
        retain_cli_diagnostics(ctx, name)
    run(["docker", "rm", "--force", cli.name], check=False, timeout=60)
    row = await ctx.fetchrow("SELECT * FROM exomem_cloud_imports WHERE cell_id = $1 AND import_id = $2::uuid",
                             tenant.cell_id, cli.import_id)
    await ctx.wait_cell(tenant.cell_id, lambda cell: bool(cell.get("ready")) and cell.get("observed_state") == "running"
                        and cell.get("hold_kind") is None, timeout=900, description="the imported cell to resume ready")
    return f"_Imports/{cli.import_id}", row or {}


def cli_coverage(collection: Path, row: Mapping[str, Any]) -> dict[str, Any]:
    """The files the CLI declared against the files in the collection, so a file it left behind shows as a number."""
    on_disk = sum(1 for path in collection.rglob("*") if path.is_file())
    declared = row.get("declared_files")
    return {"collection_files": on_disk, "declared_files": declared,
            "left_behind_by_the_cli": None if declared is None else on_disk - declared}


async def adopt(ctx: Context, plan: RawImportPlan, tenant: Tenant, session: Any, collection: Path, name: str,
                label: str, *, initialize_kb: bool) -> dict[str, Any]:
    """Send `collection` to the tenant's cell and file it with the journey `drive.py` runs locally."""
    stage_path, row = await import_collection(ctx, plan, tenant, collection, name)
    started = time.perf_counter()
    # The scorer reads the refusals of the journey named "first". The CLI stages the collection in one folder
    # inside the stage, so the folder-kind rule probes name that folder, as the local arm's do.
    journey = drive.Journey(session, "first", label, collection, [], path=stage_path,
                            collection_root=f"{stage_path}/{collection.name}", request=drive.hosted_request,
                            initialize_kb=initialize_kb)
    rules_state: dict[str, Any] = {"folder_kind_rules": "not-probed", "collection_label": "not-tried"}
    summary = await journey.run(collection_label="try", rules_state=rules_state, probe_rules=True, include_rules=[])
    media = await drive.wait_for_media(session, "first", MEDIA_DEADLINE_S, drive.hosted_media_status)
    return {"stage_path": stage_path, "import": import_summary(row), "cli": cli_coverage(collection, row),
            "journey": summary, "rules_state": rules_state, "media": media,
            "stopped_at": summary.get("stopped_at"), "converged": media.get("converged"),
            "seconds": round(time.perf_counter() - started, 1)}


def write_drive_log(plan: RawImportPlan, name: str, session: Any, adopted: Mapping[str, Any], vault: Path,
                    state: Path, prefix: str) -> Path:
    """The cloud arm's drive log, in the format `score.py` reads. The staged copy is consumed by the first pass,
    so there is no second pass: the log repeats the first snapshot, and the re-run duplicates stay unknown."""
    pages = drive.kb_pages(vault)
    calls = session.calls
    refused: dict[str, int] = {}
    for call in calls:
        if call["outcome"] != "ok":
            label = f"{call['outcome']}:{call['error_code']}"
            refused[label] = refused.get(label, 0) + 1
    log = {
        "format": drive.LOG_FORMAT, "profile": "cloud", "label": f"raw-import cloud arm {name}",
        "server": drive.git_head(images.REPO_ROOT), "python": str(plan.python),
        "paths": {"vault": str(vault), "state": str(state), "collection_name": prefix},
        "server_root": str(images.REPO_ROOT), "env_extra": drive.PROFILE_ENV["cloud-like"],
        "search_mode_note": "EXOMEM_DISABLE_EMBEDDINGS=1: search_memory runs its lexical lane only",
        "tool_calls_total": len(calls), "calls": calls, "total_wall_s": adopted["seconds"],
        "journeys": [adopted["journey"]], "media": {"after_first_run": adopted["media"], "after_rerun": adopted["media"]},
        "snapshot_after_first_run": pages, "snapshot_after_rerun": pages,
        "call_failures": dict(sorted(refused.items())), **adopted["rules_state"],
    }
    path = plan.root / f"cloud-{name}.drive.json"
    path.write_text(json.dumps(log, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return path


async def score_cloud_vault(ctx: Context, plan: RawImportPlan, tenant: Tenant, session: Any, adopted: Mapping[str, Any],
                            key: Path, name: str, vault: Path) -> dict[str, Any]:
    """Copy the vault out, tell the scorer where the product wrote each original, and score it."""
    copied = await copy_vault(ctx, tenant, vault)
    key_paths = [item["path"] for item in json.loads(key.read_text(encoding="utf-8"))["items"]]
    scan = score.scan_vault(vault)
    values = [page["front"]["imported_from"] for page in scan["pages"].values() if isinstance(page["front"].get("imported_from"), str)]
    prefix = imported_from_prefix(values, key_paths)
    log = write_drive_log(plan, name, session, adopted, vault, plan.root / f"cloud-{name}-state", prefix or adopted["stage_path"])
    metrics = await asyncio.to_thread(score_vault, plan, key, log, plan.root / f"cloud-{name}.metrics.json")
    return {"vault_copy": copied, "imported_from_prefix": prefix, "metrics": metrics}


def arm_report(metrics: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if metrics is None:
        return None
    return score.headline(metrics)


def target_report(adopted: Mapping[str, Any], scored: Mapping[str, Any], groups: Mapping[str, Any], verdict: str,
                  gaps: Sequence[str]) -> dict[str, Any]:
    return {"verdict": verdict, "gaps": list(gaps), "journey": adopted["stopped_at"] or "finished",
            "media_converged": adopted["converged"], "import": adopted["import"], "cli": adopted["cli"],
            "vault_copy": scored["vault_copy"], "imported_from_prefix": scored["imported_from_prefix"],
            "cloud": arm_report(scored["metrics"]), "groups": groups}


async def run_raw_import(ctx: Context, plan: RawImportPlan) -> None:
    """The stage body: write into `report.stages["raw_import"]` as it goes, so partial results survive."""
    out = ctx.report.stages["raw_import"]
    out.update({"collection": MAIN_COLLECTION, "earlier_collection": EARLIER_COLLECTION, "findings": [], "targets": {},
                "local": None, "verdict": None,
                "verdict_meaning": "pass means no loss on the first pass: one import of each folder; a second `exomem cloud import` of the same folder is not run",
                "rerun_duplicates": "unknown: the first pass consumes the staged copy, so the Cloud arm has no second pass"})
    plan.root.mkdir(parents=True, exist_ok=True)
    collection, key_path = generate_collection(plan, "main", MAIN_COLLECTION)
    earlier, earlier_key_path = generate_collection(plan, "earlier", EARLIER_COLLECTION)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    earlier_key = json.loads(earlier_key_path.read_text(encoding="utf-8"))
    out["key"] = {"profile": key["profile"], "seed": key["seed"], "fixture_tree_sha256": key["fixture_tree_sha256"],
                  "summary": key["summary"]}
    record = StepRecord(0, "raw_import")
    from .scenarios import first_session

    local = await asyncio.to_thread(local_arm, plan, collection, key_path)
    local_metrics = local.pop("metrics")
    local_gaps = arm_gaps("local", local_metrics, key, stopped_at=local.get("stopped_at"), converged=local.get("converged"))
    out["local"] = {**local, "scored": local_metrics is not None, "headline": arm_report(local_metrics), "gaps": local_gaps}

    async def connect(tenant: Tenant) -> CloudSession:
        client = tenant.client = TenantClient(ctx.resolver, ctx.browser, tenant.session, ctx.substrate.redirect_uri)
        await first_session(client, record)
        return CloudSession(client)

    verdicts: dict[str, str | None] = {"new": None, "existing": None}
    try:
        # Target 1: tenant A's fresh vault.
        session = await connect(ctx.a)
        adopted = await adopt(ctx, plan, ctx.a, session, collection, "new", MAIN_LABEL, initialize_kb=True)
        scored = await score_cloud_vault(ctx, plan, ctx.a, session, adopted, key_path, "new", plan.root / "cloud-new-vault")
        cloud_metrics = scored["metrics"] or {}
        groups = compare_arms(key, cloud_metrics.get("per_item"), (local_metrics or {}).get("per_item"))
        gaps = [*arm_gaps("cloud", scored["metrics"], key, stopped_at=adopted["stopped_at"], converged=adopted["converged"]), *local_gaps]
        verdicts["new"] = target_verdict(groups, (), gaps)
        out["targets"]["new"] = target_report(adopted, scored, groups, verdicts["new"], gaps)
        # Target 2: tenant B's vault, which holds a different collection first.
        await provision_paid_tenant(ctx, ctx.b)
        session = await connect(ctx.b)
        earlier_adopted = await adopt(ctx, plan, ctx.b, session, earlier, "earlier", EARLIER_LABEL, initialize_kb=True)
        before_vault = plan.root / "cloud-earlier-vault"
        await copy_vault(ctx, ctx.b, before_vault)
        before = vault_snapshot(before_vault)
        earlier_values = [score.scan_vault(before_vault)["pages"][page]["front"]["imported_from"] for page in before.sourced]
        earlier_notes = [item["path"] for item in earlier_key["items"] if item["kind"] == "note"]
        earlier_landed = sum(imported_from_matches(earlier_values, earlier_notes).values())
        baseline_gaps = []
        if earlier_adopted["stopped_at"]:
            baseline_gaps.append(f"earlier collection: the journey stopped at {earlier_adopted['stopped_at']}")
        if earlier_adopted["converged"] is not True:
            baseline_gaps.append("earlier collection: the media queue had not converged before the snapshot")
        if not earlier_landed:
            baseline_gaps.append("earlier collection: none of its key notes landed, so no page is protected")
        session.calls.clear()
        adopted = await adopt(ctx, plan, ctx.b, session, collection, "existing", MAIN_LABEL, initialize_kb=False)
        scored = await score_cloud_vault(ctx, plan, ctx.b, session, adopted, key_path, "existing", plan.root / "cloud-existing-vault")
        after = vault_snapshot(plan.root / "cloud-existing-vault")
        page_problems = check_existing_pages(before, after) if before.sourced else None
        cloud_metrics = scored["metrics"] or {}
        groups = compare_arms(key, cloud_metrics.get("per_item"), (local_metrics or {}).get("per_item"))
        gaps = [*arm_gaps("cloud", scored["metrics"], key, stopped_at=adopted["stopped_at"], converged=adopted["converged"]),
                *local_gaps, *baseline_gaps]
        verdicts["existing"] = target_verdict(groups, page_problems, gaps)
        out["targets"]["existing"] = {
            **target_report(adopted, scored, groups, verdicts["existing"], gaps),
            "earlier_journey": earlier_adopted["stopped_at"] or "finished",
            "existing_pages": {"sourced_before": len(before.sourced), "files_before": len(before.files),
                               "files_after": len(after.files), "earlier_key_notes_landed": f"{earlier_landed}/{len(earlier_notes)}",
                               "problems": page_problems if page_problems is None else page_problems[:LISTED_ITEMS],
                               "problem_count": None if page_problems is None else len(page_problems)},
        }
    except StepFailure as error:
        out["findings"].append({"message": str(error)[:1500]})
    finally:
        diagnostics = ctx.stack.workdir / "diagnostics" / "raw-import"
        diagnostics.mkdir(parents=True, exist_ok=True)
        for path in plan.root.glob("*.json"):
            shutil.copy2(path, diagnostics / path.name)
    out["verdict"] = FAIL if out["findings"] else worst(list(verdicts.values()))
    if not out["local"]["scored"]:
        out["verdict"] = worst([out["verdict"], INCOMPLETE])
    print(f"[rehearsal] raw-import verdict: {out['verdict']}; findings {len(out['findings'])}", flush=True)
