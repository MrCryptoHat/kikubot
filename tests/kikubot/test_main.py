"""Tests for main() CLI dispatch — unknown args must never start the bot.

Before this routing existed, `kikubot --help` on a configured machine started
a SECOND polling instance that fought the running bot over getUpdates
(Telegram 409s). Anything that is not `hook`, help or version must exit
without touching config/bot imports.
"""

import sys

import pytest

from kikubot import __version__
from kikubot.main import main


class TestCliDispatch:
    def test_help_prints_usage(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["kikubot", "--help"])
        main()
        out = capsys.readouterr().out
        assert "Usage:" in out
        assert "kikubot hook" in out

    def test_short_help(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["kikubot", "-h"])
        main()
        assert "Usage:" in capsys.readouterr().out

    def test_version(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["kikubot", "--version"])
        main()
        assert capsys.readouterr().out.strip() == f"kikubot {__version__}"

    def test_unknown_command_exits_2_without_starting_bot(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "argv", ["kikubot", "start"])
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "Unknown command: start" in err
        assert "Usage:" in err
