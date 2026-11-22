"""Answer an agent's AskUserQuestion with keys — from /answer, a button, or text.

Three ways in, one engine:

- ``/answer <id> 1:2 2:1,3 3:"own text"`` — a client (or a person) answers
  every question at once; the bot walks the widget, checks the review screen
  against what was asked for, and submits.
- the buttons under the widget's photo (interactive_ui) — one step per tap:
  pick an option, tick a checkbox, «Next» on a multi-choice question, «Submit».
- a plain message while the widget is up — the free-text answer to the
  question on screen (delivery.py).

The id is the AskUserQuestion's tool_use_id (ask_state.py): an answer meant
for another or an earlier question presses nothing. Every key goes through
``SessionManager.drive_keys`` — the send lock held for the whole walk, the
keys path's guards on each press — and the screen is read before and after
each one: a question that isn't the expected one, a cursor that didn't move,
a checkbox that didn't flip stops the walk (``moved``) instead of pressing on
blind. What the widget accepts (Claude Code 2.1.329): a digit picks an
option (single choice: picks and moves on; multi-choice: toggles), the
free-text row takes typed text (Enter submits it on a single choice),
←/→ switch questions, a multi-choice question ends on its «Submit» row, and
the review screen submits on «1».
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from ..ask_state import (
    CHAT_ROW,
    SUBMIT_ROW,
    Ask,
    AskQuestion,
    WidgetView,
    load_ask,
    parse_view,
    same_text,
)
from ..i18n import tr
from ..session import KeyDriver, session_manager
from ..terminal_keys import KeysError, KeyToken
from ..terminal_parser import extract_interactive_content
from . import effective_user, get_thread_id, is_user_allowed
from .callback_data import CB_ASK_NEXT, CB_ASK_PICK, CB_ASK_SUBMIT
from .message_sender import safe_reply

logger = logging.getLogger(__name__)

STEP_TIMEOUT = 2.5  # how long one key may take to show on screen
_POLL = 0.1
MAX_TEXT_CHARS = 2000


class AnswerError(Exception):
    """An answer refused or stopped; ``code`` picks the message (ask.err.*)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Choice:
    """One question's answer: option numbers (1-based) and/or own text."""

    options: tuple[int, ...] = ()
    text: str | None = None


# --- /answer syntax ------------------------------------------------------------

_QUOTES = {'"': '"', "“": "”", "„": "“", "«": "»"}
_ITEM_HEAD_RE = re.compile(r"(\d+):")


def clean_text(raw: str) -> str:
    """Own-text answer as one typed line: control characters out, spaces folded."""
    text = "".join(" " if unicodedata.category(c) == "Cc" else c for c in raw)
    return " ".join(text.split())[:MAX_TEXT_CHARS]


def _read_quoted(s: str, i: int) -> tuple[str, int]:
    close = _QUOTES[s[i]]
    out: list[str] = []
    i += 1
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            out.append(s[i + 1])
            i += 2
            continue
        if c == close:
            return "".join(out), i + 1
        out.append(c)
        i += 1
    raise AnswerError("bad-answer")


def parse_answer(args: str) -> tuple[str, dict[int | None, Choice]]:
    """(ask id, {question number → Choice}) from ``/answer``'s arguments.

    ``<id> 1:2 2:1,3 3:"text"`` — numbers from 1; a question takes option
    numbers and at most one quoted text, comma-separated. A lone question may
    skip its number: ``<id> 2`` (key None). Quotes: "…", “…”, „…“, «…».
    """
    s = args.strip()
    ask_id, _, rest = s.partition(" ")
    if not ask_id:
        raise AnswerError("bad-answer")
    answers: dict[int | None, Choice] = {}
    i = 0
    rest = rest.strip()
    while i < len(rest):
        if rest[i].isspace():
            i += 1
            continue
        key: int | None = None
        m = _ITEM_HEAD_RE.match(rest, i)
        if m:
            key = int(m.group(1))
            i = m.end()
        elif answers:
            raise AnswerError("bad-answer")
        if key in answers:
            raise AnswerError("bad-answer")
        options: list[int] = []
        text: str | None = None
        while True:
            if i < len(rest) and rest[i] in _QUOTES:
                if text is not None:
                    raise AnswerError("bad-answer")
                text, i = _read_quoted(rest, i)
            else:
                m = re.compile(r"\d+").match(rest, i)
                if not m:
                    raise AnswerError("bad-answer")
                options.append(int(m.group(0)))
                i = m.end()
            if i < len(rest) and rest[i] == ",":
                i += 1
                continue
            break
        if i < len(rest) and not rest[i].isspace():
            raise AnswerError("bad-answer")
        answers[key] = Choice(tuple(options), clean_text(text) if text else None)
    if not answers:
        raise AnswerError("bad-answer")
    return ask_id, answers


def check_answers(ask: Ask, answers: dict[int | None, Choice]) -> dict[int, Choice]:
    """The answers keyed by question index (0-based), each one possible here."""
    if None in answers:
        if len(answers) != 1 or len(ask.questions) != 1:
            raise AnswerError("bad-answer")
        answers = {1: answers[None]}
    out: dict[int, Choice] = {}
    for num, choice in answers.items():
        if num is None or not 1 <= num <= len(ask.questions):
            raise AnswerError("bad-answer")
        q = ask.questions[num - 1]
        opts = choice.options
        if len(set(opts)) != len(opts) or any(
            not 1 <= n <= len(q.options) for n in opts
        ):
            raise AnswerError("bad-answer")
        picked = len(opts) + (1 if choice.text else 0)
        if picked == 0 or (not q.multi and picked != 1):
            raise AnswerError("bad-answer")
        out[num - 1] = choice
    if len(out) != len(ask.questions):
        raise AnswerError("unanswered")
    return out


# --- the walk ------------------------------------------------------------------


def _lit(text: str) -> KeyToken:
    return KeyToken(text, literal=True)


def _key(name: str) -> KeyToken:
    return KeyToken(name, literal=False)


def _is_placeholder(label: str) -> bool:
    return label.rstrip(".").strip() == "Type something"


async def _view(drv: KeyDriver) -> WidgetView | None:
    """The widget on screen now; None once it's gone."""
    pane = await drv.capture()
    if pane is None:
        raise AnswerError("unavailable")
    content = extract_interactive_content(pane)
    if content is None or content.name != "AskUserQuestion":
        return None
    return parse_view(pane)


async def _press(drv: KeyDriver, tokens: list[KeyToken]) -> None:
    _, error = await drv.press(tokens)
    if error:
        raise AnswerError(error)


async def _await_view(
    drv: KeyDriver, ok: Callable[[WidgetView | None], bool]
) -> WidgetView | None:
    """Poll until the screen satisfies ``ok``; ``moved`` when it never does."""
    deadline = time.monotonic() + STEP_TIMEOUT
    while True:
        await asyncio.sleep(_POLL)
        view = await _view(drv)
        if ok(view):
            return view
        if time.monotonic() > deadline:
            raise AnswerError("moved")


def _ours(ask: Ask, view: WidgetView | None) -> WidgetView:
    """``view`` if it shows this ask (a question of it, or its review)."""
    if view is None:
        raise AnswerError("no-question")
    if view.review:
        if view.answers and all(
            any(same_text(q.question, asked) for q in ask.questions)
            for asked, _ in view.answers
        ):
            return view
        raise AnswerError("no-question")
    if ask.index_of(view) is None:
        raise AnswerError("no-question")
    return view


async def _start(drv: KeyDriver, binding: str, ask_id: str) -> tuple[Ask, WidgetView]:
    """The pending ask with this id and the screen showing it, or refuse."""
    ask = load_ask(binding)
    if ask is None or ask.id != ask_id:
        raise AnswerError("no-question")
    return ask, _ours(ask, await _view(drv))


def _view_pos(view: WidgetView, row: int) -> int:
    """Top-to-bottom position of a row on screen: options, free text, then
    «Submit»/«Next» (multi-choice), then «Chat about this»."""
    text_row = view.options[-1].n
    if row == SUBMIT_ROW:
        return text_row + 1
    if row == CHAT_ROW:
        return text_row + (2 if view.has_submit else 1)
    return row


async def _unfocus(drv: KeyDriver, view: WidgetView) -> WidgetView:
    """The cursor onto an option row, where digits pick and ←/→ switch questions.

    On the free-text row a digit is typed as text and ←/→ move the text
    cursor; on «Next»/«Submit» or «Chat about this» ←/→ do nothing at all
    (Claude Code 2.1.329). Either way, a walk would stall there.
    """
    if view.review or not view.options:
        return view
    if view.cursor is None:
        raise AnswerError("moved")
    target = view.options[-1].n - 1  # the last real option
    ups = _view_pos(view, view.cursor) - target
    if ups <= 0:
        return view
    await _press(drv, [_key("Up")] * ups)
    moved = await _await_view(drv, lambda w: w is not None and w.cursor == target)
    assert moved is not None
    return moved


async def _goto(drv: KeyDriver, ask: Ask, index: int) -> WidgetView:
    """Bring question ``index`` on screen with ←/→."""
    for _ in range(2 * len(ask.questions) + 2):
        view = _ours(ask, await _view(drv))
        current = ask.index_of(view)
        if current == index:
            return view
        view = await _unfocus(drv, view)
        before = (view.review, current)
        go_left = view.review or (current is not None and current > index)
        await _press(drv, [_key("Left" if go_left else "Right")])
        await _await_view(
            drv,
            lambda w: w is None or (w.review, ask.index_of(w)) != before,
        )
    raise AnswerError("moved")


def _row_pos(question: AskQuestion, row: int) -> int:
    """Top-to-bottom position of a row: options, free text, «Submit»/«Next»,
    «Chat about this»."""
    if row == SUBMIT_ROW:
        return question.text_row + 1
    if row == CHAT_ROW:
        return question.text_row + (2 if question.multi else 1)
    return row


async def _move_cursor(
    drv: KeyDriver, question: AskQuestion, view: WidgetView, row: int
) -> WidgetView:
    if view.cursor is None:
        raise AnswerError("moved")
    delta = _row_pos(question, row) - _row_pos(question, view.cursor)
    if delta:
        key = _key("Down" if delta > 0 else "Up")
        await _press(drv, [key] * abs(delta))
        moved = await _await_view(drv, lambda w: w is not None and w.cursor == row)
        assert moved is not None
        return moved
    return view


def _left_question(ask: Ask, index: int) -> Callable[[WidgetView | None], bool]:
    return lambda w: w is None or w.review or ask.index_of(w) != index


async def _pick_single(
    drv: KeyDriver, ask: Ask, index: int, view: WidgetView, choice: Choice
) -> None:
    q = ask.questions[index]
    view = await _unfocus(drv, view)
    if choice.text is None:
        await _press(drv, [_lit(str(choice.options[0]))])
    else:
        await _press(drv, [_lit(str(q.text_row))])
        focused = await _await_view(
            drv, lambda w: w is not None and w.text_focus and w.cursor == q.text_row
        )
        row = focused.option(q.text_row) if focused else None
        if row is None or not _is_placeholder(row.label):
            raise AnswerError("draft")
        await _press(drv, [_lit(choice.text), _key("Enter")])
    await _await_view(drv, _left_question(ask, index))


async def _toggle(drv: KeyDriver, view: WidgetView, n: int) -> WidgetView:
    row = view.option(n)
    if row is None or row.checked is None:
        raise AnswerError("moved")
    want = not row.checked
    await _press(drv, [_lit(str(n))])

    def flipped(w: WidgetView | None) -> bool:
        o = w.option(n) if w is not None else None
        return o is not None and o.checked == want

    toggled = await _await_view(drv, flipped)
    assert toggled is not None
    return toggled


async def _type_into_text_row(
    drv: KeyDriver, q: AskQuestion, view: WidgetView, text: str
) -> WidgetView:
    """Tick the multi-choice free-text row and type ``text`` into it."""
    row = view.option(q.text_row)
    if row is None or not _is_placeholder(row.label):
        raise AnswerError("draft")
    if not row.checked:
        view = await _toggle(drv, view, q.text_row)
    view = await _move_cursor(drv, q, view, q.text_row)
    await _press(drv, [_lit(text)])
    head = text[:20]

    def typed(w: WidgetView | None) -> bool:
        o = w.option(q.text_row) if w is not None else None
        return o is not None and " ".join(o.label.split()).startswith(head.strip())

    after = await _await_view(drv, typed)
    assert after is not None
    return after


async def _finish_multi(drv: KeyDriver, ask: Ask, index: int, view: WidgetView) -> None:
    """«Submit» on a multi-choice question: on to the next one or the review."""
    q = ask.questions[index]
    view = await _move_cursor(drv, q, view, SUBMIT_ROW)
    await _press(drv, [_key("Enter")])
    await _await_view(drv, _left_question(ask, index))


async def _answer_multi(
    drv: KeyDriver, ask: Ask, index: int, view: WidgetView, choice: Choice
) -> None:
    q = ask.questions[index]
    view = await _unfocus(drv, view)
    for n in range(1, len(q.options) + 1):
        row = view.option(n)
        if row is None or row.checked is None:
            raise AnswerError("moved")
        if row.checked != (n in choice.options):
            view = await _toggle(drv, view, n)
    text_row = view.option(q.text_row)
    if choice.text:
        view = await _type_into_text_row(drv, q, view, choice.text)
    elif text_row is not None and text_row.checked:
        view = await _toggle(drv, view, q.text_row)
    await _finish_multi(drv, ask, index, view)


def _expected_parts(q: AskQuestion, choice: Choice) -> list[str]:
    parts = [q.options[n - 1].label for n in sorted(choice.options)]
    if choice.text:
        parts.append(choice.text)
    return parts


def _review_matches(ask: Ask, view: WidgetView, answers: dict[int, Choice]) -> bool:
    for index, choice in answers.items():
        q = ask.questions[index]
        listed = next(
            (a for asked, a in view.answers if same_text(asked, q.question)), None
        )
        if listed is None:
            return False
        flat = " ".join(listed.split())
        if not all(
            " ".join(p.split())[:30] in flat for p in _expected_parts(q, choice)
        ):
            return False
    return True


async def _submit_review(
    drv: KeyDriver, ask: Ask, answers: dict[int, Choice] | None
) -> None:
    view = await _view(drv)
    if view is None:
        return  # a lone single-choice question closes on its pick
    view = _ours(ask, view)
    for _ in range(len(ask.questions)):
        if view.review:
            break
        view = await _unfocus(drv, view)
        before = ask.index_of(view)
        await _press(drv, [_key("Right")])
        moved = await _await_view(
            drv, lambda w: w is None or w.review or ask.index_of(w) != before
        )
        if moved is None:
            return
        view = _ours(ask, moved)
    if not view.review:
        raise AnswerError("moved")
    submit = view.option(1)
    if submit is None or not submit.label.startswith("Submit"):
        raise AnswerError("moved")
    if answers is not None and not _review_matches(ask, view, answers):
        raise AnswerError("moved")
    await _press(drv, [_lit("1")])
    await _await_view(drv, lambda w: w is None)


# --- entry points ----------------------------------------------------------------


async def _drive(binding: str, script: Callable[[KeyDriver], Awaitable[None]]) -> None:
    try:
        await session_manager.drive_keys(binding, script)
    except KeysError as e:
        raise AnswerError(e.code) from e


async def answer_all(binding: str, ask_id: str, raw: dict[int | None, Choice]) -> None:
    """Answer every question of the pending ask and submit. Raises AnswerError."""

    async def script(drv: KeyDriver) -> None:
        ask, _ = await _start(drv, binding, ask_id)
        answers = check_answers(ask, raw)
        for index in range(len(ask.questions)):
            view = await _goto(drv, ask, index)
            if ask.questions[index].multi:
                await _answer_multi(drv, ask, index, view, answers[index])
            else:
                await _pick_single(drv, ask, index, view, answers[index])
        await _submit_review(drv, ask, answers)

    await _drive(binding, script)


async def pick(binding: str, ask_id: str, index: int, n: int) -> None:
    """One option of question ``index``: chosen (single) or toggled (multi)."""

    async def script(drv: KeyDriver) -> None:
        ask, _ = await _start(drv, binding, ask_id)
        if not 0 <= index < len(ask.questions):
            raise AnswerError("bad-answer")
        q = ask.questions[index]
        if not 1 <= n <= len(q.options):
            raise AnswerError("bad-answer")
        view = await _goto(drv, ask, index)
        if q.multi:
            await _toggle(drv, await _unfocus(drv, view), n)
        else:
            await _pick_single(drv, ask, index, view, Choice((n,)))

    await _drive(binding, script)


async def finish(binding: str, ask_id: str, index: int) -> None:
    """Done ticking multi-choice question ``index``: on to the next or the review."""

    async def script(drv: KeyDriver) -> None:
        ask, _ = await _start(drv, binding, ask_id)
        if not 0 <= index < len(ask.questions) or not ask.questions[index].multi:
            raise AnswerError("bad-answer")
        view = await _goto(drv, ask, index)
        await _finish_multi(drv, ask, index, view)

    await _drive(binding, script)


async def submit(binding: str, ask_id: str) -> None:
    """Submit from the review screen."""

    async def script(drv: KeyDriver) -> None:
        ask, view = await _start(drv, binding, ask_id)
        if not view.review:
            raise AnswerError("moved")
        await _submit_review(drv, ask, None)

    await _drive(binding, script)


async def type_answer(binding: str, text: str) -> bool:
    """A message as the own-text answer to the question on screen.

    False when there is no recorded question on screen (the caller falls back
    to its older routing); raises AnswerError when there is one but the text
    can't go in (the review screen, a draft already in the text row…).
    """
    text = clean_text(text)
    if not text:
        return False
    handled = False

    async def script(drv: KeyDriver) -> None:
        nonlocal handled
        ask = load_ask(binding)
        view = await _view(drv)
        if ask is None or view is None:
            return
        if view.review:
            try:
                _ours(ask, view)
            except AnswerError:
                return
            handled = True
            raise AnswerError("review")
        index = ask.index_of(view)
        if index is None:
            return
        handled = True
        q = ask.questions[index]
        if q.multi:
            await _type_into_text_row(drv, q, await _unfocus(drv, view), text)
        else:
            await _pick_single(drv, ask, index, view, Choice(text=text))

    await _drive(binding, script)
    return handled


async def answer_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/answer <id> <answers> — answer the agent's pending question (AskUserQuestion).

    Meant for apps (the pending question and its id come with the agent's
    status, docs/agent-status-format.md#ask); in Telegram the buttons under
    the question's photo do the same one tap at a time. Silent on success:
    the photo goes away and the agent carries on.
    """
    del context
    user = effective_user(update)
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    thread_id = get_thread_id(update)
    if thread_id is None:
        await safe_reply(update.message, tr("common.topics_only"))
        return
    binding = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not binding:
        await safe_reply(update.message, tr("ask.err.no-question"))
        return
    parts = (update.message.text or "").split(None, 1)
    try:
        ask_id, raw = parse_answer(parts[1] if len(parts) > 1 else "")
        await answer_all(binding, ask_id, raw)
    except AnswerError as e:
        logger.info("/answer refused (%s) for %s", e.code, binding)
        await safe_reply(update.message, error_text(e.code))
        return
    logger.info("/answer: answered the pending question of %s", binding)


# --- buttons under the question's photo ---------------------------------------

TOKEN_LEN = 10  # how much of the tool_use_id a button carries
_BUTTON_CHARS = 48


def _button_label(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= _BUTTON_CHARS else text[: _BUTTON_CHARS - 1] + "…"


def ask_buttons(ask: Ask, view: WidgetView) -> list[list[InlineKeyboardButton]]:
    """Answer rows for the photo: the options of the question on screen.

    A single choice answers on tap; a checkbox toggles and «Next» moves on;
    the review screen gets «Submit». Own text is a message (type_answer).
    """
    tok = ask.id[-TOKEN_LEN:]
    if view.review:
        return [
            [
                InlineKeyboardButton(
                    tr("ask.btn.submit"), callback_data=f"{CB_ASK_SUBMIT}{tok}"
                )
            ]
        ]
    index = ask.index_of(view)
    if index is None:
        return []
    q = ask.questions[index]
    rows: list[list[InlineKeyboardButton]] = []
    for n, opt in enumerate(q.options, 1):
        if q.multi:
            row = view.option(n)
            label = f"{'☑️' if row and row.checked else '⬜'} {opt.label}"
        else:
            label = f"{n}. {opt.label}"
        rows.append(
            [
                InlineKeyboardButton(
                    _button_label(label),
                    callback_data=f"{CB_ASK_PICK}{index}:{n}:{tok}",
                )
            ]
        )
    if q.multi:
        rows.append(
            [
                InlineKeyboardButton(
                    tr("ask.btn.next"), callback_data=f"{CB_ASK_NEXT}{index}:{tok}"
                )
            ]
        )
    return rows


def parse_button(data: str) -> tuple[str, list[int], str] | None:
    """(action, numbers, tok) of an answer button's callback data, else None."""
    for prefix, action, count in (
        (CB_ASK_PICK, "pick", 2),
        (CB_ASK_NEXT, "next", 1),
        (CB_ASK_SUBMIT, "submit", 0),
    ):
        if not data.startswith(prefix):
            continue
        parts = data[len(prefix) :].split(":")
        if len(parts) != count + 1 or not all(p.isdigit() for p in parts[:count]):
            return None
        return action, [int(p) for p in parts[:count]], parts[count]
    return None


async def press_button(binding: str, data: str) -> str | None:
    """Do what an answer button says; None when done, else an error code."""
    parsed = parse_button(data)
    if parsed is None:
        return "bad-answer"
    action, nums, tok = parsed
    ask = load_ask(binding)
    if ask is None or not tok or not ask.id.endswith(tok):
        return "no-question"
    try:
        if action == "pick":
            await pick(binding, ask.id, nums[0], nums[1])
        elif action == "next":
            await finish(binding, ask.id, nums[0])
        else:
            await submit(binding, ask.id)
    except AnswerError as e:
        logger.info("Answer button refused (%s) for %s", e.code, binding)
        return e.code
    return None


def error_text(code: str) -> str:
    """The user-facing line for an AnswerError code."""
    known = {
        "bad-answer",
        "unanswered",
        "no-question",
        "moved",
        "draft",
        "review",
        "busy",
        "not-running",
    }
    return tr(f"ask.err.{code}" if code in known else "ask.err.unavailable")
