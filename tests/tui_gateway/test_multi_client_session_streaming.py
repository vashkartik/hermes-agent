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
        # The active-session slot is claimed lazily on the first turn now, so a
        # rejected cold resume must leave the slot untouched (nothing to leak).
        assert session["active_session_lease"] is None
        assert lease.released is False
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


def test_failed_delivery_does_not_deadlock_ordered_session_attach(
    monkeypatch,
) -> None:
    """A failed owner write must not hold the stream lock while closing.

    The pre-fix cycle was::

        deliver: stream lock -> WSTransport.write -> subscription lock
        attach:  subscription lock -> sessions lock -> stream lock

    The bounded wrapper lets the regression unwind cleanly instead of leaving
    two permanently blocked test threads behind.
    """
    from agent import async_utils

    sid = "failed-delivery-ordered-attach"
    write_scheduling = threading.Event()
    attach_attempted = threading.Event()
    attach_finished = threading.Event()
    attach_stalled = threading.Event()
    completion_finished = threading.Event()
    attach_threads: list[threading.Thread] = []

    loop = asyncio.new_event_loop()
    transport = ws_mod.WSTransport(object(), loop, peer="delivery-attach-deadlock")
    monkeypatch.setattr(transport, "_schedule_owner_cleanup", lambda: None)
    generation = transport.begin_session_subscription()
    session = {"transport": transport, "close_on_disconnect": False}
    server._sessions[sid] = session
    server.subscribe_session(sid, transport, owner=True, explicit=True)
    stream = server.session_streams().get(sid)
    assert stream is not None
    real_subscribe_session = server.subscribe_session

    def bounded_subscribe_session(*args, **kwargs):
        result: list[bool] = []

        def attach() -> None:
            result.append(real_subscribe_session(*args, **kwargs))
            attach_finished.set()

        attach_attempted.set()
        thread = threading.Thread(target=attach)
        attach_threads.append(thread)
        thread.start()
        if not attach_finished.wait(timeout=2):
            attach_stalled.set()
            return False
        return result[0]

    def fail_schedule(coro, _loop):
        coro.close()
        write_scheduling.set()
        assert attach_attempted.wait(timeout=3)
        return None

    monkeypatch.setattr(server, "subscribe_session", bounded_subscribe_session)
    monkeypatch.setattr(async_utils, "safe_schedule_threadsafe", fail_schedule)

    deliver_thread = threading.Thread(
        target=lambda: stream.deliver(
            server._event_frame("message.start", sid, {})
        )
    )

    def complete_subscription() -> None:
        server._record_transport_subscription(
            transport,
            "session.resume",
            {"session_id": sid},
            {"result": {"session_id": sid}},
            generation,
        )
        completion_finished.set()

    complete_thread = threading.Thread(target=complete_subscription)
    try:
        deliver_thread.start()
        assert write_scheduling.wait(timeout=3)
        complete_thread.start()
        complete_thread.join(timeout=3)
        deliver_thread.join(timeout=3)
        for thread in attach_threads:
            thread.join(timeout=3)

        assert completion_finished.is_set()
        assert not complete_thread.is_alive()
        assert not deliver_thread.is_alive()
        assert all(not thread.is_alive() for thread in attach_threads)
        assert not attach_stalled.is_set()
    finally:
        if complete_thread.ident is not None:
            complete_thread.join(timeout=3)
        deliver_thread.join(timeout=3)
        for thread in attach_threads:
            thread.join(timeout=3)
        transport.close()
        server._sessions.pop(sid, None)
        server.reset_session_streams()
        loop.close()


def test_send_failure_before_stdio_prompt_rebind_cannot_install_dead_owner(
    monkeypatch,
) -> None:
    from hermes_cli import input_sanitize

    sid = "send-failure-before-stdio-prompt-rebind"
    prompt_before_lookup = threading.Event()
    release_prompt = threading.Event()
    cleanup_done = threading.Event()
    cleanup_results: list[tuple[int, int]] = []
    queued_responses: list[dict] = []
    dispatch_responses: list[dict] = []

    class FailingWS:
        async def send_text(self, _line: str) -> None:
            raise RuntimeError("socket gone")

    loop = asyncio.new_event_loop()
    transport = ws_mod.WSTransport(
        FailingWS(), loop, peer="stdio-prompt-rebind-race-test"
    )
    session = {
        "history_lock": threading.Lock(),
        "running": True,
        "transport": server._stdio_transport,
    }
    original_cleanup = server._close_sessions_for_transport
    original_sanitize = input_sanitize.sanitize_user_prompt_text

    def record_cleanup(owner, *, end_reason="ws_disconnect"):
        result = original_cleanup(owner, end_reason=end_reason)
        cleanup_results.append(result)
        cleanup_done.set()
        return result

    def pause_before_session_lookup(raw_text: str) -> str:
        prompt_before_lookup.set()
        assert release_prompt.wait(timeout=3)
        return original_sanitize(raw_text)

    def queue_prompt(rid, *_args, **_kwargs):
        response = server._ok(rid, {"status": "queued"})
        queued_responses.append(response)
        return response

    def dispatch_prompt() -> None:
        dispatch_responses.append(
            server.dispatch(
                {
                    "id": "prompt-race",
                    "method": "prompt.submit",
                    "params": {"session_id": sid, "text": "hello"},
                },
                transport,
            )
        )

    monkeypatch.setattr(server, "_close_sessions_for_transport", record_cleanup)
    monkeypatch.setattr(
        input_sanitize,
        "sanitize_user_prompt_text",
        pause_before_session_lookup,
    )
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_handle_busy_submit", queue_prompt)
    server._sessions[sid] = session
    prompt_thread = threading.Thread(target=dispatch_prompt)
    try:
        prompt_thread.start()
        assert prompt_before_lookup.wait(timeout=3)
        asyncio.run(transport._safe_send_many(["frame"]))
        assert cleanup_done.wait(timeout=3)
        assert cleanup_results == [(0, 0)]
        assert session["transport"] is server._stdio_transport

        release_prompt.set()
        prompt_thread.join(timeout=3)
        assert not prompt_thread.is_alive()

        assert queued_responses[0]["result"]["status"] == "queued"
        assert dispatch_responses == queued_responses
        assert session["transport"] is server._stdio_transport
        assert transport._closed
    finally:
        release_prompt.set()
        prompt_thread.join(timeout=3)
        transport.close()
        server._sessions.pop(sid, None)
        loop.close()


def test_stdio_rebind_cannot_overwrite_successor_that_wins_before_claim() -> None:
    sid = "stdio-rebind-successor-race"
    successor = RecordingTransport()
    session = {
        "cols": 80,
        "transport": server._stdio_transport,
    }

    class CoordinatedTransport(ws_mod.WSTransport):
        def claim_session_if_live(self, claim_owner) -> bool:
            with server._sessions_lock:
                session["transport"] = successor
            return super().claim_session_if_live(claim_owner)

    loop = asyncio.new_event_loop()
    candidate = CoordinatedTransport(object(), loop, peer="stdio-successor-race-test")
    server._sessions[sid] = session
    token = server.bind_transport(candidate)
    try:
        response = server.handle_request(
            {
                "id": "resize-race",
                "method": "terminal.resize",
                "params": {"session_id": sid, "cols": 120},
            }
        )

        assert response["result"]["cols"] == 120
        assert session["transport"] is successor
    finally:
        server.reset_transport(token)
        candidate.close()
        server._sessions.pop(sid, None)
        loop.close()


def test_send_failure_before_queued_drain_preserves_live_successor(
    monkeypatch,
) -> None:
    sid = "send-failure-before-queued-drain"
    ran_prompt: list[str] = []
    claim_saw_unlocked_history: list[bool] = []
    history_lock = threading.Lock()

    class FailingWS:
        async def send_text(self, _line: str) -> None:
            raise RuntimeError("socket gone")

    class CoordinatedTransport(ws_mod.WSTransport):
        def claim_session_if_live(self, claim_owner) -> bool:
            acquired = history_lock.acquire(blocking=False)
            claim_saw_unlocked_history.append(acquired)
            if acquired:
                history_lock.release()
            return super().claim_session_if_live(claim_owner)

    loop = asyncio.new_event_loop()
    failed = CoordinatedTransport(FailingWS(), loop, peer="queued-drain-race-test")
    successor = RecordingTransport()
    successor.subscribe_session(sid)
    session = {
        "close_on_disconnect": False,
        "history_lock": history_lock,
        "queued_prompt": {"text": "run queued", "transport": failed},
        "running": False,
        "transport": failed,
    }

    def run_prompt(_rid, prompt_sid, _session, text, **_kwargs) -> None:
        ran_prompt.append(text)
        server._emit("message.delta", prompt_sid, {"text": "first delta"})

    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", lambda _sid: None)
    monkeypatch.setattr(server, "_claim_update_turn", lambda _session: True)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    server._sessions[sid] = session
    server.register_live_transport(failed)
    server.register_live_transport(successor)
    try:
        asyncio.run(failed._safe_send_many(["frame"]))
        deadline = time.monotonic() + 3
        while session["transport"] is failed:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert session["transport"] is successor

        # Ownership is already committed. Remove the successor from observer
        # fan-out so the first delta proves direct owner routing after drain.
        server.unregister_live_transport(successor)
        assert server._drain_queued_prompt("queued-race", sid, session)

        assert ran_prompt == ["run queued"]
        assert claim_saw_unlocked_history == [True]
        assert session["running"] is True
        assert session["transport"] is successor
        assert [frame["params"]["type"] for frame in successor.frames] == [
            "message.delta"
        ]
    finally:
        server.unregister_live_transport(failed)
        server.unregister_live_transport(successor)
        failed.close()
        server._sessions.pop(sid, None)
        loop.close()


def test_live_prompt_claim_routes_its_first_delta_to_the_submitting_socket(
    monkeypatch,
) -> None:
    sid = "live-prompt-first-delta"
    delta_emitted = threading.Event()

    class RecordingWS(ws_mod.WSTransport):
        def __init__(self, loop) -> None:
            super().__init__(object(), loop, peer="live-prompt-delta-test")
            self.frames: list[dict] = []

        def write(self, obj: dict) -> bool:
            self.frames.append(obj)
            return True

    loop = asyncio.new_event_loop()
    transport = RecordingWS(loop)
    previous_owner = RecordingTransport()
    session = {
        "attached_images": [],
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "session_key": "live-prompt-session-key",
        "transport": previous_owner,
    }

    def run_prompt(_rid, prompt_sid, _session, _text, **_kwargs) -> None:
        server._emit("message.delta", prompt_sid, {"text": "first delta"})
        delta_emitted.set()

    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    monkeypatch.setattr(server, "_claim_update_turn", lambda _session: True)
    monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
    monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
    monkeypatch.setattr(server, "_start_agent_build", lambda *_args: None)
    monkeypatch.setattr(server, "_wait_agent", lambda *_args: None)
    monkeypatch.setattr(server, "_run_prompt_submit", run_prompt)
    server._sessions[sid] = session
    token = server.bind_transport(transport)
    try:
        response = server.handle_request(
            {
                "id": "live-prompt",
                "method": "prompt.submit",
                "params": {"session_id": sid, "text": "hello"},
            }
        )
        assert response["result"]["status"] == "streaming"
        assert delta_emitted.wait(timeout=3)

        assert session["transport"] is transport
        assert [frame["params"]["type"] for frame in transport.frames] == [
            "message.delta"
        ]
    finally:
        server.reset_transport(token)
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


class _RecordingWS(ws_mod.WSTransport):
    """Real WSTransport subscription machinery with in-memory delivery."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def write_observer(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def event_session_ids(self) -> list[str]:
        return [
            (frame.get("params") or {}).get("session_id")
            for frame in self.frames
            if frame.get("method") == "event"
        ]


def _completed_cold_resume(monkeypatch, client, *, stored_session_id: str):
    """Drive a REAL session.resume through dispatch to a committed ownership."""
    runtime_sid, release_build, scheduled_reaps, lease = _start_paused_cold_resume(
        monkeypatch,
        client,
        stored_session_id=stored_session_id,
    )
    release_build.set()
    deadline = time.monotonic() + 3
    response_id = f"resume-{stored_session_id}"
    while not any(frame.get("id") == response_id for frame in client.frames):
        assert time.monotonic() < deadline
        time.sleep(0.01)
    assert server.session_owner(runtime_sid) is client
    assert server._sessions[runtime_sid]["transport"] is client
    assert client.observes_session(runtime_sid)
    return runtime_sid, scheduled_reaps, lease


def test_newer_switch_promotes_viewer_and_stops_stale_resume_routing(
    monkeypatch,
) -> None:
    """A committed cold resume loses its attachment on the next switch.

    The durable contract is one observed session per socket. Before the fix the
    old stream kept this client as OWNER after it activated another session, and
    ``SessionStream.deliver`` writes owners through the ungated ``write`` path —
    every private frame of the abandoned session kept landing on the switched
    client. A watching peer must inherit the session instead.
    """
    loop = asyncio.new_event_loop()
    client = _RecordingWS(object(), loop, peer="switch-close-owner-test")
    # A plain recording transport: once promoted to owner it legitimately
    # receives blocking writes (session.owner_changed, then the live stream).
    viewer = RecordingTransport()
    y_owner = RecordingTransport()
    newer_sid = "runtime-switch-newer"
    old_sid = ""
    lease = None
    server.register_live_transport(client)
    try:
        old_sid, scheduled_reaps, lease = _completed_cold_resume(
            monkeypatch, client, stored_session_id="stored-switch-owner"
        )
        assert server.subscribe_session(old_sid, viewer, explicit=True) is False
        viewer.subscribe_session(old_sid)

        now = time.time()
        server._sessions[newer_sid] = {
            "agent": None,
            "created_at": now,
            "history": [],
            "history_lock": threading.Lock(),
            "last_active": now,
            "running": False,
            "session_key": "stored-switch-newer",
            "transport": y_owner,
        }
        activate = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "activate-newer",
                "method": "session.activate",
                "params": {"session_id": newer_sid},
            },
            client,
        )
        assert activate is not None and "error" not in activate
        assert client.observes_session(newer_sid)

        # The stale attachment is closed and the watching peer inherits it.
        assert server.session_owner(old_sid) is viewer
        assert server._sessions[old_sid]["transport"] is viewer
        assert scheduled_reaps == []

        frames_before = len(client.frames)
        assert server.write_json(
            server._event_frame("message.delta", old_sid, {"text": "private-old"})
        )
        assert old_sid in [
            (frame.get("params") or {}).get("session_id") for frame in viewer.frames
        ]
        assert old_sid not in client.event_session_ids()[frames_before:]
        assert not any(
            frame.get("method") == "event"
            and (frame.get("params") or {}).get("session_id") == old_sid
            for frame in client.frames[frames_before:]
        )
    finally:
        server.unregister_live_transport(client)
        client.close()
        for sid in (old_sid, newer_sid):
            session = server._sessions.pop(sid, None) if sid else None
            if session is not None and (held := session.get("active_session_lease")):
                held.release()
        loop.close()


def test_newer_switch_parks_unwatched_stale_resume_for_the_reaper(
    monkeypatch,
) -> None:
    """Switching away from an unwatched cold resume parks it on the drop
    sentinel (zero further private routing to this client) and hands it to the
    grace reaper, exactly like a disconnect would."""
    loop = asyncio.new_event_loop()
    client = _RecordingWS(object(), loop, peer="switch-close-park-test")
    y_owner = RecordingTransport()
    newer_sid = "runtime-switch-park-newer"
    old_sid = ""
    lease = None
    server.register_live_transport(client)
    try:
        old_sid, scheduled_reaps, lease = _completed_cold_resume(
            monkeypatch, client, stored_session_id="stored-switch-park"
        )
        now = time.time()
        server._sessions[newer_sid] = {
            "agent": None,
            "created_at": now,
            "history": [],
            "history_lock": threading.Lock(),
            "last_active": now,
            "running": False,
            "session_key": "stored-switch-park-newer",
            "transport": y_owner,
        }
        activate = server.dispatch(
            {
                "jsonrpc": "2.0",
                "id": "activate-park-newer",
                "method": "session.activate",
                "params": {"session_id": newer_sid},
            },
            client,
        )
        assert activate is not None and "error" not in activate
        assert client.observes_session(newer_sid)

        assert server.session_owner(old_sid) is None
        assert server._sessions[old_sid]["transport"] is server._detached_ws_transport
        assert scheduled_reaps == [old_sid]

        frames_before = len(client.frames)
        server.write_json(
            server._event_frame("message.delta", old_sid, {"text": "private-old"})
        )
        assert not any(
            frame.get("method") == "event"
            and (frame.get("params") or {}).get("session_id") == old_sid
            for frame in client.frames[frames_before:]
        )
    finally:
        server.unregister_live_transport(client)
        client.close()
        for sid in (old_sid, newer_sid):
            session = server._sessions.pop(sid, None) if sid else None
            if session is not None and (held := session.get("active_session_lease")):
                held.release()
        loop.close()


def test_prompt_submit_rejects_claim_lost_after_ownership_precheck(monkeypatch) -> None:
    """Ownership moving between the pre-check and the atomic claim fails the
    submit instead of interleaving two drivers into one transcript."""
    sid = "prompt-claim-race"
    owner = RecordingTransport()
    challenger = RecordingTransport()
    session = {
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": sid,
        "transport": owner,
    }
    server._sessions[sid] = session
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", lambda: {})
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda *_args: False)
    try:
        assert server.subscribe_session(sid, owner, owner=True, explicit=True)
        server.subscribe_session(sid, challenger, explicit=True)
        # Simulate the pre-check reading stale ownership (the other device's
        # claim lands right after it); the atomic claim must still refuse.
        monkeypatch.setattr(server, "session_is_owned_by", lambda *_a, **_k: True)
        response = server.dispatch(
            {
                "id": "prompt-claim",
                "method": "prompt.submit",
                "params": {"session_id": sid, "text": "hello"},
            },
            challenger,
        )
        assert response is not None and response["error"]["code"] == 4092
        assert session["transport"] is owner
        assert server.session_owner(sid) is owner
    finally:
        server._sessions.pop(sid, None)


def test_sole_owner_unsubscribe_stops_private_routing_and_parks(monkeypatch) -> None:
    """An explicit unsubscribe is a full release: the legacy transport slot and
    the socket's own subscription must stop pointing at the session, so no
    later private frame reaches the departed client."""
    loop = asyncio.new_event_loop()
    client = _RecordingWS(object(), loop, peer="unsubscribe-release-test")
    scheduled_reaps: list[str] = []
    sid = "unsubscribe-sole-owner"
    session = {
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": sid,
        "transport": client,
    }
    server._sessions[sid] = session
    server.register_live_transport(client)
    monkeypatch.setattr(server, "_schedule_ws_orphan_reap", scheduled_reaps.append)
    try:
        assert server.subscribe_session(sid, client, owner=True, explicit=True)
        client.subscribe_session(sid)
        response = server.dispatch(
            {
                "id": "unsub",
                "method": "session.unsubscribe",
                "params": {"session_id": sid},
            },
            client,
        )
        assert response is not None
        assert response["result"]["released_ownership"] is True
        assert session["transport"] is server._detached_ws_transport
        assert scheduled_reaps == [sid]
        assert not client.observes_session(sid)

        frames_before = len(client.frames)
        server.write_json(
            server._event_frame("message.delta", sid, {"text": "private"})
        )
        assert not any(
            frame.get("method") == "event"
            and (frame.get("params") or {}).get("session_id") == sid
            for frame in client.frames[frames_before:]
        )
    finally:
        server.unregister_live_transport(client)
        client.close()
        server._sessions.pop(sid, None)
        loop.close()


def _live_session_dict(session_key: str, transport) -> dict:
    now = time.time()
    return {
        "agent": None,
        "created_at": now,
        "history": [],
        "history_lock": threading.Lock(),
        "last_active": now,
        "running": False,
        "session_key": session_key,
        "transport": transport,
    }


def test_claim_then_switch_releases_prior_ownership() -> None:
    """session.claim is an ordered attach: the socket tracks it, and a newer
    switch releases the claimed session back to the displaced owner."""
    loop = asyncio.new_event_loop()
    client = _RecordingWS(object(), loop, peer="claim-switch-test")
    x_owner = RecordingTransport()
    y_owner = RecordingTransport()
    sid_a = "claim-switch-a"
    sid_b = "claim-switch-b"
    server._sessions[sid_a] = session_a = _live_session_dict("stored-claim-a", x_owner)
    server._sessions[sid_b] = _live_session_dict("stored-claim-b", y_owner)
    server.register_live_transport(client)
    try:
        assert server.subscribe_session(sid_a, x_owner, owner=True, explicit=True)
        claim = server.dispatch(
            {"id": "claim-a", "method": "session.claim",
             "params": {"session_id": sid_a}},
            client,
        )
        assert claim is not None and claim["result"]["owner"] is True
        assert client.observes_session(sid_a)
        assert server.session_owner(sid_a) is client

        activate = server.dispatch(
            {"id": "activate-b", "method": "session.activate",
             "params": {"session_id": sid_b}},
            client,
        )
        assert activate is not None and "error" not in activate
        assert client.observes_session(sid_b)

        # The displaced owner (still watching) inherits the claimed session.
        assert server.session_owner(sid_a) is x_owner
        assert session_a["transport"] is x_owner

        frames_before = len(client.frames)
        assert server.write_json(
            server._event_frame("message.delta", sid_a, {"text": "private-a"})
        )
        assert not any(
            frame.get("method") == "event"
            and (frame.get("params") or {}).get("session_id") == sid_a
            for frame in client.frames[frames_before:]
        )
    finally:
        server.unregister_live_transport(client)
        client.close()
        server._sessions.pop(sid_a, None)
        server._sessions.pop(sid_b, None)
        loop.close()


def test_subscribe_then_switch_detaches_viewer() -> None:
    """A watch-only session.subscribe is tracked and released on switch, and
    its commit never steals ownership from the live owner."""
    loop = asyncio.new_event_loop()
    client = _RecordingWS(object(), loop, peer="subscribe-switch-test")
    x_owner = RecordingTransport()
    y_owner = RecordingTransport()
    sid_a = "subscribe-switch-a"
    sid_b = "subscribe-switch-b"
    server._sessions[sid_a] = session_a = _live_session_dict("stored-sub-a", x_owner)
    server._sessions[sid_b] = _live_session_dict("stored-sub-b", y_owner)
    server.register_live_transport(client)
    try:
        assert server.subscribe_session(sid_a, x_owner, owner=True, explicit=True)
        subscribe = server.dispatch(
            {"id": "subscribe-a", "method": "session.subscribe",
             "params": {"session_id": sid_a}},
            client,
        )
        assert subscribe is not None and subscribe["result"]["owner"] is False
        assert client.observes_session(sid_a)
        assert server.session_owner(sid_a) is x_owner

        # As an attached viewer the client mirrors the stream.
        assert server.write_json(
            server._event_frame("message.delta", sid_a, {"text": "watched"})
        )
        assert any(
            (frame.get("params") or {}).get("session_id") == sid_a
            for frame in client.frames
        )

        activate = server.dispatch(
            {"id": "activate-b", "method": "session.activate",
             "params": {"session_id": sid_b}},
            client,
        )
        assert activate is not None and "error" not in activate
        assert client.observes_session(sid_b)
        assert server.session_owner(sid_a) is x_owner
        assert session_a["transport"] is x_owner

        frames_before = len(client.frames)
        assert server.write_json(
            server._event_frame("message.delta", sid_a, {"text": "private-a"})
        )
        assert not any(
            (frame.get("params") or {}).get("session_id") == sid_a
            for frame in client.frames[frames_before:]
        )
    finally:
        server.unregister_live_transport(client)
        client.close()
        server._sessions.pop(sid_a, None)
        server._sessions.pop(sid_b, None)
        loop.close()


def test_concurrent_claims_keep_owner_and_legacy_slot_consistent() -> None:
    """Racing forced claims must leave the stream owner and the legacy
    ``session["transport"]`` slot agreeing — disconnect cleanup selects by the
    legacy slot, so divergence would strand the real owner's sessions."""
    sid = "concurrent-claim-race"
    client_a = RecordingTransport()
    client_b = RecordingTransport()
    try:
        for _ in range(50):
            session = _live_session_dict("stored-claim-race", server._stdio_transport)
            server._sessions[sid] = session
            barrier = threading.Barrier(2)

            def claim(transport) -> None:
                barrier.wait(timeout=3)
                server.subscribe_session(
                    sid, transport, owner=True, force=True, explicit=True
                )

            threads = [
                threading.Thread(target=claim, args=(client_a,)),
                threading.Thread(target=claim, args=(client_b,)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=3)
                assert not thread.is_alive()

            assert server.session_owner(sid) is session["transport"]
            server._session_streams.get(sid)
            server._sessions.pop(sid, None)
            server.reset_session_streams()
    finally:
        server._sessions.pop(sid, None)
        server.reset_session_streams()


def test_orphan_reap_rearms_while_parked_session_is_running(monkeypatch) -> None:
    """A parked record protected by in-flight work keeps its reap timer.

    Switching away from a mid-turn session parks it with exactly one scheduled
    reap; if that timer fired during the turn and never rearmed, the record
    would linger until the multi-hour idle TTL once the turn ended."""
    captured: list = []

    class FakeTimer:
        def __init__(self, _delay, fn) -> None:
            self.daemon = False
            captured.append(fn)

        def start(self) -> None:
            return None

    sid = "parked-running-reap"
    session = {
        "transport": server._detached_ws_transport,
        "running": True,
        "history_lock": threading.Lock(),
    }
    server._sessions[sid] = session
    monkeypatch.setattr(server.threading, "Timer", FakeTimer)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 1.0)
    try:
        server._schedule_ws_orphan_reap(sid)
        assert len(captured) == 1

        # Fires mid-turn: the record survives AND the timer is rearmed.
        captured[0]()
        assert sid in server._sessions
        assert len(captured) == 2

        # A revived session (live transport) stops the chain.
        session["transport"] = RecordingTransport()
        captured[1]()
        assert sid in server._sessions
        assert len(captured) == 2
    finally:
        server._sessions.pop(sid, None)


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


def test_durable_viewers_are_queued_before_owner_and_failed_viewer_detaches() -> None:
    sid = "durable-owner-stall"
    owner_started = threading.Event()
    release_owner = threading.Event()
    healthy_queued = threading.Event()
    failed_queued = threading.Event()
    blocking_viewer_write_called = threading.Event()
    owner_saw_queued_viewers: list[bool] = []
    delivery_result: list[bool] = []

    class StalledOwner(RecordingTransport):
        def write(self, obj: dict) -> bool:
            owner_saw_queued_viewers.append(
                healthy_queued.is_set() and failed_queued.is_set()
            )
            owner_started.set()
            assert release_owner.wait(timeout=3)
            return super().write(obj)

    class DurableViewer(RecordingTransport):
        def write(self, _obj: dict) -> bool:
            blocking_viewer_write_called.set()
            raise AssertionError("durable viewer used blocking write path")

    class HealthyViewer(DurableViewer):
        def write_observer(self, obj: dict) -> bool:
            healthy_queued.set()
            self.frames.append(obj)
            return True

    class OverflowedViewer(DurableViewer):
        def write_observer(self, obj: dict) -> bool:
            # WSTransport reports observer-queue overflow with False so the
            # stream can detach it and let reconnect replay heal the gap.
            self.frames.append(obj)
            failed_queued.set()
            return False

    owner = StalledOwner()
    healthy = HealthyViewer()
    failed = OverflowedViewer()
    for viewer in (healthy, failed):
        viewer.subscribe_session(sid)
    server._sessions[sid] = {"transport": owner}
    assert server.subscribe_session(sid, owner, owner=True, explicit=True)
    assert not server.subscribe_session(sid, healthy, explicit=True)
    assert not server.subscribe_session(sid, failed, explicit=True)
    for transport in (owner, healthy, failed):
        server.register_live_transport(transport)

    delivery_thread = threading.Thread(
        target=lambda: delivery_result.append(
            server.write_json(server._event_frame("tool.start", sid, {}))
        )
    )
    try:
        delivery_thread.start()
        assert healthy_queued.wait(timeout=1)
        assert failed_queued.wait(timeout=1)
        # Viewer queues are nonblocking and run before the authoritative write.
        assert owner_started.wait(timeout=1)
        assert owner_saw_queued_viewers == [True]
        assert not blocking_viewer_write_called.is_set()
        release_owner.set()
        delivery_thread.join(timeout=3)
        assert not delivery_thread.is_alive()

        stream = server.session_streams().get(sid)
        assert stream is not None
        replay, truncated, latest = stream.replay_since(0)
        assert delivery_result == [True]
        assert len(owner.frames) == len(healthy.frames) == len(failed.frames) == 1
        assert owner.frames[0]["params"]["seq"] == 1
        assert healthy.frames[0]["params"]["seq"] == 1
        assert stream.subscriber_count() == 2
        assert replay == [owner.frames[0]]
        assert (truncated, latest) == (False, 1)
    finally:
        release_owner.set()
        delivery_thread.join(timeout=3)
        for transport in (owner, healthy, failed):
            server.unregister_live_transport(transport)
        server._sessions.pop(sid, None)
        server.reset_session_streams()


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
