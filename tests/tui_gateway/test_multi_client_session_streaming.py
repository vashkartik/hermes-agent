"""Live session events must mirror across every connected dashboard client."""

from __future__ import annotations

import asyncio
import threading
import time

from tui_gateway import server
from tui_gateway import ws as ws_mod


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

    def complete_session_subscription(
        self, generation: int, session_id: str, claim_owner=None
    ) -> bool:
        with self.subscription_lock:
            if generation < self.subscription_committed_generation:
                return False
            if claim_owner is not None and not claim_owner():
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


def _start_paused_cold_resume(monkeypatch, transport, *, stored_session_id: str):
    build_reached = threading.Event()
    release_build = threading.Event()
    runtime_session_ids: list[str] = []
    scheduled_reaps: list[str] = []

    class FakeLease:
        def __init__(self):
            self.released = False

        def release(self):
            self.released = True

    class FakeDB:
        def get_session(self, session_id):
            if session_id == stored_session_id:
                return {"id": session_id, "cwd": ""}
            return None

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, session_id):
            return session_id

        def reopen_session(self, _session_id):
            return None

        def get_resume_conversations(self, _session_id):
            return [], []

        def get_ancestor_display_prefix(self, _session_id):
            return []

    lease = FakeLease()

    def pause_agent_build(session_id, _delay=0.05):
        runtime_session_ids.append(session_id)
        build_reached.set()
        assert release_build.wait(timeout=3)

    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(
        server,
        "_claim_active_session_slot",
        lambda *_args, **_kwargs: (lease, None),
    )
    monkeypatch.setattr(server, "_schedule_agent_build", pause_agent_build)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled_reaps.append)

    assert server.dispatch(
        {
            "jsonrpc": "2.0",
            "id": f"resume-{stored_session_id}",
            "method": "session.resume",
            "params": {"session_id": stored_session_id},
        },
        transport,
    ) is None
    assert build_reached.wait(timeout=3)
    assert len(runtime_session_ids) == 1
    return (
        runtime_session_ids[0],
        release_build,
        scheduled_reaps,
        lease,
    )


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


def test_stale_resume_cannot_reclaim_ownership_or_leak_private_events(
    monkeypatch,
) -> None:
    """A completed newer activation owns both subscription and event routing."""
    stored_x = "stored-session-x"
    runtime_x = "runtime-session-x"
    runtime_y = "runtime-session-y"
    resume_reached_payload = threading.Event()
    release_resume = threading.Event()

    class PausedLock:
        def __enter__(self):
            resume_reached_payload.set()
            assert release_resume.wait(timeout=3)

        def __exit__(self, _exc_type, _exc, _tb):
            return False

    class FakeDB:
        def get_session(self, session_id):
            return {"id": session_id, "cwd": ""} if session_id == stored_x else None

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, session_id):
            return session_id

        def get_messages_as_conversation(self, _session_id, **_kwargs):
            return []

    class ClientTransport(RecordingTransport):
        def write_observer_if_subscribed(self, session_id: str, obj: dict) -> bool:
            with self.subscription_lock:
                if self.subscribed_session_id != session_id:
                    return False
                self.frames.append(obj)
                return True

    def live_session(session_key, transport, history_lock):
        now = time.time()
        return {
            "agent": None,
            "created_at": now,
            "history": [],
            "history_lock": history_lock,
            "last_active": now,
            "running": False,
            "session_key": session_key,
            "transport": transport,
        }

    x_owner = RecordingTransport()
    y_owner = RecordingTransport()
    client = ClientTransport()
    scheduled_reaps: list[str] = []
    session_x = live_session(stored_x, x_owner, PausedLock())
    session_y = live_session("stored-session-y", y_owner, threading.Lock())
    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled_reaps.append)
    server._sessions[runtime_x] = session_x
    server._sessions[runtime_y] = session_y
    server.register_live_transport(client)

    try:
        assert server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "resume-x",
                "method": "session.resume",
                "params": {"session_id": stored_x},
            },
            client,
        ) is None
        assert resume_reached_payload.wait(timeout=3)

        activate_y = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "activate-y",
                "method": "session.activate",
                "params": {"session_id": runtime_y},
            },
            client,
        )
        assert activate_y is not None
        assert client.observes_session(runtime_y)

        release_resume.set()
        deadline = time.monotonic() + 3
        while not any(frame.get("id") == "resume-x" for frame in client.frames):
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert server.write_json(
            server._event_frame("message.delta", runtime_x, {"text": "private-x"})
        )
    finally:
        release_resume.set()
        server.unregister_live_transport(client)
        server._sessions.pop(runtime_x, None)
        server._sessions.pop(runtime_y, None)

    assert client.observes_session(runtime_y)
    assert session_x["transport"] is x_owner
    assert session_y["transport"] is client
    assert scheduled_reaps == []
    assert not any(
        frame.get("method") == "event"
        and (frame.get("params") or {}).get("session_id") == runtime_x
        for frame in client.frames
    )


def test_closed_transport_cannot_claim_paused_cold_resume_and_schedules_reap(
    monkeypatch,
) -> None:
    response_attempted = threading.Event()

    class ResponseAwareTransport(ws_mod.WSTransport):
        def write(self, _obj: dict) -> bool:
            response_attempted.set()
            return not self._closed

    loop = asyncio.new_event_loop()
    client = ResponseAwareTransport(object(), loop, peer="closed-resume-test")
    server.register_live_transport(client)
    runtime_sid = ""
    release_resume = threading.Event()
    lease = None
    try:
        runtime_sid, release_resume, scheduled_reaps, lease = _start_paused_cold_resume(
            monkeypatch,
            client,
            stored_session_id="stored-closed-resume",
        )
        session = server._sessions[runtime_sid]
        assert session["transport"] is server._detached_ws_transport

        server.unregister_live_transport(client)
        client.close()
        assert server._close_sessions_for_transport(client) == (0, 0)

        release_resume.set()
        assert response_attempted.wait(timeout=3)

        assert not client.observes_session(runtime_sid)
        assert session["transport"] is server._detached_ws_transport
        assert scheduled_reaps == [runtime_sid]
        assert session["active_session_lease"] is lease
    finally:
        release_resume.set()
        server.unregister_live_transport(client)
        client.close()
        session = server._sessions.pop(runtime_sid, None) if runtime_sid else None
        if session is not None and (held_lease := session.get("active_session_lease")):
            held_lease.release()
        loop.close()


def test_close_serializes_with_an_inflight_subscription_claim() -> None:
    loop = asyncio.new_event_loop()
    transport = ws_mod.WSTransport(object(), loop, peer="close-claim-race-test")
    generation = transport.begin_session_subscription()
    claim_started = threading.Event()
    close_started = threading.Event()
    release_claim = threading.Event()
    completion_result: list[bool] = []

    def claim_owner() -> bool:
        claim_started.set()
        assert close_started.wait(timeout=3)
        assert release_claim.wait(timeout=3)
        return True

    complete_thread = threading.Thread(
        target=lambda: completion_result.append(
            transport.complete_session_subscription(
                generation,
                "claimed-session",
                claim_owner,
            )
        )
    )

    def close_transport() -> None:
        close_started.set()
        transport.close()

    close_thread = threading.Thread(target=close_transport)
    try:
        complete_thread.start()
        assert claim_started.wait(timeout=3)
        close_thread.start()
        assert close_started.wait(timeout=3)
        release_claim.set()
        complete_thread.join(timeout=3)
        close_thread.join(timeout=3)

        assert not complete_thread.is_alive()
        assert not close_thread.is_alive()
        assert completion_result == [True]
        assert transport._closed
        assert not transport.observes_session("claimed-session")
    finally:
        release_claim.set()
        complete_thread.join(timeout=3)
        close_thread.join(timeout=3)
        transport.close()
        loop.close()


def test_send_failure_cleans_owner_claim_that_won_subscription_lock(
    monkeypatch,
) -> None:
    sid = "send-failure-claim-race"
    claim_started = threading.Event()
    failure_mark_attempted = threading.Event()
    release_claim = threading.Event()
    completion_result: list[bool] = []
    owner_claimed: list[bool] = []
    scheduled_reaps: list[str] = []

    class FailingWS:
        async def send_text(self, _line: str) -> None:
            raise RuntimeError("socket gone")

    class CoordinatedTransport(ws_mod.WSTransport):
        def _mark_closed(self, *, schedule_owner_cleanup: bool) -> bool:
            failure_mark_attempted.set()
            return super()._mark_closed(
                schedule_owner_cleanup=schedule_owner_cleanup
            )

    loop = asyncio.new_event_loop()
    transport = CoordinatedTransport(FailingWS(), loop, peer="send-claim-race-test")
    generation = transport.begin_session_subscription()
    session = {
        "close_on_disconnect": False,
        "running": False,
        "transport": RecordingTransport(),
    }
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled_reaps.append)
    server._sessions[sid] = session
    server.register_live_transport(transport)

    def claim_owner() -> bool:
        claim_started.set()
        assert release_claim.wait(timeout=3)
        with server._sessions_lock:
            session["transport"] = transport
            owner_claimed.append(True)
        return True

    complete_thread = threading.Thread(
        target=lambda: completion_result.append(
            transport.complete_session_subscription(
                generation,
                sid,
                claim_owner,
            )
        )
    )
    failure_thread = threading.Thread(
        target=lambda: asyncio.run(transport._safe_send_many(["frame"]))
    )
    try:
        complete_thread.start()
        assert claim_started.wait(timeout=3)
        failure_thread.start()
        assert failure_mark_attempted.wait(timeout=3)
        release_claim.set()
        complete_thread.join(timeout=3)
        failure_thread.join(timeout=3)
        assert not complete_thread.is_alive()
        assert not failure_thread.is_alive()

        deadline = time.monotonic() + 3
        while session["transport"] is transport:
            assert time.monotonic() < deadline
            time.sleep(0.01)

        assert completion_result == [True]
        assert owner_claimed == [True]
        assert transport._closed
        assert not transport.observes_session(sid)
        assert session["transport"] is server._detached_ws_transport
        assert scheduled_reaps == [sid]
    finally:
        release_claim.set()
        complete_thread.join(timeout=3)
        failure_thread.join(timeout=3)
        server.unregister_live_transport(transport)
        transport.close()
        server._sessions.pop(sid, None)
        loop.close()


def test_failed_newer_attach_does_not_invalidate_older_cold_resume(monkeypatch) -> None:
    response_attempted = threading.Event()

    class ResponseAwareTransport(ws_mod.WSTransport):
        def write(self, _obj: dict) -> bool:
            response_attempted.set()
            return not self._closed

    loop = asyncio.new_event_loop()
    client = ResponseAwareTransport(object(), loop, peer="failed-attach-test")
    server.register_live_transport(client)
    runtime_sid = ""
    release_resume = threading.Event()
    try:
        runtime_sid, release_resume, scheduled_reaps, _lease = _start_paused_cold_resume(
            monkeypatch,
            client,
            stored_session_id="stored-generation-resume",
        )
        session = server._sessions[runtime_sid]
        assert session["transport"] is server._detached_ws_transport

        failed_activate = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "failed-newer-activate",
                "method": "session.activate",
                "params": {"session_id": "missing-newer-session"},
            },
            client,
        )
        assert failed_activate is not None and "error" in failed_activate

        release_resume.set()
        assert response_attempted.wait(timeout=3)

        assert client.observes_session(runtime_sid)
        assert session["transport"] is client
        assert scheduled_reaps == []
    finally:
        release_resume.set()
        server.unregister_live_transport(client)
        client.close()
        session = server._sessions.pop(runtime_sid, None) if runtime_sid else None
        if session is not None and (lease := session.get("active_session_lease")):
            lease.release()
        loop.close()


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
