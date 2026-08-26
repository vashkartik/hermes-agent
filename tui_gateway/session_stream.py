"""Durable multi-client session streaming for the tui_gateway JSON-RPC server.

Historically a gateway session pinned its event stream to exactly ONE
transport (``session["transport"]``).  That is correct for the stdio Ink TUI,
where a session has a single peer for its whole life, but it breaks the moment
the same conversation is open on two surfaces — Desktop and the mobile/web
client — which is the supported "one durable session, many devices" story:

* **Dropped streams.**  ``write_json`` resolved a session event to that single
  transport, so the second client received nothing at all.
* **Stream theft.**  Any path that re-bound ``session["transport"]`` (resume,
  ``prompt.submit``, queued-prompt drain) silently moved the stream to the
  newest peer, blanking the previous one mid-turn.
* **Duplicate terminals.**  Two independent code paths can emit a terminal
  frame for one turn (the normal ``message.complete`` and the dispatcher's
  ``error`` handler), so a client could see a turn "finish" twice.
* **Missing results / hangs.**  A client that disconnected across a turn had no
  way to ask what happened to the prompt it submitted, and a retried submit
  simply ran the turn a second time.

This module owns the fix as an additive layer:

``SessionStream``
    Per-session fan-out with a monotonic sequence number, a bounded replay
    ring for reconnects, one *owner* transport (the only peer allowed to
    mutate the session) and any number of read-only *viewers*.  Exactly one
    terminal frame per turn reaches the wire.

``SessionStreamRegistry``
    ``session_id -> SessionStream``, plus transport-oriented teardown so one
    WebSocket disconnect cleanly detaches from every session it touched.

``RequestLedger``
    ``(session_id, request_id) -> record``.  Makes a submit queryable after a
    disconnect, makes a retry idempotent (one execution), rejects a reused id
    carrying a different payload, and settles a wedged request within a bound
    so a client is never left waiting forever.

Ownership is deliberately kept in sync with the legacy ``session["transport"]``
slot by the caller (``tui_gateway.server``), so every existing liveness,
orphan-reap and idle-eviction rule keeps working unchanged.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        value = int(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    try:
        value = float(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


# How many delivered frames a session keeps for reconnect replay. A turn's
# token stream is the bulk of this; 1024 frames covers a long reply without
# letting an idle session pin unbounded memory.
REPLAY_FRAMES = _env_int("HERMES_TUI_SESSION_REPLAY_FRAMES", 1024, minimum=0)

# Frames whose arrival ENDS a turn. The first one delivered for a given turn
# wins; later ones are suppressed so every attached client sees exactly one
# terminal event. ``message.complete`` carries ``status: "error"`` on the
# compute-host error path, and the turn dispatcher emits a bare ``error`` frame
# when it crashes — either can legitimately be first.
TERMINAL_EVENT_TYPES = frozenset({"message.complete", "error"})

# Per-session cap on remembered request ids, and how long a settled record
# stays queryable. Both bound the ledger against a long-lived session.
REQUEST_HISTORY = _env_int("HERMES_TUI_REQUEST_HISTORY", 64, minimum=1)
REQUEST_TTL_S = _env_float("HERMES_TUI_REQUEST_TTL_S", 3600.0, minimum=1.0)

# A request that has been "running" with no stream activity for this long is
# settled as ``stalled`` so status queries terminate. Streaming deltas refresh
# the clock, so a genuinely-progressing long turn is never settled early.
REQUEST_SETTLE_S = _env_float("HERMES_TUI_REQUEST_SETTLE_S", 900.0, minimum=1.0)


# ── session stream ──────────────────────────────────────────────────────────


@dataclass
class Subscriber:
    """One attached client transport.

    ``explicit`` marks a peer that attached through the durable-session
    contract (``session.subscribe`` / ``session.claim``) rather than by simply
    talking to the session. Only explicit peers are held to the "one owner
    mutates" rule: a client that predates the contract keeps the historical
    take-the-session-on-submit behaviour, so upgrading the gateway never
    strands an older Desktop build behind an ownership error it cannot clear.
    """

    transport: Any
    owner: bool = False
    explicit: bool = False
    client: str = ""
    attached_at: float = field(default_factory=time.time)
    last_seq: int = 0


class SessionStream:
    """Ordered, replayable fan-out for one session id.

    Every delivery is stamped with a monotonic ``seq`` under the stream lock,
    so the frame order observed by any two attached clients is identical.  A
    peer whose ``write`` raises or reports the socket gone is dropped from the
    fan-out rather than being allowed to stall the rest.
    """

    __slots__ = (
        "sid", "session_key", "profile_key", "_lock", "_subs", "_seq",
        "_replay", "_replay_floor", "_turn_id", "_turn_request_id",
        "_turn_terminal_seen", "_touched_at", "_ever_attached",
    )

    def __init__(self, sid: str, *, session_key: str = "", profile_key: str = "") -> None:
        self.sid = sid
        self.session_key = session_key
        self.profile_key = profile_key
        self._lock = threading.RLock()
        self._subs: list[Subscriber] = []
        self._seq = 0
        self._replay: deque[dict] = deque(maxlen=REPLAY_FRAMES or 1)
        # Lowest seq still recoverable from the ring; a client asking for
        # anything older must do a full resume instead of a replay.
        self._replay_floor = 0
        self._turn_id = 0
        self._turn_request_id: Optional[str] = None
        self._turn_terminal_seen = False
        self._touched_at = time.time()
        self._ever_attached = False

    # -- membership ---------------------------------------------------------

    def _find(self, transport: Any) -> Optional[Subscriber]:
        for sub in self._subs:
            if sub.transport is transport:
                return sub
        return None

    def owner(self) -> Any:
        with self._lock:
            for sub in self._subs:
                if sub.owner:
                    return sub.transport
        return None

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)

    def clients(self) -> list[dict]:
        """Snapshot of attached peers, for ``session.peers`` / diagnostics."""
        with self._lock:
            return [
                {
                    "client": sub.client,
                    "owner": sub.owner,
                    "attached_at": sub.attached_at,
                    "last_seq": sub.last_seq,
                }
                for sub in self._subs
            ]

    def is_explicit(self, transport: Any) -> bool:
        """True when ``transport`` attached through the durable-session RPCs."""
        with self._lock:
            sub = self._find(transport)
            return bool(sub and sub.explicit)

    def attach(
        self,
        transport: Any,
        *,
        owner: bool = False,
        force: bool = False,
        explicit: bool = False,
        client: str = "",
        is_dead: Optional[Callable[[Any], bool]] = None,
    ) -> bool:
        """Attach ``transport``; return True when it holds ownership.

        ``owner=True`` requests ownership, but a live owner is never displaced
        unless ``force`` is set (explicit handoff).  A transport whose owner
        slot is held by a dead/detached peer takes over implicitly — that is
        the reconnect case.  Re-attaching an already-present transport is
        idempotent and never downgrades it.
        """
        with self._lock:
            existing = self._find(transport)
            if existing is None:
                existing = Subscriber(transport=transport, client=client)
                self._subs.append(existing)
                self._ever_attached = True
            elif client:
                existing.client = client
            # Opting into the contract is sticky: a client cannot drop back to
            # legacy semantics for its next request.
            existing.explicit = existing.explicit or explicit

            if not owner:
                return existing.owner

            current = next((s for s in self._subs if s.owner and s is not existing), None)
            if current is not None:
                dead = bool(is_dead(current.transport)) if is_dead else False
                if not (force or dead):
                    # A live owner keeps the session; the newcomer stays a
                    # read-only viewer until it explicitly claims.
                    return False
                current.owner = False
            existing.owner = True
            return True

    def detach(self, transport: Any) -> bool:
        """Remove ``transport``. Returns True when it had been the owner."""
        with self._lock:
            sub = self._find(transport)
            if sub is None:
                return False
            self._subs.remove(sub)
            return sub.owner

    def promote_next_owner(self) -> Any:
        """Hand ownership to the longest-attached remaining peer.

        Called when the owner disconnects while another device is still
        watching: the session must survive on the surviving client rather than
        be detached to the orphan reaper. Returns the new owner transport, or
        ``None`` when nobody is left.
        """
        with self._lock:
            if any(sub.owner for sub in self._subs):
                return next(sub.transport for sub in self._subs if sub.owner)
            if not self._subs:
                return None
            heir = min(self._subs, key=lambda s: s.attached_at)
            heir.owner = True
            return heir.transport

    # -- delivery -----------------------------------------------------------

    def _stamp(self, frame: dict, seq: int) -> dict:
        params = dict(frame.get("params") or {})
        params["seq"] = seq
        stamped = dict(frame)
        stamped["params"] = params
        return stamped

    def deliver(self, frame: dict) -> Optional[bool]:
        """Fan ``frame`` out to every attached client, in order.

        Returns ``True`` when at least one peer accepted the frame, ``False``
        when every peer failed, and ``None`` when this stream has no
        subscribers at all (the caller falls back to the legacy single
        transport so stdio/Ink behaviour is untouched).

        A terminal frame arriving after this turn already produced one is
        dropped and reported as delivered — the clients already have their
        terminal event, and duplicating it is exactly the bug being fixed.
        """
        params = frame.get("params") or {}
        sid = params.get("session_id") or ""
        # Hard scoping guard: a frame may only ever reach the stream whose id
        # it names. Cross-session (and therefore cross-profile) delivery is
        # impossible by construction.
        if sid and sid != self.sid:
            logger.warning(
                "session stream refused cross-session frame stream=%s frame=%s",
                self.sid, sid,
            )
            return None

        event_type = params.get("type")
        with self._lock:
            if not self._subs:
                # Never had a device attached → stdio/Ink fallback.
                if not self._ever_attached:
                    return None
                # Last device dropped. Mac-side turn keeps going; record
                # frames so the device can replay on reconnect.
                if event_type in TERMINAL_EVENT_TYPES:
                    if self._turn_terminal_seen:
                        return True
                    self._turn_terminal_seen = True
                self._seq += 1
                seq = self._seq
                stamped = self._stamp(frame, seq)
                self._touched_at = time.time()
                if REPLAY_FRAMES:
                    if len(self._replay) == self._replay.maxlen and self._replay:
                        self._replay_floor = (self._replay[0].get("params") or {}).get("seq") or 0
                    self._replay.append(stamped)
                    if self._replay_floor == 0 and self._replay:
                        self._replay_floor = (self._replay[0].get("params") or {}).get("seq") or 0
                return False
            if event_type in TERMINAL_EVENT_TYPES:
                if self._turn_terminal_seen:
                    logger.debug(
                        "suppressed duplicate terminal event sid=%s type=%s",
                        self.sid, event_type,
                    )
                    return True
                self._turn_terminal_seen = True
            self._seq += 1
            seq = self._seq
            stamped = self._stamp(frame, seq)
            self._touched_at = time.time()
            if REPLAY_FRAMES:
                if len(self._replay) == self._replay.maxlen and self._replay:
                    self._replay_floor = (self._replay[0].get("params") or {}).get("seq") or 0
                self._replay.append(stamped)
                if self._replay_floor == 0 and self._replay:
                    self._replay_floor = (self._replay[0].get("params") or {}).get("seq") or 0

            delivered = False
            dead: list[Subscriber] = []
            for sub in list(self._subs):
                try:
                    ok = sub.transport.write(stamped)
                except Exception:
                    logger.debug(
                        "session stream write raised sid=%s client=%s",
                        self.sid, sub.client, exc_info=True,
                    )
                    ok = False
                if ok:
                    sub.last_seq = seq
                    delivered = True
                else:
                    dead.append(sub)
            for sub in dead:
                # A peer that reports "gone" is detached here; the WS teardown
                # path will also call detach() and both are idempotent.
                if sub in self._subs:
                    self._subs.remove(sub)
            return delivered

    def replay_since(self, after_seq: int) -> tuple[list[dict], bool, int]:
        """Frames newer than ``after_seq``.

        Returns ``(frames, truncated, latest_seq)``.  ``truncated`` is True when
        the ring no longer holds everything the client missed — the client must
        fall back to a full ``session.resume`` rather than stitch a gap.
        """
        with self._lock:
            latest = self._seq
            if after_seq >= latest:
                return [], False, latest
            frames = [
                f for f in self._replay
                if ((f.get("params") or {}).get("seq") or 0) > after_seq
            ]
            truncated = bool(self._replay_floor and after_seq + 1 < self._replay_floor)
            return frames, truncated, latest

    # -- turn lifecycle -----------------------------------------------------

    def begin_turn(self, request_id: Optional[str] = None) -> int:
        """Arm a new turn: re-enables exactly one terminal event."""
        with self._lock:
            self._turn_id += 1
            self._turn_request_id = request_id
            self._turn_terminal_seen = False
            self._touched_at = time.time()
            return self._turn_id

    def turn_request_id(self) -> Optional[str]:
        with self._lock:
            return self._turn_request_id

    def terminal_seen(self) -> bool:
        with self._lock:
            return self._turn_terminal_seen

    def latest_seq(self) -> int:
        with self._lock:
            return self._seq

    def touched_at(self) -> float:
        with self._lock:
            return self._touched_at


class SessionStreamRegistry:
    """All live :class:`SessionStream` objects, keyed by session id."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._streams: dict[str, SessionStream] = {}

    def get(self, sid: str) -> Optional[SessionStream]:
        with self._lock:
            return self._streams.get(sid)

    def ensure(self, sid: str, *, session_key: str = "", profile_key: str = "") -> SessionStream:
        with self._lock:
            stream = self._streams.get(sid)
            if stream is None:
                stream = SessionStream(
                    sid, session_key=session_key, profile_key=profile_key
                )
                self._streams[sid] = stream
            else:
                if session_key and not stream.session_key:
                    stream.session_key = session_key
                if profile_key and not stream.profile_key:
                    stream.profile_key = profile_key
            return stream

    def drop(self, sid: str) -> None:
        with self._lock:
            self._streams.pop(sid, None)

    def deliver(self, sid: str, frame: dict) -> Optional[bool]:
        stream = self.get(sid)
        if stream is None:
            return None
        return stream.deliver(frame)

    def detach_transport(self, transport: Any) -> tuple[list[str], list[str]]:
        """Detach ``transport`` from every stream.

        Returns ``(owned_sids, viewed_sids)`` so the caller can apply its
        disconnect policy (reap / grace-window detach) to the sessions this
        peer actually owned, while a departing viewer disturbs nothing.
        """
        with self._lock:
            streams = list(self._streams.items())
        owned: list[str] = []
        viewed: list[str] = []
        for sid, stream in streams:
            if stream.detach(transport):
                owned.append(sid)
            else:
                viewed.append(sid)
        return owned, viewed

    def sids(self) -> list[str]:
        with self._lock:
            return list(self._streams)

    def clear(self) -> None:
        with self._lock:
            self._streams.clear()


# ── request ledger ──────────────────────────────────────────────────────────


@dataclass
class LedgerResult:
    """Outcome of :meth:`RequestLedger.begin`.

    ``outcome`` is one of:

    ``accepted``
        First time this id was seen — the caller must execute the work.
    ``duplicate``
        Same id, same payload.  The caller must NOT execute; ``record``
        carries the current status (and the result, once terminal).
    ``conflict``
        Same id, different payload.  The caller must reject the request.
    """

    outcome: str
    record: dict


class RequestLedger:
    """Per-session request-id bookkeeping: execute-once, always queryable."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._by_session: dict[str, "deque[str]"] = {}
        self._records: dict[tuple[str, str], dict] = {}

    def _prune_locked(self, sid: str, now: float) -> None:
        order = self._by_session.get(sid)
        if order is None:
            return
        while len(order) > REQUEST_HISTORY:
            stale = order.popleft()
            self._records.pop((sid, stale), None)
        for rid in list(order):
            rec = self._records.get((sid, rid))
            if rec is None:
                order.remove(rid)
                continue
            if rec["status"] != "running" and (now - rec["updated_at"]) > REQUEST_TTL_S:
                order.remove(rid)
                self._records.pop((sid, rid), None)

    def _settle_stale_locked(self, sid: str, now: float) -> None:
        """Bound a request that stopped producing output without a terminal."""
        for rid in list(self._by_session.get(sid, ())):
            rec = self._records.get((sid, rid))
            if rec is None or rec["status"] != "running":
                continue
            if (now - rec["updated_at"]) > REQUEST_SETTLE_S:
                rec["status"] = "stalled"
                rec["updated_at"] = now
                rec["result"] = {
                    "error": "no terminal event within "
                    f"{int(REQUEST_SETTLE_S)}s — retry the request"
                }

    def begin(self, sid: str, request_id: str, fingerprint: str) -> LedgerResult:
        now = time.time()
        with self._lock:
            self._settle_stale_locked(sid, now)
            self._prune_locked(sid, now)
            key = (sid, request_id)
            rec = self._records.get(key)
            if rec is None:
                rec = {
                    "request_id": request_id,
                    "session_id": sid,
                    "fingerprint": fingerprint,
                    "status": "running",
                    "result": None,
                    "created_at": now,
                    "updated_at": now,
                }
                self._records[key] = rec
                self._by_session.setdefault(sid, deque()).append(request_id)
                return LedgerResult("accepted", dict(rec))
            if rec["fingerprint"] != fingerprint:
                return LedgerResult("conflict", dict(rec))
            return LedgerResult("duplicate", dict(rec))

    def touch(self, sid: str, request_id: Optional[str]) -> None:
        """Refresh the activity clock so a streaming turn is never settled."""
        if not request_id:
            return
        with self._lock:
            rec = self._records.get((sid, request_id))
            if rec is not None and rec["status"] == "running":
                rec["updated_at"] = time.time()

    def finish(
        self,
        sid: str,
        request_id: Optional[str],
        *,
        status: str,
        result: Any = None,
    ) -> None:
        """Settle a request. Idempotent — the first terminal outcome wins."""
        if not request_id:
            return
        with self._lock:
            rec = self._records.get((sid, request_id))
            if rec is None or rec["status"] != "running":
                return
            rec["status"] = status
            rec["result"] = result
            rec["updated_at"] = time.time()

    def status(self, sid: str, request_id: str) -> Optional[dict]:
        now = time.time()
        with self._lock:
            self._settle_stale_locked(sid, now)
            rec = self._records.get((sid, request_id))
            return dict(rec) if rec is not None else None

    def forget_session(self, sid: str) -> None:
        with self._lock:
            for rid in self._by_session.pop(sid, ()):  # type: ignore[arg-type]
                self._records.pop((sid, rid), None)

    def clear(self) -> None:
        with self._lock:
            self._by_session.clear()
            self._records.clear()


def fingerprint_prompt(text: Any, extras: Iterable[Any] = ()) -> str:
    """Stable payload fingerprint for a submit.

    Used to tell an honest retry (same id, same payload → idempotent) apart
    from an id collision (same id, different payload → reject).
    """
    import hashlib

    parts = [repr(text)] + [repr(e) for e in extras]
    return hashlib.sha256("\x1f".join(parts).encode("utf-8", "replace")).hexdigest()
