"""Real dev-backend proof for zero-loss deferred Hermes updates."""

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
BACKEND_TOKEN = "update-drain-dev-token"


def _run_disposable_backend() -> None:
    """Serve a deterministic clarify-producing agent on an ephemeral port."""
    from tui_gateway import server

    class FakeAgent:
        model = "dev-clarify-model"
        provider = "dev"
        api_key = "no-key-required"
        api_mode = "dev"
        base_url = ""
        reasoning_config = None
        service_tier = ""
        tools = []

        def __init__(self, sid: str, key: str):
            self.sid = sid
            self.session_id = key
            self._session_messages = []

        def clear_interrupt(self) -> None:
            return None

        def run_conversation(self, message, *, conversation_history=None, **_kwargs):
            answer = server._block(
                "clarify.request",
                self.sid,
                {
                    "question": "Which safe dev path should continue?",
                    "choices": ["A", "B"],
                },
                timeout=None,
            )
            final = f"continued with {answer}"
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
    server._ensure_session_db_row = lambda *_args, **_kwargs: None
    server._persist_branch_seed = lambda *_args, **_kwargs: None
    server._persist_session_history = lambda *_args, **_kwargs: None
    server._get_db = lambda: None
    server._load_cfg = lambda: {}
    server._config_model_target = lambda: "dev-clarify-model"
    server._resolve_model = lambda: "dev-clarify-model"
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

    start_server(
        host="127.0.0.1",
        port=0,
        open_browser=False,
        headless=True,
    )


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
    deadline = time.monotonic() + 20
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
    raise AssertionError("disposable backend did not become ready:\n" + "\n".join(lines[-80:]))


async def _receive_until(ws, predicate, seen: list[dict], timeout: float = 10) -> dict:
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


async def _rpc(ws, rid: str, method: str, params: dict, seen: list[dict]) -> dict:
    await ws.send(
        json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        )
    )
    return await _receive_until(ws, lambda item: item.get("id") == rid, seen)


def _guard_cli(env: dict[str, str], *args: str) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "hermes_cli.update_guard", *args],
        cwd=REPO_ROOT,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


async def _exercise_backend(port: int, env: dict[str, str], process: subprocess.Popen):
    uri = f"ws://127.0.0.1:{port}/api/ws?token={BACKEND_TOKEN}"
    seen: list[dict] = []
    session_id = ""
    request_id = ""

    async with websockets.connect(uri, proxy=None) as ws:
        await _receive_until(
            ws,
            lambda item: item.get("params", {}).get("type") == "gateway.ready",
            seen,
        )
        created = await _rpc(
            ws,
            "create",
            "session.create",
            {"cwd": env["HOME"], "close_on_disconnect": False},
            seen,
        )
        session_id = created["result"]["session_id"]
        submitted = await _rpc(
            ws,
            "submit",
            "prompt.submit",
            {"session_id": session_id, "text": "begin guarded work"},
            seen,
        )
        assert submitted["result"]["status"] == "streaming"
        clarify = await _receive_until(
            ws,
            lambda item: (
                item.get("params", {}).get("type") == "clarify.request"
                and item.get("params", {}).get("session_id") == session_id
            ),
            seen,
        )
        request_id = clarify["params"]["payload"]["request_id"]

        busy = await asyncio.to_thread(
            _guard_cli,
            env,
            "claim",
            "--candidate",
            "dev-before-remount",
            "--owner-pid",
            str(os.getpid()),
        )
        assert busy == {"status": "busy", "active_turns": 1}
        assert process.poll() is None

    # Model the renderer disappearing and reconnecting while the backend and
    # unanswered prompt continue to live.
    seen = []
    async with websockets.connect(uri, proxy=None) as ws:
        await _receive_until(
            ws,
            lambda item: item.get("params", {}).get("type") == "gateway.ready",
            seen,
        )
        activated = await _rpc(
            ws,
            "activate",
            "session.activate",
            {"session_id": session_id},
            seen,
        )
        assert activated["result"]["running"] is True

        pending = await _rpc(
            ws,
            "pending",
            "clarify.pending",
            {"session_id": session_id},
            seen,
        )
        assert pending["result"]["requests"] == [
            {
                "request_id": request_id,
                "session_id": session_id,
                "question": "Which safe dev path should continue?",
                "choices": ["A", "B"],
            }
        ]

        busy = await asyncio.to_thread(
            _guard_cli,
            env,
            "claim",
            "--candidate",
            "dev-after-remount",
            "--owner-pid",
            str(os.getpid()),
        )
        assert busy == {"status": "busy", "active_turns": 1}
        assert process.poll() is None

        answered = await _rpc(
            ws,
            "answer",
            "clarify.respond",
            {"request_id": request_id, "answer": "B"},
            seen,
        )
        assert answered["result"]["status"] == "ok"
        complete = await _receive_until(
            ws,
            lambda item: (
                item.get("params", {}).get("type") == "message.complete"
                and item.get("params", {}).get("session_id") == session_id
            ),
            seen,
        )
        assert complete["params"]["payload"]["text"] == "continued with B"

        pending = await _rpc(
            ws,
            "pending-after",
            "clarify.pending",
            {"session_id": session_id},
            seen,
        )
        assert pending["result"]["requests"] == []

        history = await _rpc(
            ws,
            "history",
            "session.history",
            {"session_id": session_id},
            seen,
        )
        assert [row["role"] for row in history["result"]["messages"]] == [
            "user",
            "assistant",
        ]
        assert history["result"]["messages"][-1]["text"] == "continued with B"

        closed = await _rpc(
            ws,
            "close",
            "session.close",
            {"session_id": session_id},
            seen,
        )
        assert closed["result"]["closed"] is True


def test_unanswered_clarify_survives_deferred_update_and_renderer_remount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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
            "HERMES_TUI_WS_ORPHAN_REAP_GRACE_S": "60",
            "NO_PROXY": "127.0.0.1,localhost",
            "PYTHONPATH": str(REPO_ROOT),
        }
    )
    env.pop("HERMES_DESKTOP", None)

    process, port, lines = _start_backend(env)
    try:
        asyncio.run(_exercise_backend(port, env, process))

        deadline = time.monotonic() + 5
        snapshot = _guard_cli(env, "snapshot")
        while snapshot["active_turns"] and time.monotonic() < deadline:
            time.sleep(0.02)
            snapshot = _guard_cli(env, "snapshot")
        assert snapshot["active_turns"] == 0

        # This in-process public API check intentionally skips legacy discovery:
        # the controller-facing CLI's process scan is covered independently and
        # would correctly include unrelated Hermes installs on a developer host.
        from hermes_cli.update_guard import try_claim_idle_update

        claimed = try_claim_idle_update("dev-after-answer")
        assert claimed.status == "claimed"
        assert claimed.claim is not None
        claimed.claim.release()
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
