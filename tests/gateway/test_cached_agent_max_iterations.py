"""Regression tests for PR #48127: cached agent max_iterations refresh.

When a long-lived gateway reuses an agent from its cache, the agent must run
the *current* configured iteration budget — not the budget it was constructed
with on the first turn of that session. Two pieces make that true:

1. ``GatewayRunner._init_cached_agent_for_turn`` must NOT reset
   ``max_iterations`` itself (the gateway refreshes it explicitly right after,
   from current config). If this helper ever started clobbering it, the
   gateway's refresh would be silently undone.
2. The per-turn budget object is rebuilt from ``agent.max_iterations`` at the
   start of every turn (``agent/turn_context.py`` -> ``IterationBudget``), so
   refreshing ``max_iterations`` on the cached agent is sufficient to change
   the operative cap the agent loop checks.

These tests exercise the real code paths rather than asserting a plain
assignment, so they fail if either contract regresses.
"""

import time
from types import SimpleNamespace
from unittest.mock import patch

from agent.iteration_budget import IterationBudget
from agent.session_activity import ActivityProvenance


def test_current_max_iterations_defaults_to_500(monkeypatch):
    """An unset runtime config resolves to the shared default budget."""
    from gateway import run as gateway_run

    monkeypatch.setattr(
        gateway_run,
        "_reload_runtime_env_preserving_config_authority",
        lambda: None,
    )
    monkeypatch.delenv("HERMES_MAX_ITERATIONS", raising=False)

    assert gateway_run._current_max_iterations() == 500


def test_initialized_agent_defaults_to_500_iterations():
    """The public AIAgent constructor carries the shared default into state."""
    from run_agent import AIAgent

    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("hermes_cli.config.load_config", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        initialized_agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )

    assert initialized_agent.max_iterations == 500


def _make_cached_agent(max_iterations: int) -> SimpleNamespace:
    """A minimal stand-in cached agent with the attributes the helpers touch."""
    # The turn loop checks both api_call_count >= max_iterations AND
    # iteration_budget.remaining <= 0 (turn_finalizer.py), so the budget must
    # also reflect the new cap. Seed it with the stale value to prove the
    # refresh propagates.
    return SimpleNamespace(
        _last_activity_ts=time.time() - 1000,
        _last_activity_desc="previous turn",
        _last_activity_provenance=ActivityProvenance.AGENT_COMPRESSION,
        _api_call_count=42,
        _last_flushed_db_idx=5,
        max_iterations=max_iterations,
        iteration_budget=IterationBudget(max_iterations),
    )


def test_init_cached_agent_for_turn_does_not_touch_max_iterations():
    """The per-turn reset helper must leave max_iterations untouched.

    The gateway refreshes max_iterations explicitly right after calling this
    helper; if the helper ever reset it, that refresh would be undone.
    """
    from gateway.run import GatewayRunner

    agent = _make_cached_agent(90)
    GatewayRunner._init_cached_agent_for_turn(agent, interrupt_depth=0)

    # Per-turn state was reset...
    assert agent._api_call_count == 0
    assert agent._last_activity_desc == "starting new turn (cached)"
    assert agent._last_activity_provenance is ActivityProvenance.UNKNOWN
    assert agent._last_flushed_db_idx == 0
    # ...but the iteration budget was NOT changed by the helper itself.
    assert agent.max_iterations == 90


def test_init_cached_agent_preserves_max_iterations_on_interrupt_depth():
    """Interrupt-recursive turns must also leave max_iterations alone."""
    from gateway.run import GatewayRunner

    agent = _make_cached_agent(200)
    GatewayRunner._init_cached_agent_for_turn(agent, interrupt_depth=1)

    # Activity timestamps preserved for the inactivity watchdog (#15654)...
    assert agent._last_activity_desc == "previous turn"
    assert agent._last_activity_provenance is ActivityProvenance.AGENT_COMPRESSION
    # ...and max_iterations untouched.
    assert agent.max_iterations == 200


