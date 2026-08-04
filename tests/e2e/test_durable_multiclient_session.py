"""Real-gateway proof that one session survives two devices at once.

Boots a disposable Hermes backend on an ephemeral port, attaches TWO real
WebSocket clients ("desktop" and "mobile") to ONE session, and drives a real
turn through the real ``tui_gateway`` dispatcher. It asserts the whole durable
contract end to end:

* both clients receive the same deltas, in the same order, under the same
  sequence numbers — no dropped stream on the second device;
* exactly ONE terminal event reaches each client — no duplicate completion;
* a viewer cannot mutate the session, and an explicit claim hands it over;
* the owner can drop its socket mid-turn, reconnect, and replay the frames it
  missed without a gap;
* a retried ``prompt.submit`` carrying the same ``request_id`` does NOT run the
  turn twice, and ``request.status`` answers what happened to it;
* the session ends with exactly one persisted user/assistant turn, visible
  identically to both clients.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import websockets

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_TOKEN = "durable-session-dev-token"
DELTAS = ["one ", "two ", "three ", "four "]


def _run_disposable_backend() -> None:
    """Serve a deterministic streaming agent on an ephemeral port."""
    from tui_gateway import server

    class FakeAgent:
        model = "dev-durable-model"
        provider = "dev"
        api_key = "no-key-required"
        api_mode = "dev"
        base_url = ""
        reasoning_config = None
        service_tier = ""
        tools = []

        # Process-global so a SECOND execution of the same request id would be
        # visible to the test as a second run.
        runs = 0

        def __init__(self, sid: str, key: str):
            self.sid = sid
            self.session_id = key
            self._session_messages = []

        def clear_interrupt(self) -> None:
            return None

        def run_conversation(self, message, *, conversation_history=None, **_kwargs):
            FakeAgent.runs += 1
            for chunk in DELTAS:
                server._emit("message.delta", self.sid, {"text": chunk})
            # Park the turn so the test can drop and reattach a client while the
            # turn is genuinely in flight.
            answer = server._block(
                "clarify.request",
                self.sid,
                {"question": "continue?", "choices": ["yes"]},
                timeout=None,
            )
            final = f"{''.join(DELTAS)}{answer} (runs={FakeAgent.runs})"
            history = list(conversation_history or [])
            history.extend(
                [
                    {"role": "user", "content": str(message)},
                    {"role": "assistant", "content": final},
                ]
            )
            self._session_messages = history
            return {"final_response": final, "messages": history}

    class FakeSlashWorker:
        def __init__(self, *_args, **_kwargs):
            pass

        def close(self) -> None:
            return None

    server._make_agent = lambda sid, key, **_kwargs: FakeAgent(sid, key)
    server._SlashWorker = FakeSlashWorker
    server._start_notification_poller = lambda _sid, _session: threading.Event()
    server._schedule_mcp_late_refresh = lambda *_args, **_kwargs: None
    server._notify_session_boundary = lambda *_args, **_kwargs: None
    server._wire_callbacks = lambda *_args, **_kwargs: None
    server._sync_agent_model_with_config = lambda *_args, **_kwargs: None
    server._persist_branch_seed = lambda *_args, **_kwargs: None
    server._load_cfg = lambda: {}
    server._config_model_target = lambda: "dev-durable-model"
    server._resolve_model = lambda: "dev-durable-model"
    server.resolve_skin = lambda: {"name": "dev"}
    server.make_stream_renderer = lambda *_args, **_kwargs: None
    server.render_message = lambda text, _cols: str(text)
    server._session_info = lambda agent, session=None: {
        "model": agent.model,
        "provider": agent.provider,
        "running": bool((session or {}).get("running")),
    }

    from hermes_cli import nous_auth_keepalive

    nous_auth_keepalive.start_nous_auth_keepalive = lambda: None

    from hermes_cli.web_server import start_server

    start_server(host="127.0.0.1", port=0, open_browser=False, headless=True)


def _start_backend(env: dict[str, str]) -> tuple[subprocess.Popen, int, list[str]]:
    process = subprocess.Popen(
        [sys.executable, "-u", str(Path(__file__).resolve()), "--backend"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert process.stdout is not None
    lines: list[str] = []
    output: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line.rstrip())
            output.put(line.rstrip())

    threading.Thread(target=read_output, daemon=True).start()
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(
                f"disposable backend exited with {process.returncode}:\n"
                + "\n".join(lines[-80:])
            )
        try:
            line = output.get(timeout=0.2)
        except queue.Empty:
            continue
        if line.startswith("HERMES_BACKEND_READY port="):
            return process, int(line.rsplit("=", 1)[1]), lines
    raise AssertionError(
        "disposable backend did not become ready:\n" + "\n".join(lines[-80:])
    )


async def _receive_until(ws, predicate, seen: list[dict], timeout: float = 20) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    for item in seen:
        if predicate(item):
            return item
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"timed out waiting for WebSocket frame; seen={seen!r}")
        raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        item = json.loads(raw)
        seen.append(item)
        if predicate(item):
            return item


async def _drain(ws, seen: list[dict], settle: float = 0.4) -> None:
    """Collect whatever is already queued, then stop."""
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=settle)
        except (asyncio.TimeoutError, TimeoutError):
            return
        seen.append(json.loads(raw))


async def _rpc(ws, rid: str, method: str, params: dict, seen: list[dict]) -> dict:
    await ws.send(
        json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
    )
    return await _receive_until(ws, lambda item: item.get("id") == rid, seen)


def _events(seen: list[dict], sid: str, event_type: str) -> list[dict]:
    return [
        item["params"]
        for item in seen
        if item.get("method") == "event"
        and item.get("params", {}).get("type") == event_type
        and item.get("params", {}).get("session_id") == sid
    ]


async def _ready(ws, seen: list[dict]) -> None:
    await _receive_until(
        ws, lambda item: item.get("params", {}).get("type") == "gateway.ready", seen
    )


async def _exercise_backend(port: int, env: dict[str, str], process: subprocess.Popen):
    uri = f"ws://127.0.0.1:{port}/api/ws?token={BACKEND_TOKEN}"
    desktop_seen: list[dict] = []
    mobile_seen: list[dict] = []

    desktop = await websockets.connect(uri, proxy=None)
    mobile = await websockets.connect(uri, proxy=None)
    try:
        await _ready(desktop, desktop_seen)
        await _ready(mobile, mobile_seen)

        # ── one session, two devices ────────────────────────────────
        created = await _rpc(
            desktop, "create", "session.create",
            {"cwd": env["HOME"], "close_on_disconnect": False}, desktop_seen,
        )
        sid = created["result"]["session_id"]

        joined = await _rpc(
            mobile, "join", "session.subscribe",
            {"session_id": sid, "owner": True}, mobile_seen,
        )
        # Desktop is live and owns it: mobile joins as a viewer, not a thief.
        assert joined["result"]["owner"] is False, joined
        assert len(joined["result"]["clients"]) == 2, joined

        # ── a viewer may not drive the session ──────────────────────
        refused = await _rpc(
            mobile, "steal", "prompt.submit",
            {"session_id": sid, "text": "mobile tries to drive"}, mobile_seen,
        )
        assert refused.get("error", {}).get("code") == 4092, refused

        # ── the owner starts a turn, both devices stream it ─────────
        submitted = await _rpc(
            desktop, "submit", "prompt.submit",
            {"session_id": sid, "text": "durable please", "request_id": "req-1"},
            desktop_seen,
        )
        assert submitted["result"]["status"] == "streaming", submitted
        assert submitted["result"]["request_id"] == "req-1"

        for seen, ws in ((desktop_seen, desktop), (mobile_seen, mobile)):
            await _receive_until(
                ws,
                lambda item: item.get("params", {}).get("type") == "clarify.request"
                and item.get("params", {}).get("session_id") == sid,
                seen,
            )

        desktop_deltas = _events(desktop_seen, sid, "message.delta")
        mobile_deltas = _events(mobile_seen, sid, "message.delta")
        assert [d["payload"]["text"] for d in desktop_deltas] == DELTAS
        # The whole point: the SECOND device saw every delta too, in the same
        # order, carrying the same sequence numbers.
        assert [d["payload"]["text"] for d in mobile_deltas] == DELTAS
        assert [d["seq"] for d in mobile_deltas] == [d["seq"] for d in desktop_deltas]
        assert [d["seq"] for d in desktop_deltas] == sorted(
            d["seq"] for d in desktop_deltas
        )

        # ── retry with the same request id must not run a second turn ──
        retry = await _rpc(
            desktop, "retry", "prompt.submit",
            {"session_id": sid, "text": "durable please", "request_id": "req-1"},
            desktop_seen,
        )
        assert retry["result"]["status"] == "duplicate", retry
        assert retry["result"]["request_status"] == "running", retry

        # ── same id, different payload is a conflict ────────────────
        conflict = await _rpc(
            desktop, "conflict", "prompt.submit",
            {"session_id": sid, "text": "something else", "request_id": "req-1"},
            desktop_seen,
        )
        assert conflict.get("error", {}).get("code") == 4094, conflict

        # ── owner drops mid-turn; the turn keeps running ────────────
        last_seq = max(d["seq"] for d in desktop_deltas)
        clarify_id = _events(desktop_seen, sid, "clarify.request")[0]["payload"][
            "request_id"
        ]
        await desktop.close()

        # Mobile is still attached, so the session is handed to it rather than
        # orphaned — and mobile sees why.
        await _receive_until(
            mobile,
            lambda item: item.get("params", {}).get("type") == "session.owner_changed"
            and item.get("params", {}).get("session_id") == sid,
            mobile_seen,
        )

        # ── owner reconnects and replays what it missed ─────────────
        desktop = await websockets.connect(uri, proxy=None)
        desktop_seen = []
        await _ready(desktop, desktop_seen)
        reclaim = await _rpc(
            desktop, "reclaim", "session.claim", {"session_id": sid}, desktop_seen
        )
        assert reclaim["result"]["owner"] is True, reclaim
        assert reclaim["result"]["running"] is True, reclaim

        replayed = await _rpc(
            desktop, "replay", "session.replay",
            {"session_id": sid, "after_seq": last_seq}, desktop_seen,
        )
        assert replayed["result"]["truncated"] is False, replayed
        replay_seqs = [f["params"]["seq"] for f in replayed["result"]["frames"]]
        # Contiguous from where the socket died: nothing was lost in the gap.
        assert replay_seqs == list(range(last_seq + 1, last_seq + 1 + len(replay_seqs)))

        # ── finish the turn; ONE terminal event on each device ──────
        answered = await _rpc(
            desktop, "answer", "clarify.respond",
            {"request_id": clarify_id, "answer": "yes"}, desktop_seen,
        )
        assert answered["result"]["status"] == "ok", answered

        for seen, ws in ((desktop_seen, desktop), (mobile_seen, mobile)):
            await _receive_until(
                ws,
                lambda item: item.get("params", {}).get("type") == "message.complete"
                and item.get("params", {}).get("session_id") == sid,
                seen,
            )
        await _drain(desktop, desktop_seen)
        await _drain(mobile, mobile_seen)

        expected = f"{''.join(DELTAS)}yes (runs=1)"
        for label, seen in (("desktop", desktop_seen), ("mobile", mobile_seen)):
            terminals = _events(seen, sid, "message.complete") + _events(seen, sid, "error")
            assert len(terminals) == 1, f"{label} saw {len(terminals)} terminal events"
            assert terminals[0]["payload"]["text"] == expected, label

        # ── the request id is now queryable, from either device ─────
        for ws, rid_, seen in (
            (desktop, "status-desktop", desktop_seen),
            (mobile, "status-mobile", mobile_seen),
        ):
            status = await _rpc(
                ws, rid_, "request.status",
                {"session_id": sid, "request_id": "req-1"}, seen,
            )
            assert status["result"]["status"] == "complete", status
            assert status["result"]["result"]["text"] == expected, status

        # A retry AFTER completion replays the result instead of re-running.
        replay_submit = await _rpc(
            desktop, "retry2", "prompt.submit",
            {"session_id": sid, "text": "durable please", "request_id": "req-1"},
            desktop_seen,
        )
        assert replay_submit["result"]["status"] == "duplicate", replay_submit
        assert replay_submit["result"]["request_status"] == "complete", replay_submit

        # ── exactly one persisted turn, identical on both devices ───
        histories = {}
        for label, ws, rid_, seen in (
            ("desktop", desktop, "hist-desktop", desktop_seen),
            ("mobile", mobile, "hist-mobile", mobile_seen),
        ):
            hist = await _rpc(
                ws, rid_, "session.history", {"session_id": sid}, seen
            )
            histories[label] = hist["result"]["messages"]
        assert [m["role"] for m in histories["desktop"]] == ["user", "assistant"]
        assert histories["mobile"] == histories["desktop"]
        assert histories["desktop"][-1]["text"] == expected

        # ── no cross-session delivery ───────────────────────────────
        other = await _rpc(
            desktop, "create2", "session.create",
            {"cwd": env["HOME"], "close_on_disconnect": False}, desktop_seen,
        )
        other_sid = other["result"]["session_id"]
        assert other_sid != sid
        assert _events(mobile_seen, other_sid, "message.delta") == []
        stray = await _rpc(
            mobile, "stray", "request.status",
            {"session_id": other_sid, "request_id": "req-1"}, mobile_seen,
        )
        # req-1 belongs to the first session only.
        assert stray["result"]["status"] == "unknown", stray

        assert process.poll() is None
    finally:
        for ws in (desktop, mobile):
            try:
                await ws.close()
            except Exception:
                pass


@pytest.mark.timeout(180)
def test_one_session_survives_desktop_and_mobile_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = tmp_path / "home"
    hermes_home = tmp_path / "hermes-home"
    ace_user_data = tmp_path / "ace-user-data"
    for path in (home, hermes_home, ace_user_data):
        path.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("ACE_USER_DATA", str(ace_user_data))
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "HERMES_HOME": str(hermes_home),
            "ACE_USER_DATA": str(ace_user_data),
            "HERMES_DASHBOARD_SESSION_TOKEN": BACKEND_TOKEN,
            # Long enough that nothing is reaped during the reconnect window,
            # so a failure here is a real defect and not a race with the reaper.
            "HERMES_TUI_WS_ORPHAN_REAP_GRACE_S": "120",
            "NO_PROXY": "127.0.0.1,localhost",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    env.pop("HERMES_DESKTOP", None)

    process, port, lines = _start_backend(env)
    try:
        asyncio.run(_exercise_backend(port, env, process))
        assert process.poll() is None
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.returncode not in {0, -15}:
            pytest.fail(
                f"disposable backend exited with {process.returncode}:\n"
                + "\n".join(lines[-100:])
            )


if __name__ == "__main__" and "--backend" in sys.argv:
    _run_disposable_backend()
