"""Tests for poll_buttons — buttons for a poll an agent writes as text."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kikubot.handlers import poll_buttons
from kikubot.handlers.poll_buttons import keyboard, parse, poll_json, poll_pointer

POLL = """Сделал черновик, нужно решить три вещи.

Первым в каждом пункте — мой совет.
1. Куда выкладывать?
- А. TestFlight
- Б. Только локально
2. Что проверить? (можно несколько)
- А. Вход
- Б. Оплату
- В. `push`-уведомления
3. Что-то ещё?

Скажи «залей», если всё ок."""


class TestParse:
    def test_the_rules_example(self):
        poll = parse(POLL)
        assert poll is not None
        assert [(q.number, q.text, q.multi) for q in poll.questions] == [
            (1, "Куда выкладывать?", False),
            (2, "Что проверить?", True),
            (3, "Что-то ещё?", False),
        ]
        assert poll.questions[1].options == (
            ("А", "Вход"),
            ("Б", "Оплату"),
            ("В", "push-уведомления"),
        )
        assert poll.questions[2].options == ()
        assert poll.advised and poll.words == ("залей",)

    def test_english_letters_and_several(self):
        poll = parse("1. Which?\n- A. One\n- B. Two\n2. And? (several)\n- A. X\n- B. Y")
        assert poll is not None and poll.questions[1].multi and not poll.advised

    @pytest.mark.parametrize(
        "line, words",
        [
            ("Скажи «лей» (или «стоп», если рано).", ("лей",)),
            ("Жду «ок» или «154».", ("ок", "154")),
            ("Say «go», «wait» or «stop».", ("go", "wait", "stop")),
            ("Скажи «да» — и я выложу, а «нет» отменит.", ("да",)),
            # mentions, not requests
            ("Дальше сам (если что, скажи «стоп»).", ()),
            ("Админу отправил: «Ждёшь слова — проси «Скажи «залей»»».", ()),
            (
                "Бот находит опрос («1. Вопрос?», «- А. вариант», Скажи «слово») "
                "и ставит кнопки.",
                (),
            ),
            # the rule's placeholder and templates
            ("Скажи «слово».", ()),
            ("Скажи «<слово>».", ()),
        ],
    )
    def test_go_ahead_words(self, line, words):
        poll = parse(line)
        assert (poll.words if poll else ()) == words

    def test_go_ahead_alone(self):
        poll = parse("Готово к выкладке. Жду «ок» или «154».")
        assert poll is not None and poll.questions == () and poll.words == ("ок", "154")

    @pytest.mark.parametrize(
        "text",
        [
            "Plain reply, no questions.",
            # a numbered list in prose: no options to pick
            "1. Did you check the logs?\n2. Did it restart?",
            # numbering off
            "2. Which?\n- A. One\n- B. Two",
            # inside a code block it's an example, not a poll
            "```\n1. Which?\n- A. One\n- B. Two\n```",
            # a letter twice
            "1. Which?\n- A. One\n- A. Two",
        ],
    )
    def test_no_poll(self, text):
        assert parse(text) is None


def _rows(poll) -> list[list[str]]:
    return [[b.text for b in row] for row in keyboard(poll).inline_keyboard]


class TestKeyboard:
    def test_a_short_row_per_question(self):
        poll = parse(POLL)
        assert poll is not None
        assert _rows(poll) == [
            ["⭐1А", "1Б"],
            ["⭐2А", "2Б", "2В"],
            ["залей"],
            ["👍 As advised", "📨 Send"],
        ]

    def test_ticks_compose_the_answer(self):
        poll = parse(POLL)
        assert poll is not None
        poll.picked = {0: ["Б"], 1: ["В", "А"]}
        assert poll.answer() == "1Б, 2АВ"
        assert _rows(poll)[:2] == [["⭐1А", "✅1Б"], ["✅2А", "2Б", "✅2В"]]
        assert _rows(poll)[-1] == ["👍 As advised", "📨 Send: 1Б, 2АВ"]

    def test_as_advised_is_every_first_option(self):
        poll = parse(POLL)
        assert poll is not None and poll.advised_answer() == "1А, 2А"
        unadvised = parse("1. Which?\n- A. One\n- B. Two\n2. And?\n- A. X\n- B. Y")
        assert unadvised is not None and unadvised.advised_answer() == ""
        assert _rows(unadvised)[-1] == ["📨 Send"]

    def test_lone_question_is_its_letters(self):
        poll = parse("1. Deploy now?\n- A. Yes\n- B. Later")
        assert poll is not None and poll.one_tap
        assert _rows(poll) == [["A", "B"]]

    def test_many_options_wrap(self):
        opts = "\n".join(f"- {c}. option {c}" for c in "ABCDEFG")
        poll = parse(f"1. Which?\n{opts}\n2. And?\n- A. X\n- B. Y")
        assert poll is not None
        assert [len(r) for r in _rows(poll)[:3]] == [5, 2, 2]

    def test_callback_data_fits(self):
        poll = parse(POLL)
        assert poll is not None
        for row in keyboard(poll).inline_keyboard:
            for b in row:
                assert len(b.callback_data.encode()) <= 64


class TestForClients:
    def test_pointer_and_json(self):
        poll = parse(POLL)
        assert poll is not None
        assert poll_pointer(77, poll) == {
            "id": "m77",
            "kind": "text",
            "message_id": 77,
            "count": 3,
        }
        data = poll_json(77, poll)
        assert data["replies"] == ["залей"]
        q2 = data["questions"][1]
        assert q2["number"] == 2 and q2["multi"] and q2["advised"] == 0
        assert q2["options"][2] == {"letter": "В", "label": "push-уведомления"}
        assert (
            data["questions"][2]["options"] == []
            and data["questions"][2]["advised"] is None
        )


@pytest.fixture
def live(monkeypatch):
    """An open poll in topic (1, 42) on message 100, a fake bot and delivery."""
    from kikubot.handlers import delivery
    from kikubot.session import session_manager

    poll_buttons._open.clear()
    bot = SimpleNamespace(
        edit_message_reply_markup=AsyncMock(), send_message=AsyncMock()
    )
    sent: list[str] = []

    async def deliver(user_id, thread_id, binding, text, **_):
        sent.append(text)
        return "sent", ""

    monkeypatch.setattr(delivery, "deliver_user_text", deliver)
    monkeypatch.setattr(session_manager, "resolve_window_for_thread", lambda u, t: "@5")
    yield SimpleNamespace(bot=bot, sent=sent)
    poll_buttons._open.clear()


def _tap(data: str, message_id: int = 100):
    return SimpleNamespace(
        data=data,
        message=SimpleNamespace(chat=SimpleNamespace(id=-1), message_id=message_id),
        answer=AsyncMock(),
    )


class TestTaps:
    async def test_tick_then_send(self, live):
        poll = parse(POLL)
        await poll_buttons.attach(live.bot, 1, 42, -1, 100, poll)
        assert poll_buttons.open_poll(1, 42) == (100, poll)
        await poll_buttons.handle_tap(_tap("pl:t:0:1"), live.bot, 1, 42)
        await poll_buttons.handle_tap(_tap("pl:t:1:0"), live.bot, 1, 42)
        await poll_buttons.handle_tap(_tap("pl:t:1:2"), live.bot, 1, 42)
        assert live.sent == []
        await poll_buttons.handle_tap(_tap("pl:s"), live.bot, 1, 42)
        assert live.sent == ["1Б, 2АВ"]
        assert poll_buttons.open_poll(1, 42) is None
        final = live.bot.edit_message_reply_markup.await_args.kwargs["reply_markup"]
        assert final.inline_keyboard[0][0].text == "✓ 1Б, 2АВ"

    async def test_as_advised_in_one_tap(self, live):
        await poll_buttons.attach(live.bot, 1, 42, -1, 100, parse(POLL))
        await poll_buttons.handle_tap(_tap("pl:a"), live.bot, 1, 42)
        assert live.sent == ["1А, 2А"]

    async def test_lone_question_answers_on_one_tap(self, live):
        await poll_buttons.attach(
            live.bot, 1, 42, -1, 100, parse("1. Now?\n- A. Yes\n- B. No")
        )
        await poll_buttons.handle_tap(_tap("pl:t:0:0"), live.bot, 1, 42)
        assert live.sent == ["1A"]

    async def test_go_ahead_word(self, live):
        await poll_buttons.attach(live.bot, 1, 42, -1, 100, parse(POLL))
        await poll_buttons.handle_tap(_tap("pl:w:0"), live.bot, 1, 42)
        assert live.sent == ["залей"]

    async def test_send_with_nothing_ticked(self, live):
        await poll_buttons.attach(live.bot, 1, 42, -1, 100, parse(POLL))
        tap = _tap("pl:s")
        await poll_buttons.handle_tap(tap, live.bot, 1, 42)
        assert live.sent == [] and tap.answer.await_args.kwargs == {"show_alert": True}

    async def test_a_past_poll_sends_nothing(self, live):
        await poll_buttons.attach(live.bot, 1, 42, -1, 100, parse(POLL))
        await poll_buttons.close(live.bot, 1, 42)  # the person wrote
        await poll_buttons.handle_tap(_tap("pl:w:0"), live.bot, 1, 42)
        assert live.sent == []

    async def test_an_older_message_sends_nothing(self, live):
        await poll_buttons.attach(live.bot, 1, 42, -1, 100, parse(POLL))
        await poll_buttons.handle_tap(_tap("pl:w:0", message_id=99), live.bot, 1, 42)
        assert live.sent == [] and poll_buttons.open_poll(1, 42) is not None
