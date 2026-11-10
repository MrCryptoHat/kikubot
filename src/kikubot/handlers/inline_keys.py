"""Inline query ``keys`` — press keys in a topic's agent, answer with its screen.

``keys <chat_id>:<thread_id> <seq> <token>… .`` lets a client that draws the
terminal itself (the ``screen`` query) type into it in one round trip: the
tokens are checked (terminal_keys.py), sent in order under the pane's send
lock (``SessionManager.send_key_tokens``), and the answer is the ``screen``
answer of the pane right after, with ``seq``/``sent``/``error`` in its meta.

``seq`` must grow with every query for that topic and user: an older or
repeated one sends nothing (``stale``), so a query Telegram delivers twice or
late can't press its keys again or out of order. The last ``seq`` per topic
survives a bot restart (``keys_seq.json``): a client resends an unanswered
query with the same ``seq``, and one that waited out the restart would
otherwise be typed a second time. Topic resolution and the ALLOWED_USERS gate
are inline_status.py's, as for ``screen``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

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


async def _wait_change(binding: str, before: PaneScreen | None) -> PaneScreen | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CHANGE_WAIT
    latest = before
    while loop.time() < deadline:
        await asyncio.sleep(_POLL)
        cur = await session_manager.capture_screen(binding)
        if cur is None:
            return latest
        latest = cur
        if before is None or (cur.text, cur.cursor) != (before.text, before.cursor):
            await asyncio.sleep(_SETTLE)
            return await session_manager.capture_screen(binding) or cur
    return latest


async def run_keys(
    user_id: int,
    chat_id: int,
    thread_id: int,
    binding: str,
    seq: int,
    raw_tokens: list[str],
) -> tuple[PaneScreen | None, dict[str, object]]:
    """Check and send the keys; the screen after them and the meta extras."""
    from .agent_restart import is_restarting

    error: str | None = None
    tokens: list[KeyToken] = []
    topic = (user_id, chat_id, thread_id)
    if seq > time.time() * 1000 + MAX_SEQ_AHEAD_MS:
        # A seq this far ahead would leave every later query `stale` for
        # good; a client bug, refused without being recorded.
        error = "bad-key"
    elif seq <= _seqs().get(topic, -1):
        error = "stale"
    else:
        _remember_seq(topic, seq)
        try:
            tokens = parse_tokens(raw_tokens)
        except KeysError as e:
            error = e.code
    if error is None and is_restarting(binding):
        error = "not-running"

    t0 = time.perf_counter()
    before = await session_manager.capture_screen(binding)
    sent = 0
    if error is None:
        sent, error = await session_manager.send_key_tokens(binding, tokens)
    screen = await _wait_change(binding, before) if sent else before
    logger.info(
        "keys: %s seq=%d tokens=%d sent=%d error=%s %.0fms",
        binding,
        seq,
        len(tokens),
        sent,
        error,
        (time.perf_counter() - t0) * 1000,
    )
    extra: dict[str, object] = {"seq": seq, "sent": sent}
    if error:
        extra["error"] = error
    return screen, extra
