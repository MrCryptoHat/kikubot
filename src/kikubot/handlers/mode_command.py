"""/mode — show or set the agent's permission mode (manual / accept edits / plan / auto).

Claude Code has no command for it: Shift+Tab cycles through the modes it
offers. So ``/mode plan`` presses Shift+Tab until the pane's footer reads the
target, giving up after a full cycle. The pane is re-read before every press:
with a dialog open (no input box) it does nothing — in a permission prompt
Shift+Tab means «yes, and accept edits», i.e. it would approve the tool. The
send lock is held for the whole sequence so a message can't land in between.
It is an ordinary command, so the agent
status sheet's mode picker (``"/mode {id}"``) and a
person in official Telegram reach the same thing.
"""

from __future__ import annotations

import asyncio

from telegram import Update
from telegram.ext import ContextTypes

from ..i18n import tr
from ..runtimes import get_runtime
from ..session import session_manager
from . import effective_user, get_thread_id, is_user_allowed
from .message_sender import safe_reply

# More presses than there are modes: a full cycle comes back to the start.
MAX_PRESSES = 6
PRESS_SETTLE_SEC = 0.35


def _label(mode: str | None) -> str:
    return tr(f"astatus.mode.{mode}") if mode else tr("mode.unknown")


async def mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = effective_user(update)
    if not user or not is_user_allowed(user.id) or not update.message:
        return
    thread_id = get_thread_id(update)
    binding = session_manager.resolve_window_for_thread(user.id, thread_id)
    if not binding:
        await safe_reply(update.message, tr("commands.no_session_in_topic"))
        return
    rt = get_runtime(session_manager.window_runtime(binding))
    if not rt.permission_modes:
        await safe_reply(update.message, tr("mode.unsupported"))
        return

    pane = await session_manager.capture_pane(binding)
    current = rt.permission_mode(pane) if pane else None
    target = (context.args[0] if context.args else "").lower().replace("-", "_")
    choices = list(rt.permission_modes)
    if target not in choices:
        await safe_reply(
            update.message,
            tr(
                "mode.usage",
                current=_label(current),
                options=" · ".join(f"/mode {m}" for m in choices),
            ),
        )
        return
    if current is None:
        # A dialog or a running shell owns the pane: Shift+Tab would land there.
        await safe_reply(update.message, tr("mode.unreadable"))
        return

    start = current
    async with session_manager.send_lock(binding):
        presses = 0
        while current != target and presses < MAX_PRESSES:
            if not await session_manager._send_keys_unlocked(
                binding, "BTab", enter=False, literal=False
            ):
                break
            presses += 1
            await asyncio.sleep(PRESS_SETTLE_SEC)
            pane = await session_manager.capture_pane(binding)
            current = rt.permission_mode(pane) if pane else None
            if current is None or current == start:
                break  # a dialog appeared, or a full cycle came back
    if current == target:
        await safe_reply(update.message, tr("mode.set", mode=_label(target)))
    else:
        await safe_reply(
            update.message,
            tr("mode.unreachable", mode=_label(target), current=_label(current)),
        )
