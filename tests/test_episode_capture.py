"""The pure recap builder: bounds, refusals, and the deterministic shape.

A recap is what an agent says a conversation did, never the conversation.
Everything here runs before any write, and every refusal writes nothing.
"""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from exomem import episode_capture as capture
from exomem.episode_model import EpisodeError

WHEN = dt.datetime(2026, 5, 18, 9, 12, 33, tzinfo=dt.UTC)
KEY = "ep-" + "a1" * 16


def _prepare(**overrides: object) -> capture.Recap:
    args: dict[str, object] = {
        "episode": KEY,
        "subject": "Harbor Lamp purchase",
        "summary": "Chose the brass lamp; delivery date still open.",
        "worked_on": ["Compared two lamps for Project Alpha"],
        "decided": ["Buy the brass Harbor Lamp"],
        "open": ["Confirm the delivery date"],
        "said": ["I want the brass one, not the chrome."],
        "about": [],
        "client": None,
        "when": WHEN,
        "audience": "owner",
    }
    args.update(overrides)
    return capture.prepare(**args)


def _code(**overrides: object) -> str:
    with pytest.raises(EpisodeError) as error:
        _prepare(**overrides)
    return error.value.code


def test_a_recap_renders_its_sections_and_frontmatter() -> None:
    recap = _prepare(client="claude-code")

    assert recap.key == KEY
    assert recap.subject == "Harbor Lamp purchase"
    for heading in ("### Worked on", "### Decided", "### Open", "### Said"):
        assert heading in recap.body
    assert "- Buy the brass Harbor Lamp" in recap.body
    assert "> I want the brass one, not the chrome." in recap.body
    assert recap.frontmatter == {
        "summary": "Chose the brass lamp; delivery date still open.",
        "episode": KEY,
        "episode_digest": recap.digest,
        "client": "claude-code",
    }


def test_the_digest_and_slug_are_deterministic() -> None:
    first, second = _prepare(), _prepare()

    group = hashlib.sha256(f"owner\0{KEY}".encode()).hexdigest()[:12]
    assert first.digest == second.digest
    assert first.slug == second.slug
    assert first.slug == f"harbor-lamp-purchase-ep{group}-20260518t091233000000-{first.digest[:8]}"
    assert capture.filename_parts(f"2026-05-18-{first.slug}.md") == (
        group,
        "20260518t091233000000",
        first.digest[:8],
    )


def test_the_filename_group_is_scoped_by_audience() -> None:
    """One key, two audiences: two groups, so neither ever lists, supersedes or
    replays the other's revisions. The digest is the content's alone."""
    owner, other = _prepare(), _prepare(audience="client-b")

    assert owner.group == capture.key_group(KEY, "owner")
    assert other.group == capture.key_group(KEY, "client-b")
    assert owner.group != other.group
    assert f"-ep{owner.group}-" in owner.slug and f"-ep{other.group}-" in other.slug
    assert owner.digest == other.digest


def test_any_authored_change_moves_the_digest() -> None:
    base = _prepare().digest
    assert _prepare(summary="Chose the chrome lamp instead.").digest != base
    assert _prepare(open=[]).digest != base
    assert _prepare(subject="Harbor Lamp order").digest != base


def test_the_digest_ignores_when_it_was_recorded() -> None:
    later = WHEN + dt.timedelta(hours=3)
    assert _prepare(when=later).digest == _prepare().digest
    assert _prepare(when=later).slug != _prepare().slug


def test_an_absent_key_is_minted_and_a_supplied_one_must_be_canonical() -> None:
    minted = _prepare(episode=None)
    assert capture.EPISODE_KEY_RE.fullmatch(minted.key)
    assert minted.key != _prepare(episode=None).key
    for bad in ("ep-XYZ", "ep-" + "a" * 31, "episode-1", " " + KEY, KEY.upper()):
        assert _code(episode=bad) == "EPISODE_KEY_INVALID"


def test_the_hook_key_is_a_pure_function_of_client_and_session() -> None:
    key = capture.hook_key("claude-code", "session-1234")
    assert key == capture.hook_key("claude-code", "session-1234")
    assert key != capture.hook_key("codex", "session-1234")
    assert key == "ep-" + hashlib.sha256(
        b"exomem-episode-key-v1\x00claude-code\x00session-1234"
    ).hexdigest()[:32]
    assert capture.EPISODE_KEY_RE.fullmatch(key)


def test_an_empty_record_is_refused() -> None:
    assert _code(worked_on=[], decided=[], open=[]) == "EPISODE_EMPTY"


@pytest.mark.parametrize(
    "field, value",
    [
        ("subject", "two\nlines"),
        ("summary", "tab\there"),
        ("worked_on", ["carriage\rreturn"]),
        ("decided", ["line" + chr(0x2028) + "separator"]),
        ("said", ["bidi " + chr(0x202E) + " override"]),
        ("open", [""]),
        ("subject", "   "),
        ("summary", ""),
        ("worked_on", "not a list"),
        ("decided", [7]),
    ],
)
def test_newlines_controls_and_shapeless_values_are_refused(field: str, value: object) -> None:
    assert _code(**{field: value}) == "EPISODE_INVALID"


@pytest.mark.parametrize(
    "field, value",
    [
        ("subject", "s" * 121),
        ("summary", "s" * 181),
        ("worked_on", ["x" * 201]),
        ("decided", ["one", "two", "three", "four", "five", "six"]),
        ("said", ["x" * 301]),
        ("said", ["one", "two", "three", "four"]),
        ("about", [f"exomem://memory/00000000-0000-4000-8000-{i:012x}" for i in range(4)]),
    ],
)
def test_every_field_is_capped(field: str, value: object) -> None:
    assert _code(**{field: value}) == "EPISODE_TOO_LARGE"


def test_a_five_kilobyte_smuggling_attempt_is_refused() -> None:
    """Within every per-item cap, the rendered body still may not pass 4 KiB."""
    wide = chr(0x6F22) * 199  # three UTF-8 bytes each
    code = _code(
        worked_on=[wide] * 5,
        decided=[wide] * 5,
        open=[wide] * 5,
        said=[chr(0x6F22) * 299] * 3,
    )
    assert code == "EPISODE_TOO_LARGE"
    assert _code(worked_on=["x" * 5120]) == "EPISODE_TOO_LARGE"


def test_caps_hold_after_normalisation() -> None:
    """Surrounding whitespace is not a way to carry more than the cap."""
    assert _prepare(subject="  " + "s" * 120 + "  ").subject == "s" * 120
    assert _code(subject="s" * 121 + " ") == "EPISODE_TOO_LARGE"


@pytest.mark.parametrize(
    "field",
    ["said", "worked_on", "decided", "open"],
)
def test_a_credential_shaped_value_is_refused(field: str) -> None:
    secret = "sk-proj-" + "Ab3dE" * 10
    assert _code(**{field: [f"the key is {secret}"]}) == "EPISODE_CREDENTIAL"


def test_a_credential_shaped_subject_or_summary_is_refused() -> None:
    # Spelled in pieces so no scanner mistakes the fixture for a live token.
    token = "gh" + "p_" + "aB1cD2eF3gH4iJ5kL6mN7oP8qR9sT0uV1wX2"
    assert _code(subject=f"token {token}") == "EPISODE_CREDENTIAL"
    assert _code(summary=f"token {token}") == "EPISODE_CREDENTIAL"


def test_about_refs_must_be_canonical_memory_refs_and_are_deduplicated() -> None:
    ref = "exomem://memory/69b2b4d3-d4c3-4361-8714-91b8f1b1c0b1"
    recap = _prepare(about=[ref, ref])
    assert recap.about == (ref,)
    # Never on the shared page: the recorder's ledger holds them.
    assert "about" not in recap.frontmatter
    assert ref not in recap.body
    assert recap.digest != _prepare().digest
    assert _code(about=["Knowledge Base/Notes/x.md"]) == "EPISODE_INVALID"


def test_an_invalid_client_label_is_dropped_not_refused() -> None:
    """Attribution is optional; a malformed label never costs the record."""
    recap = _prepare(client="Claude Code!")
    assert recap.client is None
    assert "client" not in recap.frontmatter
    assert _prepare(client="codex").client == "codex"


def test_the_subject_slug_falls_back_when_nothing_ascii_survives() -> None:
    recap = _prepare(subject="!!! ...")
    assert recap.slug.startswith("episode-ep")


@pytest.mark.parametrize(
    "label",
    [
        # Spelled in pieces so no scanner mistakes the fixture for a live token.
        "sk-" + "abcdefghijklmnopqrstuvw",
        "sk-proj-" + "Ab3dE" * 10,
    ],
)
def test_a_credential_shaped_client_label_is_refused(label: str) -> None:
    """The label is written to the page too, so it is scrubbed like prose;
    one that fits the label pattern is otherwise a valid-looking label."""
    assert _code(client=label) == "EPISODE_CREDENTIAL"


def test_unicode_spaces_collapse_so_every_accepted_line_is_one_the_writer_takes() -> None:
    nbsp, narrow = chr(0xA0), chr(0x202F)
    recap = _prepare(
        subject=f"Harbor{narrow}Lamp  purchase",
        summary=f"Chose{nbsp}the brass  lamp;{narrow}delivery still open.",
        worked_on=[f"Compared{nbsp}two   lamps"],
        said=[f"I want{nbsp}{nbsp}the brass one."],
    )

    assert recap.subject == "Harbor Lamp purchase"
    assert recap.summary == "Chose the brass lamp; delivery still open."
    assert recap.frontmatter["summary"] == recap.summary
    assert "- Compared two lamps" in recap.body
    assert "> I want the brass one." in recap.body


def test_the_order_token_never_goes_back_past_the_newest_revision() -> None:
    """Revisions are ordered by this token alone, so a clock that steps back
    must still yield a token after the newest revision already on disk."""
    assert capture.order_token(WHEN) == "20260518t091233000000"
    assert capture.order_token(WHEN, after="20260517t000000000000") == "20260518t091233000000"
    assert capture.order_token(WHEN, after="20260518t120000000000") == "20260518t120000000001"
    assert capture.order_token(WHEN, after="20260518t091233000000") == "20260518t091233000001"
    assert capture.order_token(WHEN, after="20261231t235959999999") == "20270101t000000000000"
    recap = _prepare()
    later = recap.slug_at("20260518t120000000001")
    assert capture.filename_parts(f"2026-05-18-{later}.md") == (
        recap.group,
        "20260518t120000000001",
        recap.digest[:8],
    )
    assert recap.slug_at("20260518t091233000000") == recap.slug
