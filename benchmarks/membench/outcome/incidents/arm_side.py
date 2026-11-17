"""The half of an incident session that runs as the arm's own product code.

The runner starts this file with the arm's ``PYTHONPATH``, so the arm's own writer, guidance text,
frontmatter parser and status registry seed the vault and read the seeded note back. The runner
never parses a note itself.

    python arm_side.py seed VAULT WORK          # note JSON (or null) on stdin; places WORK/CLAUDE.md; prints the note
    python arm_side.py seed_sources VAULT WORK  # list of Sources on stdin; captures each; prints the receipts
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
    elif command == "sources":
        result = sources(Path(args[0]))
    elif command == "mint":
        result = mint(json.load(sys.stdin))
    else:
        result = probe(Path(args[0]), args[1])
    print(json.dumps(result, default=str))
