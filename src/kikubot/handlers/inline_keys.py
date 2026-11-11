"""Inline query ``keys`` — press keys in a topic's agent, answer with its screen.

``keys <chat_id>:<thread_id> <seq> <token>… .`` lets a client that draws the
terminal itself (the ``screen`` query) type into it in one round trip: the
tokens are checked (terminal_keys.py), sent in order under the pane's send
lock (``SessionManager.send_key_tokens``), and the answer is the ``screen``
answer of the pane right after, with ``seq``/``sent``/``error`` in its meta.

``seq`` must grow with every query for that topic and user. A new seq is
recorded before its keys are sent, and its outcome (tokens sent, error) is
kept: a repeat of it answers with that outcome (``repeat``), waiting while
the keys are still going in, so a client that gave up on an answer learns
what happened instead of guessing. An older seq, or one from before a bot
restart, sends nothing (``stale``); the last seq per topic survives a restart
(``keys_seq.json``) so a query that waited it out can't be typed twice.
``t=<unix ms>`` after the seq dates the query: keys older than
MAX_KEY_AGE_MS are refused (``expired``), as the user was already told they
weren't sent. Topic resolution and the ALLOWED_USERS gate are
inline_status.py's, as for ``screen``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from ..pane_screen import PaneScreen
from ..session import session_manager
from ..terminal_keys import KeysError, KeyToken, parse_tokens
from ..utils import kikubot_dir, schedule_async_json_write

logger = logging.getLogger(__name__)

_SEQ_FILE = "keys_seq.json"

_KEYS = re.compile(
    r"^\s*keys\s+(-?\d+):(\d+)\s+(\d{1,18})\s+(\S.*?)\s*$", re.IGNORECASE
)

# After the keys: poll for the redraw, settle briefly once it starts, and
# answer at the cap anyway (the client's next poll catches a late redraw).
CHANGE_WAIT = 0.3
_POLL = 0.025
_SETTLE = 0.05

# A seq is a client counter started from a millisecond timestamp; one more
# than a day ahead of the clock is a bug, not a counter.
MAX_SEQ_AHEAD_MS = 86_400_000
# Keys the client sent (t=) longer ago than this were already reported to the
# user as not sent; typing them now would double what he presses again.
MAX_KEY_AGE_MS = 8_000
# Recent seq outcomes kept per topic, and how long a repeat waits for one
# still being sent (a send can wait 2 s for the lock and 1 s of settle).
MAX_OUTCOMES = 64
REPEAT_WAIT_SEC = 6.0

_last_seq: dict[tuple[int, int, int], int] | None = None  # loaded on first use


def _seqs() -> dict[tuple[int, int, int], int]:
    global _last_seq
    if _last_seq is None:
        _last_seq = {}
        try:
            raw = json.loads((kikubot_dir() / _SEQ_FILE).read_text())
            items = raw.items()
        except FileNotFoundError:
            items = []
        except (OSError, ValueError, AttributeError) as e:
            logger.warning("keys: unreadable %s, starting empty: %s", _SEQ_FILE, e)
            items = []
        for key, seq in items:
            try:
                uid, chat, thread = key.split(":")
                _last_seq[(int(uid), int(chat), int(thread))] = int(seq)
            except (ValueError, TypeError, AttributeError):
                continue  # a row in an older shape: that topic starts over
    return _last_seq


def _remember_seq(topic: tuple[int, int, int], seq: int) -> None:
    seqs = _seqs()
    seqs[topic] = seq
    schedule_async_json_write(
        kikubot_dir() / _SEQ_FILE,
        {f"{u}:{c}:{t}": s for (u, c, t), s in seqs.items()},
    )


def parse_keys_query(query: str) -> tuple[int, int, int, list[str]] | None:
    """(chat_id, thread_id, seq, raw tokens) of a ``keys`` query, else None."""
    m = _KEYS.match(query)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4).split()


def _differs(a: PaneScreen | None, b: PaneScreen | None) -> bool:
    if a is None or b is None:
        return a is not b
    return (a.text, a.cursor) != (b.text, b.cursor)


async def _wait_change(
    binding: str, before: PaneScreen | None
) -> tuple[PaneScreen | None, bool]:
    """The screen after the keys, and whether it changed within CHANGE_WAIT."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CHANGE_WAIT
    latest = before
    while loop.time() < deadline:
        await asyncio.sleep(_POLL)
        cur = await session_manager.capture_screen(binding)
        if cur is None:
            return latest, False
        latest = cur
        if _differs(cur, before):
            await asyncio.sleep(_SETTLE)
            return await session_manager.capture_screen(binding) or cur, True
    return latest, False


@dataclass
class _Outcome:
    """What a seq's keys did; ``done`` is set once the send is over."""

    done: asyncio.Event = field(default_factory=asyncio.Event)
    sent: int = 0
    error: str | None = None


# Per topic, the outcomes of its recent seqs (in memory: a bot restart forgets
# them, and a repeat of a seq from before it answers `stale`).
_outcomes: dict[tuple[int, int, int], OrderedDict[int, _Outcome]] = {}


def _parse_sent_at(raw_tokens: list[str]) -> tuple[int | None, list[str]]:
    """Split an optional leading ``t=<unix ms>`` off the tokens."""
    if raw_tokens and raw_tokens[0].lower().startswith("t="):
        try:
            return int(raw_tokens[0][2:]), raw_tokens[1:]
        except ValueError:
            raise KeysError("bad-key") from None
    return None, raw_tokens


async def run_keys(
    user_id: int,
    chat_id: int,
    thread_id: int,
    binding: str,
    seq: int,
    raw_tokens: list[str],
) -> tuple[PaneScreen | None, dict[str, object]]:
    """Check and send the keys; the screen after them and the meta extras.

    A repeat of a seq answers with what that seq's keys did (``repeat``),
    waiting for them if they're still being sent: a client that gave up on
    the first answer learns the truth instead of guessing. Keys that reach
    the bot more than MAX_KEY_AGE_MS after the client sent them (``t=``) are
    refused (``expired``): the user was already told they weren't sent.
    """
    topic = (user_id, chat_id, thread_id)
    known = _outcomes.get(topic, OrderedDict()).get(seq)
    if known is not None:
        return await _repeat(binding, seq, known)

    from .agent_restart import is_restarting

    error: str | None = None
    tokens: list[KeyToken] = []
    outcome: _Outcome | None = None
    now_ms = time.time() * 1000
    if seq > now_ms + MAX_SEQ_AHEAD_MS:
        # A seq this far ahead would leave every later query `stale` for
        # good; a client bug, refused without being recorded.
        error = "bad-key"
    elif seq <= _seqs().get(topic, -1):
        error = "stale"
    else:
        _remember_seq(topic, seq)
        outcome = _Outcome()
        outs = _outcomes.setdefault(topic, OrderedDict())
        outs[seq] = outcome
        while len(outs) > MAX_OUTCOMES:
            outs.popitem(last=False)
        try:
            sent_at, raw_tokens = _parse_sent_at(raw_tokens)
            if sent_at is not None and now_ms - sent_at > MAX_KEY_AGE_MS:
                error = "expired"
            else:
                tokens = parse_tokens(raw_tokens)
        except KeysError as e:
            error = e.code
    if error is None and is_restarting(binding):
        error = "not-running"

    t0 = time.perf_counter()
    sent = 0
    finished = False
    try:
        before = await session_manager.capture_screen(binding)
        changed = False
        if error is None:
            sent, error = await session_manager.send_key_tokens(binding, tokens)
        screen = before
        if sent:
            screen, changed = await _wait_change(binding, before)
        finished = True
    finally:
        if outcome is not None:
            # Raised midway: how many keys went in is unknown, so a repeat
            # must not hear "nothing sent, no error".
            if not finished and error is None:
                error = "unavailable"
            outcome.sent, outcome.error = sent, error
            outcome.done.set()
    logger.info(
        "keys: %s seq=%d tokens=%d sent=%d error=%s %.0fms",
        binding,
        seq,
        len(tokens),
        sent,
        error,
        (time.perf_counter() - t0) * 1000,
    )
    extra: dict[str, object] = {"seq": seq, "sent": sent, "changed": changed}
    if error:
        extra["error"] = error
    return screen, extra


async def _repeat(
    binding: str, seq: int, outcome: _Outcome
) -> tuple[PaneScreen | None, dict[str, object]]:
    """Answer a repeated seq with its keys' outcome and the current screen."""
    try:
        await asyncio.wait_for(outcome.done.wait(), REPEAT_WAIT_SEC)
    except TimeoutError:
        screen = await session_manager.capture_screen(binding)
        return screen, {"seq": seq, "sent": 0, "repeat": True, "error": "in-progress"}
    screen = await session_manager.capture_screen(binding)
    extra: dict[str, object] = {"seq": seq, "sent": outcome.sent, "repeat": True}
    if outcome.error:
        extra["error"] = outcome.error
    logger.info("keys: %s seq=%d repeat of sent=%d", binding, seq, outcome.sent)
    return screen, extra
