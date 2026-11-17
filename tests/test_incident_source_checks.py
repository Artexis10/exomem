"""The incident runner's source checks, fixtures and byte path, against the arm's own product code.

Run from the repository root: ``PYTHONPATH=benchmarks:src pytest benchmarks/membench/outcome/incidents``.
``INCIDENT_TEST_ARM`` names another checkout to run as the arm (default: this one).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from membench.outcome.incidents import __main__ as incidents
from membench.outcome.incidents.fixtures import build_files
from membench.outcome.incidents.server import lay_exomem_command, loopback_mcp
from membench.outcome.isolation import REPO_ROOT, child_env, fresh_vault

ARM = Path(os.environ.get("INCIDENT_TEST_ARM", REPO_ROOT)).resolve()
SPECS = {
    "paper.pdf": {
        "kind": "pdf",
        "title": "Doc title",
        "doi": "10.5555/xmp.1",
        "pages": ["Printed 10.5555/page.1", "Two"],
    },
    "note.txt": {"kind": "text", "text": "A transcript.\n"},
    "shot.png": {"kind": "png", "text": "Poster: a claim"},
}


def arm_side(root: Path, *args: str, stdin: object = None) -> dict:
    return incidents.run_arm_side(ARM, root, *args, stdin=json.dumps(stdin))


def test_fixtures_have_one_set_of_bytes_and_say_what_the_spec_says(tmp_path: Path) -> None:
    """A clock or version in the bytes would give every unit its own digest; a lost DOI would blind the case."""
    pymupdf = pytest.importorskip("pymupdf")  # the media extra builds the fixtures
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir(), second.mkdir()
    digests = build_files(SPECS, first)
    assert build_files(SPECS, second) == digests
    assert {
        name: hashlib.sha256((first / name).read_bytes()).hexdigest() for name in SPECS
    } == digests
    pdf = pymupdf.open(first / "paper.pdf")
    assert "10.5555/page.1" in pdf[0].get_text()
    assert pdf.metadata["title"] == "Doc title" and "10.5555/xmp.1" in pdf.get_xml_metadata()
    assert (first / "shot.png").read_bytes().startswith(b"\x89PNG")


def holds(*, original: str | None, corrected: bool | None = False, **frontmatter) -> dict:
    return {
        "original_sha256": original,
        "corrected": corrected,
        "frontmatter": frontmatter,
        "key": "k",
        "path": "p",
        "ingested_into": [],
    }


@pytest.mark.parametrize(
    ("rows", "if_none", "expected"),
    [
        # A correction supersedes the predecessor: only the successor's metadata counts.
        (
            [
                holds(original="d", corrected=True, url="https://doi.org/WRONG"),
                holds(original="d", url="https://doi.org/Right.1"),
            ],
            None,
            True,
        ),
        ([holds(original="d", url="https://doi.org/WRONG")], None, False),
        # The arm could not say whether a receipt exists: unknown, never a pass or a failure.
        ([holds(original="d", corrected=None, url="https://doi.org/Right.1")], None, None),
        # No Source holds the bytes: the case decides what that means.
        ([holds(original="other", url="x")], None, False),
        ([holds(original="other", url="x")], True, True),
    ],
)
def test_current_source_judges_what_no_correction_supersedes(rows, if_none, expected) -> None:
    spec = {
        "file": "f",
        "where": [
            {"field": "url", "op": "contains", "value": "right.1"},
            {"field": "url", "op": "not_contains", "value": "wrong"},
        ],
    }
    spec |= {"if_none": if_none} if if_none is not None else {}
    assert incidents.current_source({"sources": rows}, {"f": "d"}, spec) is expected


@pytest.mark.parametrize(
    ("live", "cites", "listed_back", "expected"),
    [
        (True, ["a", "b"], True, True),
        (True, ["a", "b"], False, False),  # the Source does not list the page back
        (True, ["a"], True, False),  # the page cites one paper only
        (False, ["a", "b"], True, False),
        (None, ["a", "b"], True, None),  # the arm's registry cannot classify the page
    ],
)
def test_compiled_with_backlinks_needs_a_live_page_that_cites_every_source_and_is_listed_back(
    live, cites, listed_back, expected
) -> None:
    rows = [
        {**holds(original=digest), "key": key, "ingested_into": ["n"] if listed_back else []}
        for key, digest in (("a", "da"), ("b", "db"))
    ]
    facts = {"sources": rows, "citing": [{"key": "n", "live": live, "sources": cites}]}
    assert (
        incidents.compiled_with_backlinks(facts, {"fa": "da", "fb": "db"}, ["fa", "fb"]) is expected
    )


def test_the_judge_reads_the_labels_of_the_sessions_sources_and_nothing_else() -> None:
    def row(path: str, **frontmatter) -> dict:
        return {
            "path": path,
            "frontmatter": {"title": path, "source_type": "paper", "url": "u", **frontmatter},
        }

    facts = {
        "sources": [
            row("seeded"),
            row(
                "new",
                document_form="abstract",
                identity_check={"state": "conflict", "printed_dois": ["10.5555/x"]},
                evidence_file="Knowledge Base/Sources/secret-path",
            ),
        ]
    }
    saved = incidents.session_sources(facts, before=["seeded"])
    assert saved == [
        {
            "title": "new",
            "source_kind": "paper",
            "url": "u",
            "document_form": "abstract",
            "identity_check_state": "conflict",
        }
    ]
    context = incidents.judge_context(
        {"include_sources": True, "reference": "It is a thesis."}, saved
    )
    assert (
        "secret-path" not in context and "10.5555/x" not in context and "It is a thesis." in context
    )
    assert incidents.judge_context({}, saved) == ""


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"pass": {"source_saved": ["papr.pdf"]}}, "not declared"),
        ({"transport": "ftp"}, "transport"),
        ({"pass": {"current_source": {"file": "paper.pdf", "where": []}}}, "current_source"),
        (
            {
                "seed_sources": [
                    {"file": "paper.pdf", "content": "x", "title": "t", "source_kind": "k"}
                ]
            },
            "seed source",
        ),
    ],
)
def test_a_case_that_could_pass_on_nothing_is_refused_before_a_session_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: dict, message: str
) -> None:
    case = {
        "id": "c",
        "prompt": "p",
        "files": ["paper.pdf"],
        "pass": {"source_saved": ["paper.pdf"]},
    } | change
    (tmp_path / "c.json").write_text(
        json.dumps({"library": {"paper.pdf": SPECS["paper.pdf"]}, "cases": [case]})
    )
    monkeypatch.setattr(incidents, "CASES_DIR", tmp_path)
    with pytest.raises(SystemExit, match=message):
        incidents.load_cases(None)


def test_seeded_sources_and_a_citing_page_are_read_back_through_the_arms_own_code(
    tmp_path: Path,
) -> None:
    """The check reads the fields the arm writes, so a renamed original or backlink field fails here, not after a paid session."""
    pytest.importorskip("pymupdf")  # the media extra builds the fixtures
    root, work = tmp_path / "unit", tmp_path / "unit" / "work"
    fresh_vault(root, ARM)
    work.mkdir()
    digests = build_files({"note.txt": SPECS["note.txt"]}, work)
    arm_side(
        root,
        "seed_sources",
        str(root / "vault"),
        str(work),
        stdin=[{"file": "note.txt", "title": "Seeded transcript", "source_kind": "transcript"}],
    )
    facts = arm_side(root, "sources", str(root / "vault"))
    assert incidents.source_saved(facts, digests, ["note.txt"])
    assert incidents.source_count(facts, digests, {"file": "note.txt", "equals": 1})
    assert incidents.compiled_with_backlinks(facts, digests, ["note.txt"]) is False

    [source] = facts["sources"]
    cite = (
        "import sys; from pathlib import Path; from exomem import commands; "
        "from exomem.governance.principal import owner_principal, request_scope\n"
        "with request_scope(owner_principal(surface='mcp')):\n"
        "    commands.op_note(Path(sys.argv[1]), title='Compiled', note_type='insight', "
        "content=sys.argv[3], sources=[sys.argv[2]])"
    )
    subprocess.run(
        [sys.executable, "-c", cite, str(root / "vault"), source["path"], "## Findings\n\n- [finding] x\n"],
        env=child_env(root, ARM),
        check=True,
        capture_output=True,
    )
    facts = arm_side(root, "sources", str(root / "vault"))
    assert incidents.compiled_with_backlinks(facts, digests, ["note.txt"]) is True


def test_an_http_unit_sends_a_files_bytes_and_the_stored_original_has_its_digest(
    tmp_path: Path,
) -> None:
    """upload_files, `exomem attach --handoff-file`, then save_source with the held handle, on the unit's own server.

    A product change to sign-in or the handoff would otherwise leave every http case unknown, after the sessions are paid for.
    """
    pytest.importorskip("pymupdf")  # the media extra builds the fixtures
    root, work = tmp_path / "unit", tmp_path / "unit" / "work"
    fresh_vault(root, ARM)
    work.mkdir()
    digest = build_files({"paper.pdf": SPECS["paper.pdf"]}, work)["paper.pdf"]

    def mint(session: dict) -> str:
        return arm_side(root, "mint", stdin=session)["token"]

    async def send(entry: dict) -> dict:
        transport = StreamableHttpTransport(entry["url"], headers=entry["headers"])
        async with Client(transport) as client:
            held = await client.call_tool(
                "upload_files", {"lane": "source", "hold": True, "expected_sha256": digest}
            )
            (work / "handoff.json").write_text(
                json.dumps(held.structured_content or json.loads(held.content[0].text))
            )
            attach = subprocess.run(
                [
                    str(lay_exomem_command(root, ARM) / "exomem"),
                    "attach",
                    "paper.pdf",
                    "--handoff-file",
                    "handoff.json",
                ],
                cwd=work,
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert attach.returncode == 0, attach.stderr
            handle = json.loads(attach.stdout)["file"]
            saved = await client.call_tool(
                "save_source",
                {
                    "title": "Sent paper",
                    "source_kind": "paper",
                    "url": "https://doi.org/10.5555/x.1",
                    "files": [handle],
                },
            )
            return json.loads(saved.content[0].text)

    with loopback_mcp(root, ARM, mint) as entry:
        reply = asyncio.run(send(entry))
    assert reply["summary"] == {"stored": 1, "failed": 0}, reply
    [source] = arm_side(root, "sources", str(root / "vault"))["sources"]
    assert source["original_sha256"] == digest == reply["files"][0]["hash"]
    print(
        json.dumps(
            {
                "file_sha256": digest,
                "stored_original_sha256": source["original_sha256"],
                "path": source["path"],
            }
        )
    )
