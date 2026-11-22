"""Tests for ask_state — the AskUserQuestion record and the widget on screen.

The fixtures under tests/fixtures/ask/ are the widget's widget region as
Claude Code 2.1.329 drew it, captured from a throwaway agent answering a
synthetic question («Which color?» / «Which pets?»).
"""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kikubot import ask_state
from kikubot.ask_state import (
    SUBMIT_ROW,
    ask_json,
    ask_pointer,
    current_ask,
    load_record,
    parse_record,
    parse_view,
    same_text,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ask"
SID = "11111111-2222-3333-4444-555555555555"

RECORD = {
    "id": "toolu_01ExampleQuestionId0000",
    "questions": [
        {
            "question": "Which color?",
            "header": "Color",
            "options": [
                {"label": "Red", "description": "warm"},
                {"label": "Green", "description": "calm"},
                {"label": "Blue", "description": "cool"},
            ],
            "multiSelect": False,
        },
        {
            "question": "Which pets?",
            "header": "Pets",
            "options": [
                {"label": "Cat", "description": "Cat"},
                {"label": "Dog", "description": "Dog"},
                {"label": "Fish", "description": "Fish"},
            ],
            "multiSelect": True,
        },
    ],
}


def _pane(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text()


class TestParseRecord:
    def test_reads_the_tool_input(self):
        ask = parse_record(RECORD)
        assert ask is not None
        assert ask.id == RECORD["id"]
        assert [q.header for q in ask.questions] == ["Color", "Pets"]
        assert [q.multi for q in ask.questions] == [False, True]
        assert ask.questions[0].options[1].label == "Green"
        assert ask.questions[0].text_row == 4

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda r: r.update(id="../x"),
            lambda r: r.update(questions=[]),
            lambda r: r.update(questions="x"),
            lambda r: r["questions"][0].update(question=""),
            lambda r: r["questions"][0].update(
                options=[{"label": f"o{i}"} for i in range(9)]
            ),
            lambda r: r["questions"][0]["options"][0].update(label=7),
            lambda r: r["questions"][0].update(question="x" * 5000),
        ],
    )
    def test_refuses_a_malformed_record(self, mutate):
        record = json.loads(json.dumps(RECORD))
        mutate(record)
        assert parse_record(record) is None

    def test_oversized_file_is_no_record(self, tmp_path):
        path = tmp_path / "big.json"
        path.write_text(json.dumps({**RECORD, "pad": "x" * 70_000}))
        assert load_record(path) is None

    def test_record_path_refuses_a_strange_session_id(self, tmp_path):
        assert ask_state.record_path(tmp_path, "../../etc/passwd") is None
        assert ask_state.record_path(tmp_path, SID) == tmp_path / "asks" / f"{SID}.json"


class TestParseView:
    def test_single_choice_question(self):
        view = parse_view(_pane("q1"))
        assert view is not None and not view.review
        assert view.question == "Which color?"
        assert [(o.n, o.label, o.checked) for o in view.options] == [
            (1, "Red", None),
            (2, "Green", None),
            (3, "Blue", None),
            (4, "Type something.", None),
        ]
        assert view.cursor == 1 and not view.text_focus

    def test_cursor_on_the_free_text_row_is_focus(self):
        view = parse_view(_pane("q1_textfocus"))
        assert view is not None and view.cursor == 4 and view.text_focus

    def test_multi_choice_with_cursor_on_submit(self):
        view = parse_view(_pane("q2_submit"))
        assert view is not None
        assert view.question == "Which pets?"
        assert [o.checked for o in view.options] == [True, False, False, False]
        # The footer still says «ctrl+g to edit»: it's no sign of focus.
        assert view.cursor == SUBMIT_ROW and not view.text_focus

    def test_review_screen(self):
        view = parse_view(_pane("review"))
        assert view is not None and view.review
        assert view.answers == [("Which color?", "Green"), ("Which pets?", "Cat")]
        assert view.option(1) is not None and view.option(1).label == "Submit answers"

    def test_gone_after_esc(self):
        assert parse_view(_pane("after_esc")) is None

    def test_wrapped_question_text_matches(self):
        assert same_text("A long question that\nwraps", "A long  question that wraps")
        assert not same_text("Which color?", "Which pets?")


@pytest.fixture
def pending(tmp_path, monkeypatch):
    """A recorded question for binding @5, whose session is SID."""
    from kikubot.config import config
    from kikubot.session import session_manager

    (tmp_path / "asks").mkdir()
    (tmp_path / "asks" / f"{SID}.json").write_text(json.dumps(RECORD))
    monkeypatch.setattr(config, "config_dir", tmp_path)
    monkeypatch.setitem(
        session_manager.window_states, "@5", SimpleNamespace(session_id=SID)
    )
    return tmp_path


class TestCurrentAsk:
    @pytest.mark.parametrize("name", ["q1", "q2_submit", "review"])
    def test_while_the_widget_is_up(self, pending, name):
        found = current_ask("@5", _pane(name))
        assert found is not None and found[0].id == RECORD["id"]

    def test_not_after_esc(self, pending):
        # Esc fires no hook: the record stays, the screen decides.
        assert current_ask("@5", _pane("after_esc")) is None

    def test_not_for_another_question(self, pending):
        other = _pane("q1").replace("Which color?", "Which size?")
        assert current_ask("@5", other) is None

    def test_not_without_a_record(self, pending):
        (pending / "asks" / f"{SID}.json").unlink()
        assert current_ask("@5", _pane("q1")) is None


class TestAskJson:
    def test_full_and_shrunk(self):
        ask = parse_record(RECORD)
        assert ask is not None
        view = parse_view(_pane("q2_submit"))
        assert view is not None
        full = ask_json(ask, view)
        assert full["kind"] == "widget" and full["step"] == 2
        assert full["questions"][0]["options"][0] == {
            "label": "Red",
            "description": "warm",
        }
        assert full["questions"][1]["multi"] is True
        assert ask_json(ask, view, level=1)["questions"][0]["options"][0] == {
            "label": "Red"
        }

    def test_pointer_has_no_text(self):
        ask = parse_record(RECORD)
        view = parse_view(_pane("review"))
        assert ask is not None and view is not None
        assert ask_pointer(ask, view) == {
            "id": RECORD["id"],
            "kind": "widget",
            "step": 0,
            "count": 2,
        }


def _finishes(fn, timeout: float = 3.0):
    """Run ``fn`` in a daemon thread; (finished, result). A hang can't stall pytest."""
    import threading

    out: list = []
    t = threading.Thread(target=lambda: out.append(fn()), daemon=True)
    t.start()
    t.join(timeout)
    return (not t.is_alive()), (out[0] if out else None)


class TestPlantedFiles:
    """A container writes the asks/ directory: nothing there may hang the bot."""

    def test_a_fifo_is_no_record_and_no_hang(self, tmp_path):
        import os

        fifo = tmp_path / "asks.json"
        os.mkfifo(fifo)
        finished, result = _finishes(lambda: load_record(fifo))
        assert finished and result is None

    def test_a_symlink_is_no_record(self, tmp_path):
        real = tmp_path / "real.json"
        real.write_text(json.dumps(RECORD))
        link = tmp_path / "link.json"
        link.symlink_to(real)
        assert load_record(real) is not None
        assert load_record(link) is None

    def test_a_symlinked_asks_dir_is_no_record(self, tmp_path):
        (tmp_path / "elsewhere").mkdir()
        (tmp_path / "elsewhere" / f"{SID}.json").write_text(json.dumps(RECORD))
        (tmp_path / "asks").symlink_to(tmp_path / "elsewhere")
        path = ask_state.record_path(tmp_path, SID)
        assert path is not None and load_record(path) is None
