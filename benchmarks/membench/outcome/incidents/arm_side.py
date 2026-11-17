"""The half of an incident session that runs as the arm's own product code.

The runner starts this file with the arm's ``PYTHONPATH``, so the arm's own writer, guidance text,
frontmatter parser and status registry seed the vault and read the seeded note back. The runner
never parses a note itself.

    python arm_side.py seed VAULT WORK          # note JSON (or null) on stdin; places WORK/CLAUDE.md; prints the note
    python arm_side.py seed_sources VAULT WORK  # list of Sources on stdin; captures each; prints the receipts
    python arm_side.py seed_collections VAULT   # list of collections on stdin; creates each; prints the pages written
    python arm_side.py prominence LEVEL         # sets the capture prominence; prints the level the arm then resolves
    python arm_side.py records VAULT PATH...    # prints the rows of each Records collection, by knowledge-base path
    python arm_side.py instructions             # prints the text the arm's server hands every client
    python arm_side.py probe VAULT PATH         # prints the note at the vault-relative PATH
    python arm_side.py sources VAULT            # prints every Source page and every page citing Sources
    python arm_side.py mint                     # session JSON on stdin; prints a sign-in token for a loopback server
"""

from __future__ import annotations

import hashlib
import io
import json
import mimetypes
import sys
from dataclasses import replace
from pathlib import Path


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


def seed(vault: Path, work: Path, note: dict | None) -> dict | None:
    """Write the arm's learning instruction into ``work/CLAUDE.md`` and the case's note, if any, into the vault."""
    from exomem import commands
    from exomem.client_config import place_learning_instruction
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.prominence import LEARNING_INSTRUCTION

    place_learning_instruction(work / "CLAUDE.md", LEARNING_INSTRUCTION)
    if note is None:
        return None
    with request_scope(owner_principal(surface="mcp")):
        written = commands.op_note(
            vault, title=note["title"], note_type=note["note_type"], content=note["content"]
        )
    return probe(vault, written["path"])


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


def prominence(level: str) -> dict:
    """Set the capture prominence through the arm's own preference path, and read back what it resolves.

    The server and the hooks of the unit read the same pinned config file, so the declared-route
    disposition of a write follows this level. ``capture_level`` is what the arm then applies to capture.
    """
    from exomem import prominence as arm_prominence

    arm_prominence.write_prominence(level)
    return {"level": level, "capture_level": arm_prominence.effective_capture_level()}


def records(vault: Path, collections: list[str]) -> dict:
    """The rows of each collection, as the arm's own records query returns them, by knowledge-base path.

    A collection the arm cannot read reports ``error`` and no rows: unknown, never an empty collection.
    """
    from exomem import commands
    from exomem.governance.principal import owner_principal, request_scope
    from exomem.vault import kb_root

    listed = {}
    with request_scope(owner_principal(surface="mcp")):
        for relative in collections:
            path = (kb_root(vault) / relative).relative_to(vault).as_posix()
            answer = commands.op_record_memory(vault, action="query", collection=path, limit=500)
            rows = answer.get("rows")
            listed[relative] = (
                {"rows": rows} if isinstance(rows, list) else {"error": json.dumps(answer)[:300]}
            )
    return listed


def instructions() -> dict:
    """The text the arm's own server hands every client when it connects."""
    from exomem.server import SERVER_INSTRUCTIONS

    return {"server": SERVER_INSTRUCTIONS}


def sources(vault: Path) -> dict:
    """Every Source page, and every other page that cites Sources, as the arm's own code reads them.

    A Source row carries its frontmatter, the SHA-256 of the original it stores (None when it stores no
    original), whether a correction receipt exists for it (None when the arm cannot say), and its
    ``ingested_into`` as page keys. A citing page carries its ``sources`` as page keys and whether its
    status is live (None when the arm's status registry cannot classify it). Page keys are the arm's own
    link resolution: a wikilink and the page it names have the same key.
    """
    from exomem import lifecycle_statuses, source_corrections
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

    basis, sources_dir = lifecycle_statuses.Basis(vault), kb_root(vault) / "Sources"
    source_rows, citing_rows = [], []
    for page in walk_vault_md(vault):
        relative = page.relative_to(vault).as_posix()
        frontmatter, _, _ = parse_frontmatter(page.read_text(encoding="utf-8"))
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
                    "original_sha256": (
                        hashlib.sha256((vault / original).read_bytes()).hexdigest()
                        if original
                        else None
                    ),
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
    return {"sources": source_rows, "citing": citing_rows}


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
    elif command == "prominence":
        result = prominence(args[0])
    elif command == "records":
        result = records(Path(args[0]), args[1:])
    elif command == "instructions":
        result = instructions()
    elif command == "sources":
        result = sources(Path(args[0]))
    elif command == "mint":
        result = mint(json.load(sys.stdin))
    else:
        result = probe(Path(args[0]), args[1])
    print(json.dumps(result, default=str))
