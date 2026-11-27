"""An Evernote notebook and a Gmail mailbox are filed as one Source per note and per message, each with its own time.

OpenSpec import-notes-and-mail-exports, groups 2 and 3. The vault is a temporary one with a real stage, and the cell's
filer drives `adoption_studio file` through the dispatcher, as for a Home import. The notebooks and mailboxes are
invented. Each test names the failure it catches.
"""

# ruff: noqa: F811 -- the fixtures of the first batch's tests are imported and used by name
from __future__ import annotations

import base64
import datetime as dt
import email.utils
import errno
import hashlib
import io
import itertools
import json
import subprocess
import sys
from email import policy
from email.message import EmailMessage
from pathlib import Path

import pytest
from test_app_export_import import (  # noqa: F401 -- `_private_journal_folder` is an autouse fixture that the tests need
    _private_journal_folder,
    created_at,
    expansion_rows,
    file_to_done,
    marker_of,
    source_pages,
    stage_export,
    title_of,
    vault,
)

from exomem import (
    adoption_run,
    cloud_import,
    export_expansion,
    export_mail,
    export_notes,
    import_stage,
    source_formats,
)

#: A 1x1 JPEG; the pipeline decides what it is by its extension.
JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////"
    "wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=")
ENEX_HEAD = (
    '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE en-export SYSTEM "http://xml.evernote.com/pub/evernote-export4.dtd">\n'
    '<en-export export-date="20240401T000000Z" application="Evernote" version="10.0">')


def _evernote_time(moment: dt.datetime) -> str:
    return moment.strftime("%Y%m%dT%H%M%SZ")


def enex_note(
    number: int, *, resources: tuple[tuple[str, str, bytes], ...] = (), tags: tuple[str, ...] = (), source: str | None = None,
    text: str | None = None,
) -> str:
    """One note as Evernote writes it: ENML in a CDATA section, then its times, tags and resources (name, MIME type, bytes)."""
    media = "".join(f'<div><en-media hash="{hashlib.md5(data).hexdigest()}" type="{mime}"/></div>' for _name, mime, data in resources)
    body = (
        f"<en-note><div>{text or marker_of(number)}</div><div><en-todo checked=\"true\"/>pack the tide tables</div>"
        f"<div><en-todo/>oil the gearbox</div>{media}</en-note>")
    content = (
        '<?xml version="1.0" encoding="UTF-8" standalone="no"?><!DOCTYPE en-note SYSTEM "http://xml.evernote.com/pub/enml2.dtd">'
        + body)
    attached = "".join(
        f'<resource><data encoding="base64">{base64.encodebytes(data).decode()}</data><mime>{mime}</mime><width>1</width>'
        f"<height>1</height><resource-attributes><file-name>{name}</file-name></resource-attributes></resource>"
        for name, mime, data in resources)
    return (
        f"<note><title>{title_of(number)}</title><content><![CDATA[{content}]]></content>"
        f"<created>{_evernote_time(created_at(number))}</created><updated>{_evernote_time(created_at(number) + dt.timedelta(hours=2))}</updated>"
        + "".join(f"<tag>{tag}</tag>" for tag in tags)
        + (f"<note-attributes><source-url>{source}</source-url></note-attributes>" if source else "")
        + attached + "</note>")


def enex(notes: list[str]) -> bytes:
    return (ENEX_HEAD + "".join(notes) + "</en-export>").encode()


def mail(
    number: int, *, labels: str = "Inbox,Unread", html: str | None = None, text: str | None = None, attachments: tuple = (),
    inline: tuple = (), message_id: str | None = "", cc: str | None = "carol@example.invalid",
) -> bytes:
    """One message as Takeout writes it: its headers, an `X-Gmail-Labels` line and a body of plain text, HTML or both.

    `attachments` and `inline` are (name, MIME type, bytes). An empty `message_id` is the default one; None leaves it out.
    """
    message = EmailMessage()
    message["From"] = "alice@example.invalid"
    message["To"] = "bob@example.invalid"
    if cc:
        message["Cc"] = cc
    message["Subject"] = title_of(number)
    message["Date"] = email.utils.format_datetime(created_at(number))
    if message_id is not None:
        message["Message-ID"] = message_id or f"<{number}.tide@example.invalid>"
    message["X-Gmail-Labels"] = labels
    body = text or marker_of(number)
    if html is None:
        message.set_content(body)
    elif text == "":
        message.set_content(html, subtype="html")
    else:
        message.set_content(body)
        message.add_alternative(html, subtype="html")
    for name, mime, data in inline:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name, disposition="inline", cid=f"<{name}@tide>")
    for name, mime, data in attachments:
        maintype, _, subtype = mime.partition("/")
        message.add_attachment(data, maintype=maintype, subtype=subtype, filename=name)
    return message.as_bytes(policy=policy.default.clone(linesep="\n"))


SEPARATOR = b"From 1789@xxx Mon Mar  4 09:12:00 2024\n"


def mbox(messages: list[bytes], *, crlf: bool = False) -> bytes:
    """A mailbox: each message after a `From ` line and followed by a blank line; CRLF line ends when `crlf`."""
    data = b"".join(SEPARATOR + message + b"\n" for message in messages)
    return data.replace(b"\n", b"\r\n") if crlf else data


def correspondence(vault: Path) -> dict[str, tuple[dict, str]]:
    return {path: page for path, page in source_pages(vault).items() if page[0]["source_type"] == "correspondence"}


def attachments(vault: Path) -> dict[str, dict]:
    """The Sources that an expansion derived from attachments, by file name: their front matter."""
    return {
        Path(page[0]["collection_path"]).name: page[0]
        for page in source_pages(vault).values() if " files/" in page[0].get("collection_path", "")}


def notes_of(vault: Path) -> dict[str, tuple[dict, str]]:
    return {path: page for path, page in source_pages(vault).items() if page[0]["source_type"] == "app-note"}


# ----- Evernote


def test_an_evernote_notebook_files_one_app_note_per_note_with_its_time_its_photo_and_the_notebook_itself(vault: Path) -> None:
    """Catches a notebook that files as one blob, a photo that never becomes searchable, or a note that loses its own date."""
    resources = (("Счёт.jpg", "image/jpeg", JPEG), ("scan.bin", "application/x-scan", b"\x00\x01\x02" * 40))
    notes = [
        enex_note(number, resources=resources if number == 4 else (), tags=("garden", "tides") if number == 0 else (),
                  source="https://example.invalid/tides" if number == 0 else None)
        for number in range(30)]
    data = enex(notes)

    view = file_to_done(vault, stage_export(vault, data, name="notebook.enex"))

    pages = source_pages(vault)
    found = notes_of(vault)
    assert len(found) == 30
    for number in range(30):
        (frontmatter, text), = (page for page in found.values() if page[0]["title"] == title_of(number))
        assert frontmatter["item_created"] == created_at(number).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert marker_of(number) in text and "[x] pack the tide tables" in text and "[ ] oil the gearbox" in text
    (first,) = (page for page in found.values() if page[0]["title"] == title_of(0))
    assert "Tags: garden, tides" in first[1] and "Source: https://example.invalid/tides" in first[1]
    (receipt,) = (page for page in found.values() if page[0]["title"] == title_of(4))
    assert "[Счёт.jpg]" in receipt[1]  # the photo's place in the note
    assert "Счёт.jpg (filed as its own Source)" in receipt[1] and "scan.bin (kept in the export)" in receipt[1]
    assert list(attachments(vault)) == ["Счёт.jpg"]  # a type that the pipeline cannot index stays only in the notebook
    assert attachments(vault)["Счёт.jpg"]["item_created"] == created_at(4).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert attachments(vault)["Счёт.jpg"]["media_type"] == "image"
    (kept,) = (vault / "Knowledge Base" / "Sources").rglob("*.enex")
    assert kept.read_bytes() == data
    assert len(pages) == 30 + 1 + 1  # the notes, the photo and the notebook
    assert view["state"] == "done" and view["left_out"]["by_reason"] == {}
    assert view["progress"] == {"total": 32, "filed": 32, "skipped_duplicates": 0, "failed": 0, "left_out": 0, "remaining": 0}


def test_an_attachment_files_with_the_kind_and_media_type_that_the_same_file_has_when_it_is_staged_alone(vault: Path) -> None:
    """Catches a derived attachment that is filed by a rule of its own, so that a photo in a note differs from the same photo uploaded."""
    file_to_done(vault, stage_export(vault, JPEG, name="receipt.jpg"))
    (alone,) = (page[0] for page in source_pages(vault).values() if page[0].get("original_filename") == "receipt.jpg")

    file_to_done(vault, stage_export(vault, enex([enex_note(0, resources=(("receipt2.jpg", "image/jpeg", JPEG + b"\x00"),))]), name="notebook.enex"))

    inside = attachments(vault)["receipt2.jpg"]
    assert (inside["source_type"], inside["media_type"]) == (alone["source_type"], alone["media_type"]) == ("unclassified", "image")


def test_a_link_where_a_notebook_keeps_its_photos_is_refused_and_nothing_outside_the_stage_changes(vault: Path, tmp_path: Path) -> None:
    """Catches resource parts written into, or a leftover sweep run inside, a folder that a link in the stage points to."""
    import_id = stage_export(vault, enex([enex_note(0, resources=(("Счёт.jpg", "image/jpeg", JPEG),))]), name="notebook.enex")
    outside = tmp_path / "outside"
    (outside / ".incoming").mkdir(parents=True)
    (outside / ".incoming" / "keep.txt").write_text("not the stage's")
    (vault / "_Imports" / import_id / "notebook files").symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError, match="is a link"):
        export_expansion.expand(vault, import_id, keep_going=lambda: True)

    assert sorted(path.relative_to(outside).as_posix() for path in outside.rglob("*")) == [".incoming", ".incoming/keep.txt"]


def test_an_enex_that_declares_entities_is_not_expanded_and_files_as_one_ordinary_file(vault: Path) -> None:
    """Catches an entity-expansion document that is parsed: a few bytes of declarations must never grow into gigabytes."""
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE en-export [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]><en-export><note><title>&c;</title></note></en-export>')
    import_id = stage_export(vault, bomb, name="notebook.enex")

    view = file_to_done(vault, import_id)

    assert export_expansion.read(vault, import_id).exports == ()
    pages = source_pages(vault)
    assert len(pages) == 1 and not notes_of(vault)
    assert view["state"] == "done" and view["left_out"]["by_reason"] == {}


def test_a_truncated_enex_files_the_notes_before_the_cut_and_is_listed_as_read_in_part(vault: Path) -> None:
    """Catches a notebook cut off in transit that loses every note, or one that reads as done with all of them filed."""
    data = enex([enex_note(number) for number in range(10)])
    cut = data[: data.index(b"<note>", data.index(title_of(6).encode())) + 40]

    view = file_to_done(vault, stage_export(vault, cut, name="notebook.enex"))

    assert len(notes_of(vault)) == 7  # the notes whose end tag came before the cut
    assert view["state"] == "done" and view["left_out"]["by_reason"] == {"IMPORT_EXPORT_PARTIAL": 1}
    assert view["left_out"]["items"] == [{"path": "notebook.enex items", "code": "IMPORT_EXPORT_PARTIAL"}]
    assert view["progress"]["failed"] == 0 and view["progress"]["remaining"] == 0


def test_a_note_longer_than_the_limit_fails_by_its_position_and_the_other_notes_file(vault: Path, monkeypatch) -> None:
    """Catches one huge note that stops the notebook, or one that vanishes from the counts."""
    monkeypatch.setattr(export_notes, "MAX_CONTENT_CHARS", 2000)
    notes = [enex_note(number, text="x" * 5000 if number == 2 else None) for number in range(5)]

    view = file_to_done(vault, stage_export(vault, enex(notes), name="notebook.enex"))

    assert len(notes_of(vault)) == 4 and not any(page[0]["title"] == title_of(2) for page in notes_of(vault).values())
    assert view["left_out"]["by_reason"] == {"IMPORT_ITEM_UNREADABLE": 1}
    assert view["left_out"]["items"] == [{"path": "notebook.enex item item-000003", "code": "IMPORT_ITEM_UNREADABLE"}]
    assert view["progress"]["failed"] == 1 and view["progress"]["remaining"] == 0


#: The measuring child's own peak memory in bytes. On Linux, `ru_maxrss` keeps the forking parent's high-water mark
#: across exec, so a busy test worker would be measured instead of the reader; VmHWM starts fresh at exec.
_PEAK = """
import sys
def peak():
    try:
        with open("/proc/self/status") as status:
            for line in status:
                if line.startswith("VmHWM:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    import resource
    used = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return used if sys.platform == "darwin" else used * 1024
"""

_MEASURE = _PEAK + """
import io
from pathlib import Path
from exomem import export_notes, source_formats
fmt = {f.key: f for f in source_formats.formats(Path(sys.argv[3]))}["evernote-notes"]
before = peak()
with open(sys.argv[1], "rb") as stream:
    items = list(export_notes.read(fmt, stream, workdir=Path(sys.argv[2])))
after = peak()
print(len(items), before, after)
"""


def test_a_note_with_a_200_mib_resource_is_read_in_well_under_256_mib_of_memory(tmp_path: Path, vault: Path) -> None:
    """Catches a reader that holds a resource whole: one large attachment must not decide the cell's memory."""
    size = 200 * 1024 * 1024
    line = base64.b64encode(b"\x7f" * 57) + b"\n"  # 57 bytes encode to one 76-character line
    export = tmp_path / "big.enex"
    head = (ENEX_HEAD + "<note><title>Scan</title><content><![CDATA[<en-note>one scan</en-note>]]></content>"
            '<resource><data encoding="base64">').encode()
    tail = b"</data><mime>application/x-scan</mime></resource></note></en-export>"
    with open(export, "wb") as sink:
        sink.write(head)
        for _ in range(size // 57):
            sink.write(line)
        sink.write(tail)

    run = subprocess.run(
        [sys.executable, "-c", _MEASURE, str(export), str(tmp_path / "work"), str(vault)],
        capture_output=True, text=True, check=True, cwd=tmp_path,
        env={"PYTHONPATH": ":".join(path for path in sys.path if path), "EXOMEM_STATE_ROOT": str(tmp_path / "state")})

    export.unlink()  # a 280 MB file: do not leave it to the test folder's retention
    items, before, after = (int(value) for value in run.stdout.split())
    assert items == 1
    assert after < 256 * 1024 * 1024, f"peak {after >> 20} MiB after reading, {before >> 20} MiB before"


_MEASURE_STALL = _PEAK + """
from pathlib import Path
from exomem import export_notes, export_transcripts, source_formats
fmt = {f.key: f for f in source_formats.formats(Path(sys.argv[3]))}["evernote-notes"]
items, fault = 0, ""
with open(sys.argv[1], "rb") as stream:
    try:
        for _ in export_notes.read(fmt, stream, workdir=Path(sys.argv[2])):
            items += 1
    except export_transcripts.ExportFault as error:
        fault = str(error)
print(items, peak(), len(fault) > 0)
"""


@pytest.mark.parametrize("crafted", [
    b"<a>" * 1_000 + b"</a>" * 1_000,  # deep nesting: the parser keeps a stack entry per open element
    b"".join(b"<e%d/>" % number for number in range(10_000)),  # ever-new names: the parser keeps a table entry per name
    b"".join(b'<p%d:a xmlns:p%d="u"/>' % (number, number) for number in range(10_000)),  # prefixed names hide from a name count
    b"<a " + b"".join(b'x%d="%s" ' % (number, b"v" * 2_000) for number in range(3_000)) + b"/>",  # one 6 MB start tag
], ids=["deep nesting", "distinct names", "namespace prefixes", "one huge tag"])
def test_an_enex_of_a_crafted_shape_ends_as_partial_and_keeps_the_notes_before_it(vault: Path, crafted: bytes) -> None:
    """Catches a reader whose parser memory grows with the document's shape: 20 MiB of such tags held over 1 GB."""
    data = enex([enex_note(0), enex_note(1)])
    crafted_data = data.replace(b"</en-export>", crafted + b"</en-export>")

    view = file_to_done(vault, stage_export(vault, crafted_data, name="notebook.enex"))

    assert len(notes_of(vault)) == 2
    assert view["state"] == "done" and view["left_out"]["by_reason"] == {"IMPORT_EXPORT_PARTIAL": 1}


def test_an_enex_with_one_huge_comment_ends_as_partial_in_bounded_memory_and_keeps_the_notes_before_it(tmp_path: Path, vault: Path) -> None:
    """Catches a reader that lets the parser hold a whole comment or attribute value: a 150 MiB one more than doubled the peak."""
    size = 150 * 1024 * 1024
    export = tmp_path / "comment.enex"
    with open(export, "wb") as sink:
        sink.write((ENEX_HEAD + enex_note(1)).encode() + b"<!--")
        for _ in range(size // (1024 * 1024)):
            sink.write(b"x" * 1024 * 1024)
        sink.write(b"-->" + enex_note(2).encode() + b"</en-export>")

    run = subprocess.run(
        [sys.executable, "-c", _MEASURE_STALL, str(export), str(tmp_path / "work"), str(vault)],
        capture_output=True, text=True, check=True, cwd=tmp_path,
        env={"PYTHONPATH": ":".join(path for path in sys.path if path), "EXOMEM_STATE_ROOT": str(tmp_path / "state")})

    export.unlink()
    items, peak, faulted = run.stdout.split()
    assert items == "1" and faulted == "True"  # the note before the comment is read; the export ends as partial
    assert int(peak) < 256 * 1024 * 1024, f"peak {int(peak) >> 20} MiB"


# ----- Gmail mail


def test_a_gmail_mailbox_files_one_correspondence_source_per_message_and_leaves_out_spam_and_trash(vault: Path) -> None:
    """Catches a mailbox that files as one blob, a Spam or Trash message that gets filed, labels that leak into a transcript."""
    messages = [
        mail(0),
        mail(1, html="<h2>Tides</h2><p>The <b>gearbox</b> ratio:</p><ul><li>one</li><li>two</li></ul><script>steal()</script>", text=""),
        mail(2, html=f"<p>{marker_of(2)}</p>"),
        mail(3, inline=(("image001.png", "image/png", JPEG),)),
        mail(4, attachments=(("notes.txt", "text/plain", b"the attached tide notes"), ("Счёт.jpg", "image/jpeg", JPEG),
                             ("tool.exe", "application/x-msdownload", b"MZ" * 40))),
        mail(5, labels="Inbox,Spam"), mail(6, labels="Spam"), mail(7, labels="Trash,Unread"),
    ]
    data = mbox(messages)
    import_id = stage_export(vault, data, name="takeout.mbox")

    view = file_to_done(vault, import_id)

    found = correspondence(vault)
    assert len(found) == 5  # the messages that carry neither Spam nor Trash
    for number in range(5):
        (frontmatter, text), = (page for page in found.values() if page[0]["title"] == title_of(number))
        assert frontmatter["item_created"] == created_at(number).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert "alice@example.invalid" in text and "bob@example.invalid" in text and "Subject:" in text
        assert "Inbox" not in text and "Unread" not in text and "X-Gmail-Labels" not in text  # labels are not in a transcript
    (html_only,) = (page for page in found.values() if page[0]["title"] == title_of(1))
    assert "Tides" in html_only[1] and "- one\n- two" in html_only[1] and "steal()" not in html_only[1] and "<" not in html_only[1].split("---", 2)[2]
    (with_files,) = (page for page in found.values() if page[0]["title"] == title_of(4))
    assert "notes.txt (filed as its own Source)" in with_files[1] and "Счёт.jpg (filed as its own Source)" in with_files[1]
    assert "tool.exe (kept in the export)" in with_files[1]
    (inline,) = (page for page in found.values() if page[0]["title"] == title_of(3))
    assert "image001.png (kept in the export)" in inline[1]
    attached = attachments(vault)
    assert sorted(attached) == ["notes.txt", "Счёт.jpg"]  # not the inline image, not the executable
    assert all(front["item_created"] == created_at(4).strftime("%Y-%m-%dT%H:%M:%SZ") for front in attached.values())
    assert len(source_pages(vault)) == 5 + 2 + 1  # the messages, two attachments and the mailbox
    assert view["state"] == "done" and view["left_out"]["by_reason"] == {"IMPORT_ITEM_SKIPPED_LABEL": 3}
    assert view["progress"] == {"total": 11, "filed": 8, "skipped_duplicates": 0, "failed": 0, "left_out": 3, "remaining": 0}
    assert [item["code"] for item in view["left_out"]["items"]] == ["IMPORT_ITEM_SKIPPED_LABEL"] * 3
    assert export_expansion.read(vault, import_id).failed == 0


def test_a_mailbox_with_crlf_line_ends_splits_and_reads_the_same_messages(vault: Path) -> None:
    """Catches a splitter that sees no blank line in a CRLF mailbox and reads the whole file as one message."""
    messages = [mail(number) for number in range(6)] + [mail(6, labels="Spam")]

    view = file_to_done(vault, stage_export(vault, mbox(messages, crlf=True), name="takeout.mbox"))

    found = correspondence(vault)
    assert len(found) == 6
    (frontmatter, text), = (page for page in found.values() if page[0]["title"] == title_of(2))
    assert marker_of(2) in text and "\r" not in text
    assert view["left_out"]["by_reason"] == {"IMPORT_ITEM_SKIPPED_LABEL": 1}


def test_a_mailbox_that_outlasts_one_call_resumes_at_the_next_message_and_files_each_message_once(vault: Path, monkeypatch) -> None:
    """Catches a resume that starts the mailbox again or skips a message: every message must file exactly once."""
    expand = export_expansion.expand

    def one_message_a_call(root, import_id, *, keep_going, rewalk=False):
        expand(root, import_id, keep_going=lambda: False, rewalk=rewalk)

    monkeypatch.setattr(export_expansion, "expand", one_message_a_call)
    messages = [mail(number, attachments=(("notes.txt", "text/plain", b"notes %d" % number),)) for number in range(5)]
    data = mbox(messages)
    import_id = stage_export(vault, data, name="takeout.mbox")

    view = file_to_done(vault, import_id, calls=600)

    assert len(correspondence(vault)) == 5
    assert len([name for name in (Path(page[0].get("collection_path", "")).name for page in source_pages(vault).values()) if name == "notes.txt"]) == 5
    rows = expansion_rows(vault, import_id)
    ends = list(itertools.accumulate(len(SEPARATOR) + len(message) + 1 for message in messages))
    assert [row["next"] for row in rows] == ends  # a message's position is the byte offset at which the next one starts
    assert ends[-1] == len(data)
    assert view["progress"] == {"total": 11, "filed": 11, "skipped_duplicates": 0, "failed": 0, "left_out": 0, "remaining": 0}


def test_a_mailbox_read_that_only_sets_messages_aside_does_not_look_stalled_to_the_filer(vault: Path, monkeypatch) -> None:
    """Catches a long read of Spam and Trash that derives no file and moves no count, so the filer parks the import as stalled."""
    from exomem import import_filing, write_intake
    from exomem.governance import principal as principal_module

    expand = export_expansion.expand
    # One message a call, so the read takes more calls in a row than the filer allows without movement.
    monkeypatch.setattr(
        export_expansion, "expand",
        lambda root, import_id, *, keep_going, rewalk=False: expand(root, import_id, keep_going=lambda: False, rewalk=rewalk),
    )
    messages = [mail(number, labels="Spam") for number in range(adoption_run.FILING_STALL_CALLS + 4)] + [mail(40)]
    import_id = stage_export(vault, mbox(messages), name="takeout.mbox")
    filer = import_filing.Filer(vault)
    owner = write_intake.principal_record(principal_module.owner_principal(surface="library"))
    assert filer.register(import_id, principal=owner, surface=None)

    for _ in range(60):
        if filer.run_once(import_id) is None:
            break
        assert import_filing.read_view(vault, import_id)["state"] == "working"

    assert import_filing.read_view(vault, import_id)["state"] == "done"
    assert len(correspondence(vault)) == 1


def test_a_later_takeout_whose_labels_changed_files_every_message_as_already_imported(vault: Path) -> None:
    """Catches a label in the transcript or the name: a re-export with other read and category labels would double the mailbox."""
    first = [mail(number, labels="Inbox,Unread") for number in range(6)]
    again = [mail(number, labels="Inbox,Category Updates,Starred") for number in range(6)]
    file_to_done(vault, stage_export(vault, mbox(first), name="takeout.mbox"))
    before = correspondence(vault)

    view = file_to_done(vault, stage_export(vault, mbox(again), name="takeout.mbox"))

    assert correspondence(vault) == before and len(before) == 6
    assert view["state"] == "done" and view["progress"]["skipped_duplicates"] >= 6 and view["progress"]["failed"] == 0


def test_a_message_with_no_message_id_keeps_the_same_name_wherever_it_sits_in_the_mailbox() -> None:
    """Catches a name that depends on the position or the run: the same message must be the same file on a later read."""
    plain = mail(3, message_id=None)
    fmt = {entry.key: entry for entry in source_formats.formats(Path("/nonexistent-vault"))}["gmail-mbox"]

    def stems(data: bytes) -> list[str]:
        return [item.item_id for _after, item in export_mail.read(fmt, io.BytesIO(data))]

    alone = stems(mbox([plain]))
    among = stems(mbox([mail(1), plain, mail(2, message_id=None)]))

    assert len(alone) == 1 and alone[0] == among[1]
    assert len({*among}) == 3 and all(len(stem) == 32 for stem in among)


def test_an_html_body_over_four_mib_is_converted_from_its_start_and_marked_cut_short(vault: Path) -> None:
    """Catches a huge HTML body that hangs the converter, or one that is cut without saying so."""
    row = "<p>tide table row with <b>some</b> text</p>\n"
    body = row * (4 * 1024 * 1024 // len(row) + 100)
    messages = [mail(0, html=body, text=""), mail(1, html=f"<p>{marker_of(1)}</p>", text="")]

    file_to_done(vault, stage_export(vault, mbox(messages), name="takeout.mbox"))

    found = correspondence(vault)
    (_frontmatter, cut), = (page for page in found.values() if page[0]["title"] == title_of(0))
    (_frontmatter, whole), = (page for page in found.values() if page[0]["title"] == title_of(1))
    assert "tide table row with some text" in cut and "[text cut short]" in cut
    assert "cut short" not in whole


def test_an_attachment_that_is_itself_a_mailbox_files_as_a_file_and_is_never_expanded(vault: Path) -> None:
    """Catches an export inside an export: a derived file must never be recognised, or a mailbox could multiply itself."""
    inner = mbox([mail(8), mail(9)])
    outer = mbox([mail(0, attachments=(("archive.mbox", "text/plain", inner),))])
    import_id = stage_export(vault, outer, name="takeout.mbox")

    view = file_to_done(vault, import_id)

    assert export_expansion.read(vault, import_id).exports == ("takeout.mbox",)
    assert len(correspondence(vault)) == 1  # the two messages inside the attachment were not read
    assert list(attachments(vault)) == ["archive.mbox"]
    assert attachments(vault)["archive.mbox"]["item_created"] == created_at(0).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert view["state"] == "done" and view["progress"]["failed"] == 0 and view["progress"]["remaining"] == 0
    assert view["progress"]["filed"] == 3  # the message, its attachment and the mailbox


# ----- The record: crashes, torn rows, caps and a full disk


class Died(Exception):
    """The process that was expanding died here."""


def dies_at(monkeypatch, owner, name: str, nth: int) -> None:
    """Make the `nth` call of `owner.name` raise `Died` instead of running."""
    real, seen = getattr(owner, name), []

    def call(*args, **kwargs):
        seen.append(1)
        if len(seen) == nth:
            raise Died(f"{name} call {nth}")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, call)


def two_files_each(count: int) -> list[bytes]:
    return [
        mail(number, attachments=(("tide-a.txt", "text/plain", b"tide a %d" % number), ("tide-b.txt", "text/plain", b"tide b %d" % number)))
        for number in range(count)]


@pytest.mark.parametrize(("owner", "name", "nth"), [
    pytest.param(export_expansion, "_publish", 2, id="between-the-first-and-second-attachment"),
    pytest.param(export_expansion, "_publish", 3, id="after-the-attachments-before-the-transcript"),
    pytest.param(export_expansion, "_publish", 5, id="between-the-attachments-of-the-second-message"),
    pytest.param(import_stage, "append_expansion_rows", 1, id="after-the-files-before-the-row"),
    pytest.param(import_stage, "append_expansion_rows", 3, id="after-the-last-files-before-the-last-row"),
    pytest.param(import_stage, "write_expansion_state", 2, id="after-the-last-row-before-the-state"),
])
def test_a_crash_at_any_write_step_files_each_message_once_with_both_attachments_and_counts_it_once(
    vault: Path, monkeypatch, owner, name: str, nth: int,
) -> None:
    """Catches a resume that doubles a message, loses an attachment, or counts a rewritten item twice, whichever step the process died at."""
    import_id = stage_export(vault, mbox(two_files_each(3)), name="takeout.mbox")
    stage = vault / "_Imports" / import_id
    dies_at(monkeypatch, owner, name, nth)
    with pytest.raises(Died):
        export_expansion.expand(vault, import_id, keep_going=lambda: True)
    monkeypatch.undo()

    export_expansion.expand(vault, import_id, keep_going=lambda: True)

    assert len(list((stage / "takeout items").iterdir())) == 3
    assert sorted(len(list(folder.iterdir())) for folder in (stage / "takeout files").iterdir()) == [2, 2, 2]
    view = file_to_done(vault, import_id)

    assert len(correspondence(vault)) == 3
    assert sum(1 for page in source_pages(vault).values() if " files/" in page[0].get("collection_path", "")) == 6
    assert len(source_pages(vault)) == 3 + 6 + 1
    reading = export_expansion.read(vault, import_id)
    assert reading.files == 9 and reading.failed == 0 and reading.held == ()
    assert [row["status"] for row in expansion_rows(vault, import_id)] == ["derived"] * 3
    assert view["progress"] == {"total": 10, "filed": 10, "skipped_duplicates": 0, "failed": 0, "left_out": 0, "remaining": 0}


def test_a_row_that_a_crash_cut_in_two_is_discarded_and_its_message_is_read_again(vault: Path, monkeypatch) -> None:
    """Catches a torn last line that fails the record, or one that a later row is glued onto."""
    import_id = stage_export(vault, mbox([mail(number) for number in range(3)]), name="takeout.mbox")
    real = import_stage.append_expansion_rows
    seen: list[int] = []

    def writes_half_of_the_third_row(root, stage_id, rows):
        seen.append(1)
        if len(seen) == 3:
            torn = json.dumps(rows[0]).encode()
            with open(import_stage.expansion_dir(root, stage_id) / import_stage.EXPANSION_ROWS, "ab") as sink:
                sink.write(torn[: len(torn) // 2])
            raise Died("the third row")
        return real(root, stage_id, rows)

    monkeypatch.setattr(import_stage, "append_expansion_rows", writes_half_of_the_third_row)
    with pytest.raises(Died):
        export_expansion.expand(vault, import_id, keep_going=lambda: True)
    monkeypatch.undo()
    rows_file = import_stage.expansion_dir(vault, import_id) / import_stage.EXPANSION_ROWS
    assert not rows_file.read_bytes().endswith(b"\n")

    file_to_done(vault, import_id)

    assert len(correspondence(vault)) == 3
    lines = rows_file.read_bytes().splitlines()
    assert len(lines) == 3 and all(json.loads(line)["status"] == "derived" for line in lines)


def test_a_row_that_appears_twice_counts_once(vault: Path) -> None:
    """Catches a record that counts a repeated row twice, so that a crash would inflate the counts."""
    import_id = stage_export(vault, mbox([mail(number) for number in range(3)]), name="takeout.mbox")
    export_expansion.expand(vault, import_id, keep_going=lambda: True)
    rows_file = import_stage.expansion_dir(vault, import_id) / import_stage.EXPANSION_ROWS
    last = rows_file.read_bytes().splitlines()[-1]
    with open(rows_file, "ab") as sink:
        sink.write(last + b"\n")

    assert export_expansion.read(vault, import_id).files == 3
    file_to_done(vault, import_id)

    assert len(correspondence(vault)) == 3 and len(source_pages(vault)) == 4


def test_skipped_messages_do_not_count_toward_the_entry_limit(vault: Path, monkeypatch) -> None:
    """Catches a mailbox that is full of Spam and Trash reaching the cap, so that its real mail is left out."""
    monkeypatch.setenv(cloud_import.MAX_MEMBERS_ENV, "4")
    messages = [mail(number, labels="Spam") for number in range(6)] + [mail(number) for number in range(6, 9)]

    view = file_to_done(vault, stage_export(vault, mbox(messages), name="takeout.mbox"))

    assert len(correspondence(vault)) == 3
    assert view["left_out"]["by_reason"] == {"IMPORT_ITEM_SKIPPED_LABEL": 6}


def test_an_attachment_counts_toward_the_entry_limit_like_a_transcript(vault: Path, monkeypatch) -> None:
    """Catches a limit that counts messages only, so that attachments could fill the stage without bound."""
    monkeypatch.setenv(cloud_import.MAX_MEMBERS_ENV, "4")

    view = file_to_done(vault, stage_export(vault, mbox(two_files_each(3)), name="takeout.mbox"))

    assert len(correspondence(vault)) == 1  # one message and its two attachments are three entries; a second message would make six
    assert view["left_out"]["by_reason"] == {"IMPORT_EXPANSION_CAP": 1}
    assert view["progress"]["remaining"] == 0


def test_a_derived_bytes_bound_below_the_export_stops_with_the_cap_code(vault: Path, monkeypatch) -> None:
    """Catches a byte bound that is never applied: the derived files of an export must stay within its size plus the slack."""
    monkeypatch.setattr(export_expansion, "_SLACK_BYTES", -10_000_000)

    view = file_to_done(vault, stage_export(vault, mbox([mail(number) for number in range(3)]), name="takeout.mbox"))

    assert not correspondence(vault)
    assert view["left_out"]["by_reason"] == {"IMPORT_EXPANSION_CAP": 1}


def test_a_full_disk_while_writing_parks_the_import_with_a_free_space_need_and_a_later_call_resumes(vault: Path, monkeypatch) -> None:
    """Catches a full disk that fails the import as an internal error, or one that loses the item that met it."""
    from exomem import import_filing, write_intake
    from exomem.governance import principal as principal_module

    import_id = stage_export(vault, mbox([mail(number) for number in range(4)]), name="takeout.mbox")
    real = export_expansion._publish
    seen: list[int] = []

    def full_at_the_third_file(*args, **kwargs):
        seen.append(1)
        if len(seen) == 3:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(*args, **kwargs)

    monkeypatch.setattr(export_expansion, "_publish", full_at_the_third_file)
    filer = import_filing.Filer(vault)
    owner = write_intake.principal_record(principal_module.owner_principal(surface="library"))
    assert filer.register(import_id, principal=owner, surface=None)

    view = import_filing.read_view(vault, import_id)
    for _ in range(60):
        filer.run_once(import_id)
        view = import_filing.read_view(vault, import_id)
        if view["needs"]:
            break

    assert [need["need"] for need in view["needs"]] == ["free_space"]
    assert view["state"] == "parked" and len(expansion_rows(vault, import_id)) == 2
    # The call that read the export ends at the stop, so the scan that files the two messages written before the full disk is the first step of the call after it.
    assert not correspondence(vault)
    monkeypatch.undo()
    for _ in range(60):
        if filer.run_once(import_id) is None:
            break

    view = import_filing.read_view(vault, import_id)
    assert view["state"] == "done" and view["needs"] == []
    assert len(correspondence(vault)) == 4 and view["progress"]["failed"] == 0
