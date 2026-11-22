"""An agent's AskUserQuestion as data: the hook's record, and the widget on screen.

Claude Code keeps an asked question out of the transcript until it's answered,
so the pane used to be the only source and a client had to guess. Now:

- **The record.** A PreToolUse hook on ``AskUserQuestion`` writes
  ``asks/<session_id>.json`` — ``{"id": <tool_use_id>, "questions": [...]}``,
  the tool's own input — and PostToolUse removes it once answered. On the host
  the hook is ``kikubot hook``, registered in the ``--settings`` file the bot
  launches its Claude windows with (``agent_status.write_agent_settings``); a
  container runs its own (docs/docker-agents.md) into the directory beside its
  session map. A container writes that file, so every record is read as
  untrusted input: size, shape and counts are checked, or there is no record.
- **The gate.** Esc and a declined question fire no hook at all, so a record
  alone proves nothing. ``current_ask`` hands one out only while the pane
  shows the widget and the question on screen is one of the record's.
- **The view.** ``parse_view`` reads the live widget: which question is up
  (or the review screen before submitting), its options with their
  checkboxes, the cursor, and whether the free-text row has focus — what
  ``handlers/ask_answer`` checks before and after every key it presses.

Widget layout (Claude Code 2.1.329): an optional tab bar (``← ☐ Q1  ☒ Q2  ✔
Submit →``; a lone single-choice question shows just `` ☐ Header``), the
question, numbered options (``[ ]``/``[✔]`` when several may be chosen), then
``N+1. Type something`` — free text — and for multi-choice a ``Submit`` row;
``N+2. Chat about this`` sits under a separator. A digit picks option N (a
single choice moves on at once, a checkbox toggles); the review screen lists
the answers above ``1. Submit answers``.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

ASK_DIR = "asks"
MAX_RECORD_BYTES = 64 * 1024
# AskUserQuestion takes 1–4 questions of 2–4 options; the caps leave room
# without letting a container hand the bot something unbounded. Options stop
# at 8 so the free-text row (N+1) still has a digit of its own.
MAX_QUESTIONS = 8
MAX_OPTIONS = 8
MAX_QUESTION_CHARS = 2000
MAX_HEADER_CHARS = 100
MAX_LABEL_CHARS = 300
MAX_DESCRIPTION_CHARS = 1000

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_SESSION_RE = re.compile(r"^[0-9A-Fa-f-]{8,64}$")


@dataclass(frozen=True)
class AskOption:
    label: str
    description: str = ""


@dataclass(frozen=True)
class AskQuestion:
    question: str
    header: str
    options: tuple[AskOption, ...]
    multi: bool

    @property
    def text_row(self) -> int:
        """The number of the free-text row («Type something»)."""
        return len(self.options) + 1


@dataclass(frozen=True)
class Ask:
    """One pending AskUserQuestion call."""

    id: str
    questions: tuple[AskQuestion, ...]

    def to_json(self, *, descriptions: bool = True) -> dict:
        return {
            "id": self.id,
            "questions": [
                {
                    "question": q.question,
                    "header": q.header,
                    "multi": q.multi,
                    "options": [
                        {"label": o.label, "description": o.description}
                        if descriptions and o.description
                        else {"label": o.label}
                        for o in q.options
                    ],
                }
                for q in self.questions
            ],
        }

    def index_of(self, view: WidgetView) -> int | None:
        """0-based index of the question ``view`` shows, None if not one of ours."""
        if view.review:
            return None
        for i, q in enumerate(self.questions):
            if same_text(q.question, view.question):
                return i
        return None


def _text(value: object, cap: int) -> str | None:
    if not isinstance(value, str) or len(value) > cap:
        return None
    return value


def parse_record(data: object) -> Ask | None:
    """The Ask in a hook record, or None if it isn't a well-formed one."""
    if not isinstance(data, dict):
        return None
    ask_id = data.get("id")
    raw_questions = data.get("questions")
    if not isinstance(ask_id, str) or not _ID_RE.match(ask_id):
        return None
    if not isinstance(raw_questions, list) or not (
        1 <= len(raw_questions) <= MAX_QUESTIONS
    ):
        return None
    questions: list[AskQuestion] = []
    for rq in raw_questions:
        if not isinstance(rq, dict):
            return None
        text = _text(rq.get("question"), MAX_QUESTION_CHARS)
        header = _text(rq.get("header", ""), MAX_HEADER_CHARS)
        raw_options = rq.get("options")
        if not text or not text.strip() or header is None:
            return None
        if not isinstance(raw_options, list) or not (
            1 <= len(raw_options) <= MAX_OPTIONS
        ):
            return None
        options: list[AskOption] = []
        for ro in raw_options:
            if not isinstance(ro, dict):
                return None
            label = _text(ro.get("label"), MAX_LABEL_CHARS)
            desc = _text(ro.get("description", ""), MAX_DESCRIPTION_CHARS)
            if not label or not label.strip() or desc is None:
                return None
            options.append(AskOption(label, desc))
        questions.append(
            AskQuestion(text, header, tuple(options), rq.get("multiSelect") is True)
        )
    return Ask(ask_id, tuple(questions))


def record_path(directory: Path, session_id: str) -> Path | None:
    """``<directory>/asks/<session_id>.json``, None for a malformed session id."""
    if not _SESSION_RE.match(session_id):
        return None
    return directory / ASK_DIR / f"{session_id}.json"


def load_record(path: Path) -> Ask | None:
    """Read and check one record; None when absent, oversized or malformed.

    A container writes this directory, so only a regular file is read — no
    FIFO (a plain open would block the event loop, and every topic with it),
    no device, no symlink, neither for the file nor for ``asks/`` itself.
    """
    from .utils import regular_nofollow_opener

    if path.parent.is_symlink():
        return None
    try:
        with open(path, "rb", opener=regular_nofollow_opener) as fh:
            raw = fh.read(MAX_RECORD_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_RECORD_BYTES:
        logger.warning("Ignoring oversized ask record %s", path)
        return None
    try:
        return parse_record(json.loads(raw))
    except (ValueError, UnicodeDecodeError):
        return None


def same_text(a: str, b: str) -> bool:
    """Whether two question texts match once the pane's wrapping is undone."""
    na, nb = " ".join(a.split()), " ".join(b.split())
    if not na or not nb:
        return False
    if na == nb:
        return True
    # The pane may cut a very long question; a long shared start is enough.
    n = min(len(na), len(nb))
    return n >= 60 and na[:n] == nb[:n]


# --- the widget on screen ----------------------------------------------------

SUBMIT_ROW = 0  # the cursor sits on a multi-choice question's «Submit» row

_TAB_BAR_RE = re.compile(r"^\s*(?:←\s+)?[☐☒✔]\s+\S")
_SEPARATOR_RE = re.compile(r"^\s*[─━]{6,}\s*$")
_OPTION_RE = re.compile(r"^(\s*)(❯)?\s*(\d+)\.\s+(?:\[([ ✔✓xX])\]\s*)?(.*?)\s*$")
_SUBMIT_RE = re.compile(r"^\s*(❯)?\s+Submit\s*$")
_FOOTER_RE = re.compile(r"^\s*Enter to (?:select|confirm)\b")
_REVIEW_RE = re.compile(r"^\s*Review your answers\s*$")
_REVIEW_Q_RE = re.compile(r"^\s*●\s+(.*?)\s*$")
_REVIEW_A_RE = re.compile(r"^\s*→\s+(.*?)\s*$")
_CHAT_ABOUT_RE = re.compile(r"Chat about this\s*$")


@dataclass(frozen=True)
class ViewOption:
    n: int
    label: str
    checked: bool | None  # None: a single-choice row (no checkbox)


@dataclass
class WidgetView:
    """What the AskUserQuestion widget shows right now."""

    review: bool = False
    question: str = ""
    options: list[ViewOption] = field(default_factory=list)
    cursor: int | None = None  # option number, SUBMIT_ROW, or None
    text_focus: bool = False  # the cursor is on the free-text row
    # Review screen: (question, answer) pairs as listed.
    answers: list[tuple[str, str]] = field(default_factory=list)

    def option(self, n: int) -> ViewOption | None:
        return next((o for o in self.options if o.n == n), None)


def parse_view(pane_text: str) -> WidgetView | None:
    """The live AskUserQuestion widget in a plain pane capture, else None.

    Anchors on the LAST tab bar in the pane, so a widget quoted higher up in
    the transcript is never read as the live one.
    """
    lines = pane_text.split("\n")
    start = None
    for i in range(len(lines) - 1, -1, -1):
        if _TAB_BAR_RE.match(lines[i]):
            start = i
            break
    if start is None:
        return None
    body = lines[start + 1 :]
    view = WidgetView()

    if any(_REVIEW_RE.match(ln) for ln in body[:3]):
        view.review = True
        question = None
        for ln in body:
            if _OPTION_RE.match(ln):
                m = _OPTION_RE.match(ln)
                assert m is not None
                n = int(m.group(3))
                view.options.append(ViewOption(n, m.group(5), None))
                if m.group(2):
                    view.cursor = n
                continue
            mq = _REVIEW_Q_RE.match(ln)
            if mq:
                question = mq.group(1)
                continue
            ma = _REVIEW_A_RE.match(ln)
            if ma and question is not None:
                view.answers.append((question, ma.group(1)))
                question = None
        return view

    question_lines: list[str] = []
    in_options = False
    for ln in body:
        if _SEPARATOR_RE.match(ln) or _FOOTER_RE.match(ln) or _CHAT_ABOUT_RE.search(ln):
            if in_options:
                break
            continue
        m = _OPTION_RE.match(ln)
        if m:
            in_options = True
            n = int(m.group(3))
            box = m.group(4)
            checked = None if box is None else box != " "
            view.options.append(ViewOption(n, m.group(5), checked))
            if m.group(2):
                view.cursor = n
            continue
        sm = _SUBMIT_RE.match(ln)
        if sm and in_options:
            if sm.group(1):
                view.cursor = SUBMIT_ROW
            continue
        if not in_options and ln.strip():
            question_lines.append(ln.strip())
    view.question = " ".join(question_lines)
    if not view.options or not view.question:
        return None
    # The free-text row is the last numbered one; with the cursor on it,
    # keys are typed into it. (The footer's «ctrl+g to edit» hint stays up
    # after the cursor leaves that row, so it can't tell.)
    view.text_focus = view.cursor is not None and view.cursor == view.options[-1].n
    return view


# --- the gate ----------------------------------------------------------------


def record_dir(binding: str) -> Path | None:
    """Where the records for ``binding``'s agent live, None if nowhere known."""
    from .config import config
    from .session import session_manager

    if binding.startswith("@"):
        return config.config_dir
    target = session_manager.resolve_docker_target(binding)
    if target is None:
        return None
    return target.agent.session_map_path.parent


def load_ask(binding: str) -> Ask | None:
    """The record for ``binding``'s current session, without the screen check."""
    from .session import session_manager

    ws = session_manager.window_states.get(binding)
    directory = record_dir(binding)
    if ws is None or not ws.session_id or directory is None:
        return None
    path = record_path(directory, ws.session_id)
    return load_record(path) if path else None


def current_ask(binding: str, pane_text: str | None) -> tuple[Ask, WidgetView] | None:
    """The pending question and its view, only while the pane shows that widget."""
    if not pane_text:
        return None
    from .terminal_parser import extract_interactive_content, strip_escapes

    plain = strip_escapes(pane_text)
    content = extract_interactive_content(plain)
    if content is None or content.name != "AskUserQuestion":
        return None
    view = parse_view(plain)
    if view is None:
        return None
    ask = load_ask(binding)
    if ask is None:
        return None
    if view.review:
        # The review lists the questions it is about: they must be ours.
        if not view.answers or not all(
            any(same_text(q.question, asked) for q in ask.questions)
            for asked, _ in view.answers
        ):
            return None
    elif ask.index_of(view) is None:
        return None
    return ask, view


def prune_records(directory: Path, max_age_sec: float = 7 * 86400) -> int:
    """Delete records nobody answered (an Esc leaves one behind). Returns count."""
    import time

    removed = 0
    try:
        paths = list((directory / ASK_DIR).glob("*.json"))
    except OSError:
        return 0
    now = time.time()
    for path in paths:
        try:
            if now - path.stat().st_mtime > max_age_sec:
                path.unlink()
                removed += 1
        except OSError:
            continue
    return removed


# --- for clients ---------------------------------------------------------------

_CUT_QUESTION = 300
ASK_KIND = "widget"  # a question the agent waits on in its terminal


def _where(ask: Ask, view: WidgetView) -> dict:
    index = ask.index_of(view)
    return {
        "id": ask.id,
        "kind": ASK_KIND,
        "step": 0 if view.review or index is None else index + 1,
    }


def ask_pointer(ask: Ask, view: WidgetView) -> dict:
    """What the status JSON says of a pending question: that it's there, no text.

    The status carries no message text (it may get sent into a chat by
    mistake); the question itself is one inline ``ask`` query away.
    """
    return {**_where(ask, view), "count": len(ask.questions)}


def ask_json(ask: Ask, view: WidgetView, level: int = 0) -> dict:
    """The pending question as the inline ``ask`` query gives it.

    docs/agent-status-format.md#ask. ``level`` makes it smaller for
    Telegram's size cap: 1 drops option descriptions, 2 also cuts long
    question texts.
    """
    body = ask.to_json(descriptions=level < 1)
    if level >= 2:
        for q in body["questions"]:
            if len(q["question"]) > _CUT_QUESTION:
                q["question"] = q["question"][: _CUT_QUESTION - 1] + "…"
    return {**_where(ask, view), "questions": body["questions"]}
