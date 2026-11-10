"""Runs inside one arm's environment (`PYTHONPATH=<arm checkout>/src`): makes a scenario's real tool calls on a
throwaway copy of that checkout's fixture vault and writes what the tools returned.

    python record_worker.py <plan.json> <product root> <out.json> [<seed folder>]

The plan is the scenario's `record` object. Its `steps` run in order. A step is `{"tool", "arguments", "as"}`:
`as` names the reply so a later step can read it (`{"$ref": "name.field"}`) and a scenario can serve it
(`{"$shape": "name"}`); `{"$product": "skill_contract"}` in an argument is that arm's skill digest;
`arguments_from` starts a step from an earlier step's arguments without its `validate_only`; `expect` lists
top-level fields the reply must hold, so a scenario that assumes a state (indexing still pending) fails here
instead of serving a different one. `{"settle_graph": true}` writes
throwaway pages until the graph rebuild that a fresh vault starts has finished.

With a seed folder the vault is not the fixture vault: it holds the arm's own scaffold with the seed's pages copied over
it (`session_seed.py`, the same code a session-close run seeds with), and the recording writes nothing to it. A
`settle_graph` step is refused there, because a throwaway page is one that `recall_context` could then serve.
`"engagement"` in the plan pins `EXOMEM_PROMINENCE`, as a session-close run does for its case.
`"ready_graph": true` in the plan builds the relation graph of a seeded vault first, as a live vault has it from its
writes (`session_seed.py` says how, and writes no page): the arm's own reconcile runs before the server is built, and the
arm's own `recall_context` is read, with a bound, until its map reports the graph available. The recording then
holds `ready` with the graph state that was seen. The `warm` poll below still runs after it.

A read step may carry `"warm": {"seconds": N, "ignore": [field, ...], "within": W}`. On a seeded vault the first read
starts a background index pass and comes back cold (an empty slice, no leads), so the worker discards that read, then
reads every N seconds until two successive replies agree apart from the fields in `ignore` (ids that change on every
call), and records the later one. When no two successive replies agree within W seconds (`WARM_WITHIN_SECONDS` by
default), the recording fails instead of serving a reply that is still changing.

`"mode": "cloud"` in the plan makes the product report Exomem Cloud (`EXOMEM_CLOUD_CELL=1`), for this process only. The
product refuses to build a Cloud server without a cell id and a bearer, so the worker invents throwaway ones; it makes
no network call and the values leave nothing behind.

The state root, the vault and every scratch file live under one temporary folder in `TMPDIR`. The caller removes it
after this process exits: a graph rebuild still running at exit would race a removal made from inside.
"""

from __future__ import annotations

import itertools
import json
import os
import secrets
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

SETTLE_SECONDS = 120.0
#: How long a `warm` read is polled for two successive replies that agree, unless the step names its own `within`.
WARM_WITHIN_SECONDS = 60.0
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


def warmed_reply(call, tool: str, arguments: dict, warm: dict) -> dict:
    """The reply of a read that the vault's background index pass has stopped changing; see the module docstring."""
    seconds, ignore = float(warm["seconds"]), set(warm.get("ignore", ()))
    within = float(warm.get("within", WARM_WITHIN_SECONDS))
    call(tool, arguments)

    def settled(reply: dict) -> dict:
        return {key: value for key, value in reply.items() if key not in ignore}

    deadline = time.monotonic() + within
    time.sleep(seconds)
    previous = call(tool, arguments)
    while True:
        time.sleep(seconds)
        reply = call(tool, arguments)
        if settled(previous) == settled(reply):
            return reply
        if time.monotonic() >= deadline:
            raise SystemExit(
                f"{tool} still changed between two reads {seconds:g}s apart: the index is not warm (polled for {within:g}s)"
            )
        previous = reply


def resolve_refs(value: Any, replies: dict[str, Any], product: dict[str, str]) -> Any:
    """Replace `{"$ref": "name.field.sub"}` with that field of the named reply, and `{"$product": "name"}` with a
    value that the arm's own code computes (`product`)."""
    if isinstance(value, dict) and set(value) == {"$ref"}:
        name, *path = value["$ref"].split(".")
        found = replies[name]
        for part in path:
            found = found[part]
        return found
    if isinstance(value, dict) and set(value) == {"$product"}:
        return product[value["$product"]]
    if isinstance(value, dict):
        return {key: resolve_refs(item, replies, product) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_refs(item, replies, product) for item in value]
    return value


def main(plan_path: Path, product_root: Path, out_path: Path, seed: Path | None = None) -> None:
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if seed is not None and any(step.get("settle_graph") for step in plan["steps"]):
        raise SystemExit("settle_graph writes throwaway pages, which a seeded recording must not hold")
    if plan.get("ready_graph") and seed is None:
        raise SystemExit("ready_graph builds the graph of a seeded vault, and this recording has no seed")
    scratch = Path(tempfile.mkdtemp(prefix="ig-record-"))
    state, vault = scratch / "state", scratch / "vault"
    state.mkdir()
    if seed is None:
        shutil.copytree(product_root / "tests" / "fixtures", vault)
    os.environ.update(
        OFFLINE_ENV
        | {
            "EXOMEM_STATE_ROOT": str(state),
            "EXOMEM_WRITER_LEASE_STATE_DIR": str(state / "writer-lease"),
            "EXOMEM_VAULT_PATH": str(vault),
            "EXOMEM_CONFIG_PATH": str(scratch / "no-such-exomem-config.json"),
        }
        | ({"EXOMEM_PROMINENCE": plan["engagement"]} if plan.get("engagement") else {})
    )
    if plan.get("mode") == "cloud":
        os.environ.update(
            {
                "EXOMEM_CLOUD_CELL": "1",
                "EXOMEM_CLOUD_CELL_ID": "".join(secrets.choice("abcdefghijklmnopqrstuvwxyz234567") for _ in range(16)),
                "EXOMEM_CLOUD_CELL_TOKEN": secrets.token_urlsafe(48),
            }
        )
    # Imported here: the environment above must be set before the product reads it.
    import session_seed

    import exomem
    from exomem import server as server_module
    from exomem import state_migration, workflow_skills

    if not Path(exomem.__file__).resolve().is_relative_to(product_root / "src"):
        raise SystemExit(f"imported exomem from {exomem.__file__}, not from {product_root}/src")
    ready_started = time.monotonic()
    if seed is not None:
        # `init_vault` has initialized this vault's state, as it does for a session-close run, so no migration follows.
        session_seed.main(seed, vault, product_root)
        if plan.get("ready_graph"):
            session_seed.reconcile()
    else:
        authority = state_migration.assert_offline_migration_authority(source="write-narration")
        state_migration.migrate_vault_state_offline(vault, authority=authority)
    call = session_seed.open_server(vault)
    ready = None
    if plan.get("ready_graph"):
        ready = {"graph": session_seed.wait_for_graph(call), "seconds": round(time.monotonic() - ready_started, 1)}

    # The digest that a session-profile bootstrap needs, from this arm's skill files.
    product = {"skill_contract": workflow_skills.skill_contract()}
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
        arguments = resolve_refs(arguments, replies, product)
        reply = (
            warmed_reply(call, step["tool"], arguments, step["warm"])
            if "warm" in step
            else call(step["tool"], arguments)
        )
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
            {"instructions": server_module.SERVER_INSTRUCTIONS, "shapes": replies}
            | ({"ready": ready} if ready else {}),
            indent=1,
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
    main(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        Path(sys.argv[3]),
        Path(sys.argv[4]) if len(sys.argv) > 4 else None,
    )
