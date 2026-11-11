"""Tests for command handlers — /bind canonical-name binding, /start group hints.

get_docker_agent matches case-insensitively (phone keyboards capitalize
the first word), but every session_map key and route uses the canonical
lowercase binding value — binding the user's spelling silently kills
inbound delivery for the topic.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kikubot.config import DockerAgentConfig
from kikubot.handlers.commands import (
    bind_command,
    build_bot_commands,
    pending_menu_removal,
    start_command,
)
from kikubot.i18n import tr


def _agent(name: str = "assistant") -> DockerAgentConfig:
    return DockerAgentConfig(
        name=name,
        container=f"{name}-ctn",
        workspace_host_path=Path(f"/tmp/{name}/workspace"),
        claude_home_host_path=Path(f"/tmp/{name}/claude-home"),
        ipc_dir=Path(f"/tmp/{name}/ipc"),
        session_map_path=Path(f"/tmp/{name}/session-map.json"),
    )


class TestBindCanonicalName:
    @pytest.mark.asyncio
    async def test_bind_uses_canonical_agent_name(self):
        """/bind Assistant must bind docker:assistant, not docker:Assistant."""
        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        update.effective_chat.id = -100123
        context = MagicMock()
        context.args = ["Assistant"]

        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=42),
            patch("kikubot.handlers.commands.config") as mock_cfg,
            patch("kikubot.handlers.commands.session_manager") as mock_sm,
            patch("kikubot.handlers.commands.safe_reply", new=AsyncMock()),
        ):
            mock_cfg.docker_agents_enabled = True
            mock_cfg.get_docker_agent.return_value = _agent("assistant")

            await bind_command(update, context)

            mock_sm.bind_thread.assert_called_once_with(
                1, 42, "docker:assistant", window_name="assistant"
            )

    @pytest.mark.asyncio
    async def test_bind_unknown_agent_does_not_bind(self):
        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        context = MagicMock()
        context.args = ["nosuch"]

        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=42),
            patch("kikubot.handlers.commands.config") as mock_cfg,
            patch("kikubot.handlers.commands.session_manager") as mock_sm,
            patch("kikubot.handlers.commands.safe_reply", new=AsyncMock()),
        ):
            mock_cfg.docker_agents_enabled = True
            mock_cfg.get_docker_agent.return_value = None
            mock_cfg.docker_agents = []
            mock_sm.find_sub_agent_binding.return_value = None

            await bind_command(update, context)

            mock_sm.bind_thread.assert_not_called()

    def _sub_update(self, name: str):
        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        update.effective_chat.id = -100123
        context = MagicMock()
        context.args = [name]
        return update, context

    @pytest.mark.asyncio
    async def test_bind_known_sub_agent(self):
        """/bind assistant-notes reattaches docker:assistant/notes, keeping
        its kikubot-owned display name (no window_name override)."""
        update, context = self._sub_update("assistant-notes")
        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=42),
            patch("kikubot.handlers.commands.config") as mock_cfg,
            patch("kikubot.handlers.commands.session_manager") as mock_sm,
            patch("kikubot.handlers.commands.safe_reply", new=AsyncMock()),
        ):
            mock_cfg.docker_agents_enabled = True
            mock_cfg.get_docker_agent.return_value = None
            mock_sm.find_sub_agent_binding.return_value = "docker:assistant/notes"
            mock_sm.iter_thread_bindings.return_value = iter([])

            await bind_command(update, context)

            mock_sm.bind_thread.assert_called_once_with(1, 42, "docker:assistant/notes")

    @pytest.mark.asyncio
    async def test_bind_sub_agent_taken_by_other_topic(self):
        """One sub serves one topic — a second binding would mirror replies."""
        update, context = self._sub_update("assistant/notes")
        reply = AsyncMock()
        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=42),
            patch("kikubot.handlers.commands.config") as mock_cfg,
            patch("kikubot.handlers.commands.session_manager") as mock_sm,
            patch("kikubot.handlers.commands.safe_reply", new=reply),
        ):
            mock_cfg.docker_agents_enabled = True
            mock_cfg.get_docker_agent.return_value = None
            mock_sm.find_sub_agent_binding.return_value = "docker:assistant/notes"
            mock_sm.iter_thread_bindings.return_value = iter(
                [(1, 77, "docker:assistant/notes")]
            )
            mock_sm.get_display_name.return_value = "assistant-notes"

            await bind_command(update, context)

            mock_sm.bind_thread.assert_not_called()
            assert reply.call_args.args[1] == tr(
                "commands.bind_taken", name="assistant-notes"
            )


class TestStartCommandGroupHints:
    """/start in a group detects the two setup traps (no admin / no Topics).

    Commands are the only updates Telegram delivers to a non-admin bot
    (privacy mode), so /start must explain the broken setup instead of
    greeting into silence.
    """

    def _update(self, chat_type: str = "supergroup", is_forum: bool = False):
        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        chat = update.effective_chat
        chat.type = chat_type
        chat.is_forum = is_forum
        chat.get_member = AsyncMock(return_value=MagicMock(status="administrator"))
        context = MagicMock()
        context.bot.id = 999
        return update, context

    async def _run(self, update, context):
        reply = AsyncMock()
        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=None),
            patch("kikubot.handlers.commands.session_manager"),
            patch("kikubot.handlers.commands.safe_reply", new=reply),
        ):
            await start_command(update, context)
        return reply.call_args.args[1]

    @pytest.mark.asyncio
    async def test_not_admin_asks_for_admin_rights(self):
        update, context = self._update(is_forum=True)
        update.effective_chat.get_member = AsyncMock(
            return_value=MagicMock(status="member")
        )
        assert await self._run(update, context) == tr("bot.make_me_admin")

    @pytest.mark.asyncio
    async def test_admin_without_topics_asks_to_enable_topics(self):
        update, context = self._update(is_forum=False)
        assert await self._run(update, context) == tr("bot.enable_topics_hint")

    @pytest.mark.asyncio
    async def test_admin_with_topics_gets_welcome(self):
        update, context = self._update(is_forum=True)
        assert await self._run(update, context) == tr("bot.start_welcome")

    @pytest.mark.asyncio
    async def test_get_member_failure_does_not_nag(self):
        update, context = self._update(is_forum=True)
        update.effective_chat.get_member = AsyncMock(side_effect=RuntimeError("api"))
        assert await self._run(update, context) == tr("bot.start_welcome")

    @pytest.mark.asyncio
    async def test_private_chat_gets_dm_walkthrough(self):
        update, context = self._update(chat_type="private")
        assert await self._run(update, context) == tr("bot.private_start")


class TestKillForgetsRuntime:
    """/kill is a DELIBERATE end of the session: the topic must also forget
    which CLI ran in it, so the next launch offers the agent picker instead of
    silently relaunching the same runtime (a topic stuck on a broken CLI could
    otherwise never be moved off it)."""

    @pytest.mark.asyncio
    async def test_kill_forgets_thread_runtime(self):
        from kikubot.handlers.commands import kill_command

        update = MagicMock()
        update.effective_user.id = 1
        update.message = MagicMock()
        context = MagicMock()

        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=42),
            patch("kikubot.handlers.commands.session_manager") as mock_sm,
            patch("kikubot.handlers.commands.tmux_manager") as mock_tm,
            patch("kikubot.handlers.commands.clear_topic_state", new=AsyncMock()),
            patch("kikubot.handlers.commands.safe_reply", new=AsyncMock()),
        ):
            mock_sm.get_window_for_thread.return_value = "@7"
            mock_sm._is_docker_binding.return_value = False
            mock_tm.find_window_by_id = AsyncMock(
                return_value=MagicMock(window_id="@7")
            )
            mock_tm.kill_window = AsyncMock()

            await kill_command(update, context)

            mock_sm.unbind_thread.assert_called_once_with(1, 42)
            mock_sm.forget_thread_runtime.assert_called_once_with(1, 42)


def test_agent_and_server_lead_the_command_list() -> None:
    # No bottom menu: "/" is the way to the panel and the summary.
    names = [c.command for c in build_bot_commands()]
    assert names[:2] == ["agent", "server"]
    # Aliases still work but stay off the list.
    assert not {"commands", "status", "menu", "screenshot"} & set(names)


def test_menu_removal_only_where_the_old_menu_may_be() -> None:
    from telegram import ReplyKeyboardRemove

    with patch("kikubot.handlers.commands.session_manager") as sm:
        sm.menu_needs_clearing.return_value = True
        assert isinstance(pending_menu_removal(100, 42), ReplyKeyboardRemove)
        sm.menu_needs_clearing.return_value = False
        assert pending_menu_removal(100, 42) is None


class TestRoleCommand:
    """/role — the topic's standing instructions, kept past every session."""

    @pytest.fixture
    def sm(self, monkeypatch):
        from kikubot.session import SessionManager

        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        return SessionManager()

    async def _run(
        self, sm, text: str, *, wid: str = "", sent: bool = True, dialog: bool = False
    ):
        from kikubot.handlers.commands import role_command

        update = MagicMock()
        update.effective_user.id = 1
        update.message.text = text
        send = AsyncMock(return_value=(sent, ""))
        reply = AsyncMock()
        sm.resolve_window_for_thread = MagicMock(return_value=wid or None)
        sm.capture_pane = AsyncMock(return_value="pane")
        sm.send_to_window = send
        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=42),
            patch("kikubot.handlers.commands.session_manager", sm),
            patch("kikubot.handlers.commands.safe_reply", new=reply),
            patch("kikubot.handlers.commands.is_interactive_ui", return_value=dialog),
        ):
            await role_command(update, MagicMock())
        return reply.await_args.args[1], send

    @pytest.mark.asyncio
    async def test_set_in_a_topic_without_an_agent(self, sm):
        said, deliver = await self._run(sm, "/role Log gym notes")
        assert sm.get_topic_role(1, 42) == "Log gym notes"
        assert said == tr("role.saved")
        deliver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_agent_is_told_at_once(self, sm):
        said, deliver = await self._run(
            sm, "/role@yourbot Log gym notes\n- no questions", wid="@5"
        )
        # Line breaks survive: the raw text, not context.args.
        assert sm.get_topic_role(1, 42) == "Log gym notes\n- no questions"
        note = deliver.await_args.args[1]
        assert note.startswith("[kikubot]") and note.endswith("- no questions")
        assert said == tr("role.saved_told")

    @pytest.mark.asyncio
    async def test_open_dialog_gets_no_note(self, sm):
        # Typed into a dialog, the note would become its answer.
        said, send = await self._run(sm, "/role Log", wid="@5", dialog=True)
        assert sm.get_topic_role(1, 42) == "Log"
        assert said == tr("role.saved_later")
        send.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_send_says_when(self, sm):
        said, _ = await self._run(sm, "/role Log", wid="@5", sent=False)
        assert said == tr("role.saved_later")

    @pytest.mark.asyncio
    async def test_show_and_none(self, sm):
        said, _ = await self._run(sm, "/role")
        assert said == tr("role.none")
        sm.set_topic_role(1, 42, "Log gym notes")
        said, deliver = await self._run(sm, "/role", wid="@5")
        assert "Log gym notes" in said
        deliver.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_clears_and_tells_the_agent(self, sm):
        sm.set_topic_role(1, 42, "Log gym notes")
        said, deliver = await self._run(sm, "/role off", wid="@5")
        assert sm.get_topic_role(1, 42) == ""
        assert "no longer apply" in deliver.await_args.args[1]
        assert said == tr("role.cleared_told")

    @pytest.mark.asyncio
    async def test_too_long_is_refused(self, sm):
        from kikubot.session import TOPIC_ROLE_MAX_CHARS

        said, _ = await self._run(sm, "/role " + "x" * (TOPIC_ROLE_MAX_CHARS + 1))
        assert sm.get_topic_role(1, 42) == ""
        assert str(TOPIC_ROLE_MAX_CHARS) in said

    @pytest.mark.asyncio
    async def test_container_agent_gets_it_too(self, sm):
        # Its own hook reads it from the file the bot keeps beside its map.
        said, deliver = await self._run(sm, "/role Log", wid="docker:assistant")
        assert sm.get_topic_role(1, 42) == "Log"
        assert deliver.await_args.args[0] == "docker:assistant"
        assert said == tr("role.saved_told")

    @pytest.mark.asyncio
    async def test_hookless_agent_hears_it_wont_come_back(self, sm):
        sm.window_runtime = MagicMock(return_value="codex")
        said, _ = await self._run(sm, "/role Log", wid="@5")
        assert said.startswith(tr("role.saved_told"))
        assert said.endswith(tr("role.no_hook", agent="Codex"))

    @pytest.mark.asyncio
    async def test_hookless_topic_without_an_agent_hears_it_too(self, sm):
        sm.record_thread_directory(1, 42, "/w", runtime="codex")
        said, _ = await self._run(sm, "/role Log")
        assert said == f"{tr('role.saved')}\n{tr('role.no_hook', agent='Codex')}"


class TestTopicRenamed:
    """A renamed topic's agent keeps the new name past its window."""

    @pytest.fixture
    def sm(self, monkeypatch):
        from kikubot.session import SessionManager

        monkeypatch.setattr(SessionManager, "_load_state", lambda self: None)
        monkeypatch.setattr(SessionManager, "_save_state", lambda self: None)
        return SessionManager()

    async def _rename(self, sm, name: str) -> AsyncMock:
        from kikubot.handlers.commands import topic_edited_handler

        update = MagicMock()
        update.effective_user.id = 1
        update.message.forum_topic_edited.name = name
        rename = AsyncMock(return_value=True)
        with (
            patch("kikubot.handlers.commands.is_user_allowed", return_value=True),
            patch("kikubot.handlers.commands.get_thread_id", return_value=42),
            patch("kikubot.handlers.commands.session_manager", sm),
            patch("kikubot.handlers.commands.tmux_manager.rename_window", new=rename),
        ):
            await topic_edited_handler(update, MagicMock())
        return rename

    @pytest.mark.asyncio
    async def test_unbound_topic_remembers_the_name(self, sm):
        # The next window (a revive, a picker launch) takes it.
        sm.set_topic_agent_name(1, 42, "old")
        rename = await self._rename(sm, "➕  gym  notes")
        assert sm.topic_window_name(1, 42) == "gym notes"
        rename.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_live_window_takes_the_name_without_the_mark(self, sm):
        sm.bind_thread(1, 42, "@5", window_name="project")
        rename = await self._rename(sm, "🌳 project: fix login")
        rename.assert_awaited_once_with("@5", "project: fix login")
        assert sm.get_display_name("@5") == "project: fix login"
        assert sm.topic_window_name(1, 42) == "project: fix login"

    @pytest.mark.asyncio
    async def test_a_bare_mark_keeps_the_old_name(self, sm):
        sm.set_topic_agent_name(1, 42, "gym")
        await self._rename(sm, "➕")
        assert sm.topic_window_name(1, 42) == "gym"

    @pytest.mark.asyncio
    async def test_docker_binding_renames_no_tmux_window(self, sm):
        sm.bind_thread(1, 42, "docker:assistant", window_name="assistant")
        rename = await self._rename(sm, "Helper")
        rename.assert_not_awaited()
        assert sm.get_display_name("docker:assistant") == "Helper"
