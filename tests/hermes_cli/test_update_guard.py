"""Atomic exclusion between live Hermes turns and code updates.

Every test uses a disposable HERMES_HOME.  Nothing in this module may inspect
or signal a real Hermes/Ace process.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from hermes_cli import update_guard


@pytest.fixture
def guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "hermes-home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    return update_guard


def test_live_turn_makes_update_claim_busy(guard):
    lease = guard.acquire_turn("desktop")

    result = guard.try_claim_idle_update("abc123")

    assert result.status == "busy"
    assert result.active_turns == 1
    assert result.claim is None
    assert guard.snapshot()["update"] is None

    lease.release()
    claimed = guard.try_claim_idle_update("abc123")
    assert claimed.status == "claimed"
    assert claimed.claim is not None
    claimed.claim.release()


def test_update_claim_blocks_new_turn_until_released(guard):
    result = guard.try_claim_idle_update("abc123")
    assert result.status == "claimed"
    assert result.claim is not None

    with pytest.raises(guard.UpdateDrainActive):
        guard.acquire_turn("desktop")

    result.claim.release()
    lease = guard.acquire_turn("desktop")
    lease.release()


def test_turn_and_update_race_has_exactly_one_winner(guard):
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    handles = []

    def start_turn() -> None:
        barrier.wait()
        try:
            handles.append(guard.acquire_turn("desktop"))
            outcomes.append("turn")
        except guard.UpdateDrainActive:
            outcomes.append("blocked")

    def start_update() -> None:
        barrier.wait()
        result = guard.try_claim_idle_update("abc123")
        outcomes.append("drain" if result.status == "claimed" else result.status)
        if result.claim is not None:
            handles.append(result.claim)

    threads = [
        threading.Thread(target=start_turn),
        threading.Thread(target=start_update),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert all(not thread.is_alive() for thread in threads)
    assert sorted(outcomes) in (["busy", "turn"], ["blocked", "drain"])
    for handle in handles:
        handle.release()


def test_dead_update_owner_is_pruned_before_turn_start(guard):
    result = guard.try_claim_idle_update("abc123", owner_pid=999_999_999)
    assert result.status == "claimed"

    lease = guard.acquire_turn("desktop")

    assert guard.snapshot()["update"] is None
    lease.release()


def test_dead_turn_owner_is_pruned_before_update_claim(
    guard, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    home = Path(os.environ["HERMES_HOME"])
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from hermes_cli.update_guard import acquire_turn; acquire_turn('desktop')",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert child.returncode == 0, child.stderr

    result = guard.try_claim_idle_update("abc123")

    assert result.status == "claimed"
    assert result.active_turns == 0
    assert result.claim is not None
    result.claim.release()


def test_release_handles_are_idempotent_and_owner_checked(guard):
    turn = guard.acquire_turn("desktop")
    turn.release()
    turn.release()
    assert guard.snapshot()["active_turns"] == 0

    first = guard.try_claim_idle_update("abc123")
    assert first.claim is not None
    forged = guard.UpdateClaim("not-the-owner")
    forged.release()
    assert guard.snapshot()["update"]["candidate"] == "abc123"
    first.claim.release()
    first.claim.release()
    assert guard.snapshot()["update"] is None


def test_registry_contains_no_session_or_prompt_content(guard):
    lease = guard.acquire_turn("desktop", session_id="secret-session-name")
    state_path = Path(os.environ["HERMES_HOME"]) / "runtime" / "update-guard.json"

    raw = state_path.read_text(encoding="utf-8")
    state = json.loads(raw)

    assert "secret-session-name" not in raw
    assert set(state) == {"participants", "turns", "update", "version"}
    assert set(state["participants"][0]) == {
        "id",
        "kind",
        "pid",
        "process_start",
        "started_at",
    }
    assert set(state["turns"][0]) == {
        "id",
        "kind",
        "pid",
        "process_start",
        "started_at",
    }
    lease.release()


def test_corrupt_state_denies_update_but_does_not_brick_turns(guard):
    state_path = Path(os.environ["HERMES_HOME"]) / "runtime" / "update-guard.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not-json", encoding="utf-8")

    result = guard.try_claim_idle_update("abc123")

    assert result.status == "corrupt"
    assert result.claim is None
    lease = guard.acquire_turn("desktop")
    assert list(state_path.parent.glob("update-guard.json.corrupt-*"))
    lease.release()


def test_cli_busy_claim_is_machine_readable(guard):
    lease = guard.acquire_turn("desktop")
    env = os.environ.copy()

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "hermes_cli.update_guard",
            "claim",
            "--candidate",
            "candidate-cli",
            "--owner-pid",
            str(os.getpid()),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"status": "busy", "active_turns": 1}
    lease.release()


def test_legacy_runtime_refuses_update_claim(guard, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        guard,
        "_discover_affected_processes",
        lambda: [
            {
                "pid": 4242,
                "process_start": "legacy-start",
                "command": "python -m hermes_cli.main serve",
            }
        ],
    )

    result = guard.try_claim_idle_update("candidate", check_legacy=True)

    assert result.status == "legacy_runtime"
    assert result.active_turns == 0
    assert result.claim is None


def test_registered_runtime_is_not_misclassified_as_legacy(
    guard, monkeypatch: pytest.MonkeyPatch
):
    guard.register_runtime("dashboard")
    monkeypatch.setattr(
        guard,
        "_discover_affected_processes",
        lambda: [
            {
                "pid": os.getpid(),
                "process_start": guard._process_start(os.getpid()),
                "command": "python -m hermes_cli.main serve",
            }
        ],
    )

    result = guard.try_claim_idle_update("candidate", check_legacy=True)

    assert result.status == "claimed"
    assert result.claim is not None
    result.claim.release()


def test_runtime_scan_error_denies_update_claim(
    guard, monkeypatch: pytest.MonkeyPatch
):
    def fail_scan():
        raise OSError("process table unavailable")

    monkeypatch.setattr(guard, "_discover_affected_processes", fail_scan)

    result = guard.try_claim_idle_update("candidate", check_legacy=True)

    assert result.status == "legacy_runtime"
    assert result.claim is None


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("/repo/venv/bin/hermes dashboard --no-open", True),
        ("python -m hermes_cli.main serve --port 0", True),
        ("python /repo/hermes_cli/main.py -p coder dashboard", True),
        ("python -m tui_gateway.entry", True),
        ("python /repo/tui_gateway/entry.py", True),
        ("python -m gateway.run", True),
        ("/repo/venv/bin/hermes gateway run --replace", True),
        ("/repo/venv/bin/hermes gateway status", False),
        ("python worker.py 'please run hermes dashboard later'", False),
        ("rg -n 'hermes dashboard' hermes_cli", False),
    ],
)
def test_affected_runtime_command_matching_is_entrypoint_exact(
    guard, command: str, expected: bool
):
    assert guard._looks_like_affected_runtime_command(command) is expected


def test_cli_release_clears_only_matching_claim(guard, capsys: pytest.CaptureFixture):
    result = guard.try_claim_idle_update("candidate")
    assert result.claim is not None

    assert guard.main(["release", "--claim-id", "wrong-id"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "released"}
    assert guard.snapshot()["update"] is not None

    assert guard.main(["release", "--claim-id", result.claim.id]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "released"}
    assert guard.snapshot()["update"] is None


def test_cli_claimed_result_includes_releasable_claim_id(
    guard,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
):
    monkeypatch.setattr(guard, "_discover_affected_processes", lambda: [])

    assert guard.main(
        [
            "claim",
            "--candidate",
            "candidate-cli",
            "--owner-pid",
            str(os.getpid()),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["status"] == "claimed"
    assert payload["active_turns"] == 0
    assert isinstance(payload["claim_id"], str) and payload["claim_id"]

    assert guard.main(["release", "--claim-id", payload["claim_id"]]) == 0
    capsys.readouterr()
    assert guard.snapshot()["update"] is None
