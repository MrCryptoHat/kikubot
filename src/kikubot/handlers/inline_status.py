"""Inline query ``status`` — the agent status card and JSON (agent_status.py).

Inert until the bot owner enables inline mode in @BotFather (/setinline):
Telegram sends no inline queries before that. Two shapes:

- ``status <chat_id>:<thread_id>`` — one topic's agent: an article ``status``
  (the readable card) and an article ``status.v1.json`` (the JSON a client such
  as a custom Telegram app reads without sending anything). The ids are a
  hint, never authorization: an inline query carries no chat, so the topic is
  looked up in the asking user's OWN bindings and its stored chat id must match.
- anything else (``@bot``, ``@bot status``, ``@bot <name>``) — one card per
  agent of the user, filtered by name, for official Telegram.

Only ``ALLOWED_USERS`` get results; everyone else gets an empty answer, cached
per user (``is_personal``: a shared cache would hand that emptiness to the
owner too). Answers come from caches, never from tmux.
"""

from __future__ import annotations

import logging
import re

from telegram import (
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.ext import Application, CommandHandler, ContextTypes

from ..agent_status import AgentStatus, build_status
from ..i18n import tr
from ..session import session_manager
from . import effective_user, is_user_allowed

logger = logging.getLogger(__name__)

_TARGETED = re.compile(r"^\s*status\s+(-?\d+):(\d+)\s*$", re.IGNORECASE)
MAX_CARDS = 20
_STRANGER_CACHE_SEC = 3600


def parse_target(query: str) -> tuple[int, int] | None:
    """(chat_id, thread_id) from ``status <chat_id>:<thread_id>``, else None."""
    m = _TARGETED.match(query)
    return (int(m.group(1)), int(m.group(2))) if m else None


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
