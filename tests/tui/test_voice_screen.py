"""Keeping a voice from the terminal Home: the sample is shown first, and nothing is kept until the owner says so."""

from __future__ import annotations

import pytest

pytest.importorskip("textual")

pytestmark = pytest.mark.anyio


async def _settle(app, pilot):
    await pilot.pause()
    await app.workers.wait_for_complete()
    await pilot.pause()


async def _fill(app, pilot, recording="Knowledge Base/Evidence/Audio/talk.m4a", label="Speaker A", name="Alex"):
    await _settle(app, pilot)
    await pilot.press("v")
    await _settle(app, pilot)
    assert app.screen.SCREEN_TITLE == "Keep a voice"
    for text in (recording, label, name):
        await pilot.press(*text)
        await pilot.press("enter")
        await pilot.pause()
    await _settle(app, pilot)


def _calls(fake_backend, method):
    return [kwargs for called, kwargs in fake_backend.calls if called == method]


async def test_home_opens_keep_a_voice_and_keeps_it_only_after_the_owner_confirms(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _fill(app, pilot)
        shown = str(app.screen.query_one("#recovery-fact").render())
        assert "Welcome back to the show." in shown
        assert _calls(fake_backend, "voice_sample") and _calls(fake_backend, "voice_enroll") == []

        await pilot.press("enter")  # the first option is "Keep this voice"
        await _settle(app, pilot)

        assert _calls(fake_backend, "voice_enroll") == [
            {"recording": "Knowledge Base/Evidence/Audio/talk.m4a", "label": "Speaker A", "name": "Alex"}
        ]
        assert "voice kept" in str(app.screen.query_one("#voice-status").render())


async def test_declining_keeps_no_voice_and_a_failure_names_its_code(make_app, fake_backend):
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _fill(app, pilot)
        await pilot.press("down", "enter")  # "Do not keep it"
        await _settle(app, pilot)
        assert _calls(fake_backend, "voice_enroll") == []

    fake_backend.fail_next("voice_sample", "ENROLL_NEEDS_TIMED_LINES", "merged turns have no times")
    app = make_app()
    async with app.run_test(size=(100, 30)) as pilot:
        await _fill(app, pilot)
        failure = str(app.screen.query_one("#recovery-fact").render())
        assert "ENROLL_NEEDS_TIMED_LINES" in failure and "No voice was stored." in failure
        assert _calls(fake_backend, "voice_enroll") == []
