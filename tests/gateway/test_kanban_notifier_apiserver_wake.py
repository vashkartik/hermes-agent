"""Kanban notifier behavior on stateless (api_server) subscriptions.

Covers the wrong-session-wake / silent-loss fixes:
* a SendResult(success=False) return (the API server's send() stub) rewinds
  the cursor instead of advancing past a never-delivered event;
* api_server subscriptions wake the creator's REAL session via the
  /v1/chat/completions self-post (raw task.session_id), never via
  handle_message (which would run under a build_session_key()-derived key
  that never matches the raw X-Hermes-Session-Id session real turns use).
"""

import asyncio

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


class SoftFailAdapter:
    """Push-capable adapter whose send() returns SendResult(success=False)
    WITHOUT raising — previously treated as delivered (event lost)."""

    def __init__(self):
        self.attempts = 0

    async def send(self, chat_id, text, metadata=None):
        self.attempts += 1
        return SendResult(success=False, error="soft failure")


class ApiServerLikeAdapter:
    supports_async_delivery = False

    def __init__(self):
        self._host = "127.0.0.1"
        self._port = 8642
        self._api_key = "k"
        self._model_name = "hermes"
        self.handle_message_calls = []
        self.send_calls = 0

    async def send(self, chat_id, text, metadata=None):
        self.send_calls += 1
        return SendResult(
            success=False,
            error="API server uses HTTP request/response, not send()",
        )

    async def handle_message(self, event):
        self.handle_message_calls.append(event)


async def _run_one_notifier_tick(monkeypatch, runner):
    real_sleep = asyncio.sleep

    async def fake_sleep(delay):
        if delay == 5:
            return None
        runner._running = False
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await runner._kanban_notifier_watcher(interval=1)


def _make_runner(adapters):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = adapters
    runner._kanban_sub_fail_counts = {}
    runner._kanban_dispatcher_lock_handle = object()
    return runner


def _create_completed_subscription(platform, chat_id, session_id=None):
    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn, title="notify once", assignee="worker", session_id=session_id,
        )
        kb.add_notify_sub(conn, task_id=tid, platform=platform, chat_id=chat_id)
        kb.complete_task(conn, tid, summary="done once")
        return tid
    finally:
        conn.close()


def _unseen_terminal_events(tid, platform, chat_id):
    conn = kb.connect()
    try:
        _, events = kb.unseen_events_for_sub(
            conn,
            task_id=tid,
            platform=platform,
            chat_id=chat_id,
            kinds=["completed", "blocked", "gave_up", "crashed", "timed_out"],
        )
        return events
    finally:
        conn.close()


def test_apiserver_sub_wakes_real_session_via_self_post(tmp_path, monkeypatch):
    """An api_server subscription wakes the creator's REAL session by
    self-posting with the task's raw session_id — never handle_message (which
    would run the wake under a build_session_key()-derived key that can't
    match the raw X-Hermes-Session-Id session)."""
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "apiserver.db"))
    kb.init_db()
    tid = _create_completed_subscription(
        "api_server", "raw-sid-123", session_id="raw-sid-123",
    )

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    adapter = ApiServerLikeAdapter()
    runner = _make_runner({Platform.API_SERVER: adapter})
    asyncio.run(_run_one_notifier_tick(monkeypatch, runner))

    assert adapter.handle_message_calls == [], (
        "api_server wake must not go through handle_message (wrong-session bug)"
    )
    assert len(posts) == 1
    assert posts[0]["session_id"] == "raw-sid-123"
    assert tid in posts[0]["text"]
    # The wake self-post IS the delivery on this path (no separate text-ping
    # fallback is attempted for stateless api_server subs) — cursor advances
    # once the wake succeeds.
    assert _unseen_terminal_events(tid, "api_server", "raw-sid-123") == []


def test_apiserver_controller_return_is_consumed_and_acknowledged(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "controller.db"))
    monkeypatch.setattr(
        kb,
        "_is_authorized_controller_assignee",
        lambda assignee: assignee == "vector-controller",
    )
    kb.init_db()

    conn = kb.connect()
    try:
        tid = kb.create_task(
            conn,
            title="controller API return",
            assignee="vector-controller",
            body="Deliver the controller return to the originating API session.",
            session_id="raw-controller-session",
        )
        projection = kb.controller_status_projection(conn, tid)
        assert projection is not None
        kb.add_notify_sub(
            conn,
            task_id=tid,
            platform="api_server",
            chat_id="raw-controller-session",
            notifier_profile="default",
        )
        common = {
            "task_id": tid,
            "correlation_id": projection["correlation_id"],
            "ace_identity": projection["ace_identity"],
        }
        kb.record_controller_envelope(
            conn,
            event_type="OUTBOUND",
            idempotency_key="api-outbound",
            occurred_at="2026-08-13T22:30:00Z",
            payload={"brief": "full controller brief"},
            **common,
        )
        kb.record_controller_envelope(
            conn,
            event_type="TRANSITION",
            idempotency_key="api-transition",
            occurred_at="2026-08-13T22:30:01Z",
            payload={"state": "running"},
            ace_receipt={"run_id": "api-run"},
            **common,
        )
        kb.record_controller_envelope(
            conn,
            event_type="RETURN",
            idempotency_key="api-return",
            occurred_at="2026-08-13T22:30:02Z",
            payload={"summary": "complete"},
            terminal_receipt={"head": "exact-head", "status": "passed"},
            **common,
        )
        before = kb.controller_status_projection(conn, tid)
        assert before is not None
        assert before["terminal"] is False
    finally:
        conn.close()

    posts = []

    async def fake_self_post(adapter, *, text, session_id):
        posts.append({"text": text, "session_id": session_id})

    import gateway.wake as wake_mod

    monkeypatch.setattr(wake_mod, "_self_post_chat_completion", fake_self_post)

    adapter = ApiServerLikeAdapter()
    asyncio.run(
        _run_one_notifier_tick(
            monkeypatch,
            _make_runner({Platform.API_SERVER: adapter}),
        )
    )

    assert adapter.send_calls == 0
    assert len(posts) == 1
    assert posts[0]["session_id"] == "raw-controller-session"
    assert "SENT" in posts[0]["text"]
    assert "ACE ACCEPTED" in posts[0]["text"]
    assert "RESPONSE RECEIVED" in posts[0]["text"]
    assert "VECTOR ACKNOWLEDGED" not in posts[0]["text"]

    conn = kb.connect()
    try:
        after = kb.controller_status_projection(conn, tid)
        assert after is not None
        assert after["terminal"] is True
    finally:
        conn.close()

    asyncio.run(
        _run_one_notifier_tick(
            monkeypatch,
            _make_runner({Platform.API_SERVER: adapter}),
        )
    )
    assert len(posts) == 2
    assert "VECTOR ACKNOWLEDGED" in posts[1]["text"]

    conn = kb.connect()
    try:
        assert kb.complete_task(conn, tid, summary="API return acknowledged") is True
    finally:
        conn.close()

