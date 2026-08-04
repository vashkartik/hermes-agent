"""Reproduction of the durable multi-client session defects (pre-fix).

Kept as a permanent regression file: each test names one concrete way a single
Hermes session broke when Desktop and mobile were attached at the same time.

1. ``write_json`` routed a session event to ONE stored transport, so the second
   attached client saw nothing (dropped stream).
2. Re-binding a session to a newly-arriving client stole the stream from the
   live owner instead of adding a viewer (dropped stream + hijacked mutation).
3. A retried ``prompt.submit`` carrying the same ``request_id`` ran the turn a
   second time (duplicate execution), and there was no way to ask what happened
   to a request id after a disconnect (missing result / hang).
"""

import threading
import types

from tui_gateway import server


class FakeTransport:
    """Transport double that records every frame it is handed."""

    def __init__(self, name):
        self.name = name
        self.frames = []
        self._closed = False

    def write(self, obj):
        if self._closed:
            return False
        self.frames.append(obj)
        return True

    def close(self):
        self._closed = True

    def events(self):
        return [
            f["params"]["type"]
            for f in self.frames
            if f.get("method") == "event"
        ]


def _session(sid, transport, **extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": f"key-{sid}",
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": False,
        "transport": transport,
        "attached_images": [],
        "created_at": 0.0,
        "last_active": 0.0,
        **extra,
    }


def test_two_clients_on_one_session_both_receive_ordered_deltas():
    """Desktop + mobile attached to one session must both see every delta."""
    desktop = FakeTransport("desktop")
    mobile = FakeTransport("mobile")
    server._sessions.clear()
    server.reset_session_streams()
    try:
        server._sessions["s1"] = _session("s1", desktop)
        server.subscribe_session("s1", desktop, owner=True)
        server.subscribe_session("s1", mobile)

        server._emit("message.start", "s1", {})
        server._emit("message.delta", "s1", {"text": "a"})
        server._emit("message.delta", "s1", {"text": "b"})
        server._emit("message.complete", "s1", {"text": "ab"})

        assert desktop.events() == [
            "message.start", "message.delta", "message.delta", "message.complete"
        ]
        # Pre-fix this was [] — the mobile client got nothing.
        assert mobile.events() == desktop.events()
        # Same order, same sequence numbers on both peers.
        seqs = [f["params"]["seq"] for f in desktop.frames]
        assert seqs == sorted(seqs)
        assert [f["params"]["seq"] for f in mobile.frames] == seqs
    finally:
        server._sessions.clear()
        server.reset_session_streams()


def test_second_client_does_not_steal_the_stream_from_the_owner():
    """A late viewer must not hijack the owner's transport binding."""
    desktop = FakeTransport("desktop")
    mobile = FakeTransport("mobile")
    server._sessions.clear()
    server.reset_session_streams()
    try:
        session = _session("s1", desktop)
        server._sessions["s1"] = session
        server.subscribe_session("s1", desktop, owner=True)

        # Mobile attaches while desktop is alive: viewer, not owner.
        assert server.subscribe_session("s1", mobile) is False
        assert server.session_owner("s1") is desktop

        server._emit("message.delta", "s1", {"text": "x"})
        assert desktop.events() == ["message.delta"]
        assert mobile.events() == ["message.delta"]
    finally:
        server._sessions.clear()
        server.reset_session_streams()


def test_exactly_one_terminal_event_per_turn():
    """Two terminal emits for one turn must collapse to one on the wire."""
    desktop = FakeTransport("desktop")
    server._sessions.clear()
    server.reset_session_streams()
    try:
        server._sessions["s1"] = _session("s1", desktop)
        server.subscribe_session("s1", desktop, owner=True)
        server.begin_session_turn("s1", "req-1")

        server._emit("message.delta", "s1", {"text": "a"})
        server._emit("message.complete", "s1", {"text": "a"})
        # Duplicate terminal (error path racing the normal completion).
        server._emit("message.complete", "s1", {"text": "Error: boom", "status": "error"})

        assert desktop.events().count("message.complete") == 1
    finally:
        server._sessions.clear()
        server.reset_session_streams()


def test_request_ledger_makes_retry_idempotent_and_status_queryable():
    """Same request id twice = one execution; status stays queryable."""
    server.reset_session_streams()
    try:
        ledger = server.request_ledger()
        first = ledger.begin("s1", "req-1", "hello")
        assert first.outcome == "accepted"

        retry = ledger.begin("s1", "req-1", "hello")
        assert retry.outcome == "duplicate"
        assert retry.record["status"] == "running"

        conflict = ledger.begin("s1", "req-1", "different text")
        assert conflict.outcome == "conflict"

        ledger.finish("s1", "req-1", status="complete", result={"text": "hi"})
        assert ledger.status("s1", "req-1")["status"] == "complete"
        # Retry after completion replays the terminal result, never re-runs.
        replay = ledger.begin("s1", "req-1", "hello")
        assert replay.outcome == "duplicate"
        assert replay.record["result"] == {"text": "hi"}
    finally:
        server.reset_session_streams()
