"""Queued work must survive the client leaving and run without one attached.

The production failure this pins ("it only starts working when I open the
session"): the server accepts a prompt into ``session["queued_prompt"]``, the
client disconnects, and then either

* the 20-second WS-orphan reaper (or the TTL/LRU evictors) tears the session
  down and silently drops the accepted prompt, or
* the end-of-turn drain misses (update-guard race, dead turn thread) and no
  client-driven hook ever retries, so the prompt sits until the user happens
  to reopen that exact session.

Both are fixed by (a) exempting sessions with pending queued work from every
reaper, and (b) a background sweeper that starts accepted prompts on idle
sessions regardless of whether anyone is watching.

Also covers the ``client_request_id`` / ``prompt.status`` wire aliases kept
for clients built against the earlier hand-applied production hotfix.
"""

import threading
import time
import types

import pytest

from tui_gateway import server


@pytest.fixture
def clean_server():
    server._sessions.clear()
    server.reset_session_streams()
    yield server
    server._sessions.clear()
    server.reset_session_streams()


def _session(**extra):
    base = {
        "agent": types.SimpleNamespace(),
        "session_key": "key-1",
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "running": False,
        "transport": server._detached_ws_transport,
        "attached_images": [],
        "created_at": 0.0,
        "last_active": 0.0,
    }
    base.update(extra)
    return base


# ── reaper exemptions ──────────────────────────────────────────────────────


def test_orphan_reaper_spares_a_session_with_queued_work(clean_server):
    session = _session(queued_prompt={"text": "run this later", "transport": None})
    assert clean_server._ws_session_is_orphaned(session) is False
    # Without the queued prompt the same session IS orphaned.
    session["queued_prompt"] = None
    assert clean_server._ws_session_is_orphaned(session) is True


def test_ttl_evictor_spares_a_session_with_queued_work(clean_server):
    session = _session(queued_prompt={"text": "later", "transport": None})
    now = time.time() + clean_server._SESSION_TTL_S * 2
    assert clean_server._session_is_evictable("s1", session, now) is False
    session["queued_prompt"] = None
    assert clean_server._session_is_evictable("s1", session, now) is True


def test_lru_evictor_spares_a_session_with_queued_work(clean_server):
    session = _session(queued_prompt={"text": "later", "transport": None})
    assert clean_server._session_is_lru_evictable("s1", session) is False
    session["queued_prompt"] = None
    assert clean_server._session_is_lru_evictable("s1", session) is True


# ── background sweeper ─────────────────────────────────────────────────────


def test_sweeper_drains_idle_queued_sessions_without_a_client(clean_server, monkeypatch):
    drained = []
    monkeypatch.setattr(
        clean_server, "_drain_queued_prompt",
        lambda rid, sid, session: drained.append(sid) or True,
    )
    clean_server._sessions["ready"] = _session(
        queued_prompt={"text": "go", "transport": None}
    )
    clean_server._sessions["busy"] = _session(
        queued_prompt={"text": "wait", "transport": None}, running=True
    )
    clean_server._sessions["empty"] = _session()
    clean_server._sessions["dead"] = _session(
        queued_prompt={"text": "x", "transport": None}, _finalized=True
    )

    clean_server._sweep_queued_prompts()

    # Only the idle session with accepted work is started; a running turn,
    # an empty queue, and a finalized session are all left alone.
    assert drained == ["ready"]


def test_sweeper_kicks_agent_build_before_draining(clean_server, monkeypatch):
    built, drained = [], []
    monkeypatch.setattr(
        clean_server, "_start_agent_build", lambda sid, session: built.append(sid)
    )
    monkeypatch.setattr(
        clean_server, "_drain_queued_prompt",
        lambda rid, sid, session: drained.append(sid) or True,
    )
    ready = threading.Event()  # not set: build still in flight
    clean_server._sessions["building"] = _session(
        agent=None, agent_ready=ready,
        queued_prompt={"text": "go", "transport": None},
    )

    clean_server._sweep_queued_prompts()
    assert built == ["building"] and drained == []

    # Build finished → the next sweep starts the turn.
    ready.set()
    clean_server._sessions["building"]["agent"] = types.SimpleNamespace()
    clean_server._sweep_queued_prompts()
    assert drained == ["building"]


def test_sweeper_survives_a_drain_crash(clean_server, monkeypatch):
    def _boom(rid, sid, session):
        raise RuntimeError("drain exploded")

    monkeypatch.setattr(clean_server, "_drain_queued_prompt", _boom)
    clean_server._sessions["s1"] = _session(
        queued_prompt={"text": "go", "transport": None}
    )
    # Must not raise — a broken session can't stall the sweep loop.
    clean_server._sweep_queued_prompts()


# ── wire aliases ───────────────────────────────────────────────────────────


def test_coerce_request_id_accepts_both_field_names(clean_server):
    assert clean_server._coerce_request_id("r", {"request_id": "a"}) == ("a", None)
    assert clean_server._coerce_request_id("r", {"client_request_id": "b"}) == ("b", None)
    # Contract name wins when both are present.
    assert clean_server._coerce_request_id(
        "r", {"request_id": "a", "client_request_id": "b"}
    ) == ("a", None)
    assert clean_server._coerce_request_id("r", {}) == ("", None)


def test_coerce_request_id_rejects_malformed_values(clean_server):
    for bad in ["  padded  ", "x" * 201, "ctrl\x00char", 42]:
        value, err = clean_server._coerce_request_id("r", {"request_id": bad})
        assert value == "" and err is not None, bad
        assert err["error"]["code"] == 4004


def test_prompt_status_alias_answers_from_the_ledger(clean_server):
    clean_server._sessions["s1"] = _session()
    ledger = clean_server.request_ledger()
    ledger.begin("s1", "req-9", "fp")
    ledger.finish("s1", "req-9", status="complete", result={"text": "done"})

    resp = clean_server._methods["prompt.status"](
        "r1", {"session_id": "s1", "client_request_id": "req-9"}
    )
    assert resp["result"]["status"] == "complete"
    assert resp["result"]["client_request_id"] == "req-9"
    assert resp["result"]["result"] == {"text": "done"}

    unknown = clean_server._methods["prompt.status"](
        "r2", {"session_id": "s1", "client_request_id": "nope"}
    )
    assert unknown["result"]["status"] == "unknown"
