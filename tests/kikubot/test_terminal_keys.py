"""Tests for the inline `keys` guards: the token allow-list, the terminator,
the last-key rule for keys that can end the CLI, and the exit-hint check."""

from __future__ import annotations

import pytest

from kikubot.runtimes import get_runtime
from kikubot.terminal_keys import KeysError, KeyToken, exit_armed, parse_tokens


def test_parses_named_keys_and_literal_text():
    assert parse_tokens(["Up", "=yes%2C%20go", "BTab", "C-a", "Enter", "."]) == [
        KeyToken("Up", False),
        KeyToken("yes, go", True),
        KeyToken("BTab", False),
        KeyToken("C-a", False),
        KeyToken("Enter", False),
    ]
    assert parse_tokens(["=%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%82", "."]) == [
        KeyToken("привет", True)
    ]


@pytest.mark.parametrize(
    "raw",
    [
        ["Up"],  # no terminator: a half-typed or cut query
        ["."],  # nothing to press
        ["Up", "Down"],
        ["rm", "."],  # not a key name
        ["C-z", "."],  # suspends the CLI back to the shell
        ["up", "."],  # names are exact
        ["=", "."],  # empty text
        ["=a%0Ab", "."],  # control characters never travel as text
        ["=%1b[2J", "."],
        ["=%0d", "."],
        ["=%ff", "."],  # not UTF-8
        ["Enter", "=ls", "."],  # nothing may follow a key that can end the CLI
        ["C-c", "C-c", "."],
        ["C-d", "Up", "."],
        ["C-m", "=x", "."],
        ["Clear", "Up", "."],  # Clear is last only, like Enter
    ],
)
def test_refuses_bad_queries(raw):
    with pytest.raises(KeysError) as e:
        parse_tokens(raw)
    assert e.value.code == "bad-key"


def test_exit_armed_spots_the_second_press_prompt():
    assert exit_armed("…\n  Press Ctrl-C again to exit\n")
    assert exit_armed("x\n ctrl + c again to quit \n")
    assert exit_armed("Press Ctrl-D again to exit")
    assert not exit_armed("ctrl+c to interrupt\nesc to cancel")
    # Only the bottom of the pane counts: the hint quoted in the transcript
    # far above isn't the CLI asking.
    assert not exit_armed("Press Ctrl-C again to exit\n" + "line\n" * 20)


def _claude_pane(box: str) -> str:
    rule = "─" * 60
    return f"● earlier reply\n\n{rule}\n❯ {box}\n{rule}\n  ⏵⏵ auto mode on\n"


@pytest.mark.parametrize(
    ("box", "exits"),
    [
        ("/exit", True),
        ("/exit now", True),
        ("/quit", True),
        ("/ex", True),  # Enter completes a partial slash command
        ("/q", True),
        ("/effort", False),
        ("/e x", False),
        ("hello /exit", False),
        ("", False),
        ("/", False),
    ],
)
def test_claude_enter_that_would_quit(box, exits):
    assert get_runtime("claude").would_exit_on_enter(_claude_pane(box)) is exits


def test_fresh_session_box_near_the_top_is_found():
    """A fresh session draws the box under the logo with ~40 blank rows below
    it; those rows must not push it out of the search window."""
    rule = "─" * 100
    pane = (
        " ▐▛███▛█   Claude Code\n ▝▝   ▝▝   ~/dev/x\n"
        f"{rule}\n❯\xa0/exit\n{rule}\n  ❯ /exit    Exit the CLI\n" + "\n" * 44
    )
    assert get_runtime("claude").would_exit_on_enter(pane) is True


def test_clear_is_a_token():
    assert parse_tokens(["=x", "Clear", "."]) == [
        KeyToken("x", True),
        KeyToken("Clear", False),
    ]
