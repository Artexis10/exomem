"""The half of an incident session that runs as the arm's own product code.

The runner starts this file with the arm's ``PYTHONPATH``, so the arm's own writer, guidance text,
frontmatter parser and status registry seed the vault and read the seeded note back. The runner
never parses a note itself.

    python arm_side.py seed VAULT WORK          # note JSON, list of notes or null on stdin; places WORK/CLAUDE.md; prints the probe of each
    python arm_side.py seed_sources VAULT WORK  # list of Sources on stdin; captures each; prints the receipts
    python arm_side.py seed_collections VAULT   # list of collections on stdin; creates each; prints the pages written
    python arm_side.py prominence LEVEL         # sets the capture prominence; prints the level the arm then resolves
    python arm_side.py records VAULT PATH...    # prints the rows of each Records collection, by knowledge-base path
    python arm_side.py collections VAULT        # prints every collection of the vault with its schema, rows and automatic flag
    python arm_side.py instructions             # prints the text the arm's server hands every client
    python arm_side.py write_tools              # prints the names of the tools the arm's surface classes as writes
    python arm_side.py withhold                 # prints what the transport needs to withhold one write's acknowledgement
    python arm_side.py probe VAULT PATH         # prints the note at the vault-relative PATH
    python arm_side.py sources VAULT            # prints every Source page and every page citing Sources
    python arm_side.py mint                     # session JSON on stdin; prints a sign-in token for a loopback server
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import os
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

#: Seconds a process that serves the unit's collection store waits for the store to be admitted.
STORE_ADMISSION_SECONDS = 120


def probe(vault: Path, relative: str) -> dict:
    """The note's body and lifecycle class, as the arm's product code reads them.

    ``lifecycle_class`` is None when the arm's status registry cannot classify the label.
    """
    from exomem import lifecycle_statuses
    from exomem.vault import parse_frontmatter

    page = vault / relative
    if not page.exists():
        return {"path": relative, "exists": False, "body": None, "lifecycle_class": None}
    frontmatter, body, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
    classification = lifecycle_statuses.Basis(vault).classify(
        frontmatter.get("status"), path=relative, frontmatter=frontmatter
    )
    try:
        lifecycle = classification.require()
    except lifecycle_statuses.ClassificationUnavailable:
        lifecycle = None
    return {"path": relative, "exists": True, "body": body, "lifecycle_class": lifecycle}


def seed(vault: Path, work: Path, notes: dict | list[dict] | None) -> dict | list[dict] | None:
    """Write the arm's learning instruction into ``work/CLAUDE.md`` and the case's notes, if any, into the vault.

    One note object gives its probe; a list gives the probe of each note, written in order. A note may carry
    ``relation_disposition`` and ``relation_review_reason``, which go to the writer as given: the reviewed
    outcome for a page that has no truthful relation to the others.
    """
    from exomem import commands
    from exomem.client_config import place_learning_instruction
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.prominence import LEARNING_INSTRUCTION

    place_learning_instruction(work / "CLAUDE.md", LEARNING_INSTRUCTION)
    if notes is None:
        return None
    paths = []
    with request_scope(owner_principal(surface="mcp")):
        for note in notes if isinstance(notes, list) else [notes]:
            review = {
                key: note[key]
                for key in ("relation_disposition", "relation_review_reason")
                if key in note
            }
            written = commands.op_note(
                vault,
                title=note["title"],
                note_type=note["note_type"],
                content=note["content"],
                **review,
            )
            paths.append(written["path"])
    probes = [probe(vault, path) for path in paths]
    return probes if isinstance(notes, list) else probes[0]


def seed_sources(vault: Path, work: Path, sources: list[dict]) -> list[dict]:
    """Capture each source as the arm's own capture command does: a file of ``work`` or authored text.

    A file goes through a held upload, so the Source holds its original as an attachment does. A failed
    capture raises: a case whose seed is missing would measure nothing.
    """
    from exomem import commands, held_uploads, upload_handoff
    from exomem.governance.principal import (
        ClientBinding,
        OriginSessionBinding,
        owner_principal,
        request_scope,
    )
    from exomem.schema import load_source_schema

    owner = owner_principal(surface="mcp")
    # A hold belongs to a verified client session. Seeding is the owner acting on its own disposable
    # vault, so it binds one.
    session = replace(
        owner,
        client_binding=ClientBinding("incident-seed", "incident-seed"),
        origin_session=OriginSessionBinding("incident-seed", "1", owner.audience_id),
    )
    schema, receipts = load_source_schema(vault), []
    with request_scope(session):
        for source in sources:
            files = []
            if "file" in source:
                path = work / source["file"]
                held = held_uploads.hold(
                    vault,
                    io.BytesIO(path.read_bytes()),
                    lane="source",
                    filename=path.name,
                    content_type=mimetypes.guess_type(path.name)[0],
                    max_bytes=upload_handoff.MAX_BYTES,
                )
                files = [held["file"]]
            result = commands.op_capture_source(
                vault,
                schema,
                title=source["title"],
                source_kind=source["source_kind"],
                url=source.get("url"),
                content=source.get("content"),
                files=files,
            )
            if result.get("error") or result.get("summary", {}).get("failed"):
                raise RuntimeError(
                    f"seeding {source['title']!r} failed: {json.dumps(result)[:300]}"
                )
            receipts.append({"title": source["title"], "path": result.get("path")})
    return receipts


def method_link(vault: Path, path: str, title: str, form: str) -> str:
    """How a manifest's ``capture.method`` names the page just written: its ``exomem://`` ref (the form the
    guidance recommends, which needs no resolver), its ``[[title]]`` or its vault-relative path."""
    from exomem import memory_refs
    from exomem.vault import parse_frontmatter

    if form == "title":
        return f"[[{title}]]"
    if form == "path":
        return path
    frontmatter, _, _ = parse_frontmatter((vault / path).read_text(encoding="utf-8"))
    return memory_refs.memory_ref(frontmatter["exomem_id"])


def parses_capture() -> bool:
    """Whether the arm's own manifest carries a declared capture method.

    An arm from before the declaration shipped stores a `capture` block inertly and never reads it, so
    seeding one would measure an arm that cannot see the method, and reading it back would fail.
    """
    from exomem import structured_collections

    return hasattr(structured_collections.CollectionManifest, "capture_method")


def manifest_method(vault: Path, path: str) -> str | None:
    """The ``capture.method`` that the arm's own manifest parser reads from the collection at ``path``."""
    from exomem import structured_collections

    return structured_collections.load_manifest(vault, vault / path).capture_method


def seed_collections(vault: Path, collections: list[dict]) -> list[dict]:
    """Create each Records collection as the arm's own records command does, with its method page and rows.

    A collection is ``{manifest_path, manifest, body?, rows?, method_page?}``. ``manifest_path`` is relative to
    the knowledge base. ``manifest`` is the manifest's frontmatter (the arm's own serializer writes it) or its
    whole text. A method page is ``{title, note_type, content, revisions?, link?}``: it is written before the
    manifest, which then declares ``capture.method`` as the page's ``link`` form (``ref`` by default), so the
    link resolves by construction. An arm that does not parse ``capture`` (``parses_capture``) gets the same page
    as a note and a manifest without the block, and reports ``capture_supported: false``. Each revision replaces the page's body through the arm's edit command, so
    its current version is the last one. A refused write raises: a case whose seed is missing would measure
    nothing. The arm's own due-state heal then runs, as the maintenance job of a vault that has held its
    collections for a while would have run it: the write path routes by that projection.
    """
    from exomem import commands, due_state
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.vault import (
        content_hash,
        kb_root,
        render_frontmatter_document,
        serialize_frontmatter,
    )

    why = "seeded by the incident harness"
    capture_supported, written = parses_capture(), []
    with request_scope(owner_principal(surface="mcp")):
        for spec in collections:
            page = spec.get("method_page")
            if page:
                note = commands.op_note(
                    vault, title=page["title"], note_type=page["note_type"], content=page["content"]
                )
                for body in page.get("revisions", ()):
                    # The arm's edit is guarded twice: by the hash of the page as it now stands, and by a
                    # reviewed relation outcome that a preview of the same edit issues.
                    edit = {
                        "why": why,
                        "new_body": body,
                        "expected_hash": content_hash(
                            (vault / note["path"]).read_text(encoding="utf-8")
                        ),
                    }
                    preview = commands.op_edit(vault, note["path"], validate_only=True, **edit)
                    commands.op_edit(
                        vault,
                        note["path"],
                        transition_token=preview["semantic"]["transition_token"],
                        relation_disposition="reviewed_none",
                        relation_review_hash=preview["semantic"]["relation_review_hash"],
                        relation_review_reason="The harness revises the method's wording only.",
                        **edit,
                    )
            manifest = spec["manifest"]
            if page and capture_supported:
                manifest = {
                    **manifest,
                    "capture": {
                        "method": method_link(
                            vault, note["path"], page["title"], page.get("link", "ref")
                        )
                    },
                }
            if not isinstance(manifest, str):
                manifest = render_frontmatter_document(
                    serialize_frontmatter(manifest), spec.get("body", ""), blank_line=True
                )
            path = (kb_root(vault) / spec["manifest_path"]).relative_to(vault).as_posix()
            created = commands.op_record_memory(
                vault, action="create", manifest_path=path, manifest_text=manifest, why=why
            )
            for item in spec.get("rows", ()):
                created = commands.op_record_memory(
                    vault, action="append", collection=path, item=item, why=why
                )
                if created.get("error") or created.get("operation") != "append":
                    raise RuntimeError(
                        f"seeding a row of {path!r} failed: {json.dumps(created)[:300]}"
                    )
            written.append(
                {
                    "path": path,
                    "method_page": note["path"] if page else None,
                    "method_declared": manifest_method(vault, path)
                    if page and capture_supported
                    else None,
                    "capture_supported": capture_supported,
                }
            )
    due_state.reconcile(vault)
    return written


def build_indexes(vault: Path) -> dict:
    """Build the on-disk indexes that a long-lived vault already has, through the arm's own classes.

    The link graph, the working-set index, the context catalogue and the lexical store. The service builds
    them as its watcher sees pages change; a vault seeded a moment ago has none, and a turn on it would
    read ``partial`` for as long as the server runs, because the graph lane has nothing to read.
    """
    import time

    from exomem import context_catalogue, epistemic_graph, lexstore, working_set_index

    started = time.perf_counter()
    epistemic_graph.EpistemicGraphIndex(vault).rebuild_all()
    working_set_index.WorkingSetIndex(vault).rebuild()
    context_catalogue.ContextCatalogue(vault).rebuild()
    lexstore.ensure_fresh(vault)
    return {"seconds": round(time.perf_counter() - started, 2)}


def prominence(level: str) -> dict:
    """Set the capture prominence through the arm's own preference path, and read back what it resolves.

    The server and the hooks of the unit read the same pinned config file, so the declared-route
    disposition of a write follows this level. ``capture_level`` is what the arm then applies to capture.
    """
    from exomem import prominence as arm_prominence

    arm_prominence.write_prominence(level)
    return {"level": level, "capture_level": arm_prominence.effective_capture_level()}


def capture_disposition(disposition: str) -> dict:
    """Set the owner's `proactive_capture` through the arm's own envelope setter, and read back what is in force.

    The server of the unit reads the same pinned config file, so an `advisory` case measures the advisory route.
    """
    from exomem import envelope

    envelope.set_disposition("proactive_capture", disposition)
    return {"set": disposition, "in_force": envelope.active()["proactive_capture"]}


def has_store() -> bool:
    """Whether the unit has a lease coordinator, which is what a store vault is served under."""
    return bool(os.environ.get("EXOMEM_WRITER_LEASE_URL"))


def record_memory(vault: Path, **arguments) -> dict:
    """The arm's ``record_memory`` command. A store collection answers only through the dispatcher that its tools
    use, so a unit with a store reads through that; a file vault calls the command as it always has."""
    from exomem import commands

    if not has_store():
        return commands.op_record_memory(vault, **arguments)
    from exomem import writer_lease

    command = next(c for c in commands.product_commands_for("mcp") if c.name == "record_memory")
    return writer_lease.invoke_command(command, vault, **arguments)


@contextmanager
def served_store():
    """Serve the unit's collection store in this process, as the personal service does, when the unit has a lease
    coordinator (``EXOMEM_WRITER_LEASE_URL``); a plain file vault needs nothing.

    A store collection answers only in a process that serves its store. The lease is handed back when the block
    ends, so the unit's own server takes it at once and not after the lease's time to live.
    """
    if not has_store():
        yield
        return
    from exomem import writer_lease

    manager = writer_lease.start_server_lifecycle()
    try:
        deadline = time.monotonic() + STORE_ADMISSION_SECONDS
        while manager.status().get("collection_store", {}).get("status") != "admitted":
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"the collection store was not admitted in {STORE_ADMISSION_SECONDS} s: "
                    f"{json.dumps(manager.status().get('collection_store'), default=str)[:200]}"
                )
            time.sleep(0.2)
        yield
    finally:
        writer_lease.stop_server_lifecycle()


def records(vault: Path, collections: list[str]) -> dict:
    """The rows of each collection, as the arm's own records query returns them, by knowledge-base path.

    A collection the arm cannot read reports ``error`` and no rows: unknown, never an empty collection.
    """
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.vault import kb_root

    listed = {}
    with served_store(), request_scope(owner_principal(surface="mcp")):
        for relative in collections:
            path = (kb_root(vault) / relative).relative_to(vault).as_posix()
            answer = record_memory(vault, action="query", collection=path, limit=500)
            rows = answer.get("rows")
            listed[relative] = (
                {"rows": rows} if isinstance(rows, list) else {"error": json.dumps(answer)[:300]}
            )
    return listed


def collections(vault: Path) -> dict:
    """Every collection the vault holds, as the arm's own discovery reads it, with its rows and whether the arm says it
    was created automatically.

    A collection is ``{path, kb_path, collection_id, title, semantic_profile, natural_key, fields, automatic, rows}``:
    ``path`` is vault-relative, ``kb_path`` is relative to the knowledge base (how a case names a collection), and
    ``automatic`` is the inventory's own flag, ``None`` when the arm's inventory does not report one. ``rows`` is
    ``None`` for a collection that is not a Records collection and ``{"error": ...}`` in its place when the arm could
    not read it. A vault the arm could not list in full reports ``error`` and no collections: unknown, never an
    empty vault.
    """
    from exomem import structured_collections
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.vault import kb_root

    with served_store(), request_scope(owner_principal(surface="mcp")):
        found, unreadable = structured_collections.discover_collections_with_errors(vault)
        inventory = record_memory(vault, action="inspect")
        listed = inventory.get("collections")
        if not isinstance(listed, list):
            return {"error": json.dumps({"inventory": inventory}, default=str)[:300]}
        # The file sweep cannot see a store collection and reports its manifest as unreadable; the arm's own
        # inventory lists it, and its manifest reads in this process because it serves the store.
        stored = [
            entry["manifest_path"]
            for entry in listed
            if entry["manifest_path"] not in {m.path for m in found}
        ]
        unreadable = [item for item in unreadable if item.path not in stored]
        if unreadable or any((inventory.get("truncated") or {}).values()):
            return {
                "error": json.dumps(
                    {"unreadable": len(unreadable), "inventory": inventory}, default=str
                )[:300]
            }
        found = [
            *found,
            *(structured_collections.load_manifest(vault, vault / path) for path in stored),
        ]
        automatic = {entry["manifest_path"]: entry.get("automatic") for entry in listed}
        described = []
        for manifest in found:
            rows = None
            if manifest.semantic_profile == "records":
                answer = record_memory(vault, action="query", collection=manifest.path, limit=500)
                rows = (
                    answer["rows"]
                    if isinstance(answer.get("rows"), list)
                    else {"error": json.dumps(answer, default=str)[:300]}
                )
            try:
                kb_path = (
                    Path(manifest.path).relative_to(kb_root(vault).relative_to(vault)).as_posix()
                )
            except ValueError:
                kb_path = None
            described.append(
                {
                    "path": manifest.path,
                    "kb_path": kb_path,
                    "collection_id": manifest.collection_id,
                    "title": manifest.title,
                    "semantic_profile": manifest.semantic_profile,
                    "natural_key": list(manifest.schema.natural_key),
                    "fields": list(manifest.schema.fields),
                    "automatic": automatic.get(manifest.path),
                    "rows": rows,
                }
            )
    return {"collections": described}


def write_tools() -> dict:
    """The tools the arm's own surface classes as writes: each typed tool whose effect is ``write``, and the write door.

    A check that says "no write" reads this list from the arm, so a tool that the arm adds later is counted
    without a change here.
    """
    from exomem import public_surface, public_tools

    typed = [name for name, tool in public_tools.TOOLS.items() if tool.effect == "write"]
    return {"tools": sorted([*typed, public_surface.DOORS["write"]])}


def withhold() -> dict:
    """What a transport needs to answer a write that committed as the arm answers one whose acknowledgement is uncertain.

    ``result`` is the MCP result of the arm's ``MUTATION_ACKNOWLEDGEMENT_PENDING``, built as the arm builds it: its own
    error constructor, called with the arguments of the arm's own raise (read from the source of the store runtime
    that raises it, so no copy of its wording or details can drift), then its own public refusal, its own agent-facing
    reply and the MCP tool conversion that its server applies. ``writers`` is the arm's own list of write tools, and
    ``idempotency_key`` the metadata key that carries a call's replay identity.
    """
    import ast
    import inspect

    from fastmcp.tools import FunctionTool
    from mcp.types import CallToolResult

    from exomem import cli_ops, command_surface
    from exomem.collection_store import runtime

    code = "MUTATION_ACKNOWLEDGEMENT_PENDING"
    raised = next(
        (
            node
            for node in ast.walk(ast.parse(inspect.getsource(runtime)))
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", None) == "OpError"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == code
        ),
        None,
    )
    if raised is None:
        raise RuntimeError(
            f"the arm's store runtime raises no literal {code}; the unit cannot withhold one"
        )
    error = cli_ops.OpError(
        *(ast.literal_eval(argument) for argument in raised.args),
        **{keyword.arg: ast.literal_eval(keyword.value) for keyword in raised.keywords},
    )
    writers = write_tools()["tools"]
    body = command_surface.agent_refusal_result(
        cli_ops.public_refusal(error), error, writers[0], {}, (), write=True
    )
    content, structured = (
        FunctionTool.from_function(lambda: None, name=writers[0])
        .convert_result(body)
        .to_mcp_result()
    )
    result = CallToolResult(content=content, structuredContent=structured, isError=False)
    return {
        "writers": writers,
        "result": result.model_dump(mode="json", by_alias=True, exclude_none=True),
        "idempotency_key": command_surface.MCP_IDEMPOTENCY_META_KEY,
    }


def instructions() -> dict:
    """The text the arm's own server hands every client when it connects."""
    from exomem.server import SERVER_INSTRUCTIONS

    return {"server": SERVER_INSTRUCTIONS}


def sources(vault: Path) -> dict:
    """Every Source page, every Evidence page, and every other page that cites Sources, as the arm's
    own code reads them.

    A Source row carries its frontmatter, the SHA-256 of the original it stores (None when it stores no
    original), whether a correction receipt exists for it (None when the arm cannot say), and its
    ``ingested_into`` as page keys. A citing page carries its ``sources`` as page keys and whether its
    status is live (None when the arm's status registry cannot classify it). Page keys are the arm's own
    link resolution: a wikilink and the page it names have the same key. An Evidence row carries its
    frontmatter and the SHA-256 of the original beside it (None when there is none).
    """
    from exomem import lifecycle_statuses, media_store, reserved_paths, source_corrections
    from exomem.vault import (
        kb_root,
        parse_frontmatter,
        split_wikilink_target,
        walk_vault_md,
        wikilink_page_key,
    )

    def keys(links: object) -> list[str]:
        return [
            wikilink_page_key(split_wikilink_target(link)[0])
            for link in links or ()
            if isinstance(link, str)
        ]

    def original_digest(relative: str | None) -> str | None:
        """The stored original's SHA-256, or None when it stores no readable original.

        Shared original storage keeps the bytes in the content-addressed pool and no file at the
        logical path, so the hash goes through the storage-aware opener; a missing or unreadable
        original is a check failure the row reports, not a crash.
        """
        if not relative:
            return None
        try:
            with media_store.open_original(vault, relative) as original:
                return hashlib.sha256(original.stream.read()).hexdigest()
        except (OSError, reserved_paths.ReservedPathLeafError, media_store.MediaStoreError):
            return None

    basis, sources_dir = lifecycle_statuses.Basis(vault), kb_root(vault) / "Sources"
    evidence_dir = kb_root(vault) / "Evidence"
    source_rows, citing_rows, evidence_rows = [], [], []
    for page in walk_vault_md(vault):
        relative = page.relative_to(vault).as_posix()
        frontmatter, _, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
        if evidence_dir in page.parents and page.name != "index.md":
            # An Evidence page describes the original beside it, named without the `.md`.
            evidence_rows.append(
                {
                    "path": relative,
                    "frontmatter": frontmatter,
                    "original_sha256": original_digest(relative.removesuffix(".md")),
                }
            )
        if sources_dir in page.parents:
            if page.name == "index.md":
                continue
            original = frontmatter.get("evidence_file")
            try:
                corrected = source_corrections.has_correction(vault, frontmatter)
            except source_corrections.SourceCorrectionUnavailable:
                corrected = None
            source_rows.append(
                {
                    "path": relative,
                    "key": wikilink_page_key(relative),
                    "frontmatter": frontmatter,
                    "original_sha256": original_digest(original),
                    "corrected": corrected,
                    "ingested_into": keys(frontmatter.get("ingested_into")),
                }
            )
        elif frontmatter.get("sources"):
            try:
                live = (
                    basis.classify(
                        frontmatter.get("status"), path=relative, frontmatter=frontmatter
                    ).require()
                    == "live"
                )
            except lifecycle_statuses.ClassificationUnavailable:
                live = None
            citing_rows.append(
                {
                    "key": wikilink_page_key(relative),
                    "live": live,
                    "sources": keys(frontmatter["sources"]),
                }
            )
    return {"sources": source_rows, "citing": citing_rows, "evidence": evidence_rows}


def mint(session: dict) -> dict:
    """A sign-in token for the loopback server that the session's signing key and storage name.

    The server runs its own sign-in verifier on a disposable key; the token is the owner's session on
    it, issued by the arm's own session authority, as a browser sign-in would leave it.
    """
    import asyncio

    from exomem.auth_sessions import SessionAuthority, SessionIdentity

    authority = SessionAuthority.local(
        directory=Path(session["home"]) / "oauth-sessions",
        signing_root=session["signing_root"],
        issuer=session["base_url"],
        audience=session["base_url"] + "/mcp",
    )
    identity = SessionIdentity(github_user_id=session["user_id"], github_login=session["login"])
    token, _ = asyncio.run(
        authority.issue(
            client_id="incident", scopes=["exomem:read", "exomem:write"], identity=identity
        )
    )
    return {"token": token}


if __name__ == "__main__":
    command, *args = sys.argv[1:]
    if command == "seed":
        result = seed(Path(args[0]), Path(args[1]), json.load(sys.stdin))
    elif command == "seed_sources":
        result = seed_sources(Path(args[0]), Path(args[1]), json.load(sys.stdin))
    elif command == "seed_collections":
        result = seed_collections(Path(args[0]), json.load(sys.stdin))
    elif command == "build_indexes":
        result = build_indexes(Path(args[0]))
    elif command == "prominence":
        result = prominence(args[0])
    elif command == "capture_disposition":
        result = capture_disposition(args[0])
    elif command == "records":
        result = records(Path(args[0]), args[1:])
    elif command == "collections":
        result = collections(Path(args[0]))
    elif command == "instructions":
        result = instructions()
    elif command == "write_tools":
        result = write_tools()
    elif command == "withhold":
        result = withhold()
    elif command == "sources":
        result = sources(Path(args[0]))
    elif command == "mint":
        result = mint(json.load(sys.stdin))
    else:
        result = probe(Path(args[0]), args[1])
    print(json.dumps(result, default=str))
