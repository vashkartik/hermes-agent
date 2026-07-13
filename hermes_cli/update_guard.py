"""Atomic exclusion between Hermes agent turns and live code updates.

One Hermes checkout can serve the default profile and any number of named
profiles.  Updating that checkout affects all of them, so this registry is
root-scoped rather than profile-scoped.  It intentionally stores no message or
session content: only opaque lease ids and process identity metadata.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from hermes_constants import get_default_hermes_root


_VERSION = 1
_PROCESS_LOCK = threading.RLock()
_SELF_START_LOCK = threading.Lock()
_SELF_PROCESS_PID: int | None = None
_SELF_PROCESS_START: str | None = None


class UpdateDrainActive(RuntimeError):
    """Raised when a new turn loses the atomic claim to an update."""

    def __init__(self, message: str = "Hermes is applying an update; retry shortly"):
        super().__init__(message)


class _CorruptState(ValueError):
    pass


def _runtime_dir() -> Path:
    return get_default_hermes_root() / "runtime"


def _state_path() -> Path:
    return _runtime_dir() / "update-guard.json"


def _lock_path() -> Path:
    return _runtime_dir() / "update-guard.lock"


def _empty_state() -> dict[str, Any]:
    return {"version": _VERSION, "participants": [], "turns": [], "update": None}


class _FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = open(self.path, "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            self._handle.close()
            self._handle = None
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        if self._handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._handle = None


class _LockedState:
    def __enter__(self):
        _PROCESS_LOCK.acquire()
        try:
            self._file_lock = _FileLock(_lock_path())
            self._file_lock.__enter__()
        except Exception:
            _PROCESS_LOCK.release()
            raise
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            self._file_lock.__exit__(exc_type, exc, tb)
        finally:
            _PROCESS_LOCK.release()


def _validate_owner(row: Any, *, update: bool = False) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise _CorruptState("owner row is not an object")
    required = {"id", "kind", "pid", "process_start", "started_at"}
    if not required.issubset(row):
        raise _CorruptState("owner row is missing required fields")
    if not isinstance(row["id"], str) or not row["id"]:
        raise _CorruptState("owner id is invalid")
    if not isinstance(row["kind"], str) or not row["kind"]:
        raise _CorruptState("owner kind is invalid")
    if not isinstance(row["pid"], int) or isinstance(row["pid"], bool):
        raise _CorruptState("owner pid is invalid")
    if row["process_start"] is not None and not isinstance(row["process_start"], str):
        raise _CorruptState("owner process start is invalid")
    if not isinstance(row["started_at"], str) or not row["started_at"]:
        raise _CorruptState("owner timestamp is invalid")
    if update and (not isinstance(row.get("candidate"), str) or not row["candidate"]):
        raise _CorruptState("update candidate is invalid")
    return row


def _read_state() -> dict[str, Any]:
    path = _state_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return _empty_state()
    except OSError as exc:
        raise _CorruptState(str(exc)) from exc
    try:
        state = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _CorruptState("state is not valid JSON") from exc
    if not isinstance(state, dict) or state.get("version") != _VERSION:
        raise _CorruptState("state version is unsupported")
    # ``participants`` was added before the first guarded release shipped.
    # Accept a missing key so an interrupted dev rollout can self-migrate.
    participants = state.get("participants", [])
    turns = state.get("turns")
    update = state.get("update")
    if (
        not isinstance(participants, list)
        or not isinstance(turns, list)
        or (update is not None and not isinstance(update, dict))
    ):
        raise _CorruptState("state shape is invalid")
    for row in participants:
        _validate_owner(row)
    for row in turns:
        _validate_owner(row)
    if update is not None:
        _validate_owner(update, update=True)
    return {
        "version": _VERSION,
        "participants": participants,
        "turns": turns,
        "update": update,
    }


def _write_state(state: dict[str, Any]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(temp, flags, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _backup_corrupt_state() -> None:
    path = _state_path()
    if not path.exists():
        return
    backup = path.with_name(f"{path.name}.corrupt-{time.time_ns()}")
    os.replace(path, backup)


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        return _windows_pid_alive(pid)
    try:
        os.kill(pid, 0)  # windows-footgun: ok — POSIX-only branch above
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_pid_alive(pid: int) -> bool:
    """Probe a Windows PID without sending a console control event."""
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        kernel32.WaitForSingleObject.restype = ctypes.c_uint32
        process_query_limited_information = 0x1000
        synchronize = 0x100000
        wait_timeout = 0x00000102
        error_access_denied = 5
        handle = kernel32.OpenProcess(
            process_query_limited_information | synchronize,
            False,
            int(pid),
        )
        if not handle:
            return ctypes.get_last_error() == error_access_denied
        try:
            return kernel32.WaitForSingleObject(handle, 0) == wait_timeout
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError):
        return False


def _read_process_start(pid: int) -> str | None:
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            return stat.rsplit(")", 1)[1].split()[19]
        except (OSError, IndexError):
            return None
    if os.name == "posix":
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            return None
        value = (result.stdout or "").strip()
        return value or None
    return None


def _process_start(pid: int) -> str | None:
    global _SELF_PROCESS_PID, _SELF_PROCESS_START
    if pid != os.getpid():
        return _read_process_start(pid)
    if _SELF_PROCESS_PID == pid and _SELF_PROCESS_START is not None:
        return _SELF_PROCESS_START
    with _SELF_START_LOCK:
        # A fork inherits module globals. Never let a child publish its
        # parent's process-start identity or a live child lease could be
        # mistaken for a recycled/dead PID by another process.
        if _SELF_PROCESS_PID != pid:
            _SELF_PROCESS_PID = pid
            _SELF_PROCESS_START = _read_process_start(pid)
    return _SELF_PROCESS_START


def _owner_alive(row: dict[str, Any]) -> bool:
    pid = row["pid"]
    if not _pid_alive(pid):
        return False
    expected = row.get("process_start")
    if expected is None:
        return True
    actual = _process_start(pid)
    return actual is None or actual == expected


def _prune(state: dict[str, Any]) -> bool:
    participants_before = len(state["participants"])
    state["participants"] = [
        row for row in state["participants"] if _owner_alive(row)
    ]
    before = len(state["turns"])
    state["turns"] = [row for row in state["turns"] if _owner_alive(row)]
    changed = (
        len(state["participants"]) != participants_before
        or len(state["turns"]) != before
    )
    if state.get("update") is not None and not _owner_alive(state["update"]):
        state["update"] = None
        changed = True
    return changed


def _new_owner(kind: str, *, pid: int | None = None) -> dict[str, Any]:
    owner_pid = os.getpid() if pid is None else int(pid)
    return {
        "id": uuid.uuid4().hex,
        "kind": kind,
        "pid": owner_pid,
        "process_start": _process_start(owner_pid),
        "started_at": str(time.time_ns()),
    }


def _same_process_identity(
    row: dict[str, Any],
    *,
    pid: int,
    process_start: str | None,
) -> bool:
    if row.get("pid") != pid:
        return False
    expected = row.get("process_start")
    if expected is None or process_start is None:
        return expected is None and process_start is None
    return expected == process_start


def _ensure_runtime_participant(state: dict[str, Any], kind: str) -> bool:
    pid = os.getpid()
    process_start = _process_start(pid)
    if any(
        _same_process_identity(row, pid=pid, process_start=process_start)
        for row in state["participants"]
    ):
        return False
    state["participants"].append(_new_owner(kind))
    return True


def _release_turn(lease_id: str) -> None:
    with _LockedState():
        try:
            state = _read_state()
        except _CorruptState:
            return
        kept = [row for row in state["turns"] if row["id"] != lease_id]
        if len(kept) == len(state["turns"]):
            return
        state["turns"] = kept
        _write_state(state)


def _release_update(claim_id: str) -> None:
    with _LockedState():
        try:
            state = _read_state()
        except _CorruptState:
            return
        update = state.get("update")
        if update is None or update["id"] != claim_id:
            return
        state["update"] = None
        _write_state(state)


def _strip_profile_selectors(args: list[str]) -> list[str]:
    filtered: list[str] = []
    skip_next = False
    for token in args:
        if skip_next:
            skip_next = False
            continue
        if token in {"--profile", "-p"}:
            skip_next = True
            continue
        if token.startswith("--profile=") or token.startswith("-p="):
            continue
        filtered.append(token)
    return filtered


def _hermes_command_hosts_runtime(args: list[str]) -> bool:
    filtered = _strip_profile_selectors(args)
    if not filtered:
        return False
    command = filtered[0]
    if command in {"dashboard", "serve"}:
        return True
    if command != "gateway":
        return False
    return len(filtered) == 1 or filtered[1] in {"run", "restart"}


def _looks_like_affected_runtime_command(command: str | None) -> bool:
    """Match only Hermes runtime entrypoints, never incidental prompt text."""
    if not command:
        return False
    try:
        tokens = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        tokens = command.split()
    tokens = [token.strip("\"'").replace("\\", "/").lower() for token in tokens]
    if not tokens:
        return False

    # Unwrap the small set of process launch prefixes used by service files.
    index = 0
    while index < len(tokens):
        basename = tokens[index].rsplit("/", 1)[-1]
        if basename == "nohup":
            index += 1
            continue
        if basename == "env":
            index += 1
            while index < len(tokens) and "=" in tokens[index]:
                index += 1
            continue
        break
    if index >= len(tokens):
        return False

    executable = tokens[index]
    basename = executable.rsplit("/", 1)[-1]
    args = tokens[index + 1 :]

    if basename in {"hermes-gateway", "hermes-gateway.exe"}:
        return True
    if basename in {"hermes", "hermes.exe"}:
        return _hermes_command_hosts_runtime(args)

    is_python = (
        basename.startswith("python")
        or basename.startswith("pypy")
        or basename in {"py", "py.exe"}
    )
    if not is_python:
        return False

    # Python flags may precede ``-m`` (for example ``python -u -m ...``).
    module_index = None
    for offset, token in enumerate(args):
        if token == "-m":
            module_index = offset
            break
        if not token.startswith("-"):
            break
    if module_index is not None and module_index + 1 < len(args):
        module = args[module_index + 1]
        module_args = args[module_index + 2 :]
        if module in {"tui_gateway.entry", "gateway.run"}:
            return True
        if module == "hermes_cli.main":
            return _hermes_command_hosts_runtime(module_args)
        return False

    script = next((token for token in args if not token.startswith("-")), "")
    if not script:
        return False
    if script == "gateway/run.py" or script.endswith("/gateway/run.py"):
        return True
    if script == "tui_gateway/entry.py" or script.endswith("/tui_gateway/entry.py"):
        return True
    if script == "hermes_cli/main.py" or script.endswith("/hermes_cli/main.py"):
        try:
            script_index = args.index(script)
        except ValueError:
            return False
        return _hermes_command_hosts_runtime(args[script_index + 1 :])
    return False


def _discover_affected_processes() -> list[dict[str, Any]]:
    """Return live dashboard/TUI/gateway processes from the OS process table.

    This is called only by the controller-facing CLI claim path. Ordinary turn
    acquisition never scans processes.
    """
    rows: list[tuple[int, str]] = []
    if sys.platform == "win32":
        try:
            from hermes_cli._subprocess_compat import windows_hide_flags

            result = subprocess.run(
                ["wmic", "process", "get", "ProcessId,CommandLine", "/FORMAT:LIST"],
                capture_output=True,
                text=True,
                timeout=10,
                encoding="utf-8",
                errors="ignore",
                creationflags=windows_hide_flags(),
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise OSError("unable to scan Windows process table") from exc
        if result.returncode != 0 or result.stdout is None:
            raise OSError("unable to scan Windows process table")
        current_command = ""
        for line in result.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("CommandLine="):
                current_command = stripped[len("CommandLine=") :]
            elif stripped.startswith("ProcessId="):
                try:
                    rows.append((int(stripped[len("ProcessId=") :]), current_command))
                except ValueError:
                    continue
    else:
        try:
            result = subprocess.run(
                ["ps", "-A", "-o", "pid=,command="],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
            raise OSError("unable to scan process table") from exc
        if result.returncode != 0:
            raise OSError("unable to scan process table")
        for line in (result.stdout or "").splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2:
                continue
            try:
                rows.append((int(parts[0]), parts[1]))
            except ValueError:
                continue

    affected: list[dict[str, Any]] = []
    self_pid = os.getpid()
    for pid, process_command in rows:
        if pid == self_pid or not _looks_like_affected_runtime_command(process_command):
            continue
        affected.append(
            {
                "pid": pid,
                "process_start": _read_process_start(pid),
                "command": process_command,
            }
        )
    return affected


def _classify_affected_processes(
    rows: list[dict[str, Any]],
    guarded_rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    classified: list[dict[str, Any]] = []
    for row in rows:
        try:
            pid = int(row["pid"])
        except (KeyError, TypeError, ValueError):
            continue
        process_start = row.get("process_start", row.get("start"))
        guarded = any(
            _same_process_identity(
                owner,
                pid=pid,
                process_start=process_start,
            )
            for owner in guarded_rows
        )
        classified.append(
            {
                "pid": pid,
                "process_start": process_start,
                "guarded": guarded,
            }
        )
    return classified


@dataclass
class TurnLease:
    id: str
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        _release_turn(self.id)
        self._released = True


@dataclass
class UpdateClaim:
    id: str
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        _release_update(self.id)
        self._released = True


@dataclass(frozen=True)
class ClaimResult:
    status: str
    active_turns: int
    claim: UpdateClaim | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": self.status,
            "active_turns": self.active_turns,
        }
        if self.claim is not None:
            payload["claim_id"] = self.claim.id
        return payload


def register_runtime(kind: str) -> None:
    """Mark this long-lived process as capable of the atomic update guard."""
    normalized_kind = str(kind).strip()[:64]
    if not normalized_kind:
        raise ValueError("runtime kind is required")
    with _LockedState():
        try:
            state = _read_state()
        except _CorruptState:
            _backup_corrupt_state()
            state = _empty_state()
        changed = _prune(state)
        changed = _ensure_runtime_participant(state, normalized_kind) or changed
        if changed:
            _write_state(state)


def acquire_turn(kind: str, session_id: str = "") -> TurnLease:
    """Register a live agent turn or raise when an update owns the drain.

    ``session_id`` is accepted so callers can use a natural API, but is never
    persisted: session identifiers may contain user-selected titles.
    """
    del session_id
    normalized_kind = str(kind).strip()[:64]
    if not normalized_kind:
        raise ValueError("turn kind is required")
    with _LockedState():
        try:
            state = _read_state()
        except _CorruptState:
            _backup_corrupt_state()
            state = _empty_state()
        changed = _prune(state)
        changed = _ensure_runtime_participant(state, normalized_kind) or changed
        if state.get("update") is not None:
            if changed:
                _write_state(state)
            raise UpdateDrainActive()
        row = _new_owner(normalized_kind)
        state["turns"].append(row)
        _write_state(state)
    return TurnLease(row["id"])


def try_claim_idle_update(
    candidate: str,
    *,
    owner_pid: int | None = None,
    check_legacy: bool = False,
) -> ClaimResult:
    """Atomically claim update drain only when no live turn owns a lease."""
    normalized_candidate = str(candidate).strip()[:128]
    if not normalized_candidate:
        raise ValueError("update candidate is required")
    with _LockedState():
        try:
            state = _read_state()
        except _CorruptState:
            return ClaimResult("corrupt", 0)
        changed = _prune(state)
        if state["turns"]:
            if changed:
                _write_state(state)
            return ClaimResult("busy", len(state["turns"]))
        if state.get("update") is not None:
            if changed:
                _write_state(state)
            return ClaimResult("busy", 0)
        if check_legacy:
            try:
                affected = _discover_affected_processes()
            except Exception:
                if changed:
                    _write_state(state)
                return ClaimResult("legacy_runtime", 0)
            classified = _classify_affected_processes(
                affected,
                state["participants"],
            )
            if any(not row["guarded"] for row in classified):
                if changed:
                    _write_state(state)
                return ClaimResult("legacy_runtime", 0)
        row = _new_owner("update", pid=owner_pid)
        row["candidate"] = normalized_candidate
        state["update"] = row
        _write_state(state)
    return ClaimResult("claimed", 0, UpdateClaim(row["id"]))


def snapshot() -> dict[str, Any]:
    """Return a content-free, pruned status snapshot."""
    with _LockedState():
        try:
            state = _read_state()
        except _CorruptState:
            return {
                "version": _VERSION,
                "status": "corrupt",
                "active_turns": 0,
                "update": None,
            }
        if _prune(state):
            _write_state(state)
        update = state.get("update")
        return {
            "version": _VERSION,
            "status": "ok",
            "active_turns": len(state["turns"]),
            "update": (
                {"candidate": update["candidate"], "started_at": update["started_at"]}
                if update is not None
                else None
            ),
        }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes atomic update guard")
    subparsers = parser.add_subparsers(dest="command", required=True)

    claim = subparsers.add_parser("claim")
    claim.add_argument("--candidate", required=True)
    claim.add_argument("--owner-pid", type=int)

    release = subparsers.add_parser("release")
    release.add_argument("--claim-id", required=True)

    subparsers.add_parser("snapshot")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command == "claim":
        result = try_claim_idle_update(
            args.candidate,
            owner_pid=args.owner_pid,
            check_legacy=True,
        )
        print(json.dumps(result.to_json(), sort_keys=True))
        return 0
    if args.command == "release":
        UpdateClaim(args.claim_id).release()
        print(json.dumps({"status": "released"}, sort_keys=True))
        return 0
    print(json.dumps(snapshot(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
