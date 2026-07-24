"""Desktop/TUI agent work participates in the atomic update guard."""

from __future__ import annotations

import io
import threading
import types
from pathlib import Path

import pytest

from hermes_cli import update_guard
from tui_gateway import server


def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "stored-session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        "cols": 80,
        **extra,
    }


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


class _CapturedThread:
    created = []

    def __init__(self, target=None, daemon=None, **_kwargs):
        self.target = target
        self.daemon = daemon
        self.started = False
        self.__class__.created.append(self)

    def start(self):
        self.started = True

    def is_alive(self):
        return self.started


def test_prompt_submit_rejects_without_mutation_when_update_owns_drain(
    isolated_home, monkeypatch
):
    claim = update_guard.try_claim_idle_update("candidate")
    assert claim.claim is not None
    session = _session()
    server._sessions["sid"] = session
    _CapturedThread.created = []
    monkeypatch.setattr(server.threading, "Thread", _CapturedThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_args: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_args: None)
    try:
        response = server.handle_request(
            {
                "id": "r1",
                "method": "prompt.submit",
                "params": {"session_id": "sid", "text": "do not lose this"},
            }
        )

        assert response["error"]["code"] == 4091
        assert session["running"] is False
        assert session.get("inflight_turn") is None
        assert _CapturedThread.created == []
    finally:
        server._sessions.pop("sid", None)
        claim.claim.release()


def test_prompt_submit_claims_update_lease_before_agent_build(
    isolated_home, monkeypatch
):
    order = []
    original_acquire = update_guard.acquire_turn

    def acquire(kind, session_id=""):
        order.append("lease")
        return original_acquire(kind, session_id=session_id)

    session = _session()
    server._sessions["sid"] = session
    _CapturedThread.created = []
    monkeypatch.setattr(update_guard, "acquire_turn", acquire)
    monkeypatch.setattr(server.threading, "Thread", _CapturedThread)
    monkeypatch.setattr(
        server, "_start_agent_build", lambda *_args: order.append("agent-build")
    )
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_args: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_args: None)
    try:
        response = server.handle_request(
            {
                "id": "r1",
                "method": "prompt.submit",
                "params": {"session_id": "sid", "text": "hello"},
            }
        )

        assert response["result"]["status"] == "streaming"
        assert order[:2] == ["lease", "agent-build"]
        assert update_guard.snapshot()["active_turns"] == 1
    finally:
        lease = session.pop("_update_turn_lease", None)
        if lease is not None:
            lease.release()
        server._sessions.pop("sid", None)


def test_pending_clarify_keeps_update_busy_until_answered(
    isolated_home, monkeypatch
):
    session = _session(running=True)
    assert server._claim_update_turn(session, kind="desktop") is True
    emitted = threading.Event()
    request = {}

    def capture(event, sid, payload):
        if event == "clarify.request":
            request.update(payload)
            emitted.set()

    monkeypatch.setattr(server, "_emit", capture)
    worker = threading.Thread(
        target=lambda: server._block(
            "clarify.request",
            "sid",
            {"question": "Which path?", "choices": ["A", "B"]},
            timeout=None,
        )
    )
    worker.start()
    try:
        assert emitted.wait(1)
        busy = update_guard.try_claim_idle_update("candidate")
        assert busy.status == "busy"
        assert busy.active_turns == 1

        response = server._methods["clarify.respond"](
            "r2", {"request_id": request["request_id"], "answer": "A"}
        )
        assert response["result"]["status"] == "ok"
        worker.join(timeout=1)
        assert not worker.is_alive()

        session["running"] = False
        server._release_update_turn_if_idle(session)
        claimed = update_guard.try_claim_idle_update("candidate")
        assert claimed.status == "claimed"
        assert claimed.claim is not None
        claimed.claim.release()
    finally:
        if worker.is_alive():
            server._clear_pending("sid")
            worker.join(timeout=1)
        server._release_update_turn(session)


def test_queued_prompt_handoff_keeps_same_update_lease(
    isolated_home, monkeypatch
):
    session = _session(
        queued_prompt={"text": "second turn", "transport": None},
    )
    assert server._claim_update_turn(session, kind="desktop") is True
    dispatched = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, _session, text: dispatched.append((rid, sid, text)),
    )
    try:
        assert server._drain_queued_prompt("r1", "sid", session) is True
        assert dispatched == [("r1", "sid", "second turn")]
        assert session["running"] is True
        assert update_guard.try_claim_idle_update("candidate").status == "busy"

        session["running"] = False
        server._release_update_turn_if_idle(session)
        claimed = update_guard.try_claim_idle_update("candidate")
        assert claimed.status == "claimed"
        assert claimed.claim is not None
        claimed.claim.release()
    finally:
        server._release_update_turn(session)


def test_completed_prompt_releases_update_lease(isolated_home, monkeypatch):
    class ImmediateThread:
        def __init__(self, target=None, daemon=None, **_kwargs):
            self.target = target

        def start(self):
            self.target()

        def is_alive(self):
            return False

    class Agent:
        def run_conversation(self, _text, **_kwargs):
            return {
                "final_response": "done",
                "messages": [{"role": "assistant", "content": "done"}],
            }

    session = _session(agent=Agent())
    server._sessions["sid"] = session
    monkeypatch.setattr(server.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda *_args: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda *_args: None)
    monkeypatch.setattr(server, "_emit", lambda *_args: None)
    monkeypatch.setattr(server, "make_stream_renderer", lambda *_args: None)
    monkeypatch.setattr(server, "render_message", lambda *_args: None)
    try:
        response = server.handle_request(
            {
                "id": "r1",
                "method": "prompt.submit",
                "params": {"session_id": "sid", "text": "hello"},
            }
        )

        assert response["result"]["status"] == "streaming"
        assert session["running"] is False
        assert update_guard.snapshot()["active_turns"] == 0
    finally:
        server._release_update_turn(session)
        server._sessions.pop("sid", None)


@pytest.mark.parametrize(
    ("method", "params", "complete_event"),
    [
        ("prompt.background", {"text": "background work"}, "background.complete"),
        (
            "preview.restart",
            {"url": "http://127.0.0.1:4173", "cwd": ""},
            "preview.restart.complete",
        ),
    ],
)
def test_hidden_agent_run_blocks_update_until_completion(
    isolated_home,
    monkeypatch,
    method,
    params,
    complete_event,
):
    import run_agent

    started = threading.Event()
    finish = threading.Event()
    completed = threading.Event()

    class HiddenAgent:
        def __init__(self, **_kwargs):
            pass

        def run_conversation(self, **_kwargs):
            started.set()
            assert finish.wait(2)
            return {"final_response": "done"}

    def emit(event, _sid, _payload):
        if event == complete_event:
            completed.set()

    session = _session(cwd=str(isolated_home))
    server._sessions["sid"] = session
    monkeypatch.setattr(run_agent, "AIAgent", HiddenAgent)
    monkeypatch.setattr(server, "_emit", emit)
    try:
        response = server.handle_request(
            {
                "id": "r1",
                "method": method,
                "params": {"session_id": "sid", **params},
            }
        )
        assert response["result"]["task_id"]
        assert started.wait(2)

        attempted = update_guard.try_claim_idle_update("candidate")
        if attempted.claim is not None:
            attempted.claim.release()
        assert attempted.status == "busy"
        assert attempted.active_turns == 1

        finish.set()
        assert completed.wait(2)
        claimed = update_guard.try_claim_idle_update("candidate")
        assert claimed.status == "claimed"
        assert claimed.claim is not None
        claimed.claim.release()
    finally:
        finish.set()
        server._sessions.pop("sid", None)


def test_tui_entry_registers_guarded_runtime_before_ready(
    isolated_home, monkeypatch
):
    import sys

    from hermes_cli import config as hermes_config
    from tui_gateway import entry

    order = []
    monkeypatch.setattr(
        update_guard,
        "register_runtime",
        lambda kind: order.append(("register", kind)),
    )
    monkeypatch.setattr(hermes_config, "read_raw_config", lambda: {})
    monkeypatch.setattr(entry, "_install_sidecar_publisher", lambda: None)
    monkeypatch.setattr(
        entry,
        "write_json",
        lambda payload: order.append(("ready", payload)) or True,
    )
    monkeypatch.setattr(entry, "_log_exit", lambda _reason: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))

    entry.main()

    assert order[0] == ("register", "tui-gateway")
    assert order[1][0] == "ready"
