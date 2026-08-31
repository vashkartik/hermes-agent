"""Unit coverage for the durable multi-client session layer.

Companion to ``tests/test_tui_gateway_durable_session_repro.py`` (which pins the
user-visible defects) and ``tests/e2e/test_durable_multiclient_session.py``
(which proves the whole contract over a real gateway). This file exercises the
edges the E2E cannot reach cheaply: replay-ring truncation, the stall bound,
ownership handoff on disconnect, and cross-session scoping.
"""

import threading
import types

import pytest

from tui_gateway import server, session_stream
from tui_gateway.session_stream import (
    RequestLedger,
    SessionStream,
    SessionStreamRegistry,
    fingerprint_prompt,
)


class FakeTransport:
    def __init__(self, name="peer", fail=False):
        self.name = name
        self.frames = []
        self.fail = fail
        self._closed = False

    def write(self, obj):
        if self.fail or self._closed:
            return False
        self.frames.append(obj)
        return True

    def close(self):
        self._closed = True


def _frame(sid, event_type, payload=None):
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"type": event_type, "session_id": sid, "payload": payload or {}},
    }


# ── fan-out ordering and membership ────────────────────────────────────────


def test_deliver_returns_none_without_subscribers():
    """No attached peer → caller falls back to the legacy stdio route."""
    stream = SessionStream("s1")
    assert stream.deliver(_frame("s1", "message.delta")) is None


def test_deliver_keeps_replay_after_last_device_disconnects():
    """Mac-side turn keeps going after the phone drops; reconnect can replay."""
    stream = SessionStream("s1")
    phone = FakeTransport("phone")
    stream.attach(phone, owner=True)
    assert stream.deliver(_frame("s1", "message.delta", {"i": 1})) is True
    stream.detach(phone)
    assert stream.deliver(_frame("s1", "message.delta", {"i": 2})) is False
    frames, truncated, latest = stream.replay_since(0)
    assert truncated is False
    assert latest == 2
    assert [f["params"]["payload"]["i"] for f in frames] == [1, 2]


def test_every_subscriber_sees_the_same_sequence():
    stream = SessionStream("s1")
    a, b, c = FakeTransport("a"), FakeTransport("b"), FakeTransport("c")
    stream.attach(a, owner=True)
    stream.attach(b)
    stream.attach(c)

    for i in range(5):
        stream.deliver(_frame("s1", "message.delta", {"i": i}))

    seqs = [f["params"]["seq"] for f in a.frames]
    assert seqs == [1, 2, 3, 4, 5]
    assert [f["params"]["seq"] for f in b.frames] == seqs
    assert [f["params"]["seq"] for f in c.frames] == seqs


def test_a_wedged_peer_does_not_stall_the_others():
    stream = SessionStream("s1")
    good, bad = FakeTransport("good"), FakeTransport("bad", fail=True)
    stream.attach(good, owner=True)
    stream.attach(bad)

    assert stream.deliver(_frame("s1", "message.delta")) is True
    assert len(good.frames) == 1
    # The failing peer is dropped rather than retried forever.
    assert stream.subscriber_count() == 1


def test_a_live_owner_is_not_displaced_without_force():
    stream = SessionStream("s1")
    owner, newcomer = FakeTransport("owner"), FakeTransport("newcomer")
    stream.attach(owner, owner=True)

    assert stream.attach(newcomer, owner=True, is_dead=lambda t: False) is False
    assert stream.owner() is owner
    # Explicit handoff does displace it, and the old owner stays attached.
    assert stream.attach(newcomer, owner=True, force=True) is True
    assert stream.owner() is newcomer
    assert stream.subscriber_count() == 2


def test_a_dead_owner_is_replaced_implicitly():
    """Reconnect: the returning client takes the session back."""
    stream = SessionStream("s1")
    stale, fresh = FakeTransport("stale"), FakeTransport("fresh")
    stream.attach(stale, owner=True)
    assert stream.attach(fresh, owner=True, is_dead=lambda t: t is stale) is True
    assert stream.owner() is fresh


def test_owner_disconnect_promotes_the_surviving_device():
    stream = SessionStream("s1")
    desktop, mobile = FakeTransport("desktop"), FakeTransport("mobile")
    stream.attach(desktop, owner=True)
    stream.attach(mobile)

    assert stream.detach(desktop) is True
    assert stream.promote_next_owner() is mobile
    assert stream.owner() is mobile


def test_promote_returns_none_when_nobody_is_left():
    stream = SessionStream("s1")
    only = FakeTransport("only")
    stream.attach(only, owner=True)
    stream.detach(only)
    assert stream.promote_next_owner() is None


# ── terminal-once ──────────────────────────────────────────────────────────


def test_terminal_latch_is_per_turn():
    stream = SessionStream("s1")
    peer = FakeTransport()
    stream.attach(peer, owner=True)

    stream.begin_turn("req-1")
    stream.deliver(_frame("s1", "message.complete", {"text": "a"}))
    stream.deliver(_frame("s1", "error", {"message": "late crash"}))
    assert [f["params"]["type"] for f in peer.frames] == ["message.complete"]

    # The next turn gets its own terminal.
    stream.begin_turn("req-2")
    stream.deliver(_frame("s1", "message.complete", {"text": "b"}))
    assert [f["params"]["type"] for f in peer.frames] == [
        "message.complete", "message.complete"
    ]


def test_an_error_can_be_the_terminal_event():
    stream = SessionStream("s1")
    peer = FakeTransport()
    stream.attach(peer, owner=True)
    stream.begin_turn("req-1")

    stream.deliver(_frame("s1", "error", {"message": "boom"}))
    stream.deliver(_frame("s1", "message.complete", {"text": "too late"}))
    assert [f["params"]["type"] for f in peer.frames] == ["error"]


# ── replay ─────────────────────────────────────────────────────────────────


def test_replay_returns_only_the_missed_frames():
    stream = SessionStream("s1")
    peer = FakeTransport()
    stream.attach(peer, owner=True)
    for i in range(6):
        stream.deliver(_frame("s1", "message.delta", {"i": i}))

    frames, truncated, latest = stream.replay_since(3)
    assert [f["params"]["seq"] for f in frames] == [4, 5, 6]
    assert truncated is False
    assert latest == 6


def test_replay_reports_truncation_when_the_ring_overflowed(monkeypatch):
    monkeypatch.setattr(session_stream, "REPLAY_FRAMES", 3)
    stream = SessionStream("s1")
    peer = FakeTransport()
    stream.attach(peer, owner=True)
    for i in range(10):
        stream.deliver(_frame("s1", "message.delta", {"i": i}))

    frames, truncated, latest = stream.replay_since(1)
    assert latest == 10
    # Only the tail survives, and the client is told to do a full resume.
    assert truncated is True
    assert len(frames) <= 3


def test_replay_at_head_is_empty():
    stream = SessionStream("s1")
    stream.attach(FakeTransport(), owner=True)
    stream.deliver(_frame("s1", "message.delta"))
    assert stream.replay_since(1) == ([], False, 1)


# ── scoping ────────────────────────────────────────────────────────────────


def test_a_frame_for_another_session_is_refused():
    stream = SessionStream("s1")
    peer = FakeTransport()
    stream.attach(peer, owner=True)
    assert stream.deliver(_frame("s2", "message.delta")) is None
    assert peer.frames == []


def test_registry_delivers_only_to_the_named_session():
    registry = SessionStreamRegistry()
    a, b = FakeTransport("a"), FakeTransport("b")
    registry.ensure("s1").attach(a, owner=True)
    registry.ensure("s2").attach(b, owner=True)

    registry.deliver("s1", _frame("s1", "message.delta"))
    assert len(a.frames) == 1
    assert b.frames == []


def test_detach_transport_separates_owned_from_viewed():
    registry = SessionStreamRegistry()
    peer = FakeTransport("peer")
    other = FakeTransport("other")
    registry.ensure("owned").attach(peer, owner=True)
    registry.ensure("viewed").attach(other, owner=True)
    registry.ensure("viewed").attach(peer)

    owned, _viewed = registry.detach_transport(peer)
    assert owned == ["owned"]
    assert registry.ensure("viewed").owner() is other


# ── request ledger ─────────────────────────────────────────────────────────


def test_ledger_scopes_ids_per_session():
    ledger = RequestLedger()
    assert ledger.begin("s1", "req", "x").outcome == "accepted"
    # Same id in a different session is a different request.
    assert ledger.begin("s2", "req", "x").outcome == "accepted"
    assert ledger.status("s2", "nope") is None


def test_ledger_finish_is_first_writer_wins():
    ledger = RequestLedger()
    ledger.begin("s1", "req", "x")
    ledger.finish("s1", "req", status="complete", result={"text": "a"})
    ledger.finish("s1", "req", status="error", result={"message": "late"})
    assert ledger.status("s1", "req")["status"] == "complete"


def test_ledger_settles_a_stalled_request_within_the_bound(monkeypatch):
    monkeypatch.setattr(session_stream, "REQUEST_SETTLE_S", 0.01)
    ledger = RequestLedger()
    ledger.begin("s1", "req", "x")
    time_module = __import__("time")
    time_module.sleep(0.02)
    record = ledger.status("s1", "req")
    assert record["status"] == "stalled"
    assert "no terminal event" in record["result"]["error"]


def test_ledger_touch_keeps_a_streaming_turn_alive(monkeypatch):
    monkeypatch.setattr(session_stream, "REQUEST_SETTLE_S", 0.05)
    ledger = RequestLedger()
    ledger.begin("s1", "req", "x")
    time_module = __import__("time")
    for _ in range(4):
        time_module.sleep(0.02)
        ledger.touch("s1", "req")
    assert ledger.status("s1", "req")["status"] == "running"


def test_ledger_history_is_bounded(monkeypatch):
    monkeypatch.setattr(session_stream, "REQUEST_HISTORY", 3)
    ledger = RequestLedger()
    for i in range(6):
        ledger.begin("s1", f"req-{i}", "x")
    assert ledger.status("s1", "req-0") is None
    assert ledger.status("s1", "req-5") is not None


def test_fingerprint_distinguishes_payloads():
    assert fingerprint_prompt("a") == fingerprint_prompt("a")
    assert fingerprint_prompt("a") != fingerprint_prompt("b")
    assert fingerprint_prompt("a", (1,)) != fingerprint_prompt("a", (2,))


# ── server wiring ──────────────────────────────────────────────────────────


@pytest.fixture
def clean_server():
    server._sessions.clear()
    server.reset_session_streams()
    yield server
    server._sessions.clear()
    server.reset_session_streams()


def _live_session(sid, transport):
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
    }


def test_ownership_mirrors_into_the_legacy_transport_slot(clean_server):
    desktop, mobile = FakeTransport("desktop"), FakeTransport("mobile")
    clean_server._sessions["s1"] = _live_session("s1", None)

    assert clean_server.subscribe_session("s1", desktop, owner=True) is True
    assert clean_server._sessions["s1"]["transport"] is desktop
    # A viewer must not move the legacy slot — that was the stream theft.
    assert clean_server.subscribe_session("s1", mobile, owner=True) is False
    assert clean_server._sessions["s1"]["transport"] is desktop


def test_contract_viewer_is_not_allowed_to_mutate(clean_server):
    """A client that opted into the contract is held to single-owner rules."""
    desktop, mobile = FakeTransport("desktop"), FakeTransport("mobile")
    clean_server._sessions["s1"] = _live_session("s1", desktop)
    clean_server.subscribe_session("s1", desktop, owner=True, explicit=True)
    clean_server.subscribe_session("s1", mobile, explicit=True)

    assert clean_server.session_is_owned_by("s1", desktop) is True
    assert clean_server.session_is_owned_by("s1", mobile) is False
    # After an explicit claim the roles swap.
    assert clean_server.subscribe_session(
        "s1", mobile, owner=True, force=True, explicit=True
    ) is True
    assert clean_server.session_is_owned_by("s1", mobile) is True
    assert clean_server.session_is_owned_by("s1", desktop) is False


def test_legacy_client_keeps_the_historical_take_over(clean_server):
    """A client predating the contract must never be stranded.

    It has no ``session.claim`` to call, so it keeps driving the session as it
    always did. The difference from before the fix is that the peer it takes
    over from stays attached and keeps receiving the stream.
    """
    desktop, legacy = FakeTransport("desktop"), FakeTransport("legacy")
    clean_server._sessions["s1"] = _live_session("s1", desktop)
    clean_server.subscribe_session("s1", desktop, owner=True, explicit=True)
    clean_server.subscribe_session("s1", legacy)  # implicit attach, e.g. resume

    assert clean_server.session_is_owned_by("s1", legacy) is True
    clean_server._emit("message.delta", "s1", {"text": "x"})
    assert len(desktop.frames) == 1 and len(legacy.frames) == 1


def test_opting_into_the_contract_is_sticky(clean_server):
    desktop, mobile = FakeTransport("desktop"), FakeTransport("mobile")
    clean_server._sessions["s1"] = _live_session("s1", desktop)
    clean_server.subscribe_session("s1", desktop, owner=True, explicit=True)
    clean_server.subscribe_session("s1", mobile, explicit=True)
    # A later implicit attach must not silently downgrade to legacy semantics.
    clean_server.subscribe_session("s1", mobile)
    assert clean_server.session_is_owned_by("s1", mobile) is False


def test_a_dead_owner_never_wedges_a_reconnecting_client(clean_server):
    """Half-open socket on the far side must not lock the user out."""
    stale, fresh = FakeTransport("stale"), FakeTransport("fresh")
    clean_server._sessions["s1"] = _live_session("s1", stale)
    clean_server.subscribe_session("s1", stale, owner=True, explicit=True)
    clean_server.subscribe_session("s1", fresh, explicit=True)
    assert clean_server.session_is_owned_by("s1", fresh) is False

    stale.close()  # the socket latched closed
    assert clean_server.session_is_owned_by("s1", fresh) is True


def test_sessions_without_a_stream_stay_unrestricted(clean_server):
    """Standalone stdio/Ink gateways keep the historical behaviour."""
    clean_server._sessions["s1"] = _live_session("s1", None)
    assert clean_server.session_is_owned_by("s1", FakeTransport("anything")) is True


def test_disconnect_hands_the_session_to_the_surviving_device(clean_server, monkeypatch):
    monkeypatch.setattr(clean_server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    desktop, mobile = FakeTransport("desktop"), FakeTransport("mobile")
    session = _live_session("s1", desktop)
    clean_server._sessions["s1"] = session
    clean_server.subscribe_session("s1", desktop, owner=True)
    clean_server.subscribe_session("s1", mobile)

    reaped, detached = clean_server._close_sessions_for_transport(desktop)

    # Nothing was reaped or orphaned: another device is still holding it.
    assert (reaped, detached) == (0, 0)
    assert session["transport"] is mobile
    assert clean_server.session_owner("s1") is mobile
    assert clean_server.session_is_owned_by("s1", mobile) is True
    types_seen = [f["params"]["type"] for f in mobile.frames]
    assert "session.owner_changed" in types_seen


def test_last_client_leaving_still_detaches_to_the_reaper(clean_server, monkeypatch):
    monkeypatch.setattr(clean_server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    desktop = FakeTransport("desktop")
    session = _live_session("s1", desktop)
    clean_server._sessions["s1"] = session
    clean_server.subscribe_session("s1", desktop, owner=True)

    reaped, detached = clean_server._close_sessions_for_transport(desktop)
    assert (reaped, detached) == (0, 1)
    assert session["transport"] is clean_server._detached_ws_transport


def test_closing_a_session_forgets_its_stream_and_requests(clean_server):
    desktop = FakeTransport("desktop")
    clean_server._sessions["s1"] = _live_session("s1", desktop)
    clean_server.subscribe_session("s1", desktop, owner=True)
    clean_server.request_ledger().begin("s1", "req-1", "x")

    clean_server._pop_session_by_id("s1")

    assert clean_server.session_streams().get("s1") is None
    assert clean_server.request_ledger().status("s1", "req-1") is None
