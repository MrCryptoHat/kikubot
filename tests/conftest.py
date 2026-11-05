"""Root conftest — sets env vars BEFORE any kikubot module is imported.

The config.py module-level singleton requires TELEGRAM_BOT_TOKEN and
ALLOWED_USERS at import time, so these must be set before pytest
discovers any test that transitively imports kikubot.
"""

import os
import tempfile

import dotenv
import pytest

# Neutralize dotenv for the whole run, before kikubot.config binds the name.
#
# config loads the repo-root .env at import, so the suite used to inherit the
# operator's own deployment settings: a developer whose .env set, say,
# KIKUBOT_TOPIC_DIR_ROOTS or KIKUBOT_BROWSE_ROOT watched unrelated tests fail with
# assertions about defaults they never touched. Pinning the offending
# variables one at a time only fixes the ones already known — every new config
# knob re-opens the hole. Tests must see the documented defaults plus whatever
# they set explicitly, and nothing else.
_real_load_dotenv = dotenv.load_dotenv
dotenv.load_dotenv = lambda *args, **kwargs: False


@pytest.fixture
def real_dotenv(monkeypatch):
    """Restore genuine .env loading for the tests that exercise it.

    Patches the name inside kikubot.config, not dotenv: config does
    `from dotenv import load_dotenv`, so it holds its own reference bound at
    import time — patching the dotenv module afterwards would do nothing.
    """
    from kikubot import config as config_mod

    monkeypatch.setattr(config_mod, "load_dotenv", _real_load_dotenv)


# Force-set (not setdefault) to prevent real env vars from leaking into tests
os.environ["TELEGRAM_BOT_TOKEN"] = "test:0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
os.environ["ALLOWED_USERS"] = "12345"
os.environ["KIKUBOT_USER_ALIASES"] = ""
os.environ["KIKUBOT_DIR"] = tempfile.mkdtemp(prefix="kikubot-test-")
