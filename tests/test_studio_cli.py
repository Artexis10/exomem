from __future__ import annotations

import pytest

from exomem import __main__ as cli


def test_studio_prints_url_without_opening_browser(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    assert cli.main(["studio"]) == 0
    assert capsys.readouterr().out == "http://127.0.0.1:8765/studio/\n"
    assert opened == []


def test_studio_open_is_explicit_and_uses_configured_origin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)

    assert cli.main(["studio", "--url", "https://kb.example.test", "--open"]) == 0
    assert capsys.readouterr().out == "https://kb.example.test/studio/\n"
    assert opened == ["https://kb.example.test/studio/"]


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@example.test",
        "https://example.test/?key=secret",
        "javascript:alert(1)",
        "https://example.test/not-studio",
    ],
)
def test_studio_rejects_credentialed_or_non_origin_urls(url: str) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["studio", "--url", url])
    assert exc.value.code == 2
