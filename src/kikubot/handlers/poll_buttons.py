"""Buttons for a poll an agent writes as text: numbered questions, lettered options.

An agent that needs several answers at once writes them in one fixed shape
(an operator can give their agents this as a rule), and the answer is one
line, ``1A, 2BC, 3: own words``::

    1. Which color?
    - A. Red
    - B. Green
    2. Which pets? (several)
    - A. Cat
    - B. Dog
    3. Anything else?

Letters may be Latin or Cyrillic (А, Б, В…); the multi-choice mark is
«(можно несколько)», «(several)» or «(multiple)». A go-ahead the agent waits
for is written ``Say «word»`` (or ``Скажи «слово»``).

The bot leaves the text as it is (another client may read it too) and hangs
short buttons under the reply, a row per question («⭐1A» «1B»): a lone
single-choice question answers on one tap; otherwise «As advised» sends the
agent's advice whole, or taps tick options and «Send» delivers the line. A question
without options is answered by a message. The reply reaches the agent through
``delivery.deliver_user_text`` like a typed one.

Only the topic's newest poll is live, held in memory: its buttons come off
when the person writes there, when the agent writes again, or when a newer
poll replaces it; a tap on buttons that outlived that (a bot restart forgets
them all) sends nothing. What was sent stays under the reply as «✓ 1A, 2BC».
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from telegram import Bot, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from ..i18n import tr
from .callback_data import CB_POLL_ADVISED, CB_POLL_SEND, CB_POLL_TICK, CB_POLL_WORD

logger = logging.getLogger(__name__)

MAX_QUESTIONS = 9
MAX_OPTIONS = 10
MAX_WORDS = 4
_LABEL_CHARS = 44

_QUESTION_RE = re.compile(
    r"^\s*(?:\*\*)?(\d{1,2})\.\s+(.*?\?)(?:\*\*)?"
    r"\s*(\((?:можно несколько|several|multiple)[^)]*\))?\s*$",
    re.IGNORECASE,
)
_OPTION_RE = re.compile(r"^\s*[-•*]\s+([A-ZА-ЯЁ])[.)]\s+(.+?)\s*$")
# A go-ahead the agent waits for: Скажи «залей», Жду «ок» или «154», Say «go».
_VERB_RE = re.compile(
    r"\b(?:Скажи|Жду|Напиши|Ответь|Say|Reply)\s+(?=[«\"“])", re.IGNORECASE
)
_QUOTE_CLOSE = {"«": "»", "“": "”", '"': '"'}
# Between two words of one request: «ок» или «154», «да», «нет».
_JOINER_RE = re.compile(r"\s*(?:,|/|или|либо|or)\s*", re.IGNORECASE)
# The rule's own placeholder, never a real word.
_PLACEHOLDERS = frozenset({"слово", "слова", "word", "words"})
# «Первым в каждом пункте — мой совет»: the first option of each question is
# the agent's advice.
_ADVICE_RE = re.compile(
    r"^\s*(?:Первым в каждом пункте|First in each (?:item|question|point))",
    re.IGNORECASE,
)
_CODE_RE = re.compile(r"`([^`]*)`")


@dataclass(frozen=True)
class PollQuestion:
    number: int
    text: str
    options: tuple[tuple[str, str], ...]  # (letter, text)
    multi: bool


@dataclass
class Poll:
    questions: tuple[PollQuestion, ...] = ()
    words: tuple[str, ...] = ()
    advised: bool = False  # the first option of each question is the advice
    # question index -> letters ticked so far
    picked: dict[int, list[str]] = field(default_factory=dict)

    @property
    def one_tap(self) -> bool:
        """A lone single-choice question with options: a tap answers it."""
        return (
            len(self.questions) == 1
            and not self.questions[0].multi
            and bool(self.questions[0].options)
        )

    def advised_answer(self) -> str:
        """The line «As advised» sends: the advised option of each question."""
        if not self.advised:
            return ""
        return ", ".join(
            f"{q.number}{q.options[0][0]}" for q in self.questions if q.options
        )

    def answer(self) -> str:
        """The composed reply line: «1A, 2BC» (questions in their order)."""
        parts = []
        for i, q in enumerate(self.questions):
            letters = self.picked.get(i)
            if letters:
                order = [letter for letter, _ in q.options]
                parts.append(f"{q.number}{''.join(sorted(letters, key=order.index))}")
        return ", ".join(parts)


def _inside(prefix: str) -> bool:
    """Whether a line's ``prefix`` leaves a bracket or a quote open."""
    return (
        prefix.count("(") > prefix.count(")")
        or prefix.count("[") > prefix.count("]")
        or prefix.count("«") > prefix.count("»")
        or prefix.count("“") > prefix.count("”")
        or prefix.count('"') % 2 == 1
    )


def _quoted(line: str, start: int) -> tuple[str, int] | None:
    """(text, index after the close) of the quote opening at ``start``."""
    opener = line[start]
    close = _QUOTE_CLOSE[opener]
    depth = 0
    for i in range(start, len(line)):
        c = line[i]
        if c == close and (depth == 1 or opener == close) and i > start:
            return line[start + 1 : i], i + 1
        if c == opener and opener != close:
            depth += 1
        elif c == close:
            depth -= 1
    return None


def _go_ahead_words(line: str) -> list[str]:
    """The words a line asks for: «Скажи «лей»», «Жду «ок» или «154»».

    Only a request counts: a verb inside brackets or quotes is a mention
    («(если что, скажи «стоп»)», a quoted rule), and after the first word
    only words joined by «или», «or» or a comma follow it — a later aside
    («(или «стоп», если рано)») is not one. The rule's placeholder («слово»)
    and anything templated (<…>, […]) never is.
    """
    out: list[str] = []
    for m in _VERB_RE.finditer(line):
        if _inside(line[: m.start()]):
            continue
        pos = m.end()
        while pos < len(line) and line[pos] in _QUOTE_CLOSE:
            found = _quoted(line, pos)
            if found is None:
                break
            word, pos = found[0].strip(), found[1]
            if (
                word
                and len(word) <= 30
                and word.lower() not in _PLACEHOLDERS
                and not any(c in word for c in '<>[]{}«»“”"…')
            ):
                out.append(word)
            joiner = _JOINER_RE.match(line, pos)
            if joiner is None or joiner.end() == pos:
                break
            pos = joiner.end()
    return out


def _plain(text: str) -> str:
    return " ".join(_CODE_RE.sub(r"\1", text).replace("**", "").split())


def parse(text: str) -> Poll | None:
    """The poll in an agent's reply, or None when there is none to answer.

    A poll needs a question with two or more options, or a go-ahead word.
    Questions are numbered 1, 2, 3… in order; letters within a question are
    distinct. Anything off that shape is no poll — no buttons beat wrong ones.
    """
    questions: list[PollQuestion] = []
    current: dict | None = None

    def flush() -> None:
        if current is not None:
            questions.append(
                PollQuestion(
                    current["number"],
                    current["text"],
                    tuple(current["options"]),
                    current["multi"],
                )
            )

    in_fence = False
    advised = False
    words: list[str] = []
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if _ADVICE_RE.match(line):
            advised = True
        words += _go_ahead_words(line)
        mq = _QUESTION_RE.match(line)
        if mq:
            flush()
            current = {
                "number": int(mq.group(1)),
                "text": _plain(mq.group(2)),
                "options": [],
                "multi": bool(mq.group(3)),
            }
            continue
        mo = _OPTION_RE.match(line)
        if mo and current is not None:
            current["options"].append((mo.group(1), _plain(mo.group(2))))
            continue
        if line.strip() and current is not None and not current["options"]:
            continue  # a question wrapped onto a second line
        if line.strip():
            flush()
            current = None
    flush()

    good = [
        q
        for q in questions
        if len({letter for letter, _ in q.options}) == len(q.options)
    ]
    numbered = [q.number for q in good] == list(range(1, len(good) + 1))
    if not (
        numbered
        and len(good) == len(questions)
        and len(good) <= MAX_QUESTIONS
        and all(len(q.options) <= MAX_OPTIONS for q in good)
        and any(len(q.options) >= 2 for q in good)
    ):
        good = []
    found = tuple(dict.fromkeys(w.strip() for w in words if w.strip()))[:MAX_WORDS]
    if not good and not found:
        return None
    return Poll(tuple(good), found, advised and bool(good))


def _label(text: str) -> str:
    return text if len(text) <= _LABEL_CHARS else text[: _LABEL_CHARS - 1] + "…"


_PER_ROW = 5


def keyboard(poll: Poll) -> InlineKeyboardMarkup:
    """Short buttons, a row per question: «⭐1A» «1B» «1C», then the actions.

    The options' text is in the message right above, so a button carries only
    its number and letter: «⭐» marks the advice, «✅» a tick. Below: the
    go-ahead words, then «As advised» (every advised option in one tap) and
    «Send» with the line it would send. A lone single-choice question needs
    neither: its letter is the answer.
    """
    rows: list[list[InlineKeyboardButton]] = []
    lone = len(poll.questions) == 1
    for qi, q in enumerate(poll.questions):
        picked = poll.picked.get(qi, [])
        row: list[InlineKeyboardButton] = []
        for oi, (letter, _) in enumerate(q.options):
            head = letter if lone else f"{q.number}{letter}"
            if letter in picked:
                head = f"✅{head}"
            elif poll.advised and oi == 0:
                head = f"⭐{head}"
            row.append(
                InlineKeyboardButton(head, callback_data=f"{CB_POLL_TICK}{qi}:{oi}")
            )
            if len(row) == _PER_ROW:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
    if poll.words:
        rows.append(
            [
                InlineKeyboardButton(_label(w), callback_data=f"{CB_POLL_WORD}{i}")
                for i, w in enumerate(poll.words)
            ]
        )
    actions: list[InlineKeyboardButton] = []
    if poll.advised and poll.advised_answer() and not poll.one_tap:
        actions.append(
            InlineKeyboardButton(tr("poll.advised"), callback_data=CB_POLL_ADVISED)
        )
    if poll.questions and not poll.one_tap:
        answer = poll.answer()
        actions.append(
            InlineKeyboardButton(
                _label(
                    tr("poll.send", answer=answer) if answer else tr("poll.send_empty")
                ),
                callback_data=CB_POLL_SEND,
            )
        )
    if actions:
        rows.append(actions)
    return InlineKeyboardMarkup(rows)


# --- for clients (the inline ``ask`` query, kind "text") ----------------------

POLL_KIND = "text"
_CUT = 300


def _poll_id(message_id: int) -> str:
    return f"m{message_id}"


def poll_pointer(message_id: int, poll: Poll) -> dict:
    """What the status JSON says of an open poll: that it's there, no text."""
    return {
        "id": _poll_id(message_id),
        "kind": POLL_KIND,
        "message_id": message_id,
        "count": len(poll.questions),
    }


def poll_json(message_id: int, poll: Poll, level: int = 0) -> dict:
    """The open poll for the inline ``ask`` query (docs/agent-status-format.md#ask).

    ``level`` 1 cuts long texts for Telegram's size cap.
    """

    def cut(text: str) -> str:
        return text if level < 1 or len(text) <= _CUT else text[: _CUT - 1] + "…"

    return {
        **poll_pointer(message_id, poll),
        "questions": [
            {
                "number": q.number,
                "question": cut(q.text),
                "multi": q.multi,
                "advised": 0 if poll.advised and q.options else None,
                "options": [
                    {"letter": letter, "label": cut(t)} for letter, t in q.options
                ],
            }
            for q in poll.questions
        ],
        "replies": list(poll.words),
    }


def open_poll(user_id: int, thread_id: int | None) -> tuple[int, Poll] | None:
    """(message_id, poll) of the topic's open poll, if any."""
    held = _open.get((user_id, thread_id or 0))
    return (held[1], held[2]) if held else None


# --- the live poll per topic ----------------------------------------------------

# (user_id, thread_id_or_0) -> (chat_id, message_id, poll)
_open: dict[tuple[int, int], tuple[int, int, Poll]] = {}


async def _set_markup(
    bot: Bot, chat_id: int, message_id: int, markup: InlineKeyboardMarkup | None
) -> bool:
    try:
        await bot.edit_message_reply_markup(
            chat_id=chat_id, message_id=message_id, reply_markup=markup
        )
        return True
    except Exception as e:  # noqa: BLE001 — gone, unchanged or too old
        if "not modified" in str(e).lower():
            return True
        logger.debug("poll buttons: markup edit failed (%s): %s", message_id, e)
        return False


async def attach(
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    chat_id: int,
    message_id: int,
    poll: Poll,
) -> None:
    """Hang the poll's buttons under the agent's reply; it becomes the live poll."""
    await close(bot, user_id, thread_id)
    if await _set_markup(bot, chat_id, message_id, keyboard(poll)):
        _open[(user_id, thread_id or 0)] = (chat_id, message_id, poll)
        logger.info(
            "Poll buttons on msg %s (thread %s): %d question(s), %d word(s)",
            message_id,
            thread_id,
            len(poll.questions),
            len(poll.words),
        )


async def send_with_poll(
    bot: Bot, user_id: int, thread_id: int | None, chat_id: int, poll: Poll
) -> None:
    """The buttons on a message of their own (a voiced reply has no text)."""
    await close(bot, user_id, thread_id)
    try:
        sent = await bot.send_message(
            chat_id=chat_id,
            text=tr("poll.pick"),
            message_thread_id=thread_id,
            reply_markup=keyboard(poll),
            disable_notification=True,
        )
    except Exception as e:  # noqa: BLE001 — the reply itself is delivered
        logger.warning("poll buttons: send failed: %s", e)
        return
    _open[(user_id, thread_id or 0)] = (chat_id, sent.message_id, poll)


async def close(bot: Bot, user_id: int, thread_id: int | None) -> None:
    """Take the live poll's buttons off: the conversation moved on."""
    held = _open.pop((user_id, thread_id or 0), None)
    if held is not None:
        removed = await _set_markup(bot, held[0], held[1], None)
        logger.info(
            "Poll buttons off msg %s (thread %s)%s",
            held[1],
            thread_id,
            "" if removed else ": the edit failed",
        )


def is_open(user_id: int, thread_id: int | None) -> bool:
    return (user_id, thread_id or 0) in _open


async def _send(
    query: CallbackQuery,
    bot: Bot,
    user_id: int,
    thread_id: int | None,
    held: tuple[int, int, Poll],
    text: str,
) -> None:
    from ..session import session_manager
    from .delivery import deliver_user_text

    binding = session_manager.resolve_window_for_thread(user_id, thread_id)
    if not binding:
        await query.answer(tr("poll.stale"), show_alert=True)
        return
    status, _ = await deliver_user_text(user_id, thread_id, binding, text)
    if status not in ("sent", "routed"):
        await query.answer(tr("poll.not_sent"), show_alert=True)
        return
    _open.pop((user_id, thread_id or 0), None)
    logger.info("Poll answered by button on msg %s (thread %s)", held[1], thread_id)
    await query.answer(tr("ask.done"))
    await _set_markup(
        bot,
        held[0],
        held[1],
        InlineKeyboardMarkup(
            [[InlineKeyboardButton(_label(f"✓ {text}"), callback_data="noop")]]
        ),
    )


async def handle_tap(
    query: CallbackQuery, bot: Bot, user_id: int, thread_id: int | None
) -> None:
    """A tick, a go-ahead word or «Send» on the live poll."""
    message = query.message
    data = query.data or ""
    held = _open.get((user_id, thread_id or 0))
    if (
        message is None
        or held is None
        or held[:2] != (message.chat.id, message.message_id)
    ):
        await query.answer(tr("poll.stale"), show_alert=True)
        if message is not None:
            await _set_markup(bot, message.chat.id, message.message_id, None)
        return
    poll = held[2]
    if data.startswith(CB_POLL_WORD):
        idx = data[len(CB_POLL_WORD) :]
        if not idx.isdigit() or int(idx) >= len(poll.words):
            await query.answer()
            return
        await _send(query, bot, user_id, thread_id, held, poll.words[int(idx)])
        return
    if data == CB_POLL_ADVISED:
        if poll.advised_answer():
            await _send(query, bot, user_id, thread_id, held, poll.advised_answer())
        else:
            await query.answer()
        return
    if data == CB_POLL_SEND:
        answer = poll.answer()
        if not answer:
            await query.answer(tr("poll.nothing"), show_alert=True)
            return
        await _send(query, bot, user_id, thread_id, held, answer)
        return
    qi_s, _, oi_s = data[len(CB_POLL_TICK) :].partition(":")
    if not (qi_s.isdigit() and oi_s.isdigit()):
        await query.answer()
        return
    qi, oi = int(qi_s), int(oi_s)
    if qi >= len(poll.questions) or oi >= len(poll.questions[qi].options):
        await query.answer()
        return
    q = poll.questions[qi]
    letter = q.options[oi][0]
    picked = poll.picked.setdefault(qi, [])
    if letter in picked:
        picked.remove(letter)
    elif q.multi:
        picked.append(letter)
    else:
        picked[:] = [letter]
    if poll.one_tap:
        await _send(query, bot, user_id, thread_id, held, poll.answer())
        return
    await query.answer()
    await _set_markup(bot, held[0], held[1], keyboard(poll))
