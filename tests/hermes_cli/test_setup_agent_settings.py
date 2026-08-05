"""Tests for agent-settings copy in the interactive setup wizard."""

from argparse import Namespace

import pytest

from hermes_cli import config as config_mod
from hermes_cli import setup as setup_mod
from hermes_cli.setup import setup_agent_settings


def _patch_agent_settings_interactions(monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.setup.prompt",
        lambda _label, default="", **_kwargs: default,
    )
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 4)
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.remove_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda *args, **kwargs: None)


@pytest.mark.parametrize(
    "setup_mode",
    [pytest.param(1, id="full-setup"), pytest.param(0, id="quick-setup")],
)
def test_first_install_recommended_budget_is_persisted_and_displayed(
    setup_mode, tmp_path, monkeypatch, capsys
):
    """Both first-install entry points persist the advertised runtime default."""
    choices = iter([setup_mode] + ([1] if setup_mode == 0 else []))
    args = Namespace(
        non_interactive=False,
        portal=False,
        quick=False,
        reconfigure=False,
        reset=False,
        section=None,
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(config_mod, "is_managed", lambda: False)
    monkeypatch.setattr(setup_mod, "ensure_hermes_home", lambda: None)
    monkeypatch.setattr(setup_mod, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(setup_mod, "get_env_value", lambda _key: "")
    monkeypatch.setattr(setup_mod, "is_interactive_stdin", lambda: True)
    monkeypatch.setattr(setup_mod, "_offer_openclaw_migration", lambda _home: False)
    monkeypatch.setattr(setup_mod, "prompt_choice", lambda *args, **kwargs: next(choices))
    monkeypatch.setattr(setup_mod, "setup_model_provider", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup_mod, "setup_terminal_backend", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup_mod, "setup_gateway", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup_mod, "setup_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(setup_mod, "remove_env_value", lambda _key: None)
    monkeypatch.setattr(setup_mod, "_print_setup_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.auth.get_active_provider", lambda: None)
    monkeypatch.setattr("hermes_cli.main._model_flow_nous", lambda _config: None)

    setup_mod.run_setup_wizard(args)

    assert config_mod.load_config()["agent"]["max_turns"] == 500
    assert "Max iterations: 500" in capsys.readouterr().out


def test_setup_agent_settings_enter_accepts_500_when_unconfigured(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_agent_settings_interactions(monkeypatch)
    config = {}

    setup_agent_settings(config)

    assert config["agent"]["max_turns"] == 500
    assert "Press Enter to keep 500." in capsys.readouterr().out


def test_setup_agent_settings_keeps_explicit_configured_override(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _patch_agent_settings_interactions(monkeypatch)
    config = {"agent": {"max_turns": 237}}

    setup_agent_settings(config)

    assert config["agent"]["max_turns"] == 237
    assert "Press Enter to keep 237." in capsys.readouterr().out


def test_setup_agent_settings_uses_displayed_max_iterations_value(tmp_path, monkeypatch, capsys):
    """The helper text should match the value shown in the prompt.

    After PR#18413 max_turns is read exclusively from config.yaml — the
    .env `HERMES_MAX_ITERATIONS` fallback was removed because it was
    shadowing the user's current config (see the 60-vs-500 incident).
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = {
        "agent": {"max_turns": 60},
        "display": {"tool_progress": "all"},
        "compression": {"threshold": 0.50},
        "session_reset": {"mode": "both", "idle_minutes": 1440, "at_hour": 4},
    }

    prompt_answers = iter(["60", "all", "0.5"])

    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_answers))
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 4)
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.remove_env_value", lambda *args, **kwargs: None)
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda *args, **kwargs: None)

    setup_agent_settings(config)

    out = capsys.readouterr().out
    assert "Press Enter to keep 60." in out
    assert "Default is 90" not in out


def test_setup_agent_settings_prefers_config_over_stale_env(tmp_path, monkeypatch, capsys):
    """Config.yaml wins even when a stale .env value disagrees.

    Regression guard for the bug where `.env HERMES_MAX_ITERATIONS=60`
    from an old `hermes setup` run shadowed `agent.max_turns: 500` in
    config.yaml. The wizard must now display the config value.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))

    config = {
        "agent": {"max_turns": 500},  # user bumped this in config.yaml
        "display": {"tool_progress": "all"},
        "compression": {"threshold": 0.50},
        "session_reset": {"mode": "both", "idle_minutes": 1440, "at_hour": 4},
    }

    prompt_answers = iter(["500", "all", "0.5"])

    # Simulate stale .env value — the wizard must ignore this.
    monkeypatch.setattr(
        "hermes_cli.setup.get_env_value",
        lambda key: "60" if key == "HERMES_MAX_ITERATIONS" else "",
    )
    monkeypatch.setattr("hermes_cli.setup.prompt", lambda *args, **kwargs: next(prompt_answers))
    monkeypatch.setattr("hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 4)
    monkeypatch.setattr("hermes_cli.setup.save_env_value", lambda *args, **kwargs: None)

    removed_keys: list[str] = []
    monkeypatch.setattr(
        "hermes_cli.setup.remove_env_value",
        lambda key: (removed_keys.append(key), True)[1],
    )
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda *args, **kwargs: None)

    setup_agent_settings(config)

    out = capsys.readouterr().out
    # Config value wins
    assert "Press Enter to keep 500." in out
    assert "Press Enter to keep 60." not in out
    # And the stale .env entry gets cleaned up
    assert "HERMES_MAX_ITERATIONS" in removed_keys
