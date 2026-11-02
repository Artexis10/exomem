"""Runs inside one arm's environment (`PYTHONPATH=<arm checkout>/src`): makes a scenario's real tool calls on a
throwaway copy of that checkout's fixture vault and writes what the tools returned.

    python record_worker.py <plan.json> <product root> <out.json>

The plan is the scenario's `record` object. Its `steps` run in order. A step is `{"tool", "arguments", "as"}`:
`as` names the reply so a later step can read it (`{"$ref": "name.field"}`) and a scenario can serve it
(`{"$shape": "name"}`); `arguments_from` starts a step from an earlier step's arguments without its
`validate_only`; `expect` lists top-level fields the reply must hold, so a scenario that assumes a state
(indexing still pending) fails here instead of serving a different one. `{"settle_graph": true}` writes
throwaway pages until the graph rebuild that a fresh vault starts has finished.

The state root, the vault and every scratch file live under one temporary folder in `TMPDIR`. The caller removes it
after this process exits: a graph rebuild still running at exit would race a removal made from inside.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SETTLE_SECONDS = 120.0
#: A page that links an existing fixture note needs no relation review.
SETTLE_LINK = "Knowledge Base/Notes/Insights/percentage-based-feature-flag-rollout"
SETTLE_PAGE = (
    "## Observations\n\n- [finding] A throwaway page keeps one finding. ^one\n\n"
    f"## Connections\n\n- [[{SETTLE_LINK}]]\n"
)
# The environment the test suite gives every test: no model, no watcher, no network warm-up.
OFFLINE_ENV = {
    "EXOMEM_DISABLE_EMBEDDINGS": "1",
    "EXOMEM_DISABLE_FILE_WATCHER": "1",
    "EXOMEM_DISABLE_RELEVANCE_CHECK": "1",
    "EXOMEM_DISABLE_WARMUP": "1",
    "EXOMEM_DISABLE_RANKING_CONFIG": "1",
    "EXOMEM_DISABLE_MEDIA_EXTRACTION": "1",
    "EXOMEM_DISABLE_CLIP": "1",
    "EXOMEM_DISABLE_MODE_WATCH": "1",
    "EXOMEM_DISABLE_CORPUS_CACHE": "1",
    "EXOMEM_DISABLE_RESOLVER_WARM": "1",
}


def resolve_refs(value: Any, replies: dict[str, Any]) -> Any:
    """Replace `{"$ref": "name.field.sub"}` with that field of the named reply."""
    if isinstance(value, dict) and set(value) == {"$ref"}:
        name, *path = value["$ref"].split(".")
        found = replies[name]
        for part in path:
            found = found[part]
        return found
    if isinstance(value, dict):
        return {key: resolve_refs(item, replies) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, replies) for item in value]
    return value


def main(plan_path: Path, product_root: Path, out_path: Path) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    scratch = Path(tempfile.mkdtemp(prefix="ig-record-"))
    state, vault = scratch / "state", scratch / "vault"
    state.mkdir()
    shutil.copytree(product_root / "tests" / "fixtures", vault)
    os.environ.update(
        OFFLINE_ENV
        | {
            "EXOMEM_STATE_ROOT": str(state),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(state / "writer-lease"),
            "EXOMEM_VAULT_PATH": str(vault),
            "EXOMEM_CONFIG_PATH": str(scratch / "no-such-exomem-config.json"),
        }
    )
    # Imported here: the environment above must be set before the product reads it.
    import exomem
    from exomem import server as server_module
    from exomem import state_migration
    from exomem.governance import principal as principal_module

    if not Path(exomem.__file__).resolve().is_relative_to(product_root / "src"):
        raise SystemExit(f"imported exomem from {exomem.__file__}, not from {product_root}/src")
    authority = state_migration.assert_offline_migration_authority(source="write-narration")
    state_migration.migrate_vault_state_offline(vault, authority=authority)
    state_migration.reset_state_resolution_cache_for_tests()
    state_migration.require_vault_state_ready(vault)
    server_module.load_dotenv = lambda *args, **kwargs: None
    # The server is built before the first write: a write before it fails on the graph rebuild's lifetime.
    mcp = server_module.build_server(require_auth=False)

    def call(tool: str, arguments: dict) -> dict:
        with principal_module.request_scope(principal_module.owner_principal(surface="mcp")):
            result = asyncio.run(mcp.call_tool(tool, arguments, run_middleware=True))
        return json.loads(result.content[0].text)

    replies: dict[str, Any] = {}
    arguments_of: dict[str, dict] = {}
    for index, step in enumerate(plan["steps"]):
        if step.get("settle_graph"):
            settle(call)
            continue
        arguments = dict(step.get("arguments", {}))
        if "arguments_from" in step:
            earlier = {
                k: v
                for k, v in arguments_of[step["arguments_from"]].items()
                if k != "validate_only"
            }
            arguments = {**earlier, **arguments}
        arguments = resolve_refs(arguments, replies)
        reply = call(step["tool"], arguments)
        for field, wanted in step.get("expect", {}).items():
            if reply.get(field) != wanted:
                raise SystemExit(
                    f"step {index} ({step.get('as')}): expected {field}={wanted!r}, got {reply.get(field)!r}"
                )
        if step.get("as"):
            replies[step["as"]] = reply
            arguments_of[step["as"]] = arguments
    out_path.write_text(
        json.dumps(
            {"instructions": server_module.SERVER_INSTRUCTIONS, "shapes": replies}, indent=1
        ),
        encoding="utf-8",
    )


def settle(call) -> None:
    """Write throwaway pages until a commit reports the graph rebuilt."""
    deadline = time.monotonic() + SETTLE_SECONDS
    for n in itertools.count():
        page = {"title": f"Settle probe {n}", "slug": f"settle-probe-{n}", "content": SETTLE_PAGE}
        draft = call("create_note", {**page, "validate_only": True})
        committed = call(
            "create_note",
            {
                **page,
                "draft_id": draft["draft_id"],
                "draft_hash": draft["draft_hash"],
                "draft_token": draft["draft_token"],
            },
        )
        if committed.get("graph_sync") == "completed":
            return
        if time.monotonic() > deadline:
            raise SystemExit(f"the graph rebuild had not finished after {SETTLE_SECONDS:.0f}s")
        time.sleep(2.0)


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
