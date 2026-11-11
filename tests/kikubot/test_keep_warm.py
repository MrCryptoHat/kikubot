"""The keep-warm loop: a getMe every WARM_SEC, and a failed one doesn't end it."""

import asyncio
from unittest.mock import AsyncMock

from kikubot import bot as bot_module


async def test_keep_warm_survives_a_failed_get_me(monkeypatch):
    monkeypatch.setattr(bot_module, "WARM_SEC", 0)
    calls = 0
    third = asyncio.Event()

    async def get_me():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("network blip")
        if calls == 3:
            third.set()

    fake = AsyncMock()
    fake.get_me = get_me
    task = asyncio.create_task(bot_module._keep_connection_warm(fake))
    await asyncio.wait_for(third.wait(), 1)
    task.cancel()
    assert calls >= 3
