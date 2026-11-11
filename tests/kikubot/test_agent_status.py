"""Tests for agent_status: the status built from the caches, its JSON (v1) and
its card."""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from kikubot import agent_status
from kikubot import session as session_mod
from kikubot.config import config
from kikubot.runtimes import AgentUsage, UsageLimit

NOW = 1_900_000_000.0
_SNAP = {
    "ts": int(NOW) - 5,
    "session_id": "sid-claude",
    "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
    "effort": "high",
    "thinking": True,
    "cost": {"total_lines_added": 12, "total_lines_removed": 3},
    "context_window": {"used_percentage": 43, "context_window_size": 200000},
    "rate_limits": {
        "five_hour": {"used_percentage": 62, "resets_at": int(NOW) + 8100},
        "seven_day": {"used_percentage": 20, "resets_at": int(NOW) + 3 * 86400},
    },
}


class _FakeSM:
    def __init__(self, runtime="claude", working=False, queued=False):
        self.runtime = runtime
        self.window_states = {"@5": SimpleNamespace(session_id="sid-claude")}
        self.working = working
        self.queued = queued

    def window_runtime(self, binding):
        return self.runtime

    def get_display_name(self, binding):
        return "proj"

    def is_agent_working(self, binding, pane):
        return self.working

    def agent_has_queued_input(self, binding, pane):
        return self.queued

    def can_offer_sibling(self, binding):
        return True

    def can_offer_worktree(self, binding):
        return binding.startswith("@")

    extra_agents: frozenset[str] = frozenset()

    def is_worktree_window(self, binding):
        return binding in self.extra_agents

    def can_delete_agent(self, binding):
        return binding in self.extra_agents

    # The real resolver: siblings (docker:<agent>/<slug>) must resolve to
    # their configured agent exactly as in production.
    split_docker_binding = staticmethod(session_mod.SessionManager.split_docker_binding)
    resolve_docker_target = session_mod.SessionManager.resolve_docker_target


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "agent_status_dir", tmp_path)
    monkeypatch.setattr(config, "usage_limits_enabled", True)
    monkeypatch.setattr(config, "session_map_file", tmp_path / "no-session-map.json")
    from kikubot import hook

    claude_settings = tmp_path / "claude-settings.json"
    monkeypatch.setattr(hook, "_claude_settings_file", lambda: claude_settings)
    for cache in (
        agent_status._panes,
        agent_status._usage,
        agent_status._files,
        agent_status._agents,
    ):
        cache.clear()
    sm = _FakeSM()
    monkeypatch.setattr(session_mod, "session_manager", sm)
    yield SimpleNamespace(dir=tmp_path, sm=sm, claude_settings=claude_settings)
    agent_status._panes.clear()
    agent_status._usage.clear()


def _write(dir_, window, snap):
    (dir_ / f"{window}.json").write_text(json.dumps(snap))


def test_claude_status_from_snapshot(env):
    _write(env.dir, "@5", _SNAP)
    agent_status.note_usage("sid-claude", AgentUsage(context_tokens=86000))
    st = agent_status.build_status("@5", now=NOW)

    assert st.model == "Opus 5.5"
    assert (st.context_pct, st.context_used, st.context_window) == (43, 86000, 200000)
    assert [(lim.id, lim.pct_used, lim.window_minutes) for lim in st.limits] == [
        ("5h", 62.0, 300),
        ("7d", 20.0, 10080),
    ]
    ids = [i.id for i in st.items]
    assert ids[:2] == ["thinking", "lines"]
    assert st.effort == "high"
    assert [c["cmd"] for c in st.commands] == [
        "/sibling",
        "/worktree",
        "/compact",
        "/context",
        "/model",
        "/clear",
        "/rewind",
        "/resume",
        "/bgtask",
        "/diff",
        "/restart",
        "/kill",
    ]


def test_json_v1_shape(env):
    _write(env.dir, "@5", _SNAP)
    agent_status.note_usage("sid-claude", AgentUsage(context_tokens=86000))
    data = json.loads(agent_status.build_status("@5", now=NOW).to_json())
    assert data["v"] == 1 and data["runtime"] == "claude" and data["ts"] == int(NOW)
    assert data["context"] == {"pct": 43, "used": 86000, "window": 200000}
    assert data["limits"][0] == {
        "id": "5h",
        "label": "5 h",
        "pct_used": 62,
        "resets_at": int(NOW) + 8100,
        "window_minutes": 300,
    }
    secs = {s["id"]: s for s in data["sections"]}
    assert [s["id"] for s in data["sections"]] == [
        "model",
        "effort",
        "context",
        "limits",
        "session",
        "commands",
    ]
    assert data["summary"] == ["model", "effort", "context", "limits"]
    assert secs["model"]["selected"] == "opus"
    assert secs["model"]["command"] == "/model {id}"
    assert {"id": "opus", "label": "Opus 5.5"} in secs["model"]["options"]
    assert secs["effort"]["selected"] == "high"
    assert secs["effort"]["command"] == "/effort {id}"
    assert secs["context"] == {
        "type": "meter",
        "id": "context",
        "title": "Context window",
        "pct": 43,
        "caption": "86k of 200k",
    }
    assert secs["limits"]["items"][0] == {
        "label": "5 h",
        "pct": 62,
        "caption": "38% left",
        "resets_at": int(NOW) + 8100,
    }
    assert {"id": "thinking", "label": "Thinking", "value": "on"} in secs["session"][
        "items"
    ]
    compact = next(i for i in secs["commands"]["items"] if i["id"] == "compact")
    assert compact["cmd"] == "/compact" and compact["group"] == "session"
    assert compact["label"] == "Compact context" and compact["confirm"]
    assert "/home" not in json.dumps(data)


def test_mode_choice_section(env):
    rule = "─" * 40
    agent_status.note_pane("@5", f"x\n{rule}\n❯ \n{rule}\n  ⏵⏵ auto mode on\n")
    data = json.loads(agent_status.build_status("@5", now=NOW).to_json())
    mode = next(s for s in data["sections"] if s["id"] == "mode")
    assert mode["type"] == "choice" and mode["selected"] == "auto"
    assert mode["command"] == "/mode {id}"
    assert [o["id"] for o in mode["options"]] == [
        "manual",
        "accept_edits",
        "plan",
        "auto",
    ]


def test_card_shows_used_left_and_reset(env):
    _write(env.dir, "@5", _SNAP)
    card = agent_status.build_status("@5", now=NOW).card(now=NOW)
    assert "👾 proj · Claude Code" in card
    assert "5 h limit: 62% used · 38% left · resets in 2 h 15 min" in card
    assert "Context: 43% · 86k of 200k" in card
    assert "Effort: high" in card


def test_expired_limit_dropped(env):
    snap = dict(_SNAP)
    snap["rate_limits"] = {
        "five_hour": {"used_percentage": 99, "resets_at": int(NOW) - 1}
    }
    _write(env.dir, "@5", snap)
    assert agent_status.build_status("@5", now=NOW).limits == []


def test_account_limits_from_another_window(env):
    # @5 was launched before the tap: no snapshot of its own; limits are the
    # account's, so another window's report stands in.
    _write(env.dir, "@9", _SNAP)
    st = agent_status.build_status("@5", now=NOW)
    assert [lim.id for lim in st.limits] == ["5h", "7d"]
    assert st.model is None


def test_model_running_differs_from_selected(env):
    _write(env.dir, "@5", _SNAP)
    agent_status.note_usage("sid-claude", AgentUsage(model="claude-sonnet-5-5"))
    st = agent_status.build_status("@5", now=NOW)
    assert st.model == "Opus 5.5"
    assert st.model_running == "Sonnet 5.5"
    assert "Answering now: Sonnet 5.5" in st.card(now=NOW)


def test_codex_limits_from_transcript_and_reserved_slash(env):
    env.sm.runtime = "codex"
    agent_status.note_usage(
        "sid-claude",
        AgentUsage(
            context_tokens=19695,
            context_window=258400,
            model="gpt-5.5",
            limits=(UsageLimit("5h", 1.0, int(NOW) + 60, 300),),
        ),
    )
    st = agent_status.build_status("@5", reserved_commands={"status"}, now=NOW)
    assert st.model == "gpt-5.5"
    assert st.context_pct == 8
    assert [lim.id for lim in st.limits] == ["5h"]
    # Codex's context command is /status — the bot's own command, never the agent's.
    cmds = [c["cmd"] for c in st.commands]
    assert "/status" not in cmds
    assert "/rewind" not in cmds  # Claude Code's, not Codex's


@pytest.mark.parametrize(
    ("pane", "working", "expected"),
    [
        (None, False, "gone"),
        ("❯ \n", True, "working"),
        ("❯ \n", False, "idle"),
    ],
)
def test_state_from_pane(env, pane, working, expected):
    env.sm.working = working
    agent_status.note_pane("@5", pane)
    assert agent_status.build_status("@5", now=NOW).state == expected


def test_stale_pane_says_nothing(env, monkeypatch):
    agent_status.note_pane("@5", "❯ \n")
    real = time.monotonic
    monkeypatch.setattr(
        agent_status.time, "monotonic", lambda: real() + agent_status.PANE_FRESH_SEC + 1
    )
    assert agent_status.build_status("@5", now=NOW).state is None


def test_a_failing_provider_does_not_sink_the_card(env, monkeypatch):
    def boom(src):
        raise RuntimeError("x")

    monkeypatch.setattr(agent_status, "ITEM_PROVIDERS", [boom])
    st = agent_status.build_status("@5", now=NOW)
    assert st.items == [] and st.name == "proj"


def test_write_statusline_settings(tmp_path, monkeypatch):
    from kikubot import hook

    user = tmp_path / "settings.json"
    user.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "x", "padding": 2}})
    )
    monkeypatch.setattr(hook, "_claude_settings_file", lambda: user)
    monkeypatch.setattr(hook, "_find_kikubot_path", lambda: "/venv/bin/kikubot")
    monkeypatch.setattr(config, "config_dir", tmp_path)
    monkeypatch.setattr(config, "agent_status_dir", tmp_path / "agent_status")

    path = agent_status.write_statusline_settings()
    assert path is not None
    sl = json.loads(path.read_text())["statusLine"]
    assert sl["padding"] == 2
    assert sl["command"].startswith("/venv/bin/kikubot statusline ")
    assert sl["command"].endswith(f" {config.tmux_session_name}")


def test_per_model_weekly_limit(env):
    snap = dict(_SNAP)
    snap["rate_limits"] = {
        "seven_day_fable": {"used_percentage": 4, "resets_at": int(NOW) + 3600}
    }
    _write(env.dir, "@5", snap)
    st = agent_status.build_status("@5", now=NOW)
    assert [(lim.id, lim.window_minutes) for lim in st.limits] == [("7d_fable", 10080)]
    assert json.loads(st.to_json())["limits"][0]["label"] == "7 days · Fable"


def test_mode_and_shells_from_footer(env):
    rule = "─" * 40
    agent_status.note_pane(
        "@5", f"x\n{rule}\n❯ \n{rule}\n  ⏸ plan mode on · 2 shells · ← 1 agent\n"
    )
    st = agent_status.build_status("@5", now=NOW)
    assert st.mode == "plan"
    assert {i.id: i.value for i in st.items}["shells"] == "2"
    assert "Mode: Plan" in st.card(now=NOW)


def test_branch_from_git_head(env, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/feature/x\n")
    env.sm.window_states["@5"] = SimpleNamespace(session_id="sid-claude", cwd=str(repo))
    items = {i.id: i.value for i in agent_status.build_status("@5", now=NOW).items}
    assert items["branch"] == "feature/x"


def test_branch_of_a_worktree(tmp_path):
    main_git = tmp_path / "main" / ".git" / "worktrees" / "wt"
    main_git.mkdir(parents=True)
    (main_git / "HEAD").write_text("ref: refs/heads/wt-branch\n")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main_git}\n")
    assert agent_status.git_branch(str(wt)) == "wt-branch"


def test_model_without_aliases_opens_the_cli_picker(env):
    env.sm.runtime = "codex"
    agent_status.note_usage("sid-claude", AgentUsage(model="gpt-5.5"))
    data = json.loads(agent_status.build_status("@5", now=NOW).to_json())
    model = next(s for s in data["sections"] if s["id"] == "model")
    assert model["command"] == "/model"
    assert model["options"] == [{"id": "current", "label": "gpt-5.5"}]


def test_snapshot_of_another_session_is_ignored(env):
    # Window ids restart with tmux: @5.json may be a previous window's.
    _write(env.dir, "@5", {**_SNAP, "session_id": "someone-else"})
    st = agent_status.build_status("@5", now=NOW)
    assert st.model is None and st.effort is None
    assert [lim.id for lim in st.limits] == ["5h", "7d"]  # account-wide still


def test_resumed_session_matches_the_hook_id(env):
    (env.dir / "map.json").write_text(
        json.dumps({f"{config.tmux_session_name}:@5": {"session_id": "new-sid"}})
    )
    config.session_map_file = env.dir / "map.json"
    _write(env.dir, "@5", {**_SNAP, "session_id": "new-sid"})
    assert agent_status.build_status("@5", now=NOW).model == "Opus 5.5"


def test_flag_off_serves_no_snapshot(env, monkeypatch):
    monkeypatch.setattr(config, "usage_limits_enabled", False)
    _write(env.dir, "@5", _SNAP)
    st = agent_status.build_status("@5", now=NOW)
    assert st.model is None and st.limits == []


def test_freshest_limit_wins_across_windows(env):
    _write(env.dir, "@5", _SNAP)  # 5h at 62%
    newer = {
        **_SNAP,
        "session_id": "other",
        "rate_limits": {
            "five_hour": {"used_percentage": 70, "resets_at": int(NOW) + 8100}
        },
    }
    _write(env.dir, "@9", newer)
    lims = {
        lim.id: lim.pct_used for lim in agent_status.build_status("@5", now=NOW).limits
    }
    assert lims["5h"] == 70.0


def test_docker_binding_gets_no_host_limits(env):
    _write(env.dir, "@9", _SNAP)
    env.sm.window_states["docker:a"] = SimpleNamespace(session_id="x")
    assert agent_status.build_status("docker:a", now=NOW).limits == []


def _docker_agent(name, host_account):
    from kikubot.config import DockerAgentConfig

    return DockerAgentConfig(
        name=name,
        container=name,
        workspace_host_path=Path("/w"),
        claude_home_host_path=Path("/c"),
        ipc_dir=Path("/i"),
        session_map_path=Path("/s"),
        host_account=host_account,
    )


@pytest.mark.parametrize(
    ("binding", "has_limits"),
    [
        ("docker:shared", True),
        ("docker:shared/two", True),  # a sibling shares its container's login
        ("docker:own", False),
        ("docker:own/two", False),
    ],
)
def test_docker_limits_follow_the_host_account_flag(
    env, monkeypatch, binding, has_limits
):
    monkeypatch.setattr(
        config,
        "docker_agents",
        [_docker_agent("shared", True), _docker_agent("own", False)],
    )
    _write(env.dir, "@9", _SNAP)
    env.sm.window_states[binding] = SimpleNamespace(session_id="x")
    st = agent_status.build_status(binding, now=NOW)
    assert bool(st.limits) is has_limits
    if has_limits:
        assert [lim.id for lim in st.limits] == ["5h", "7d"]
    # The flag is about the account, not the settings: no host effort.
    assert st.effort is None


def test_no_mode_picker_while_a_dialog_waits(env, monkeypatch):
    rule = "─" * 40
    agent_status.note_pane("@5", f"x\n{rule}\n❯ \n{rule}\n  ⏵⏵ auto mode on\n")
    monkeypatch.setattr("kikubot.terminal_parser.is_interactive_ui", lambda p: True)
    assert agent_status.build_status("@5", now=NOW).mode is None


def test_one_m_tag_is_not_a_model_switch(env):
    snap = {**_SNAP, "model": {"id": "claude-opus-5-5[1m]", "display_name": "Opus 5.5"}}
    _write(env.dir, "@5", snap)
    agent_status.note_usage("sid-claude", AgentUsage(model="claude-opus-5-5"))
    assert agent_status.build_status("@5", now=NOW).model_running is None


def test_json_never_exceeds_the_inline_cap(env, monkeypatch):
    big = [agent_status.Item(f"r{i}", "x" * 50, "y" * 50) for i in range(80)]
    monkeypatch.setattr(agent_status, "ITEM_PROVIDERS", [lambda src: big])
    _write(env.dir, "@5", _SNAP)
    text = agent_status.build_status("@5", now=NOW).to_json()
    assert len(text) <= agent_status.MAX_JSON_CHARS
    assert json.loads(text)["v"] == 1


def test_old_window_without_snapshot_reads_well(env):
    agent_status.note_usage(
        "sid-claude", AgentUsage(context_tokens=720000, model="claude-opus-5-5")
    )
    st = agent_status.build_status("@5", now=NOW)
    assert st.model == "Opus 5.5"
    assert (st.context_pct, st.context_window) == (72, 1_000_000)
    # Below 200k the window could be either size: no guess.
    agent_status._usage.clear()
    agent_status.note_usage("sid-claude", AgentUsage(context_tokens=150000))
    st = agent_status.build_status("@5", now=NOW)
    assert st.context_pct is None and st.context_window is None


def test_effort_from_claude_settings_for_an_old_window(env):
    env.claude_settings.write_text(
        json.dumps(
            {
                "effortLevel": "high",
                "modelSettings": {"claude-opus-5-5": {"effortLevel": "xhigh"}},
            }
        )
    )
    agent_status.note_usage("sid-claude", AgentUsage(model="claude-opus-5-5"))
    st = agent_status.build_status("@5", now=NOW)
    assert st.effort == "xhigh"
    data = json.loads(st.to_json())
    assert data["summary"][:2] == ["model", "effort"]


def test_every_alias_named_with_its_version(env):
    # Another window ran Fable 5.2: the picker names the alias with it.
    _write(
        env.dir,
        "@9",
        {
            **_SNAP,
            "session_id": "x",
            "model": {"id": "claude-fable-5-2", "display_name": "Fable 5.2"},
        },
    )
    agent_status.note_usage("sid-claude", AgentUsage(model="claude-opus-5-5"))
    data = json.loads(agent_status.build_status("@5", now=NOW).to_json())
    model = next(s for s in data["sections"] if s["id"] == "model")
    labels = {o["id"]: o["label"] for o in model["options"]}
    assert labels["opus"] == "Opus 5.5" and model["selected"] == "opus"
    assert labels["fable"] == "Fable 5.2"
    assert labels["haiku"] == "Haiku"  # never seen here: no version to claim


def test_branch_at_top_level(env, tmp_path):
    repo = tmp_path / "r"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    env.sm.window_states["@5"] = SimpleNamespace(session_id="sid-claude", cwd=str(repo))
    assert (
        json.loads(agent_status.build_status("@5", now=NOW).to_json())["branch"]
        == "main"
    )


def test_old_window_learns_its_window_from_a_snapshot(env):
    _write(
        env.dir,
        "@9",
        {
            **_SNAP,
            "session_id": "x",
            "model": {"id": "claude-opus-5-5", "display_name": "Opus 5.5"},
            "context_window": {"context_window_size": 200000},
        },
    )
    agent_status.note_usage(
        "sid-claude", AgentUsage(context_tokens=150000, model="claude-opus-5-5")
    )
    st = agent_status.build_status("@5", now=NOW)
    assert (st.context_pct, st.context_window) == (75, 200000)


def test_docker_binding_gets_no_host_effort(env):
    env.claude_settings.write_text(json.dumps({"effortLevel": "max"}))
    env.sm.window_states["docker:a"] = SimpleNamespace(session_id="x")
    assert agent_status.build_status("docker:a", now=NOW).effort is None


def _assistant(*blocks):
    return {"type": "assistant", "message": {"content": list(blocks)}}


def _user(content):
    return {"type": "user", "message": {"content": content}}


def test_files_and_subagents_from_the_transcript(env, tmp_path):
    from kikubot.runtimes import CLAUDE

    env.sm.window_states["@5"] = SimpleNamespace(session_id="sid-claude", cwd="/p/proj")
    batch1 = [
        _assistant(
            {
                "type": "tool_use",
                "id": "e1",
                "name": "Edit",
                "input": {"file_path": "/p/proj/src/a.py"},
            },
            {
                "type": "tool_use",
                "id": "e2",
                "name": "Write",
                "input": {"file_path": "/elsewhere/notes.md"},
            },
            {
                "type": "tool_use",
                "id": "ag1",
                "name": "Agent",
                "input": {"description": "Review the diff"},
            },
            {
                "type": "tool_use",
                "id": "ag2",
                "name": "Agent",
                "input": {"description": "Quick lookup"},
            },
        ),
        _user(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "ag1",
                    "content": "Async agent launched successfully.",
                }
            ]
        ),
        _user(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "ag2",
                    "content": [{"type": "text", "text": "Found it."}],
                }
            ]
        ),
    ]
    agent_status.note_usage("sid-claude", CLAUDE.latest_usage(batch1))
    st = agent_status.build_status("@5", now=NOW)
    assert st.files == ["src/a.py", "notes.md"]  # never an absolute path
    assert st.agents == [("ag1", "Review the diff")]  # ag2 finished in the foreground
    data = json.loads(st.to_json())
    secs = {s["id"]: s for s in data["sections"]}
    assert secs["files"]["title"] == "Files changed (2)"
    assert secs["agents"]["items"][0]["label"] == "Review the diff"

    done = _user(
        "<task-notification>\n<task-id>x</task-id>\n<tool-use-id>ag1</tool-use-id>\n<status>completed</status>"
    )
    agent_status.note_usage("sid-claude", CLAUDE.latest_usage([done]))
    assert agent_status.build_status("@5", now=NOW).agents == []


def test_remote_control_from_the_pane():
    from kikubot.runtimes import CLAUDE

    rule = "─" * 40
    connected = (
        f"x\n{'─' * 30} /rc active ─\n❯ \n{rule}\n  ⏵⏵ auto mode on · ← 1 agent /rc\n"
    )
    available = f"x\n{rule}\n❯ \n{rule}\n  ⏵⏵ auto mode on · ← 1 agent /rc\n"
    off = f"x\n{rule}\n❯ \n{rule}\n  ⏵⏵ auto mode on\n"
    assert CLAUDE.remote_control(connected) == "connected"
    assert CLAUDE.remote_control(available) == "available"
    assert CLAUDE.remote_control(off) is None


def test_version_and_output_style_rows(env):
    _write(env.dir, "@5", {**_SNAP, "version": "2.1.322", "output_style": "default"})
    items = {i.id: i.value for i in agent_status.build_status("@5", now=NOW).items}
    assert items["version"] == "2.1.322" and items["output_style"] == "default"


def test_a_report_mentioning_launched_still_ends_the_subagent(env):
    from kikubot.runtimes import CLAUDE

    batch = [
        _assistant(
            {
                "type": "tool_use",
                "id": "ag3",
                "name": "Agent",
                "input": {"description": "Start it"},
            }
        ),
        _user(
            [
                {
                    "type": "tool_result",
                    "tool_use_id": "ag3",
                    "content": "I launched the server on :8080.",
                }
            ]
        ),
    ]
    agent_status.note_usage("sid-claude", CLAUDE.latest_usage(batch))
    assert agent_status.build_status("@5", now=NOW).agents == []


def test_a_foreground_subagent_ending_in_a_later_batch(env):
    # Its result arrives minutes later, in another monitor batch.
    from kikubot.runtimes import CLAUDE

    start = [
        _assistant(
            {
                "type": "tool_use",
                "id": "ag4",
                "name": "Agent",
                "input": {"description": "Long job"},
            }
        )
    ]
    agent_status.note_usage("sid-claude", CLAUDE.latest_usage(start))
    assert agent_status.build_status("@5", now=NOW).agents == [("ag4", "Long job")]
    later = [_user([{"type": "tool_result", "tool_use_id": "ag4", "content": "Done."}])]
    agent_status.note_usage("sid-claude", CLAUDE.latest_usage(later))
    assert agent_status.build_status("@5", now=NOW).agents == []


def test_session_actions_shape(env):
    """Every action is a command the topic accepts, grouped, and the ones a
    client must confirm or show in the terminal say so."""
    st = agent_status.build_status("@5", now=NOW)
    by_id = {c["id"]: c for c in st.commands}
    assert {c["group"] for c in st.commands} <= {"new", "session", "danger"}
    assert by_id["sibling"]["group"] == by_id["worktree"]["group"] == "new"
    assert by_id["restart"]["group"] == by_id["end"]["group"] == "danger"
    for danger in ("restart", "end", "clear", "compact"):
        assert by_id[danger]["confirm"]
    assert by_id["rewind"]["terminal"] and by_id["resume"]["terminal"]
    assert "confirm" not in by_id["rewind"] and "terminal" not in by_id["restart"]
    assert by_id["background"]["cmd"] == "/bgtask"  # never the CLI's /bg
    assert all(c["icon"] in agent_status.ACTION_ICONS for c in st.commands)


def test_delete_is_offered_only_for_an_extra_agent(env):
    """As on the panel: a main topic holds the project's history, no delete."""
    st = agent_status.build_status("@5", now=NOW)
    assert "delete" not in {c["id"] for c in st.commands}
    env.sm.extra_agents = frozenset({"@5"})
    st = agent_status.build_status("@5", now=NOW)
    delete = {c["id"]: c for c in st.commands}["delete"]
    assert delete["cmd"] == "/delete" and delete["group"] == "danger"
    assert "confirm" not in delete  # the bot asks, with what would be lost


def test_docker_topic_offers_no_worktree(env):
    st = agent_status.build_status("docker:assistant", now=NOW)
    assert "worktree" not in {c["id"] for c in st.commands}
