"""A real-server item-annotation ablation; no cases, references, or model calls."""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Literal

from fastmcp.server.middleware import Middleware
from fastmcp.tools import ToolResult
from mcp.types import TextContent

Arm = Literal["baseline", "candidate"]
# Closed output fields and container paths from the review listing producers.
ITEM_FIELDS = ("dimensions", "dimension_status", "dimension_diagnostics")
ITEM_PATHS = (("items",), ("approvals",), ("groups", "*", "items"), ("runs", "*", "items"))


def stage_actor(
    product: Path, work: Path, server_env: dict[str, str], *, arm: Arm,
    executable: Path, interpreter: Path, model: str, effort: str,
    budget_usd: float, business_time: dt.datetime | None = None,
    mcp_servers: dict[str, dict] | None = None,
) -> dict:
    """Stage the shipped client surface for the maintained session driver.

    Keep provider credentials in the driver's environment, outside server_env.
    This prepares files and arguments; it starts no process or model call.
    """
    from epistemic.journeys import f27_replay

    from exomem import client_config, prominence

    actor = work / "actor"
    actor.mkdir()
    plugin = product / "plugins" / "claude-code"
    instructions, citation = f27_replay.custom_instructions_block("balanced", product)
    custom = work / "custom-instructions.md"
    custom.write_text(instructions, encoding="utf-8")
    client_config.place_learning_instruction(actor / "CLAUDE.md", prominence.LEARNING_INSTRUCTION)
    settings = work / "settings.json"
    settings.write_text(json.dumps({"permissions": {"blockReadsOutsideWorkingDirectories": True}}), encoding="utf-8")
    arguments = ["-m", "import_guidance.review_dimensions", "--arm", arm,
                 "--call-log", str(work / "calls.jsonl"), "--ready-file", str(work / "readiness.json")]
    if business_time is not None:
        arguments.extend(("--business-time", business_time.isoformat()))
    mcp = work / "mcp.json"
    servers = mcp_servers if mcp_servers is not None else {"projection": {
        "command": str(interpreter), "args": arguments, "env": server_env,
    }}
    mcp.write_text(json.dumps({"mcpServers": servers}), encoding="utf-8")
    mcp.chmod(0o600)
    surface = f27_replay.Arm(
        arm_id=arm, prominence="balanced", tools="Skill,Read",
        allowed_tools=",".join(("Skill", "Read", *(f"mcp__{name}__*" for name in servers))), uses_plugin=True,
        uses_custom_instructions=True, configure_ref=citation,
    )
    argv = f27_replay.build_turn_argv(
        executable=executable, arm=surface, prompt=None, mcp_config=mcp,
        session_id=None, model=model, max_turns=None, first=True,
        append_system_prompt_file=custom, plugin_dir=plugin, settings_file=settings,
    )
    argv.extend(("--add-dir", str(plugin), "--permission-mode", "dontAsk",
                 "--permission-prompts", "none", "--effort", effort,
                 "--max-budget-usd", str(budget_usd)))
    return {"cwd": actor, "argv": argv, "mcp": mcp, "settings": settings,
            "custom_instructions": custom, "custom_instructions_source": citation}


@contextmanager
def business_clock(moment: dt.datetime | None):
    """Use existing review clock arguments; keep elapsed clocks and real surfacing writes."""
    if moment is None:
        yield
        return
    if moment.tzinfo is None:
        raise ValueError("business time must include a UTC offset")
    moment = moment.astimezone(dt.UTC)
    from exomem import attention, review_state

    def pinned(function):
        @wraps(function)
        def call(*args, **kwargs):
            kwargs.setdefault("now", moment)
            return function(*args, **kwargs)
        return call

    original_attention, original_surfaced = attention.attention, review_state.record_surfaced
    attention.attention = pinned(original_attention)
    review_state.record_surfaced = pinned(original_surfaced)
    try:
        yield
    finally:
        attention.attention, review_state.record_surfaced = original_attention, original_surfaced


def observe_readiness(path: Path) -> None:
    """Record the native process's prepared-fixture proof before the actor starts."""
    from exomem import lexstore, readiness, reserved_paths
    from exomem.governance.principal import library_scope
    from import_guidance.session_seed import prepare_recall, wait_for_graph

    root = Path(os.environ["EXOMEM_VAULT_PATH"])
    started = time.monotonic()
    try:
        # Closed startup component names from exomem.readiness.COMPONENTS.
        for component in ("retrieval_catalog", "graph_handoff"):
            if not readiness.wait(component, timeout=120):
                raise TimeoutError(f"native startup did not finish {component}")
        prepare_recall(root)
        graph = wait_for_graph(root)
        # Prepare private identity reads before measuring the annotation treatment.
        with library_scope():
            reserved_paths.warm_identity_catalogue_before_boundary(root)
        identity_current = reserved_paths.identity_catalogue_ready(root)
        admission = readiness.retrieval_admission(root)
        current = lexstore.runtime_retrieval_catalog_current(root, schedule_repair=False)
        # Refuse a confounded experiment; this never gates a product request.
        if not admission["admitted"] or not current or not identity_current:
            raise RuntimeError("native startup has no current admitted catalogue or identity inventory")
        result = {"status": "ready", "graph": graph, "admission": admission,
                  "catalogue_current": current, "identity_catalogue_current": identity_current}
    except (Exception, SystemExit) as error:  # noqa: BLE001 - The graph waiter exits on failed preparation.
        result = {"status": "failed", "error": {"type": type(error).__name__, "message": str(error)}}
    result["seconds"] = time.monotonic() - started
    temporary = path.with_suffix(path.suffix + ".pending")
    temporary.write_text(json.dumps(result) + "\n", encoding="utf-8")
    temporary.replace(path)


def project_listing(reply: dict) -> dict:
    """Remove item annotations without changing selection, source facts, or coverage."""
    projected = copy.deepcopy(reply)

    def visit(value, path):
        if not path:
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, dict):
                        for field in ITEM_FIELDS:
                            item.pop(field, None)
        elif path[0] == "*":
            if isinstance(value, list):
                for item in value:
                    visit(item, path[1:])
        elif isinstance(value, dict) and path[0] in value:
            visit(value[path[0]], path[1:])

    for path in ITEM_PATHS:
        visit(projected, path)
    return projected


class ReviewDimensionProjection(Middleware):
    """Capture actual arguments and both replies, outside the product middleware."""

    def __init__(self, arm: Arm, log: Path, *, observe_principal: bool = False):
        if arm not in ("baseline", "candidate"):
            raise ValueError("arm must be baseline or candidate")
        self.arm, self.log = arm, Path(log)
        self.observe_principal = observe_principal

    def record(self, row: dict) -> None:
        with self.log.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def on_call_tool(self, context, call_next):
        request = context.message.model_dump(mode="json", by_alias=True)
        ctx = context.fastmcp_context
        row = {"request_id": str(ctx.request_id) if ctx.request_context is not None else None,
               "request": request}
        try:
            if self.observe_principal:
                from exomem.governance import principal

                who = principal.resolve_mcp_principal()
                row["principal"] = {
                    "resolved": who.resolved, "audience_id": who.audience_id,
                    "remote_owner": who.remote_owner, "administrative_ingress": who.administrative_ingress,
                    "client_binding": asdict(who.client_binding) if who.client_binding else None,
                    "origin_session": asdict(who.origin_session) if who.origin_session else None,
                }
            result = await call_next(context)
            row["raw"] = result.model_dump(mode="json", by_alias=True)
            if context.message.name == "list_review_items" and not result.is_error:
                # Stop a confounded experiment if the product adds a second rendered channel.
                # This harness refusal never gates a product request outside this experiment.
                if len(result.content) != 1 or not isinstance(result.content[0], TextContent):
                    raise ValueError("review listing must have one JSON text channel")
                raw = json.loads(result.content[0].text)
                if not isinstance(raw, dict):
                    raise ValueError("review listing must be a JSON object")
                if result.structured_content is not None and result.structured_content != raw:
                    raise ValueError("review listing channels disagree before projection")
                served = project_listing(raw) if self.arm == "baseline" else raw
                result = ToolResult(
                    content=[result.content[0].model_copy(update={
                        "text": json.dumps(served, ensure_ascii=False),
                    })],
                    structured_content=served if result.structured_content is not None else None,
                    meta=result.meta, is_error=result.is_error,
                )
            row["served"] = result.model_dump(mode="json", by_alias=True)
            return result
        except Exception as error:
            row["error"] = {"type": type(error).__name__, "message": str(error)}
            raise
        finally:
            self.record(row)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=("baseline", "candidate"), required=True)
    parser.add_argument("--call-log", type=Path, required=True)
    parser.add_argument("--ready-file", type=Path,
                        help="Record native catalogue and graph readiness before actor capture.")
    parser.add_argument("--business-time", type=dt.datetime.fromisoformat,
                        help="Pin attention eligibility and first-surfaced timestamps; include a UTC offset.")
    parser.add_argument("--transport", choices=("stdio", "http", "streamable-http"), default="stdio")
    parser.add_argument("--host", help="Bind the product HTTP transport to this address.")
    parser.add_argument("--port", type=int, help="Bind the product HTTP transport to this port.")
    parser.add_argument("--observe-principal", action="store_true",
                        help="Include the actual principal in each private call-log row.")
    args = parser.parse_args()
    if args.business_time is not None and args.business_time.tzinfo is None:
        parser.error("--business-time must include a UTC offset")
    from exomem import __main__ as product_cli
    from exomem import server

    build = server.build_server

    def build_with_projection(**kwargs):
        mcp = build(**kwargs)
        mcp.middleware.insert(0, ReviewDimensionProjection(
            args.arm, args.call_log, observe_principal=args.observe_principal,
        ))
        if args.ready_file is not None:
            threading.Thread(target=observe_readiness, args=(args.ready_file,),
                             name="review-fixture-readiness", daemon=True).start()
        return mcp

    server.build_server = build_with_projection
    try:
        with business_clock(args.business_time):
            arguments = ["--transport", args.transport]
            if args.host is not None:
                arguments.extend(("--host", args.host))
            if args.port is not None:
                arguments.extend(("--port", str(args.port)))
            return product_cli.main(arguments)
    finally:
        server.build_server = build


if __name__ == "__main__":
    raise SystemExit(main())
