"""Tests for pane_screen: parsing the one-call capture and cutting the inline
`screen` answer into JSON chunks that survive Telegram intact."""

from __future__ import annotations

import json

from kikubot.pane_screen import (
    MAX_PARTS,
    PaneScreen,
    parse_screen_output,
    screen_messages,
)


def _units(s: str) -> int:
    return len(s.encode("utf-16-le")) // 2


def _join(messages: list[tuple[str, str]]) -> str:
    return "".join(json.loads(text)["d"] for _, text in messages[1:])


def test_parse_visible_screen():
    out = "row1\n  row2\n\x1b[1mrow3\x1b[0m\n4 1 80 3 1 120\n"
    s = parse_screen_output(out)
    assert s == PaneScreen(
        text="row1\n  row2\n\x1b[1mrow3\x1b[0m\n",
        cols=80,
        rows=3,
        cursor=(4, 1),
        back=0,
        history=120,
    )


def test_parse_with_history_and_hidden_cursor():
    out = "old1\nold2\nrow1\nrow2\n0 0 80 2 0 2\n"
    s = parse_screen_output(out)
    assert s is not None
    assert s.back == 2 and s.cursor is None
    assert s.text.endswith("row1\nrow2\n")


def test_parse_keeps_blank_rows():
    # A blank last row is a line too: every row ends with a newline, so a
    # reader that drops a final empty piece still gets all of them.
    s = parse_screen_output("\n\n\n0 0 80 3 1 0\n")
    assert s is not None and s.text == "\n\n\n"
    s = parse_screen_output("top\n\n0 0 80 2 1 0\n")
    assert s is not None and s.text == "top\n\n"


def test_parse_rejects_a_missing_format_line():
    assert parse_screen_output("row1\nrow2\n") is None
    assert parse_screen_output("") is None


def test_round_trip_keeps_every_character():
    text = "\n".join(
        [
            "\x1b[38;5;174m●\x1b[0m   leading spaces",
            'quotes " and \\ backslash\ttab',
            "кириллица ─────│ box",
            "emoji 👍🏽 zwj 👩‍💻 sep  pua  c1 \x85",
        ]
    )
    screen = PaneScreen(text, cols=100, rows=4, cursor=(0, 3), back=0, history=0)
    messages = screen_messages(screen, ts=7, panel=55)
    meta = json.loads(messages[0][1])
    assert meta == {
        "v": 1,
        "cols": 100,
        "rows": 4,
        "cursor": [0, 3],
        "ts": 7,
        "parts": 1,
        "panel": 55,
        "back": 0,
        "history": 0,
    }
    assert _join(messages) == text
    raw = messages[1][1]
    # Nothing Telegram might strip or fold travels raw.
    for ch in ("\x1b", "‍", " ", "", "\x85", "\n", "\t"):
        assert ch not in raw


def test_long_text_splits_under_the_message_limit():
    text = "\n".join(f"\x1b[31m{'щ' * 90}👍\x1b[0m" for _ in range(400))
    screen = PaneScreen(text, cols=100, rows=400, cursor=None, back=0, history=0)
    messages = screen_messages(screen, ts=1, panel=None)
    meta = json.loads(messages[0][1])
    assert meta["parts"] == len(messages) - 1 > 1
    assert [rid for rid, _ in messages[1:3]] == ["screen.1", "screen.2"]
    assert all(_units(t) <= 4096 for _, t in messages)
    assert _join(messages) == text


def test_history_is_trimmed_from_the_top_to_fit():
    history = [f"\x1b[3{i % 8}mhistory line {i} " + "x" * 80 for i in range(5000)]
    rows = [f"screen row {i}" for i in range(50)]
    screen = PaneScreen(
        "".join(f"{line}\n" for line in history + rows),
        cols=100,
        rows=50,
        cursor=(0, 0),
        back=5000,
        history=5000,
    )
    messages = screen_messages(screen, ts=1, panel=None)
    meta = json.loads(messages[0][1])
    assert meta["parts"] == len(messages) - 1 <= MAX_PARTS
    assert 0 < meta["back"] < 5000 and meta["history"] == 5000
    text = _join(messages)
    assert text.endswith("\n")
    lines = text.split("\n")[:-1]
    assert len(lines) == meta["back"] + 50
    assert lines[-50:] == rows  # the screen itself is never trimmed
    assert lines[0] == history[5000 - meta["back"]]


def test_fewer_parts_on_request():
    history = ["h" * 99] * 2000
    screen = PaneScreen(
        "\n".join(history + ["row"]),
        cols=100,
        rows=1,
        cursor=None,
        back=2000,
        history=2000,
    )
    messages = screen_messages(screen, ts=1, panel=None, max_parts=12)
    assert json.loads(messages[0][1])["parts"] <= 12


def test_unavailable_screen_answers_meta_only():
    messages = screen_messages(None, ts=3, panel=None)
    assert len(messages) == 1
    meta = json.loads(messages[0][1])
    assert meta["parts"] == 0 and meta["error"] == "unavailable"


def test_a_screen_too_big_for_the_parts_says_so():
    rows = ["\x1b[38;2;1;2;3m" + "щ" * 99 + "\x1b[0m"] * 60
    screen = PaneScreen(
        "".join(r + "\n" for r in rows),
        cols=100,
        rows=60,
        cursor=None,
        back=0,
        history=0,
    )
    meta = json.loads(screen_messages(screen, ts=1, panel=None, max_parts=1)[0][1])
    assert meta["parts"] == 1 and meta["truncated"] is True
    meta = json.loads(screen_messages(screen, ts=1, panel=None)[0][1])
    assert "truncated" not in meta
