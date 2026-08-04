"""WebSocket transport for the tui_gateway JSON-RPC server.

Reuses :func:`tui_gateway.server.dispatch` verbatim so every RPC method, every
slash command, every approval/clarify/sudo flow, and every agent event flows
through the same handlers whether the client is Ink over stdio or an iOS /
web client over WebSocket.

Wire protocol
-------------
Identical to stdio: newline-delimited JSON-RPC in both directions. The server
emits a ``gateway.ready`` event immediately after connection accept, then
echoes responses/events for inbound requests. No framing differences.

Mounting
--------
    from fastapi import WebSocket
    from tui_gateway.ws import handle_ws

    @app.websocket("/api/ws")
    async def ws(ws: WebSocket):
        await handle_ws(ws)
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import socket
import threading
from collections import deque
from typing import Any, Callable

from tui_gateway import server

_log = logging.getLogger(__name__)

# Max seconds a pool-dispatched handler will block waiting for the event loop
# to flush a WS frame before we mark the transport dead. Protects handler
# threads from a wedged socket.
_WS_WRITE_TIMEOUT_S = 10.0
_WS_LOG_PAYLOAD_PREVIEW = 240

# Per-token streaming frames are coalesced: buffered and flushed as a batch on
# a short timer instead of waking the event loop once per token. A model reply
# emits hundreds of these in a burst, and each one is a loop wakeup competing
# with the agent turn for the GIL — coalescing cuts that churn (CF-2). The task
# that introduced this called them "agent.token"/"agent.thinking"; in this
# codebase the per-token frames are the ``*.delta`` stream events below. Keep
# this set to genuinely high-frequency, display-only events — anything a client
# must see promptly (tool/approval/status/completion frames) is non-streaming
# and flushes the buffer ahead of itself, so ordering is preserved.
_STREAMING_EVENT_TYPES = frozenset({
    "message.delta",
    "reasoning.delta",
    "thinking.delta",
})
# Max time a streamed token waits in the buffer before flush (~30 fps). Short
# enough to stay imperceptible to the live token cadence.
_TOKEN_COALESCE_S = 0.033
# A secondary dashboard must never backpressure the agent-owning client. Keep
# observer lifecycle batches bounded; if a suspended peer falls this far behind,
# disconnect it and let session replay heal on resume.
_OBSERVER_BATCH_QUEUE_MAX = 256
_OBSERVER_BATCH_FRAME_MAX = 128

# Advertised in ``gateway.ready`` so Desktop / mobile can tell a gateway that
# supports one durable session across several devices from an older one. Bump
# ``version`` only for a breaking change to these methods or to the ``seq``
# stamping on session events.
DURABLE_SESSION_CONTRACT = {
    "version": 1,
    "methods": [
        "session.subscribe",
        "session.unsubscribe",
        "session.claim",
        "session.replay",
        "request.status",
    ],
    "event_seq": True,
    "request_id": True,
}

# Keep starlette optional at import time; handle_ws uses the real class when
# it's available and falls back to a generic Exception sentinel otherwise.
try:
    from starlette.websockets import WebSocketDisconnect as _WebSocketDisconnect
except ImportError:  # pragma: no cover - starlette is a required install path
    _WebSocketDisconnect = Exception  # type: ignore[assignment]


class WSTransport:
    """Per-connection WS transport.

    ``write`` is safe to call from any thread *other than* the event loop
    thread that owns the socket. Pool workers (the only real caller) run in
    their own threads, so marshalling onto the loop via
    :func:`asyncio.run_coroutine_threadsafe` + ``future.result()`` is correct
    and deadlock-free there.

    When called from the loop thread itself (e.g. by ``handle_ws`` for an
    inline response) the same call would deadlock: we'd schedule work onto
    the loop we're currently blocking. We detect that case and fire-and-
    forget instead. Callers that need to know when the bytes are on the wire
    should use :meth:`write_async` from the loop thread.
    """

    def __init__(
        self,
        ws: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        peer: str = "unknown",
    ) -> None:
        self._ws = ws
        self._loop = loop
        self._peer = peer
        self._closed = False
        # Token-coalescing buffer (CF-2). Streamed token frames land here and a
        # short timer flushes the batch. The lock guards the buffer + the
        # "armed" flag against the worker threads that call write(); the timer
        # handle is only ever touched on the loop thread.
        self._token_lock = threading.Lock()
        self._pending_tokens: list[str] = []
        self._token_flush_handle: asyncio.TimerHandle | None = None
        self._token_flush_armed = False
        # Buffer mutation is protected by the thread lock above; actual socket
        # writes need an async boundary because several batches can be queued on
        # the owning loop while it recovers from a stall.
        self._send_lock = asyncio.Lock()
        self._observer_lock = threading.Lock()
        self._observer_batches: deque[list[str]] = deque()
        self._observer_drain_armed = False
        self._observer_flush_handle: asyncio.TimerHandle | None = None
        self._observer_generation = 0
        # RLock permits a subscribed observer write to latch a failure after
        # releasing _observer_lock while write_observer_if_subscribed still
        # owns the subscription lock. No closed-state path nests observer ->
        # subscription, preserving the subscription -> observer order.
        self._subscription_lock = threading.RLock()
        self._subscribed_session_id: str | None = None
        self._subscription_next_generation = 0
        self._subscription_committed_generation = 0
        self._owner_cleanup_scheduled = False

    def _schedule_owner_cleanup(self) -> None:
        """Detach/reap sessions claimed just before this transport failed."""

        def cleanup() -> None:
            try:
                server._close_sessions_for_transport(
                    self,
                    end_reason="ws_transport_failure",
                )
            except Exception:
                _log.debug(
                    "ws failure owner cleanup failed peer=%s",
                    self._peer,
                    exc_info=True,
                )

        async def cleanup_off_loop() -> None:
            await asyncio.to_thread(cleanup)

        def enqueue() -> None:
            self._loop.create_task(cleanup_off_loop())

        try:
            if self._loop.is_running():
                self._loop.call_soon_threadsafe(enqueue)
                return
        except (AttributeError, RuntimeError):
            pass
        threading.Thread(target=cleanup, daemon=True).start()

    def _mark_closed(self, *, schedule_owner_cleanup: bool) -> bool:
        """Latch closed state and invalidate attachment state atomically."""
        should_schedule_cleanup = False
        with self._subscription_lock:
            transitioned = not self._closed
            if transitioned:
                self._closed = True
                self._subscription_next_generation += 1
            self._subscribed_session_id = None
            if schedule_owner_cleanup and not self._owner_cleanup_scheduled:
                self._owner_cleanup_scheduled = True
                should_schedule_cleanup = True
        if should_schedule_cleanup:
            self._schedule_owner_cleanup()
        return transitioned

    def subscribe_session(self, session_id: str) -> None:
        """Replace this socket's live-session stream subscription."""
        with self._subscription_lock:
            self._subscription_next_generation += 1
            self._subscription_committed_generation = self._subscription_next_generation
            self._subscribed_session_id = session_id

    def begin_session_subscription(self) -> int:
        """Reserve an ordering token for an attach RPC before it starts."""
        with self._subscription_lock:
            self._subscription_next_generation += 1
            return self._subscription_next_generation

    def complete_session_subscription(
        self,
        generation: int,
        session_id: str,
        claim_owner: Callable[[], bool] | None = None,
    ) -> bool:
        """Commit ownership unless a newer successful attach already won."""
        with self._subscription_lock:
            if self._closed:
                return False
            if generation < self._subscription_committed_generation:
                return False
            if claim_owner is not None and not claim_owner():
                return False
            self._subscription_committed_generation = generation
            self._subscribed_session_id = session_id
            return True

    def claim_session_if_live(self, claim_owner: Callable[[], bool]) -> bool:
        """Commit session ownership only while this socket remains live."""
        with self._subscription_lock:
            if self._closed:
                return False
            return bool(claim_owner())

    def observes_session(self, session_id: str) -> bool:
        with self._subscription_lock:
            return self._subscribed_session_id == session_id

    def write_observer_if_subscribed(self, session_id: str, obj: dict) -> bool:
        """Queue ``obj`` only while this socket still observes ``session_id``.

        Keep the subscription lock through acceptance into the observer queue
        so a concurrent attach cannot commit a new subscription between the
        session check and enqueue.
        """
        with self._subscription_lock:
            if self._closed or self._subscribed_session_id != session_id:
                return False
            return self.write_observer(obj)

    def promote_session_if_subscribed(
        self,
        session_id: str,
        promote: Callable[[], bool],
    ) -> bool:
        """Run an ownership promotion while the subscription is locked."""
        with self._subscription_lock:
            if self._closed or self._subscribed_session_id != session_id:
                return False
            return bool(promote())

    @staticmethod
    def _is_streaming_frame(obj: dict) -> bool:
        """True for high-frequency per-token frames eligible for coalescing."""
        params = obj.get("params") if isinstance(obj, dict) else None
        if not isinstance(params, dict):
            return False
        return params.get("type") in _STREAMING_EVENT_TYPES

    def _merge_observer_lines_for_owner(self) -> None:
        """Atomically move queued mirror frames into the direct-write queue."""
        with self._observer_lock:
            if not self._observer_batches:
                return
            lines = [line for batch in self._observer_batches for line in batch]
            self._observer_batches.clear()
            self._observer_drain_armed = False
            self._observer_generation += 1
            handle = self._observer_flush_handle
            self._observer_flush_handle = None
            # Keep the observer lock until the lines are in the owner queue.
            # A concurrent direct delta can then only append after these older
            # frames, never between extraction and handoff.
            with self._token_lock:
                self._pending_tokens.extend(lines)
        if handle is not None:
            self._loop.call_soon_threadsafe(handle.cancel)

    def write(self, obj: dict) -> bool:
        if self._closed:
            return False

        self._merge_observer_lines_for_owner()
        line = json.dumps(obj, ensure_ascii=False)

        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False

        # Coalesce streamed token frames: buffer this frame and arm a short
        # flush timer instead of waking the loop right now. Cheap and
        # non-blocking — the worker returns immediately. Ordering is preserved
        # because every non-streaming frame (below) drains the buffer ahead of
        # itself.
        if self._is_streaming_frame(obj):
            with self._token_lock:
                self._pending_tokens.append(line)
                if not self._token_flush_armed:
                    self._token_flush_armed = True
                    # call_soon_threadsafe arms the call_later timer on the loop
                    # thread and is safe to call from a worker or the loop.
                    self._loop.call_soon_threadsafe(self._arm_token_flush)
            return not self._closed

        # Non-streaming frame (RPC response, control frame, non-token event):
        # append it behind any buffered tokens and flush the whole batch NOW so
        # it can never overtake the tokens that preceded it. The send is
        # scheduled INSIDE the lock so the on-the-wire order matches the buffer
        # order even if the coalesce timer fires on the loop at the same moment.
        from agent.async_utils import safe_schedule_threadsafe
        schedule_failed = False
        with self._token_lock:
            self._pending_tokens.append(line)
            batch = self._pending_tokens
            self._pending_tokens = []
            if on_loop:
                # Fire-and-forget — don't block the loop waiting on itself.
                self._loop.create_task(self._safe_send_many(batch))
                return True
            fut = safe_schedule_threadsafe(
                self._safe_send_many(batch), self._loop
            )
            if fut is None:
                schedule_failed = True

        if schedule_failed:
            self._mark_closed(schedule_owner_cleanup=True)
            return False

        try:
            fut.result(timeout=_WS_WRITE_TIMEOUT_S)
            return not self._closed
        except concurrent.futures.TimeoutError:  # builtin TimeoutError on 3.11+
            # The event loop is stalled (GIL-heavy agent turn, delegation
            # running N children), NOT the socket dead. The send coroutine is
            # already scheduled and will flush once the loop breathes — latching
            # _closed here permanently silenced live windows after one slow
            # write (the "subagent window shows zero streaming" bug). Unblock
            # the worker thread and keep the transport alive; _safe_send_many
            # latches on a real socket error when the frame actually fails.
            _log.warning(
                "ws write slow (loop stalled >%ss) peer=%s — frame left in flight",
                _WS_WRITE_TIMEOUT_S, self._peer,
            )
            return not self._closed
        except Exception as exc:
            self._mark_closed(schedule_owner_cleanup=True)
            _log.warning(
                "ws write failed peer=%s error_type=%s error=%s",
                self._peer, type(exc).__name__, exc,
            )
            return False

    def write_observer(self, obj: dict) -> bool:
        """Queue a mirrored session event without blocking its agent thread."""
        if self._closed:
            return False

        line = json.dumps(obj, ensure_ascii=False)
        should_arm = False
        overflowed = False
        with self._observer_lock:
            if (
                self._is_streaming_frame(obj)
                and self._observer_batches
                and len(self._observer_batches[-1]) < _OBSERVER_BATCH_FRAME_MAX
            ):
                self._observer_batches[-1].append(line)
            else:
                if len(self._observer_batches) >= _OBSERVER_BATCH_QUEUE_MAX:
                    # Lifecycle and interactive frames are not replayable on the
                    # same socket. Disconnect instead of silently evicting one;
                    # reconnect/resume can hydrate a coherent transcript and
                    # recover pending prompts.
                    self._observer_batches.clear()
                    self._observer_drain_armed = False
                    self._observer_generation += 1
                    overflowed = True
                else:
                    self._observer_batches.append([line])
            if not overflowed and not self._observer_drain_armed:
                self._observer_drain_armed = True
                self._observer_generation += 1
                should_arm = True

        if overflowed:
            # _observer_lock is released before taking _subscription_lock.
            self._mark_closed(schedule_owner_cleanup=True)
            _log.warning(
                "ws observer queue full peer=%s — closing for replay-capable reconnect",
                self._peer,
            )
            try:
                self._loop.call_soon_threadsafe(
                    lambda: self._loop.create_task(self._close_after_observer_overflow())
                )
            except RuntimeError:
                pass
            return False

        if should_arm:
            try:
                self._loop.call_soon_threadsafe(self._arm_observer_drain)
            except RuntimeError:
                self._mark_closed(schedule_owner_cleanup=True)
                return False
        return not self._closed

    async def _close_after_observer_overflow(self) -> None:
        close = getattr(self._ws, "close", None)
        if not callable(close):
            return
        try:
            result = close(
                code=1013, reason="observer backlog overflow; reconnect to resume"
            )
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            _log.debug("ws overflow close failed peer=%s", self._peer, exc_info=True)

    def _arm_observer_drain(self) -> None:
        """Coalesce observer frames, then start one drain on the owning loop."""
        with self._observer_lock:
            if self._closed:
                self._observer_batches.clear()
                self._observer_drain_armed = False
                return
            if (
                not self._observer_drain_armed
                or not self._observer_batches
                or self._observer_flush_handle is not None
            ):
                return
            self._observer_flush_handle = self._loop.call_later(
                _TOKEN_COALESCE_S, self._start_observer_drain
            )

    def _start_observer_drain(self) -> None:
        with self._observer_lock:
            self._observer_flush_handle = None
            if self._closed or not self._observer_drain_armed or not self._observer_batches:
                return
            generation = self._observer_generation
        self._loop.create_task(self._drain_observer_batches(generation))

    async def _drain_observer_batches(self, generation: int) -> None:
        while not self._closed:
            with self._observer_lock:
                if generation != self._observer_generation:
                    return
                if not self._observer_batches:
                    self._observer_drain_armed = False
                    return
                batch = self._observer_batches.popleft()
            await self._safe_send_many(batch)

        with self._observer_lock:
            if generation == self._observer_generation:
                self._observer_batches.clear()
                self._observer_drain_armed = False

    def _arm_token_flush(self) -> None:
        """Arm the coalesce timer. Runs on the loop thread (call_soon_threadsafe)."""
        if self._closed:
            return
        self._token_flush_handle = self._loop.call_later(
            _TOKEN_COALESCE_S, self._flush_tokens
        )

    def _flush_tokens(self) -> None:
        """Send buffered tokens as one batch. Runs on the loop thread (timer).

        The send is scheduled under the lock so its wire order is fixed relative
        to a concurrent non-streaming flush in :meth:`write`.
        """
        with self._token_lock:
            self._token_flush_handle = None
            self._token_flush_armed = False
            if not self._pending_tokens or self._closed:
                self._pending_tokens = []
                return
            batch = self._pending_tokens
            self._pending_tokens = []
            self._loop.create_task(self._safe_send_many(batch))

    async def write_async(self, obj: dict) -> bool:
        """Send from the owning event loop. Awaits until the frame is on the wire."""
        if self._closed:
            return False
        # Flush any buffered streamed tokens ahead of this frame (RPC response /
        # control frame) as ONE serialized batch. Sending them in two lock
        # acquisitions would let a later batch slip between the pending tokens
        # and the frame that drained them.
        self._merge_observer_lines_for_owner()
        with self._token_lock:
            batch = self._pending_tokens
            self._pending_tokens = []
            batch.append(json.dumps(obj, ensure_ascii=False))
        await self._safe_send_many(batch)
        return not self._closed

    async def _safe_send_many(self, lines: list[str]) -> None:
        """Send one indivisible batch of pre-serialized frames in wire order."""
        async with self._send_lock:
            if self._closed:
                return
            try:
                for line in lines:
                    if self._closed:
                        return
                    await self._ws.send_text(line)
            except Exception as exc:
                # Latch while still holding the writer lock so queued batches
                # observe the failure before they get a chance to touch the
                # socket.
                self._mark_closed(schedule_owner_cleanup=True)
                _log.warning(
                    "ws send failed peer=%s error_type=%s error=%s",
                    self._peer, type(exc).__name__, exc,
                )

    def close(self) -> None:
        # Serialize the explicit lifecycle close against an in-flight ordered
        # attach completion. If completion wins, handle_ws's subsequent owner
        # cleanup sees the claim; if close wins, completion observes _closed
        # and cannot claim after cleanup has already snapshotted ownership.
        self._mark_closed(schedule_owner_cleanup=False)
        with self._observer_lock:
            self._observer_batches.clear()
            self._observer_generation += 1
        observer_handle = self._observer_flush_handle
        if observer_handle is not None:
            observer_handle.cancel()
            self._observer_flush_handle = None
        # Cancel any pending coalesce flush. close() runs on the loop thread
        # (the handle_ws finally), so touching the TimerHandle here is safe.
        handle = self._token_flush_handle
        if handle is not None:
            handle.cancel()
            self._token_flush_handle = None


def _ws_peer_label(ws: Any) -> str:
    """Return ``host:port`` when available, else a stable placeholder."""
    client = getattr(ws, "client", None)
    if client is None:
        return "unknown"
    host = getattr(client, "host", None) or "unknown"
    port = getattr(client, "port", None)
    return f"{host}:{port}" if port is not None else host


def _disable_nagle(ws: Any) -> None:
    """Disable Nagle so streamed JSON-RPC frames go out individually.

    Without it the kernel coalesces the small per-token frames, so a burst after
    the model's think-pause lands on the client in one tick and no client-side
    smoothing can recover the cadence. GUI/WS only; chat platforms don't hit
    this path. Best-effort — skip silently if the socket isn't reachable.
    """
    try:
        scope = getattr(ws, "scope", None) or {}
        transport = (scope.get("extensions") or {}).get("transport") or getattr(ws, "transport", None)
        sock = transport.get_extra_info("socket") if transport is not None else None
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except Exception as exc:  # pragma: no cover - best-effort tuning
        _log.debug("ws TCP_NODELAY skip: %s", exc)


async def handle_ws(ws: Any) -> None:
    """Run one WebSocket session. Wire-compatible with ``tui_gateway.entry``."""
    peer = _ws_peer_label(ws)
    transport: WSTransport | None = None
    messages = 0
    parse_errors = 0
    dispatch_crashes = 0
    send_failures = 0
    disconnect_reason = "not_connected"

    try:
        await ws.accept()
        disconnect_reason = "connected"
        # Push small streamed frames out immediately instead of letting Nagle
        # batch them — keeps the live token cadence intact for GUI clients.
        _disable_nagle(ws)
        _log.info("ws accepted peer=%s", peer)

        transport = WSTransport(ws, asyncio.get_running_loop(), peer=peer)

        # resolve_skin() reads config + initializes the skin engine —
        # synchronous I/O + CPU work that should not block the event loop
        # during the cold-start window. Run it in the thread pool so the
        # WS read loop stays free to drain the frontend's initial RPC
        # burst (setup.status, session.list, ...) without a stall
        # (#60800). The skin payload is small (a dict of strings/arrays),
        # so the to_thread overhead is negligible.
        skin_payload = await asyncio.to_thread(server.resolve_skin)
        ready_ok = await transport.write_async(
            {
                "jsonrpc": "2.0",
                "method": "event",
                "params": {
                    "type": "gateway.ready",
                    # change_events: this backend broadcasts pet.changed /
                    # cron.changed / sessions.changed, so clients can demote
                    # their legacy polls to slow backstops.
                    # capabilities: feature detection for the durable
                    # multi-client session contract — a client that sees it may
                    # attach a second device (session.subscribe), hand the
                    # session over (session.claim), catch up after a reconnect
                    # (session.replay, using the ``seq`` on every event), and
                    # make prompt.submit retry-safe with request_id /
                    # request.status. Older clients ignore it and keep the
                    # single-transport behaviour.
                    "payload": {
                        "skin": skin_payload,
                        "change_events": True,
                        "capabilities": {"durable_sessions": DURABLE_SESSION_CONTRACT},
                    },
                },
            }
        )
        if ready_ok:
            # Live-apply skins Hermes activates mid-conversation.
            server._ensure_skin_watcher()
            # Track this peer for session-less global broadcasts (skin.changed
            # from the background watcher) — write_json can't route those.
            server.register_live_transport(transport)
        if not ready_ok:
            disconnect_reason = "ready_send_failed"
            send_failures += 1
            _log.error("ws ready frame send failed peer=%s", peer)
            return

        while True:
            try:
                raw = await ws.receive_text()
            except _WebSocketDisconnect as exc:
                disconnect_reason = (
                    "client_disconnect("
                    f"code={getattr(exc, 'code', None)},"
                    f"reason={getattr(exc, 'reason', None)})"
                )
                break
            except Exception:
                disconnect_reason = "receive_failed"
                _log.exception("ws receive failed peer=%s", peer)
                break

            line = raw.strip()
            if not line:
                continue
            messages += 1

            try:
                req = json.loads(line)
            except json.JSONDecodeError as exc:
                parse_errors += 1
                _log.warning(
                    "ws parse error peer=%s index=%d error=%s payload=%r",
                    peer,
                    messages,
                    exc,
                    line[:_WS_LOG_PAYLOAD_PREVIEW],
                )
                ok = await transport.write_async(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32700, "message": "parse error"},
                        "id": None,
                    }
                )
                if not ok:
                    disconnect_reason = "send_failed_after_parse_error"
                    send_failures += 1
                    _log.warning("ws parse-error reply send failed peer=%s", peer)
                    break
                continue

            # dispatch() may schedule long handlers on the pool; it returns
            # None in that case and the worker writes the response itself via
            # the transport we pass in (a separate thread, so transport.write
            # is the safe path there). For inline handlers it returns the
            # response dict, which we write here from the loop.
            req_id = req.get("id") if isinstance(req, dict) else None
            req_method = req.get("method") if isinstance(req, dict) else None
            try:
                resp = await asyncio.to_thread(server.dispatch, req, transport)
            except Exception:
                dispatch_crashes += 1
                _log.exception(
                    "ws dispatch crash peer=%s id=%s method=%s",
                    peer,
                    req_id,
                    req_method,
                )
                ok = await transport.write_async(
                    {
                        "jsonrpc": "2.0",
                        "error": {"code": -32603, "message": "internal error"},
                        "id": req_id if req_id is not None else None,
                    }
                )
                if not ok:
                    disconnect_reason = "send_failed_after_dispatch_crash"
                    send_failures += 1
                    _log.warning(
                        "ws dispatch-crash reply send failed peer=%s id=%s method=%s",
                        peer,
                        req_id,
                        req_method,
                    )
                    break
                continue
            if resp is not None and not await transport.write_async(resp):
                disconnect_reason = "send_failed_after_response"
                send_failures += 1
                _log.warning(
                    "ws response send failed peer=%s id=%s method=%s",
                    peer,
                    req_id,
                    req_method,
                )
                break
    finally:
        reaped_sessions = 0
        detached_sessions = 0
        if transport is not None:
            server.unregister_live_transport(transport)
            transport.close()

            try:
                await asyncio.to_thread(server._release_wake_for_transport, transport)
            except Exception:
                _log.exception("ws wake-word teardown failed peer=%s", peer)

            # Reap sessions this transport owned (close_on_disconnect sidecar
            # sessions) or detach the rest to the drop sentinel so later emits
            # don't crash into a closed socket or fall through to desktop stdout
            # logs. Detached sessions are handed to the grace-windowed WS-orphan
            # reaper inside _close_sessions_for_transport (a quick reconnect /
            # session.resume cancels it). This is the single WS-disconnect
            # teardown path.
            #
            # Offloaded: _close_session_by_id does a blocking worker.close()
            # (terminate + waits) plus a synchronous DB write — inline that
            # would freeze the uvicorn event loop for every other live
            # connection.
            try:
                reaped_sessions, detached_sessions = await asyncio.to_thread(
                    server._close_sessions_for_transport,
                    transport,
                    end_reason="ws_disconnect",
                )
            except Exception:
                _log.exception("ws transport teardown failed peer=%s", peer)
        try:
            await ws.close()
        except Exception as exc:
            _log.debug("ws close failed peer=%s error=%s", peer, exc)
        _log.info(
            "ws closed peer=%s reason=%s messages=%d parse_errors=%d "
            "dispatch_crashes=%d send_failures=%d reaped_sessions=%d detached_sessions=%d",
            peer,
            disconnect_reason,
            messages,
            parse_errors,
            dispatch_crashes,
            send_failures,
            reaped_sessions,
            detached_sessions,
        )
