import asyncio
import concurrent.futures
import json
import threading
import time

from hermes_cli import mcp_startup
from tui_gateway import server
from tui_gateway import ws as ws_mod




def _run_disconnect(monkeypatch, seed):
    """Drive handle_ws to its disconnect `finally`, seeding sessions against the
    live WSTransport the moment it exists. Returns nothing; inspect _sessions."""
    # Disable the grace-reap Timer: detached sessions normally schedule a
    # threading.Timer via _schedule_ws_orphan_reap, which would outlive the test
    # and fire _reap during interpreter teardown — touching _sessions/DB and
    # producing spurious post-run errors under the per-file CI runner. Grace=0
    # short-circuits the Timer (see _schedule_ws_orphan_reap) so the test leaves
    # no lingering thread.
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    # Mirror the real _finalize_session chokepoint: it is the single place that
    # closes the slash-worker (#38095). Stub it but keep that behavior so the
    # disconnect-reap path still exercises worker teardown.
    def _fake_finalize(s, end_reason="tui_close"):
        w = s.get("slash_worker")
        if w:
            w.close()

    monkeypatch.setattr(server, "_finalize_session", _fake_finalize)

    created = []
    real_transport = ws_mod.WSTransport
    monkeypatch.setattr(
        ws_mod, "WSTransport",
        lambda ws, loop, **kw: created.append(real_transport(ws, loop, **kw)) or created[-1],
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            seed(created[0])  # transport now exists; attach it to sessions
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))


def test_ws_disconnect_reaps_flagged_session_and_closes_worker(monkeypatch):
    closed = []

    class FakeWorker:
        def close(self):
            closed.append(True)

    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                flagged={
                    "transport": t,
                    "close_on_disconnect": True,
                    "slash_worker": FakeWorker(),
                    "session_key": "k",
                }
            ),
        )
        assert "flagged" not in server._sessions
        assert closed == [True]
    finally:
        server._sessions.clear()




def test_ws_connection_registers_then_disconnect_unregisters_live_transport(monkeypatch):
    """A connected client must be tracked in the live-transport registry so a
    session-less global broadcast (skin.changed from the background watcher)
    reaches it, and dropped on disconnect so no stale write targets a dead peer.
    This is the WS half of the cross-surface live-theme fix."""
    server._sessions.clear()
    server._live_transports.clear()
    seen = {}
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: seen.__setitem__("registered", t in server._live_transports),
        )
        # Seeded at receive_text time — i.e. after gateway.ready registered it.
        assert seen["registered"] is True
        # handle_ws's finally must have unregistered it.
        assert not server._live_transports
    finally:
        server._sessions.clear()
        server._live_transports.clear()


def test_ws_disconnect_releases_wake_word_owner(monkeypatch):
    released = []
    created = []
    monkeypatch.setattr(
        server,
        "_release_wake_for_transport",
        lambda transport: released.append(transport) or True,
    )

    _run_disconnect(monkeypatch, lambda transport: created.append(transport))

    assert released == created




def test_ws_starts_mcp_discovery_before_ready(monkeypatch):
    import tui_gateway.entry as entry

    calls = []
    events = []

    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: calls.append("mcp"))

    class FakeWS:
        async def accept(self):
            events.append("accept")

        async def send_text(self, line):
            if '"gateway.ready"' in line:
                events.append(f"ready_after_{len(calls)}")

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))

    # Discovery moved to profile-aware agent construction. WebSocket transport
    # should not start MCP discovery before a profile has been bound.
    assert calls == []
    assert events == ["accept", "ready_after_0"]


def test_ws_observer_write_does_not_wait_for_a_suspended_client():
    send_started = threading.Event()
    release_send = asyncio.Event()
    sent = []

    class FakeWS:
        async def send_text(self, line):
            send_started.set()
            await release_send.wait()
            sent.append(line)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    transport = ws_mod.WSTransport(FakeWS(), loop, peer='observer-stall-test')
    outcome = {}

    def write_observer():
        try:
            outcome['result'] = transport.write_observer({'kind': 'observer'})
        except Exception as exc:  # pragma: no cover - assertion reports details
            outcome['error'] = exc

    writer = threading.Thread(target=write_observer)
    try:
        writer.start()
        writer.join(timeout=0.25)
        assert not writer.is_alive(), 'observer write blocked on the suspended socket'
        assert outcome == {'result': True}
        assert send_started.wait(timeout=1)

        loop.call_soon_threadsafe(release_send.set)
        deadline = time.time() + 2
        while not sent and time.time() < deadline:
            time.sleep(0.01)
        assert sent == [json.dumps({'kind': 'observer'})]
    finally:
        transport.close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_observer_preserves_reasoning_text_and_completion_order():
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(json.loads(line)["params"]["type"])

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    transport = ws_mod.WSTransport(FakeWS(), loop, peer="observer-order-test")
    frames = [
        server._event_frame("reasoning.delta", "sid", {"text": "why"}),
        server._event_frame("message.delta", "sid", {"text": "answer"}),
        server._event_frame("message.complete", "sid", {"text": "answer"}),
    ]
    try:
        assert all(transport.write_observer(frame) for frame in frames)
        deadline = time.time() + 2
        while len(sent) < len(frames) and time.time() < deadline:
            time.sleep(0.01)
        assert sent == ["reasoning.delta", "message.delta", "message.complete"]
    finally:
        transport.close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_observer_to_owner_handoff_does_not_reorder_completion():
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(json.loads(line)["params"]["type"])

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    transport = ws_mod.WSTransport(FakeWS(), loop, peer="observer-handoff-test")
    try:
        assert transport.write_observer(server._event_frame("message.delta", "sid", {"text": "partial"}))
        assert transport.write(server._event_frame("message.complete", "sid", {"text": "partial"}))
        deadline = time.time() + 2
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert sent == ["message.delta", "message.complete"]
    finally:
        transport.close()
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_observer_backlog_overflow_disconnects_instead_of_dropping_lifecycle_frames():
    class FakeWS:
        async def send_text(self, _line):
            return None

    loop = asyncio.new_event_loop()
    transport = ws_mod.WSTransport(FakeWS(), loop, peer="observer-bound-test")
    try:
        for index in range(ws_mod._OBSERVER_BATCH_QUEUE_MAX):
            assert transport.write_observer(
                server._event_frame("approval.request", "sid", {"index": index})
            )

        assert not transport.write_observer(
            server._event_frame("clarify.request", "sid", {})
        )
        assert transport._closed is True
        assert not transport._observer_batches
    finally:
        transport.close()
        loop.close()


def test_ws_observer_events_precede_async_rpc_responses():
    async def scenario():
        sent = []

        class FakeWS:
            async def send_text(self, line):
                frame = json.loads(line)
                sent.append(frame.get("id") or frame.get("params", {}).get("type"))

        transport = ws_mod.WSTransport(
            FakeWS(), asyncio.get_running_loop(), peer="observer-async-handoff-test"
        )
        try:
            assert transport.write_observer(
                server._event_frame("message.delta", "sid", {"text": "old"})
            )
            assert await transport.write_async(
                {"jsonrpc": "2.0", "id": "activate", "result": {"session_id": "sid"}}
            )
            assert sent == ["message.delta", "activate"]
        finally:
            transport.close()

    asyncio.run(scenario())


def test_ws_concurrent_owner_handoff_preserves_enqueue_order():
    sent = []
    took_observer_lines = threading.Event()
    allow_handoff = threading.Event()

    class FakeWS:
        async def send_text(self, line):
            frame = json.loads(line)
            sent.append(frame.get("id") or frame.get("params", {}).get("payload", {}).get("text"))

    loop = asyncio.new_event_loop()
    transport = ws_mod.WSTransport(FakeWS(), loop, peer="concurrent-handoff-test")
    original_merge = ws_mod.WSTransport._merge_observer_lines_for_owner
    worker: threading.Thread | None = None

    def paused_merge(self):
        original_merge(self)
        if not took_observer_lines.is_set():
            took_observer_lines.set()
            assert allow_handoff.wait(timeout=1)

    ws_mod.WSTransport._merge_observer_lines_for_owner = paused_merge
    try:
        assert transport.write_observer(
            server._event_frame("message.delta", "sid", {"text": "old"})
        )
        worker = threading.Thread(
            target=lambda: loop.run_until_complete(
                transport.write_async({"jsonrpc": "2.0", "id": "response", "result": {}})
            )
        )
        worker.start()
        assert took_observer_lines.wait(timeout=1)
        assert transport.write(server._event_frame("message.delta", "sid", {"text": "new"}))
        allow_handoff.set()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert sent == ["old", "new", "response"]
    finally:
        allow_handoff.set()
        if worker is not None:
            worker.join(timeout=2)
        ws_mod.WSTransport._merge_observer_lines_for_owner = original_merge
        transport.close()
        loop.close()


def test_ws_transport_serializes_concurrent_sends():
    active_sends = 0
    max_active_sends = 0
    sent = []

    class FakeWS:
        async def send_text(self, line):
            nonlocal active_sends, max_active_sends
            active_sends += 1
            max_active_sends = max(max_active_sends, active_sends)
            try:
                await asyncio.sleep(0.05)
                sent.append(line)
            finally:
                active_sends -= 1

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        transport = ws_mod.WSTransport(FakeWS(), loop, peer="serialize-test")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(transport.write, {"idx": 1}),
                pool.submit(transport.write, {"idx": 2}),
            ]
            assert [f.result(timeout=2) for f in futures] == [True, True]

        assert len(sent) == 2
        assert max_active_sends == 1
        assert transport._closed is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_transport_preserves_cross_batch_order():
    async def scenario():
        entered = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        class FakeWS:
            async def send_text(self, line):
                entered.append(line)
                if line == "A1":
                    first_entered.set()
                    await release_first.wait()

        transport = ws_mod.WSTransport(
            FakeWS(), asyncio.get_running_loop(), peer="batch-order-test"
        )
        first = asyncio.create_task(transport._safe_send_many(["A1", "A2"]))
        await first_entered.wait()

        async def send_second():
            second_started.set()
            await transport._safe_send_many(["B1", "B2"])

        second = asyncio.create_task(send_second())
        await second_started.wait()

        # The second task has reached the transport. Without whole-batch
        # serialization it runs B1/B2 before this task can resume.
        assert entered == ["A1"]

        release_first.set()
        await asyncio.gather(first, second)
        assert entered == ["A1", "A2", "B1", "B2"]

    asyncio.run(scenario())


