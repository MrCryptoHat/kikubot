"""Inline queries ``status``, ``screen``, ``keys`` — the agent's status and terminal.

Inert until the bot owner enables inline mode in @BotFather (/setinline):
Telegram sends no inline queries before that. Shapes:

- ``status <chat_id>:<thread_id>`` — one topic's agent: an article ``status``
  (the readable card) and an article ``status.v1.json`` (the JSON a client such
  as a custom Telegram app reads without sending anything). The ids are a
  hint, never authorization: an inline query carries no chat, so the topic is
  looked up in the asking user's OWN bindings and its stored chat id must match.
- ``screen <chat_id>:<thread_id> [back=<N>]`` — that topic's terminal as text
  (pane_screen.py), for a client that draws it itself; same topic check.
- ``keys <chat_id>:<thread_id> <seq> <token>… .`` — press keys in that
  terminal and answer with its screen (inline_keys.py); same topic check.
- anything else (``@bot``, ``@bot status``, ``@bot <name>``) — one card per
  agent of the user, filtered by name, for official Telegram.

Only ``ALLOWED_USERS`` get results; everyone else gets an empty answer, cached
per user (``is_personal``: a shared cache would hand that emptiness to the
owner too). ``status`` answers come from caches, never from tmux; ``screen``
captures the pane (read-only, a few ms); ``keys`` is the only one that types.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextvars import ContextVar

from telegram import (
    InlineQuery,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, ContextTypes

from ..agent_status import AgentStatus, build_status
from ..i18n import tr
from ..pane_screen import MAX_BACK, MAX_PARTS, screen_messages
from ..session import session_manager
from . import effective_user, is_user_allowed, pane_cache
from .inline_keys import parse_keys_query, run_keys

logger = logging.getLogger(__name__)

_TARGETED = re.compile(r"^\s*status\s+(-?\d+):(\d+)\s*$", re.IGNORECASE)
_SCREEN = re.compile(r"^\s*screen\s+(-?\d+):(\d+)(?:\s+back=(\d+))?\s*$", re.IGNORECASE)
MAX_CARDS = 20
_STRANGER_CACHE_SEC = 3600
_SCREEN_RETRY_PARTS = 12
SLOW_ANSWER_SEC = 0.5


def parse_target(query: str) -> tuple[int, int] | None:
    """(chat_id, thread_id) from ``status <chat_id>:<thread_id>``, else None."""
    m = _TARGETED.match(query)
    return (int(m.group(1)), int(m.group(2))) if m else None


def parse_screen_target(query: str) -> tuple[int, int, int] | None:
    """(chat_id, thread_id, back) from ``screen <chat_id>:<thread_id> [back=N]``.

    ``back`` is clamped to ``MAX_BACK``; 0 means the visible screen only.
    """
    m = _SCREEN.match(query)
    if not m:
        return None
    back = min(int(m.group(3) or 0), MAX_BACK)
    return int(m.group(1)), int(m.group(2)), back


def _reserved_commands(app: Application | None) -> set[str]:
    """Every slash command the bot handles itself (they never reach an agent)."""
    names: set[str] = set()
    if app is None:
        return names
    for group in app.handlers.values():
        for h in group:
            if isinstance(h, CommandHandler):
                names |= set(h.commands)
    return names


def resolve_topic_binding(
    user_id: int, chat_id: int, thread_id: int
) -> tuple[int, str] | None:
    """(user_id, binding) of the user's OWN agent in that topic, else None.

    No topic-owner fallback (unlike messages): an inline query carries no
    chat, so that would let one allowed user read another's agent in a group
    they aren't in. The same person's second account is covered by aliases.
    """
    binding = session_manager.get_window_for_thread(user_id, thread_id)
    if binding is None:
        return None
    if session_manager.resolve_chat_id(user_id, thread_id) != chat_id:
        return None
    return user_id, binding


def _card_article(rid: str, st: AgentStatus) -> InlineQueryResultArticle:
    return InlineQueryResultArticle(
        id=rid,
        title=f"{st.name} · {tr(f'astatus.state.{st.state}') if st.state else ''}".rstrip(
            " ·"
        ),
        description=st.summary() or None,
        input_message_content=InputTextMessageContent(st.card()),
    )


# Time the last answerInlineQuery call took, per handler task (contextvars
# keep concurrent queries apart), so a slow answer's log line says where the
# time went: the call to Telegram, or the bot's own work before it.
_answer_took: ContextVar[float] = ContextVar("inline_answer_took", default=0.0)


async def _answer(query: InlineQuery, results: list, **kw: object) -> None:
    t = time.perf_counter()
    try:
        await query.answer(results, **kw)  # type: ignore[arg-type]
    finally:
        _answer_took.set(time.perf_counter() - t)


def _note_slow(kind: str, t0: float) -> None:
    """Log a ``screen``/``keys`` answer that took the bot over SLOW_ANSWER_SEC.

    A client polls ``screen`` every second, so only the slow ones are worth a
    line, split into the answerInlineQuery call and everything before it. A
    delay in Telegram's delivery of the query itself the bot can't see.
    """
    took = time.perf_counter() - t0
    if took > SLOW_ANSWER_SEC:
        call = _answer_took.get()
        logger.info(
            "inline %s answered slowly: %.0f ms (answerInlineQuery %.0f ms, before it %.0f ms)",
            kind,
            took * 1000,
            call * 1000,
            (took - call) * 1000,
        )


def _articles(messages: list[tuple[str, str]]) -> list[InlineQueryResultArticle]:
    return [
        InlineQueryResultArticle(
            id=rid, title=rid, input_message_content=InputTextMessageContent(text)
        )
        for rid, text in messages
    ]


async def _answer_keys(
    query: InlineQuery,
    user_id: int,
    chat_id: int,
    thread_id: int,
    seq: int,
    raw_tokens: list[str],
) -> None:
    """Answer ``keys`` (inline_keys.py); [] for a foreign topic, nothing sent."""
    found = resolve_topic_binding(user_id, chat_id, thread_id)
    if found is None:
        await query.answer([], cache_time=0, is_personal=True)
        return
    screen, extra = await run_keys(
        user_id, chat_id, thread_id, found[1], seq, raw_tokens
    )
    messages = screen_messages(
        screen,
        ts=int(time.time()),
        panel=pane_cache.latest_panel(user_id, thread_id),
        extra=extra,
    )
    await _answer(query, _articles(messages), cache_time=0, is_personal=True)


async def _answer_screen(
    query: InlineQuery, user_id: int, chat_id: int, thread_id: int, back: int
) -> None:
    """Answer ``screen``: meta, then the text chunks; [] for a foreign topic.

    A long history makes a large answer (up to 49 × 4 KB). Should Telegram
    refuse one that big, the same capture goes again in fewer parts, the
    oldest history lines dropped, and the refusal is logged.
    """
    found = resolve_topic_binding(user_id, chat_id, thread_id)
    if found is None:
        await query.answer([], cache_time=0, is_personal=True)
        return
    screen = await session_manager.capture_screen(found[1], back=back)
    ts = int(time.time())
    panel = pane_cache.latest_panel(user_id, thread_id)

    async def results(max_parts: int) -> list[InlineQueryResultArticle]:
        if back:
            # A long history takes tens of ms to escape and cut: off the loop.
            messages = await asyncio.to_thread(
                screen_messages, screen, ts=ts, panel=panel, max_parts=max_parts
            )
        else:
            messages = screen_messages(screen, ts=ts, panel=panel, max_parts=max_parts)
        return _articles(messages)

    full = await results(MAX_PARTS)
    try:
        await _answer(query, full, cache_time=0, is_personal=True)
    except BadRequest as e:
        reason = str(e).lower()
        # Only a size refusal is worth a smaller retry; an expired query
        # would just fail again.
        if (
            len(full) <= _SCREEN_RETRY_PARTS + 1
            or "too old" in reason
            or ("query id is invalid" in reason)
        ):
            raise
        logger.warning(
            "inline screen: %d parts refused (%s); retrying with %d",
            len(full) - 1,
            e,
            _SCREEN_RETRY_PARTS,
        )
        await query.answer(
            await results(_SCREEN_RETRY_PARTS), cache_time=0, is_personal=True
        )


async def inline_status_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    query = update.inline_query
    if query is None:
        return
    user = effective_user(update)
    if user is None or not is_user_allowed(user.id):
        await query.answer([], cache_time=_STRANGER_CACHE_SEC, is_personal=True)
        return
    t0 = time.perf_counter()
    screen_target = parse_screen_target(query.query)
    if screen_target is not None:
        await _answer_screen(query, user.id, *screen_target)
        _note_slow("screen", t0)
        return
    keys_target = parse_keys_query(query.query)
    if keys_target is not None:
        await _answer_keys(query, user.id, *keys_target)
        _note_slow("keys", t0)
        return

    reserved = _reserved_commands(context.application)

    target = parse_target(query.query)
    results: list[InlineQueryResultArticle] = []
    if target is not None:
        found = resolve_topic_binding(user.id, *target)
        if found is not None:
            st = build_status(found[1], reserved)
            results = [
                _card_article("status", st),
                InlineQueryResultArticle(
                    id="status.v1.json",
                    title="status.v1.json",
                    input_message_content=InputTextMessageContent(st.to_json()),
                ),
            ]
    else:
        needle = query.query.strip().lower()
        if needle == "status":
            needle = ""
        for uid, thread_id, binding in session_manager.iter_thread_bindings():
            if uid != user.id:
                continue
            name = session_manager.get_display_name(binding)
            if needle and needle not in name.lower():
                continue
            results.append(
                _card_article(f"status:{thread_id}", build_status(binding, reserved))
            )
            if len(results) >= MAX_CARDS:
                break
    await query.answer(results, cache_time=0, is_personal=True)
