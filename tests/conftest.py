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


# The bot's tmux and docker singletons drive the machine they run on, and on a
# deployment that is where the live agents are: a test that forgets to patch
# a send would type into a real agent's pane (one did reach the live tmux
# server, 2026-11-11; the window it named didn't exist). So every test gets
# them pointed away: tmux at a private socket nothing serves, docker calls
# failing. A test that needs either patches them itself, as before.
_ISOLATED_TMUX_SOCKET = f"kikubot-test-isolated-{os.getpid()}"


@pytest.fixture(autouse=True)
def _no_live_tmux_or_docker(monkeypatch):
    from kikubot.docker_driver import docker_driver
    from kikubot.tmux_manager import tmux_manager

    monkeypatch.setattr(tmux_manager, "socket_name", _ISOLATED_TMUX_SOCKET)

    async def _no_docker(argv, **_kw):
        return 1, b"", b"docker is off in tests"

    monkeypatch.setattr(docker_driver, "_run", _no_docker)


@pytest.fixture(autouse=True, scope="session")
def _reap_isolated_tmux():
    yield
    import subprocess

    # A test that created a session on the private socket started a server.
    subprocess.run(
        ["tmux", "-L", _ISOLATED_TMUX_SOCKET, "kill-server"],
        capture_output=True,
        check=False,
    )
