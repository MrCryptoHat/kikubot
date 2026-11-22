"""Tests for ask_answer — answering an AskUserQuestion with keys.

``FakeWidget`` plays Claude Code's widget (2.1.329) the way it behaved when
driven by hand on a throwaway agent: it draws the same screens as the
fixtures in tests/fixtures/ask/ and reacts to keys the same way. The engine
reads only those screens, so these tests exercise the real walk.
"""

from __future__ import annotations

import pytest

from kikubot.ask_state import CHAT_ROW, SUBMIT_ROW, parse_record, parse_view
from kikubot.handlers import ask_answer
from kikubot.handlers.ask_answer import (
    AnswerError,
    Choice,
    ask_buttons,
    check_answers,
    parse_answer,
    parse_button,
)
from kikubot.session import KeyDriver, session_manager
from kikubot.terminal_keys import KeyToken

SEP = "─" * 110


def _record(
    *questions: tuple[str, str, list[str], bool], ask_id: str = "toolu_01Abcdefghij"
):
    return parse_record(
        {
            "id": ask_id,
            "questions": [
                {
                    "question": text,
                    "header": header,
                    "options": [{"label": o, "description": o.lower()} for o in opts],
                    "multiSelect": multi,
                }
                for header, text, opts, multi in questions
            ],
        }
    )


COLOR = ("Color", "Which color?", ["Red", "Green", "Blue"], False)
PETS = ("Pets", "Which pets?", ["Cat", "Dog", "Fish"], True)
SIZE = ("Size", "Which size?", ["Small", "Medium", "Large"], False)


class FakeWidget:
    """Claude Code's AskUserQuestion widget, as far as the engine can tell."""

    def __init__(self, ask, deaf: set[str] | None = None, barred: bool = False):
        self.ask = ask
        self.qs = ask.questions
        self.tabs = len(self.qs) > 1 or any(q.multi for q in self.qs)
        self.tab = 0
        self.cursor = [1] * len(self.qs)
        self.review_cursor = 1
        self.single: list[int | None] = [None] * len(self.qs)
        self.checked: list[set[int]] = [set() for _ in self.qs]
        self.text = [""] * len(self.qs)
        self.answered = [False] * len(self.qs)
        self.result: dict[str, str] | None = None  # set once submitted
        self.declined = False
        self.deaf = deaf or set()
        # Claude Code draws some questions behind a quote bar: «│ Step 3: …».
        self.barred = barred
        self.pressed: list[str] = []

    # -- state --
    @property
    def open(self) -> bool:
        return self.result is None and not self.declined

    def _answer(self, i: int) -> str:
        q = self.qs[i]
        if q.multi:
            parts = [
                q.options[n - 1].label
                for n in sorted(self.checked[i])
                if n <= len(q.options)
            ]
            if q.text_row in self.checked[i] and self.text[i]:
                parts.append(self.text[i])
            return ", ".join(parts)
        if self.single[i] == q.text_row:
            return self.text[i]
        return q.options[self.single[i] - 1].label if self.single[i] else ""

    def _advance(self) -> None:
        if not self.tabs:
            self.result = {self.qs[0].question: self._answer(0)}
            return
        self.tab += 1
        if self.tab < len(self.qs):
            self.cursor[self.tab] = 1

    def _submit(self) -> None:
        self.result = {q.question: self._answer(i) for i, q in enumerate(self.qs)}

    # -- keys --
    def press(self, tokens: list[KeyToken]) -> None:
        for tok in tokens:
            self.pressed.append(tok.key)
            if tok.key in self.deaf or not self.open:
                continue
            self._key(tok)

    def _key(self, tok: KeyToken) -> None:
        review = self.tab >= len(self.qs)
        if review:
            if tok.key in ("1", "Enter") and (
                tok.key == "1" or self.review_cursor == 1
            ):
                self._submit()
            elif tok.key == "Left":
                self.tab = len(self.qs) - 1
            elif tok.key == "Escape" or tok.key == "2":
                self.declined = True
            return
        i, q = self.tab, self.qs[self.tab]
        focused = self.cursor[i] == q.text_row
        rows = (
            list(range(1, q.text_row + 1))
            + ([SUBMIT_ROW] if q.multi else [])
            + [CHAT_ROW]
        )
        # ←/→ switch questions only from an option row: on the free-text row
        # they move the text cursor, on «Next»/«Submit» and «Chat about this»
        # nothing happens (as Claude Code 2.1.329 does).
        on_option = 1 <= self.cursor[i] < q.text_row
        if tok.literal:
            if focused:
                self.text[i] += tok.key
                if q.multi:
                    self.checked[i].add(q.text_row)
                return
            if not tok.key.isdigit() or len(tok.key) != 1:
                return
            d = int(tok.key)
            if q.multi:
                if 1 <= d <= q.text_row:
                    self.checked[i] ^= {d}
            elif 1 <= d <= len(q.options):
                self.single[i] = d
                self.answered[i] = True
                self._advance()
            elif d == q.text_row:
                self.cursor[i] = q.text_row
            return
        key = tok.key
        if key in ("Up", "Down"):
            pos = rows.index(self.cursor[i]) + (1 if key == "Down" else -1)
            self.cursor[i] = rows[max(0, min(pos, len(rows) - 1))]
        elif key in ("Left", "Right") and on_option and self.tabs:
            self.tab = max(
                0, min(self.tab + (1 if key == "Right" else -1), len(self.qs))
            )
        elif key == "Enter":
            if q.multi:
                if self.cursor[i] == SUBMIT_ROW:
                    self.answered[i] = True
                    self._advance()
                else:
                    self.checked[i] ^= {self.cursor[i]}
            elif focused:
                if self.text[i]:
                    self.single[i] = q.text_row
                    self.answered[i] = True
                    self._advance()
            else:
                self.single[i] = self.cursor[i]
                self.answered[i] = True
                self._advance()
        elif key == "Escape":
            self.declined = True

    # -- screen --
    def render(self) -> str:
        top = ["⏺ Two quick questions before I start.", ""]
        if not self.open:
            return "\n".join(
                top + ["⏺ Answered.", "", SEP, "❯ ", SEP, "  ⏵⏵ auto mode on"]
            )
        marks = "  ".join(
            f"{'☒' if self.answered[i] else '☐'} {q.header}"
            for i, q in enumerate(self.qs)
        )
        bar = f"←  {marks}  ✔ Submit  →" if self.tabs else f" ☐ {self.qs[0].header}"
        lines = top + [SEP, bar, ""]
        if self.tab >= len(self.qs):
            lines += ["Review your answers", ""]
            for i, q in enumerate(self.qs):
                bar = "│ " if self.barred else ""
                drawn = q.question.replace("\ufe0f", "")
                lines += [f" {bar}● {drawn}", f"   → {self._answer(i)}"]
            lines += [
                "",
                f"{'❯' if self.review_cursor == 1 else ' '} 1. Submit answers",
            ]
            lines += [f"{'❯' if self.review_cursor == 2 else ' '} 2. Cancel"]
            return "\n".join(lines)
        i, q = self.tab, self.qs[self.tab]
        # The pane drops emoji variation selectors: «☑️» is drawn «☑».
        drawn = q.question.replace("\ufe0f", "")
        lines += [f"│ {drawn}" if self.barred else drawn, ""]
        for n in range(1, q.text_row + 1):
            cur = "❯" if self.cursor[i] == n else " "
            if n == q.text_row:
                label = self.text[i] or (
                    "Type something" if q.multi else "Type something."
                )
                desc = None
            else:
                label, desc = q.options[n - 1].label, q.options[n - 1].description
            if q.multi:
                box = "[✔]" if n in self.checked[i] else "[ ]"
                lines.append(f"{cur} {n}. {box} {label}")
                if desc:
                    lines.append(f"         {desc}")
            else:
                lines.append(f"{cur} {n}. {label}")
                if desc:
                    lines.append(f"     {desc}")
        if q.multi:
            # «Next» while a question follows, «Submit» on the last one.
            label = "Next" if i < len(self.qs) - 1 else "Submit"
            lines.append(f"{'❯' if self.cursor[i] == SUBMIT_ROW else ' '}    {label}")
        chat = "❯" if self.cursor[i] == CHAT_ROW else " "
        lines += [SEP, f"{chat} {q.text_row + 1}. Chat about this", ""]
        lines.append("Enter to select · Tab/Arrow keys to navigate · Esc to cancel")
        return "\n".join(lines)


@pytest.fixture
def widget(monkeypatch):
    """Install a FakeWidget for binding @5; call it with the questions."""
    monkeypatch.setattr(ask_answer, "STEP_TIMEOUT", 0.2)
    monkeypatch.setattr(ask_answer, "_POLL", 0)

    def make(*questions, deaf=None, ask_id="toolu_01Abcdefghij", barred=False):
        ask = _record(*questions, ask_id=ask_id)
        w = FakeWidget(ask, deaf, barred)

        async def capture():
            return w.render()

        async def press(tokens):
            w.press(tokens)
            return len(tokens), None

        async def drive_keys(binding, script, *, lock_wait=2.0):
            return await script(KeyDriver(capture, press))

        monkeypatch.setattr(ask_answer, "load_ask", lambda b: ask)
        monkeypatch.setattr(session_manager, "drive_keys", drive_keys)
        return w

    return make


class TestRendersLikeTheRealWidget:
    """The fake must be read by the same parser as the captured fixtures."""

    def test_question_and_review(self, widget):
        w = widget(COLOR, PETS)
        view = parse_view(w.render())
        assert view is not None and view.question == "Which color?"
        w.press([KeyToken("2", True), KeyToken("1", True), KeyToken("Right", False)])
        view = parse_view(w.render())
        assert view is not None and view.review
        assert view.answers == [("Which color?", "Green"), ("Which pets?", "Cat")]


class TestAnswerAll:
    async def test_two_questions_and_own_text(self, widget):
        w = widget(COLOR, PETS)
        _, raw = parse_answer('toolu_01Abcdefghij 1:2 2:1,3,"kiwi"')
        await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert w.result == {"Which color?": "Green", "Which pets?": "Cat, Fish, kiwi"}

    async def test_from_a_widget_already_moved_on(self, widget):
        w = widget(COLOR, PETS)
        # The person went to question 2 and ticked Dog by hand.
        w.press([KeyToken("Right", False), KeyToken("2", True)])
        _, raw = parse_answer("toolu_01Abcdefghij 1:1 2:3")
        await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert w.result == {"Which color?": "Red", "Which pets?": "Fish"}

    @pytest.mark.parametrize("downs", [4, 5], ids=["cursor-on-next", "cursor-on-chat"])
    async def test_from_a_row_where_arrows_do_nothing(self, widget, downs):
        w = widget(COLOR, PETS, SIZE)
        # Question 2, then the cursor down past its options: on «Next» and on
        # «Chat about this» ←/→ don't switch questions.
        w.press([KeyToken("1", True)] + [KeyToken("Down", False)] * downs)
        assert w.tab == 1 and w.cursor[1] == (SUBMIT_ROW if downs == 4 else CHAT_ROW)
        _, raw = parse_answer("toolu_01Abcdefghij 1:2 2:3 3:1")
        await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert w.result == {
            "Which color?": "Green",
            "Which pets?": "Fish",
            "Which size?": "Small",
        }

    async def test_barred_questions_with_emoji(self, widget):
        ticks = (
            "Ticks",
            "Tick two (⬜ becomes ☑️), then «Next».",
            ["Tea", "Coffee"],
            True,
        )
        w = widget(COLOR, ticks, SIZE, barred=True)
        _, raw = parse_answer('toolu_01Abcdefghij 1:1 2:1,2 3:"mine"')
        await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert w.result == {
            "Which color?": "Red",
            ticks[1]: "Tea, Coffee",
            "Which size?": "mine",
        }

    async def test_lone_question_by_number(self, widget):
        w = widget(SIZE)
        _, raw = parse_answer("toolu_01Abcdefghij 3")
        await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert w.result == {"Which size?": "Large"}

    async def test_lone_question_own_text(self, widget):
        w = widget(SIZE)
        _, raw = parse_answer("toolu_01Abcdefghij «extra large, 2: please»")
        await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert w.result == {"Which size?": "extra large, 2: please"}

    async def test_another_question_presses_nothing(self, widget):
        w = widget(COLOR, PETS)
        _, raw = parse_answer("toolu_other 1:1 2:1")
        with pytest.raises(AnswerError) as e:
            await ask_answer.answer_all("@5", "toolu_other", raw)
        assert e.value.code == "no-question" and w.pressed == []

    async def test_a_key_that_doesnt_land_stops_the_walk(self, widget):
        w = widget(COLOR, PETS, deaf={"3"})
        _, raw = parse_answer("toolu_01Abcdefghij 1:1 2:3")
        with pytest.raises(AnswerError) as e:
            await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert e.value.code == "moved"
        assert w.open and "Enter" not in w.pressed and "1" not in w.pressed[1:]

    async def test_a_draft_in_the_text_row_is_not_typed_over(self, widget):
        w = widget(SIZE)
        w.press([KeyToken("4", True), KeyToken("half", True), KeyToken("Up", False)])
        _, raw = parse_answer('toolu_01Abcdefghij "huge"')
        with pytest.raises(AnswerError) as e:
            await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert e.value.code == "draft" and w.open and w.text[0] == "half"

    async def test_unanswered_question_refused_before_any_key(self, widget):
        w = widget(COLOR, PETS)
        _, raw = parse_answer("toolu_01Abcdefghij 1:1")
        with pytest.raises(AnswerError) as e:
            await ask_answer.answer_all("@5", "toolu_01Abcdefghij", raw)
        assert e.value.code == "unanswered" and w.pressed == []


class TestSteps:
    async def test_buttons_one_tap_at_a_time(self, widget):
        w = widget(COLOR, PETS)
        await ask_answer.pick("@5", w.ask.id, 0, 3)
        await ask_answer.pick("@5", w.ask.id, 1, 2)
        assert ask_answer.parse_view(w.render()).option(2).checked
        await ask_answer.finish("@5", w.ask.id, 1)
        assert w.open and w.tab == 2  # the review
        await ask_answer.submit("@5", w.ask.id)
        assert w.result == {"Which color?": "Blue", "Which pets?": "Dog"}

    async def test_message_is_the_own_answer(self, widget):
        w = widget(COLOR, PETS)
        assert await ask_answer.type_answer("@5", "teal\nplease")
        assert w.tab == 1 and w.text[0] == "teal please"
        assert await ask_answer.type_answer("@5", "hamster")
        assert w.text[1] == "hamster" and 4 in w.checked[1]

    async def test_message_on_the_review_is_refused(self, widget):
        w = widget(SIZE, PETS)
        w.press([KeyToken("1", True), KeyToken("1", True), KeyToken("Right", False)])
        with pytest.raises(AnswerError) as e:
            await ask_answer.type_answer("@5", "more")
        assert e.value.code == "review" and w.open

    async def test_no_question_on_screen_is_not_ours(self, widget):
        w = widget(SIZE)
        w.declined = True
        assert not await ask_answer.type_answer("@5", "hi")


class TestParseAnswer:
    @pytest.mark.parametrize(
        "args, expected",
        [
            ("id 1:2", {1: Choice((2,))}),
            ("id 2", {None: Choice((2,))}),
            ('id 1:2 2:1,3,"x y"', {1: Choice((2,)), 2: Choice((1, 3), "x y")}),
            (
                "id 1:“curly” 2:«guillemets»",
                {1: Choice(text="curly"), 2: Choice(text="guillemets")},
            ),
            ('id 1:"say \\"hi\\""', {1: Choice(text='say "hi"')}),
        ],
    )
    def test_reads(self, args, expected):
        assert parse_answer(args) == ("id", expected)

    @pytest.mark.parametrize(
        "args",
        [
            "",
            "id",
            "id 1:",
            "id 1:x",
            'id 1:"open',
            "id 1:1 1:2",
            "id 1:1 2",
            'id 1:"a","b"',
        ],
    )
    def test_refuses(self, args):
        with pytest.raises(AnswerError):
            parse_answer(args)

    def test_checks_against_the_question(self):
        ask = _record(COLOR, PETS)
        assert check_answers(ask, {1: Choice((1,)), 2: Choice((1, 2))}) == {
            0: Choice((1,)),
            1: Choice((1, 2)),
        }
        for bad in (
            {1: Choice((1, 2)), 2: Choice((1,))},  # two picks on a single choice
            {1: Choice((4,)), 2: Choice((1,))},  # no option 4
            {None: Choice((1,))},  # number needed with two questions
        ):
            with pytest.raises(AnswerError):
                check_answers(ask, bad)


class TestButtons:
    def test_single_choice_rows(self, widget):
        w = widget(COLOR, PETS)
        rows = ask_buttons(w.ask, parse_view(w.render()))
        assert [r[0].text for r in rows] == ["1. Red", "2. Green", "3. Blue"]
        assert parse_button(rows[1][0].callback_data) == ("pick", [0, 2], "Abcdefghij")

    def test_multi_choice_rows_show_ticks(self, widget):
        w = widget(PETS)
        w.press([KeyToken("2", True)])
        rows = ask_buttons(w.ask, parse_view(w.render()))
        assert [r[0].text for r in rows] == ["⬜ Cat", "☑️ Dog", "⬜ Fish", "➡️ Next"]

    def test_review_gets_submit(self, widget):
        w = widget(PETS)
        w.press([KeyToken("1", True), KeyToken("Right", False)])
        rows = ask_buttons(w.ask, parse_view(w.render()))
        assert len(rows) == 1 and parse_button(rows[0][0].callback_data)[0] == "submit"

    def test_callback_data_fits(self, widget):
        w = widget(COLOR, PETS)
        for row in ask_buttons(w.ask, parse_view(w.render())):
            assert len(row[0].callback_data.encode()) <= 64

    async def test_tap_on_an_old_photo_presses_nothing(self, widget):
        w = widget(COLOR, PETS)
        assert (
            await ask_answer.press_button("@5", "ak:p:0:1:SomeOldTok") == "no-question"
        )
        assert w.pressed == []


class TestMessageRouting:
    """A message while a recorded question is up goes to the engine first."""

    @pytest.fixture
    def pane(self, monkeypatch):
        from pathlib import Path

        text = (
            Path(__file__).parent.parent.parent / "fixtures" / "ask" / "q1.txt"
        ).read_text()

        async def capture(*_a, **_k):
            return text

        monkeypatch.setattr(session_manager, "capture_pane", capture)

    async def test_engine_takes_it(self, pane, monkeypatch):
        from kikubot.handlers.ask_question_router import try_route_to_text_option

        seen = []

        async def type_answer(binding, text):
            seen.append((binding, text))
            return True

        monkeypatch.setattr(ask_answer, "type_answer", type_answer)
        assert await try_route_to_text_option("@5", "teal") == (True, None)
        assert seen == [("@5", "teal")]

    async def test_refusal_blocks_the_message(self, pane, monkeypatch):
        from kikubot.handlers.ask_question_router import try_route_to_text_option

        async def type_answer(binding, text):
            raise AnswerError("draft")

        monkeypatch.setattr(ask_answer, "type_answer", type_answer)
        assert await try_route_to_text_option("@5", "teal") == (
            False,
            "blocking_widget:ask:draft",
        )
