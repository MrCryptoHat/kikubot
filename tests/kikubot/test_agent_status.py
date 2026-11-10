"""Tests for agent_status: the status built from the caches, its JSON (v1) and
its card."""

from __future__ import annotations

import json
import time
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


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "agent_status_dir", tmp_path)
    monkeypatch.setattr(config, "usage_limits_enabled", True)
    monkeypatch.setattr(config, "session_map_file", tmp_path / "no-session-map.json")
    agent_status._panes.clear()
    agent_status._usage.clear()
    sm = _FakeSM()
    monkeypatch.setattr(session_mod, "session_manager", sm)
    yield SimpleNamespace(dir=tmp_path, sm=sm)
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
    assert [c for c, _ in st.commands] == ["/compact", "/context", "/model"]


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
    assert data["summary"] == ["model", "effort", "context"]
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
    assert {"label": "Compact context", "cmd": "/compact"} in secs["commands"]["items"]
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
    assert st.model_running == "claude-sonnet-5-5"
    assert "Answering now: claude-sonnet-5-5" in st.card(now=NOW)


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
    assert "/status" not in [c for c, _ in st.commands]


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
