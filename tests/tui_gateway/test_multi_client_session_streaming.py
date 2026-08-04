"""Live session events must mirror across every connected dashboard client."""

from __future__ import annotations

import threading
import time

from tui_gateway import server


EVENT_TYPES = (
    "message.start",
    "thinking.delta",
    "reasoning.delta",
    "tool.start",
    "tool.complete",
    "message.delta",
    "message.complete",
)


class RecordingTransport:
    def __init__(self) -> None:
        self.frames: list[dict] = []
        self.subscription_lock = threading.Lock()
        self.subscribed_session_id: str | None = None
        self.subscription_next_generation = 0
        self.subscription_committed_generation = 0

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        return None

    def subscribe_session(self, session_id: str) -> None:
        with self.subscription_lock:
            self.subscription_next_generation += 1
            self.subscription_committed_generation = self.subscription_next_generation
            self.subscribed_session_id = session_id

    def begin_session_subscription(self) -> int:
        with self.subscription_lock:
            self.subscription_next_generation += 1
            return self.subscription_next_generation

    def complete_session_subscription(self, generation: int, session_id: str) -> bool:
        with self.subscription_lock:
            if generation < self.subscription_committed_generation:
                return False
            self.subscription_committed_generation = generation
            self.subscribed_session_id = session_id
            return True

    def observes_session(self, session_id: str) -> bool:
        with self.subscription_lock:
            return self.subscribed_session_id == session_id

    def promote_session_if_subscribed(self, session_id: str, promote) -> bool:
        if not self.observes_session(session_id):
            return False
        return bool(promote())


class ObserverTransport(RecordingTransport):
    def write(self, obj: dict) -> bool:
        raise AssertionError("observer fan-out used the blocking transport path")

    def write_observer(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def write_observer_if_subscribed(self, session_id: str, obj: dict) -> bool:
        with self.subscription_lock:
            if self.subscribed_session_id != session_id:
                return False
            return self.write_observer(obj)


def test_mac_origin_stream_mirrors_thinking_tools_and_text_to_mobile() -> None:
    sid = "shared-session"
    mac = RecordingTransport()
    mobile = ObserverTransport()

    server._sessions[sid] = {"transport": mac}
    mobile.subscribe_session(sid)
    server.register_live_transport(mac)
    server.register_live_transport(mobile)
    try:
        for event_type in EVENT_TYPES:
            assert server.write_json(server._event_frame(event_type, sid, {"text": event_type}))
    finally:
        server.unregister_live_transport(mac)
        server.unregister_live_transport(mobile)
        server._sessions.pop(sid, None)

    assert tuple(frame["params"]["type"] for frame in mac.frames) == EVENT_TYPES
    assert tuple(frame["params"]["type"] for frame in mobile.frames) == EVENT_TYPES


def test_observer_delivery_counts_when_owner_write_fails() -> None:
    class FailedOwner(RecordingTransport):
        def write(self, obj: dict) -> bool:
            self.frames.append(obj)
            return False

    sid = "observer-only-delivery"
    owner = FailedOwner()
    observer = ObserverTransport()
    observer.subscribe_session(sid)
    server._sessions[sid] = {"transport": owner}
    server.register_live_transport(owner)
    server.register_live_transport(observer)
    try:
        assert server.write_json(server._event_frame("message.complete", sid, {}))
    finally:
        server.unregister_live_transport(owner)
        server.unregister_live_transport(observer)
        server._sessions.pop(sid, None)

    assert len(owner.frames) == 1
    assert len(observer.frames) == 1


def test_session_stream_does_not_leak_to_clients_subscribed_elsewhere() -> None:
    sid = "session-a"
    owner = RecordingTransport()
    same_session = ObserverTransport()
    unrelated = ObserverTransport()
    same_session.subscribe_session(sid)
    unrelated.subscribe_session("session-b")

    server._sessions[sid] = {"transport": owner}
    for transport in (owner, same_session, unrelated):
        server.register_live_transport(transport)
    try:
        assert server.write_json(server._event_frame("reasoning.delta", sid, {"text": "private"}))
    finally:
        for transport in (owner, same_session, unrelated):
            server.unregister_live_transport(transport)
        server._sessions.pop(sid, None)

    assert len(owner.frames) == 1
    assert len(same_session.frames) == 1
    assert unrelated.frames == []


def test_session_switch_rejects_frame_selected_before_atomic_observer_enqueue() -> None:
    session_a = "session-a-race"
    session_b = "session-b-race"
    reached_enqueue = threading.Event()
    resume_enqueue = threading.Event()

    class CoordinatedObserver(ObserverTransport):
        def _pause_before_enqueue(self) -> None:
            reached_enqueue.set()
            assert resume_enqueue.wait(timeout=3)

        def write_observer(self, obj: dict) -> bool:
            self._pause_before_enqueue()
            return super().write_observer(obj)

        def write_observer_if_subscribed(self, session_id: str, obj: dict) -> bool:
            self._pause_before_enqueue()
            return super().write_observer_if_subscribed(session_id, obj)

    owner_a = RecordingTransport()
    owner_b = RecordingTransport()
    observer = CoordinatedObserver()
    observer.subscribe_session(session_a)
    server._sessions[session_a] = {"transport": owner_a}
    server._sessions[session_b] = {"transport": owner_b}
    for transport in (owner_a, owner_b, observer):
        server.register_live_transport(transport)

    first_result: list[bool] = []
    first_write = threading.Thread(
        target=lambda: first_result.append(
            server.write_json(
                server._event_frame("message.delta", session_a, {"text": "A"})
            )
        )
    )
    try:
        first_write.start()
        assert reached_enqueue.wait(timeout=3)
        observer.subscribe_session(session_b)
        resume_enqueue.set()
        first_write.join(timeout=3)
        assert not first_write.is_alive()

        assert server.write_json(
            server._event_frame("message.complete", session_b, {"text": "B"})
        )
    finally:
        resume_enqueue.set()
        first_write.join(timeout=3)
        for transport in (owner_a, owner_b, observer):
            server.unregister_live_transport(transport)
        server._sessions.pop(session_a, None)
        server._sessions.pop(session_b, None)

    assert first_result == [True]
    assert [frame["params"]["session_id"] for frame in observer.frames] == [session_b]


def test_dispatch_subscribes_an_activated_transport_to_the_response_session() -> None:
    transport = ObserverTransport()
    original = server._methods["session.activate"]
    server._methods["session.activate"] = lambda rid, _params: server._ok(
        rid, {"session_id": "activated-session"}
    )
    try:
        response = server.dispatch(
            {"jsonrpc": "2.0", "id": 1, "method": "session.activate", "params": {}},
            transport,
        )
    finally:
        server._methods["session.activate"] = original

    assert response is not None
    assert transport.observes_session("activated-session")


def test_dispatch_subscribes_the_transport_that_submits_a_prompt() -> None:
    transport = ObserverTransport()
    original = server._methods["prompt.submit"]
    server._methods["prompt.submit"] = lambda rid, _params: server._ok(
        rid, {"accepted": True}
    )
    try:
        response = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "prompt.submit",
                "params": {"session_id": "prompt-session", "text": "hello"},
            },
            transport,
        )
    finally:
        server._methods["prompt.submit"] = original

    assert response is not None
    assert transport.observes_session("prompt-session")


def test_stale_long_attach_response_cannot_overwrite_newer_subscription() -> None:
    transport = RecordingTransport()
    started = threading.Event()
    release = threading.Event()
    original_resume = server._methods["session.resume"]
    original_activate = server._methods["session.activate"]

    def slow_resume(rid, _params):
        started.set()
        release.wait(timeout=3)
        return server._ok(rid, {"session_id": "older-session"})

    server._methods["session.resume"] = slow_resume
    server._methods["session.activate"] = lambda rid, _params: server._ok(
        rid, {"session_id": "newer-session"}
    )
    try:
        old_response = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "old",
                "method": "session.resume",
                "params": {"session_id": "stored-session"},
            },
            transport,
        )
        assert old_response is None
        assert started.wait(timeout=3)
        new_response = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "new",
                "method": "session.activate",
                "params": {"session_id": "newer-session"},
            },
            transport,
        )
        assert new_response is not None
        assert transport.observes_session("newer-session")
        release.set()
        deadline = time.monotonic() + 3
        while not any(frame.get("id") == "old" for frame in transport.frames):
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        release.set()
        server._methods["session.resume"] = original_resume
        server._methods["session.activate"] = original_activate

    assert transport.observes_session("newer-session")


def test_observer_is_queued_before_a_stalled_owner_write() -> None:
    sid = "owner-stall"
    observed_at: list[float] = []

    class SlowOwner(RecordingTransport):
        def write(self, obj: dict) -> bool:
            time.sleep(0.15)
            return super().write(obj)

    class TimedObserver(ObserverTransport):
        def write_observer(self, obj: dict) -> bool:
            observed_at.append(time.monotonic())
            return super().write_observer(obj)

    owner = SlowOwner()
    timed_observer = TimedObserver()
    timed_observer.subscribe_session(sid)
    server._sessions[sid] = {"transport": owner}
    server.register_live_transport(owner)
    server.register_live_transport(timed_observer)
    started = time.monotonic()
    try:
        assert server.write_json(server._event_frame("tool.start", sid, {}))
    finally:
        server.unregister_live_transport(owner)
        server.unregister_live_transport(timed_observer)
        server._sessions.pop(sid, None)

    assert observed_at and observed_at[0] - started < 0.05


def test_owner_disconnect_promotes_a_live_subscriber(monkeypatch) -> None:
    sid = "owner-disconnect"
    owner = RecordingTransport()
    successor = ObserverTransport()
    successor.subscribe_session(sid)
    scheduled_reaps: list[str] = []
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled_reaps.append)

    server._sessions[sid] = {"transport": owner, "close_on_disconnect": False}
    server.register_live_transport(successor)
    try:
        assert server._close_sessions_for_transport(owner) == (0, 0)
        assert server._sessions[sid]["transport"] is successor
        assert scheduled_reaps == []
    finally:
        server.unregister_live_transport(successor)
        server._sessions.pop(sid, None)


def test_owner_disconnect_does_not_detach_a_concurrently_reattached_session(
    monkeypatch,
) -> None:
    sid = "owner-reattach-race"
    teardown_snapshotted_owner = threading.Event()
    resume_teardown = threading.Event()

    class CoordinatedSession(dict):
        def get(self, key, default=None):
            value = super().get(key, default)
            if key == "close_on_disconnect":
                teardown_snapshotted_owner.set()
                assert resume_teardown.wait(timeout=3)
            return value

    old_owner = RecordingTransport()
    new_owner = ObserverTransport()
    new_owner.subscribe_session(sid)
    scheduled_reaps: list[str] = []
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled_reaps.append)
    session = CoordinatedSession(
        transport=old_owner,
        close_on_disconnect=False,
    )
    server._sessions[sid] = session
    server.register_live_transport(new_owner)

    teardown_result: list[tuple[int, int]] = []
    teardown = threading.Thread(
        target=lambda: teardown_result.append(
            server._close_sessions_for_transport(old_owner)
        )
    )
    try:
        teardown.start()
        assert teardown_snapshotted_owner.wait(timeout=3)
        with server._sessions_lock:
            session["transport"] = new_owner
        resume_teardown.set()
        teardown.join(timeout=3)
        assert not teardown.is_alive()
    finally:
        resume_teardown.set()
        teardown.join(timeout=3)
        server.unregister_live_transport(new_owner)
        server._sessions.pop(sid, None)

    assert teardown_result == [(0, 0)]
    assert session["transport"] is new_owner
    assert scheduled_reaps == []


def test_owner_disconnect_skips_a_non_atomic_subscription_check(monkeypatch) -> None:
    class NonAtomicObserver:
        def write(self, obj: dict) -> bool:
            del obj
            return True

        def write_observer(self, obj: dict) -> bool:
            del obj
            return True

        def close(self) -> None:
            return None

        def observes_session(self, session_id: str) -> bool:
            return session_id == "sid-racy"

    owner = RecordingTransport()
    observer = NonAtomicObserver()
    scheduled_reaps: list[str] = []
    session = {"transport": owner, "close_on_disconnect": False}
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled_reaps.append)
    server._sessions["sid-racy"] = session
    server.register_live_transport(observer)
    try:
        assert server._close_sessions_for_transport(owner) == (0, 1)
    finally:
        server.unregister_live_transport(observer)
        server._sessions.pop("sid-racy", None)

    assert session["transport"] is not observer
    assert scheduled_reaps == ["sid-racy"]
