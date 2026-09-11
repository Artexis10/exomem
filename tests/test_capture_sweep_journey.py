"""One turn-by-turn episode over the whole carrier: recall, capture, sweep, replay.

Every step is a real product call on a real vault. Nothing here stubs the ledger,
the gate, the seam or the terminal — the claim under test is that the pieces
compose, and each of them already passes on its own.

The vocabulary is deliberately generic. This journey is the shape of the dogfood
episode that motivated the carrier (an object captured, an adjacent operational
quirk missed), stated without any real person, client, supplier or product.

This is NOT the acceptance measurement. That is a real-agent replay family in the
f27 shape, registered as f28 through a dated sequence-4 amendment, and named in
the change's proposal as the follow-up `amend-no-nudge-bench-families-seq4`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from exomem import capture_sweep, commands, writer_lease

ROOT = Path(__file__).resolve().parents[1]


def _command(name: str):
    """The registered product command, invoked exactly as a client reaches it.

    `writer_lease.invoke_command` is where the mutation terminal is projected,
    so calling the `op_*` leaf directly would test a shape no client receives.
    """
    return next(command for command in commands.PRODUCT_COMMANDS if command.name == name)


def _remember(
    vault: Path, *, content: str, title: str, slug: str, key: str | None = None
) -> dict:
    return writer_lease.invoke_command(
        _command("remember"),
        vault,
        idempotency_key=key,
        content=content,
        title=title,
        slug=slug,
    )


CLIENT_ENTITY = "Client Organisation"
BOOKING_SYSTEM = "Venue Booking System"
MEETING_TITLE = "Client meeting scheduling decision"
QUIRK_TITLE = "Venue booking lead-time quirk"


class _Clock:
    def __init__(self, start: float = 5_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _seed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The sample vault, plus one client entity page and one prior dated note."""
    from epistemic.journeys import f26_carrier

    monkeypatch.setenv("EXOMEM_STATE_ROOT", str(tmp_path / "journey-state"))
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "no-such-exomem-config.json"))
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    vault = f26_carrier.seed_journey_vault(tmp_path / "vault", repo_root=ROOT)

    entity = vault / "Knowledge Base" / "Entities" / "Organizations" / f"{CLIENT_ENTITY}.md"
    entity.parent.mkdir(parents=True, exist_ok=True)
    entity.write_text(
        "---\n"
        "type: entity\n"
        "entity_type: organization\n"
        f"title: {CLIENT_ENTITY}\n"
        "status: active\n"
        "created: 2026-09-01\n"
        "updated: 2026-09-01\n"
        "---\n"
        f"\n# {CLIENT_ENTITY}\n\nA recurring counterparty.\n",
        encoding="utf-8",
    )

    prior = vault / "Knowledge Base" / "Notes" / "Insights" / "2026-09-01-prior-round.md"
    prior.parent.mkdir(parents=True, exist_ok=True)
    prior.write_text(
        "---\n"
        "type: insight\n"
        "title: Prior scheduling round\n"
        "status: active\n"
        "created: 2026-09-01\n"
        "updated: 2026-09-01\n"
        "---\n"
        "\n# Prior scheduling round\n\n## Observations\n\n"
        f"- [scheduling] The previous round with [[{CLIENT_ENTITY}]] ran long.\n",
        encoding="utf-8",
    )
    return vault


def _meeting_content() -> str:
    return (
        "The meeting is scheduled for the later slot.\n"
        "\n"
        "## Observations\n"
        "\n"
        f"- [scheduling] The round with [[{CLIENT_ENTITY}]] is booked through "
        f"[[{BOOKING_SYSTEM}]] for the later slot.\n"
        "\n"
        "## Relations\n"
        "\n"
        f"- relates_to [[{CLIENT_ENTITY}]]\n"
    )


def _quirk_content() -> str:
    return (
        "The booking system needs more lead time than it advertises.\n"
        "\n"
        "## Observations\n"
        "\n"
        "- [operations] The booking system confirms a slot only after a longer "
        "lead time than the interface states, so a same-week request is not "
        "reliable.\n"
        "\n"
        "## Relations\n"
        "\n"
        f"- relates_to [[{CLIENT_ENTITY}]]\n"
    )


def test_one_episode_captures_its_object_then_is_told_to_sweep_for_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _seed(tmp_path, monkeypatch)
    clock = _Clock()
    monkeypatch.setattr(capture_sweep, "_clock", clock)
    capture_sweep.reset_state()

    # 1. Recall. A read is not a durable write: it neither emits the advisory nor
    #    consumes the quiet interval.
    recalled = commands.op_ask_memory(vault, query=CLIENT_ENTITY, mode="keyword")
    assert isinstance(recalled, list)

    # 2. The agent captures the object it was thinking about. This is the first
    #    durable write of the episode, so the response asks for one bounded pass.
    meeting = _remember(
        vault,
        content=_meeting_content(),
        title=MEETING_TITLE,
        slug="client-meeting-slot",
        key="journey-meeting",
    )
    assert meeting["status"] == "committed"
    sweep = meeting["capture_sweep"]
    assert sweep["boundary"] == "quiet-interval"
    assert sweep["rule"]
    assert sweep["consider"]

    # The booking system is named on the committed page and has no page of its
    # own; the client organisation has one, so it is not an unpaged mention.
    assert BOOKING_SYSTEM in sweep["unpaged_mentions"]
    assert CLIENT_ENTITY not in sweep["unpaged_mentions"]

    # 3. The agent acts on it and writes the adjacent operational quirk — the
    #    fact the dogfood episode lost. The second write inside the interval is
    #    silent: the boundary has already been reported once.
    clock.advance(30)
    quirk = _remember(
        vault,
        content=_quirk_content(),
        title=QUIRK_TITLE,
        slug="booking-lead-time",
        key="journey-quirk",
    )
    assert "capture_sweep" not in quirk

    # 4. Replay. An acknowledgement the client never received is retried under the
    #    same idempotency key. Each replay returns its ORIGINAL terminal byte for
    #    byte -- so no second page, no second relation fact, and no second
    #    advisory: the meeting replay carries the block it already carried, and
    #    the quirk replay carries none, exactly as each did the first time.
    clock.advance(30)
    meeting_replay = _remember(
        vault,
        content=_meeting_content(),
        title=MEETING_TITLE,
        slug="client-meeting-slot",
        key="journey-meeting",
    )
    quirk_replay = _remember(
        vault,
        content=_quirk_content(),
        title=QUIRK_TITLE,
        slug="booking-lead-time",
        key="journey-quirk",
    )
    assert meeting_replay == meeting
    assert quirk_replay == quirk
    assert "capture_sweep" not in quirk_replay

    notes = sorted(
        page.relative_to(vault).as_posix()
        for page in (vault / "Knowledge Base" / "Notes").rglob("*.md")
    )
    assert sum("client-meeting-slot" in name for name in notes) == 1, notes
    assert sum("booking-lead-time" in name for name in notes) == 1, notes

    body = (vault / meeting["path"]).read_text(encoding="utf-8")
    assert body.count("- relates_to [[") == 1
    assert CLIENT_ENTITY in body


def test_a_quiet_prominence_level_writes_the_same_pages_and_says_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    vault = _seed(tmp_path, monkeypatch)
    monkeypatch.setenv("EXOMEM_PROMINENCE", "light")
    monkeypatch.setattr(capture_sweep, "_clock", _Clock())
    capture_sweep.reset_state()

    meeting = _remember(
        vault, content=_meeting_content(), title=MEETING_TITLE, slug="client-meeting-slot"
    )

    assert "capture_sweep" not in meeting
    assert (vault / meeting["path"]).is_file()


# ------------------------------------- a broken advisory never breaks the write


def _exploding_block(*_args: object, **_kwargs: object) -> None:
    raise RuntimeError("the advisory is broken")


def test_a_broken_advisory_never_breaks_a_page_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fail-open guard, exercised against a real commit rather than asserted.

    The claim the seam makes is "a fault costs the caller an advisory, never the
    write", and the only way to test that claim is to break the advisory and
    then write. Remove the `try/except` in
    `semantic_writes._capture_sweep_block` and this test raises out of the
    commit instead of returning a committed terminal.
    """
    vault = _seed(tmp_path, monkeypatch)
    capture_sweep.reset_state()
    monkeypatch.setattr(capture_sweep, "block", _exploding_block)

    result = _remember(
        vault, content=_meeting_content(), title=MEETING_TITLE, slug="client-meeting-slot"
    )

    assert result["status"] == "committed"
    assert result["mutated"] is True
    assert "capture_sweep" not in result
    assert (vault / result["path"]).is_file()


def test_a_broken_advisory_never_breaks_a_structured_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same claim on the other seam: `records`' structured-write carrier."""
    import lifecycle_fixtures

    # `seed_vault` uses tmp_path ITSELF as the vault, and the state root must sit
    # outside it. conftest's autouse `_isolate_state_root` already injects a
    # sibling, so this test deliberately does not set one.
    monkeypatch.setenv("EXOMEM_CONFIG_PATH", str(tmp_path / "no-such-exomem-config.json"))
    monkeypatch.setenv("EXOMEM_PROMINENCE", "maximal")
    lifecycle_fixtures.seed_vault(tmp_path)
    capture_sweep.reset_state()

    healthy = lifecycle_fixtures.report_event(tmp_path, "Batch 1")
    assert healthy["outcome"] == "committed"
    assert "capture_sweep" in healthy, "the structured seam must carry it when it works"

    monkeypatch.setattr(capture_sweep, "block", _exploding_block)
    broken = lifecycle_fixtures.report_event(tmp_path, "Batch 2")

    assert broken["outcome"] == "committed"
    assert "capture_sweep" not in broken
