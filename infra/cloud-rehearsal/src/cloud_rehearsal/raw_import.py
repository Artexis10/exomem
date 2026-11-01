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
import base64
import collections
import hashlib
import io
import json
import shutil
import sys
import tarfile
import time
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import images
from . import substrate as substrate_mod
from .large_import import TRANSIENT_ATTEMPTS, exec_json, import_summary
from .mcp_client import TenantClient
from .native_import import retain_cli_diagnostics, run_cli_import
from .report import StepFailure, StepRecord, innermost
from .shell import CommandFailed, run

BENCH = images.REPO_ROOT / "benchmarks" / "collection_ingest"
# The benchmark's own modules, imported from their folder so that changes there need no change here.
sys.path.insert(0, str(BENCH))
import drive  # noqa: E402
import score  # noqa: E402
from mcp_session import LedgerSession, classify  # noqa: E402

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

VAULT_SCRIPT = """import base64, io, json, os, tarfile
root = '/data/vault'
buffer = io.BytesIO()
with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
    for name in sorted(os.listdir(root)):
        if name != '_Imports':
            archive.add(os.path.join(root, name), arcname=name)
print(json.dumps({'tar': base64.b64encode(buffer.getvalue()).decode()}))
"""


# --- pure decisions ------------------------------------------------------------------------


def nfc(text: str) -> str:
    return unicodedata.normalize("NFC", text)


def item_states(key: Mapping[str, Any], per_item: Mapping[str, Any] | None) -> dict[str, dict[str, bool | None]]:
    """Per group, per key item: did this arm keep it? `None` is unknown: the arm did not measure it.

    `per_item` is the `per_item` block of `score.py`'s metrics, or `None` when the arm was not scored.
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

    link_edges = sorted({f"{n['path']} -> {lk['target']}" for n in notes for lk in n["links"] if lk["resolution"] == "resolvable"})
    embed_edges = sorted({f"{n['path']} -> {e['attachment']}" for n in notes for e in n["embeds"]})
    return {
        "notes": {i["path"]: landed(i) for i in notes},
        "attachments": {i["path"]: landed(i) for i in atts},
        "japanese_notes": {i["path"]: landed(i) for i in notes if i["lang"] == "ja"},
        "nfd_names": {i["path"]: landed(i) for i in notes + atts if i["nfd_filename"]},
        # A duplicate is an item that came out as more than one page; an item that did not land is not one.
        "no_duplicate_pages": {i["path"]: filed_at_most_once(i) for i in notes + atts},
        "junk_landed": {i["path"]: (junk_pages[i["path"]] >= 1 if i["path"] in junk_pages else None) for i in junk},
        "links": {edge: links.get(edge) for edge in link_edges},
        "embeds": {edge: embeds.get(edge) for edge in embed_edges},
    }


def compare_group(cloud: Mapping[str, bool | None], local: Mapping[str, bool | None]) -> dict[str, Any]:
    """Item by item. A loss is an item that local kept and Cloud did not; a Cloud-only extra is the reverse."""
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
    return {
        "items": len(cloud.keys() | local.keys()), "kept_by_both": kept_by_both, "missed_by_both": missed_by_both,
        **{state: {"count": len(names), "listed": names[:LISTED_ITEMS]} for state, names in states.items()},
    }


def compare_arms(key: Mapping[str, Any], cloud: Mapping[str, Any] | None,
                 local: Mapping[str, Any] | None) -> dict[str, dict[str, Any]]:
    cloud_states, local_states = item_states(key, cloud), item_states(key, local)
    return {group: compare_group(cloud_states[group], local_states[group]) for group in cloud_states}


def check_existing_pages(before: Mapping[str, str], after: Mapping[str, str]) -> list[str]:
    """Every page the vault held before the import: unchanged, still there and not copied.

    Both maps are Knowledge Base page path -> SHA-256. A new page with a pre-existing page's bytes is a copy.
    """
    problems = [f"changed: {path}" for path, digest in sorted(before.items()) if path in after and after[path] != digest]
    problems += [f"missing: {path}" for path in sorted(before) if path not in after]
    first_path = {}
    for path, digest in sorted(before.items()):
        first_path.setdefault(digest, path)
    problems += [f"duplicated: {path} repeats {first_path[digest]}" for path, digest in sorted(after.items())
                 if path not in before and digest in first_path]
    return problems


def target_verdict(groups: Mapping[str, Mapping[str, Any]], page_problems: Sequence[str] | None = ()) -> str:
    """`fail` on any loss or any damaged pre-existing page; `incomplete` on any unknown, or when a required
    page check did not run (`page_problems` is `None`); otherwise `pass`."""
    if any(group["loss"]["count"] for group in groups.values()) or page_problems:
        return FAIL
    if page_problems is None or any(group["unknown"]["count"] for group in groups.values()):
        return INCOMPLETE
    return PASS


def worst(verdicts: Sequence[str | None]) -> str:
    """The run's verdict: a target that was never judged (`None`) makes it `incomplete` at best."""
    return max((INCOMPLETE if verdict is None else verdict for verdict in verdicts), key=_SEVERITY.index, default=INCOMPLETE)


def imported_from_prefix(values: Sequence[str], key_paths: Sequence[str]) -> str | None:
    """The folder the product wrote in front of each original path in `imported_from`; `None` if none match.

    A Cloud cell names the staged folder, not the label, so the scorer is told what the data shows.
    """
    seen: collections.Counter[str] = collections.Counter()
    suffixes = [nfc("/" + path) for path in key_paths]
    for value in values:
        text = nfc(value)
        for suffix in suffixes:
            if text.endswith(suffix):
                seen[text[: -len(suffix)]] += 1
                break
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
    python: Path  # an interpreter with the server and its extractors installed: the local arm and the scorer run on it

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
    return hashlib.sha256(path.read_bytes()).hexdigest()


def kb_page_hashes(vault: Path) -> dict[str, str]:
    kb = vault / "Knowledge Base"
    return {path.relative_to(vault).as_posix(): sha256_file(path) for path in sorted(kb.rglob("*.md"))} if kb.is_dir() else {}


def score_vault(plan: RawImportPlan, key: Path, log: Path, out: Path) -> dict[str, Any] | None:
    """Run `score.py` on a finished vault. `None` when it did not finish: the arm is then unknown, not zero."""
    try:
        run([sys.executable, str(BENCH / "score.py"), "--key", str(key), "--drive-log", str(log), "--out", str(out),
             "--python", str(plan.python)], timeout=SCORE_TIMEOUT_S)
    except CommandFailed as error:
        out.with_suffix(".failure.txt").write_text(f"{error}\n{error.stdout[-2000:]}\n{error.stderr[-2000:]}")
        return None
    return json.loads(out.read_text(encoding="utf-8"))


def local_arm(plan: RawImportPlan, collection: Path, key: Path) -> dict[str, Any]:
    """The same journey on a personal server: `drive.py --profile personal`, then `score.py`."""
    work = plan.root / "local"
    log, metrics_path = plan.root / "local.drive.json", plan.root / "local.metrics.json"
    started = time.monotonic()
    out: dict[str, Any] = {"profile": "personal", "metrics": None}  # `metrics` is for the caller; the report keeps the headline
    try:
        run([sys.executable, str(BENCH / "drive.py"), "--collection", str(collection), "--workdir", str(work),
             "--profile", "personal", "--python", str(plan.python), "--server-root", str(images.REPO_ROOT),
             "--log", str(log), "--label", "raw-import local arm"], timeout=SCORE_TIMEOUT_S)
        out["metrics"] = score_vault(plan, key, log, metrics_path)
    except CommandFailed as error:
        out["failure"] = str(error)[:1500]
    out["seconds"] = round(time.monotonic() - started, 1)
    return out


async def provision_paid_tenant(ctx: Context, tenant: Tenant) -> None:
    """A second paid tenant the way step 2 admits the first: invite, redemption, checkout, `subscription.activated`."""
    token = await substrate_mod.seed_invite(ctx.substrate, tenant.email, paid=True)
    tenant.session = await ctx.browser.redeem_invite(token)
    await ctx.identify(tenant)
    if not (tenant.cell_id and tenant.tenant_id and tenant.user_id):
        raise StepFailure(f"tenant {tenant.label}: redemption left no cell")
    tenant.transaction_id = await substrate_mod.bind_checkout(ctx.substrate, tenant.tenant_id)
    tenant.subscription_id = substrate_mod.paddle_id("sub")
    await ctx.paddle(substrate_mod.paddle_event(
        event_type="subscription.activated", status="active", user_id=tenant.user_id, tenant_id=tenant.tenant_id,
        subscription_id=tenant.subscription_id, transaction_id=tenant.transaction_id))
    await ctx.wait_cell(tenant.cell_id, lambda cell: bool(cell.get("ready")) and cell.get("observed_state") == "running"
                        and cell.get("hold_kind") is None, timeout=600, description=f"tenant {tenant.label}'s cell to be running and ready")


async def copy_vault(ctx: Context, tenant: Tenant, destination: Path) -> dict[str, Any]:
    """Copy the cell's vault out, read-only, into a new folder: one tar of the volume, minus the staged imports.

    This costs one pod exec. The owner export would add the export flag, an export Job, a versioned download and
    the archive verifier, to read the same files.
    """
    found = await asyncio.to_thread(exec_json, ctx, VAULT_SCRIPT, cell_id=tenant.cell_id)
    archive = base64.b64decode(found["tar"])
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        tar.extractall(destination, filter="data")
    return {"tar_bytes": len(archive), "tar_sha256": hashlib.sha256(archive).hexdigest(),
            "files": sum(1 for path in destination.rglob("*") if path.is_file())}


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


async def adopt(ctx: Context, plan: RawImportPlan, tenant: Tenant, session: Any, collection: Path, name: str,
                label: str, *, initialize_kb: bool) -> dict[str, Any]:
    """Send `collection` to the tenant's cell and file it with the journey `drive.py` runs locally."""
    stage_path, row = await import_collection(ctx, plan, tenant, collection, name)
    started = time.perf_counter()
    # The scorer reads the refusals of the journey named "first".
    journey = drive.Journey(session, "first", label, collection, [], path=stage_path, request=drive.hosted_request,
                            initialize_kb=initialize_kb)
    rules_state: dict[str, Any] = {"folder_kind_rules": "not-probed", "collection_label": "not-tried"}
    summary = await journey.run(collection_label="try", rules_state=rules_state, probe_rules=True, include_rules=[])
    media = await drive.wait_for_media(session, "first", MEDIA_DEADLINE_S, drive.hosted_media_status)
    return {"stage_path": stage_path, "import": import_summary(row), "journey": summary, "rules_state": rules_state,
            "media": media, "seconds": round(time.perf_counter() - started, 1)}


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


async def run_raw_import(ctx: Context, plan: RawImportPlan) -> None:
    """The stage body: write into `report.stages["raw_import"]` as it goes, so partial results survive."""
    out = ctx.report.stages["raw_import"]
    out.update({"collection": MAIN_COLLECTION, "earlier_collection": EARLIER_COLLECTION, "findings": [], "targets": {},
                "local": None, "verdict": None,
                "rerun_duplicates": "unknown: the first pass consumes the staged copy, so the Cloud arm has no second pass"})
    plan.root.mkdir(parents=True, exist_ok=True)
    collection, key_path = generate_collection(plan, "main", MAIN_COLLECTION)
    earlier, _ = generate_collection(plan, "earlier", EARLIER_COLLECTION)
    key = json.loads(key_path.read_text(encoding="utf-8"))
    out["key"] = {"profile": key["profile"], "seed": key["seed"], "fixture_tree_sha256": key["fixture_tree_sha256"],
                  "summary": key["summary"]}
    record = StepRecord(0, "raw_import")
    from .scenarios import first_session

    local = await asyncio.to_thread(local_arm, plan, collection, key_path)
    local_metrics = local.pop("metrics")
    out["local"] = {**local, "scored": local_metrics is not None, "headline": arm_report(local_metrics)}

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
        groups = compare_arms(key, (scored["metrics"] or {}).get("per_item"), (local_metrics or {}).get("per_item"))
        verdicts["new"] = target_verdict(groups)
        out["targets"]["new"] = {"verdict": verdicts["new"], "journey": adopted["journey"].get("stopped_at") or "finished",
                                 "import": adopted["import"], "vault_copy": scored["vault_copy"],
                                 "imported_from_prefix": scored["imported_from_prefix"],
                                 "cloud": arm_report(scored["metrics"]), "groups": groups}
        # Target 2: tenant B's vault, which holds a different collection first.
        await provision_paid_tenant(ctx, ctx.b)
        session = await connect(ctx.b)
        earlier_adopted = await adopt(ctx, plan, ctx.b, session, earlier, "earlier", EARLIER_LABEL, initialize_kb=True)
        before_vault = plan.root / "cloud-earlier-vault"
        await copy_vault(ctx, ctx.b, before_vault)
        before = kb_page_hashes(before_vault)
        session.calls.clear()
        adopted = await adopt(ctx, plan, ctx.b, session, collection, "existing", MAIN_LABEL, initialize_kb=False)
        scored = await score_cloud_vault(ctx, plan, ctx.b, session, adopted, key_path, "existing", plan.root / "cloud-existing-vault")
        after = kb_page_hashes(plan.root / "cloud-existing-vault")
        page_problems = check_existing_pages(before, after) if before else None
        groups = compare_arms(key, (scored["metrics"] or {}).get("per_item"), (local_metrics or {}).get("per_item"))
        verdicts["existing"] = target_verdict(groups, page_problems)
        out["targets"]["existing"] = {
            "verdict": verdicts["existing"], "journey": adopted["journey"].get("stopped_at") or "finished",
            "import": adopted["import"], "vault_copy": scored["vault_copy"],
            "imported_from_prefix": scored["imported_from_prefix"], "cloud": arm_report(scored["metrics"]), "groups": groups,
            "earlier_journey": earlier_adopted["journey"].get("stopped_at") or "finished",
            "existing_pages": {"before": len(before) if before else None, "after": len(after),
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
