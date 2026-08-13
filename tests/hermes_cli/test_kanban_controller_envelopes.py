"""Behavior contract for opt-in ACE controller envelopes v1."""

from __future__ import annotations

import json
import shlex
from itertools import permutations
from pathlib import Path

import pytest

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def conn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    with kb.connect() as connection:
        yield connection


def _task(conn, *, assignee="ace-controller") -> str:
    return kb.create_task(conn, title="controller card", assignee=assignee)


def _opt_in(conn, task_id: str, *, correlation="corr-1") -> dict:
    return kb.opt_in_controller_task(
        conn,
        task_id,
        controller_assignee="ace-controller",
        correlation_id=correlation,
        ace_identity="ace:operator-1",
    )


def _record(
    conn,
    task_id: str,
    event_type: str,
    n: int,
    *,
    correlation="corr-1",
    idempotency_key=None,
    **extra,
):
    return kb.record_controller_envelope(
        conn,
        task_id,
        event_type=event_type,
        idempotency_key=idempotency_key or f"idem-{task_id}-{event_type}-{n}",
        occurred_at=f"2026-08-13T12:00:0{n}Z",
        correlation_id=correlation,
        ace_identity="ace:operator-1",
        **extra,
    )


def _terminal_flow(conn, task_id: str, *, correlation="corr-1") -> None:
    _record(conn, task_id, "OUTBOUND", 1, correlation=correlation)
    _record(
        conn,
        task_id,
        "TRANSITION",
        2,
        correlation=correlation,
        ace_receipt={"receipt_id": "ace-r-1"},
    )
    _record(
        conn,
        task_id,
        "RETURN",
        3,
        correlation=correlation,
        terminal_receipt={"receipt_id": "terminal-r-1"},
        vector_ack={"ack_id": "vector-a-1"},
    )


def test_opt_in_is_explicit_exactly_idempotent_and_correlation_unique(conn) -> None:
    task_id = _task(conn)
    projection = _opt_in(conn, task_id)
    assert projection["protocol"] == "ace.controller.v1"
    assert projection["controller_assignee"] == "ace-controller"
    assert _opt_in(conn, task_id) == projection

    with pytest.raises(kb.ControllerEnvelopeError, match="different controller binding"):
        kb.opt_in_controller_task(
            conn,
            task_id,
            controller_assignee="ace-controller",
            correlation_id="corr-other",
            ace_identity="ace:operator-1",
        )

    wrong = _task(conn, assignee="builder")
    with pytest.raises(kb.ControllerEnvelopeError, match="current assignee"):
        kb.opt_in_controller_task(
            conn,
            wrong,
            controller_assignee="ace-controller",
            correlation_id="corr-wrong",
            ace_identity="ace:operator-1",
        )

    other = _task(conn)
    with pytest.raises(kb.ControllerEnvelopeError, match="already bound"):
        _opt_in(conn, other)

    terminal = _task(conn)
    assert kb.complete_task(conn, terminal, summary="already done") is True
    with pytest.raises(kb.ControllerEnvelopeError, match="terminal task"):
        kb.opt_in_controller_task(
            conn,
            terminal,
            controller_assignee="ace-controller",
            correlation_id="corr-terminal",
            ace_identity="ace:operator-1",
        )


def test_valid_flow_projects_structured_milestones_and_gates_terminal_actions(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    assert kb.complete_task(conn, task_id, summary="too early") is False
    assert kb.request_review(conn, task_id, summary="too early") is False

    _record(conn, task_id, "OUTBOUND", 1, payload={"route": "Vector"})
    _record(
        conn,
        task_id,
        "TRANSITION",
        2,
        ace_receipt={"receipt_id": "ace-r-1"},
    )
    _record(
        conn,
        task_id,
        "ESCALATE",
        3,
        payload={"reason": "controller needs terminal response"},
    )
    with kb.write_txn(conn):
        conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at) "
            "VALUES (?, 'controller', 'RETURN: fake proof', 1)",
            (task_id,),
        )
        conn.execute(
            "INSERT INTO task_events (task_id, kind, payload, created_at) "
            "VALUES (?, 'controller_envelope', ?, 1)",
            (task_id, json.dumps({"event_type": "RETURN"})),
        )
    assert kb.complete_task(conn, task_id, summary="escalate is not terminal") is False
    assert kb.request_review(conn, task_id, summary="escalate is not terminal") is False

    _record(
        conn,
        task_id,
        "RETURN",
        4,
        terminal_receipt={"receipt_id": "terminal-r-1"},
        vector_ack={"ack_id": "vector-a-1"},
    )
    projection = kb.controller_status_projection(conn, task_id)
    assert projection is not None and projection["terminal"] is True
    assert [stage["status"] for stage in projection["status_projection"]] == [
        "SENT",
        "ACE ACCEPTED",
        "RESPONSE RECEIVED",
        "VECTOR ACKNOWLEDGED",
    ]
    assert all(stage["reached"] for stage in projection["status_projection"])
    assert projection["status_projection"][1]["evidence"] == {
        "receipt_id": "ace-r-1"
    }
    assert projection["status_projection"][2]["evidence"] == {
        "receipt_id": "terminal-r-1"
    }
    assert projection["status_projection"][3]["evidence"] == {
        "ack_id": "vector-a-1"
    }
    retry = _record(
        conn,
        task_id,
        "RETURN",
        4,
        terminal_receipt={"receipt_id": "terminal-r-1"},
        vector_ack={"ack_id": "vector-a-1"},
    )
    assert retry.duplicate is True
    assert kb.complete_task(conn, task_id, summary="terminal evidence present") is True

    review_id = _task(conn)
    _opt_in(conn, review_id, correlation="corr-review")
    _terminal_flow(conn, review_id, correlation="corr-review")
    assert kb.request_review(
        conn,
        review_id,
        summary="wrong reviewer",
        reviewer="reviewer-lane",
    ) is False
    assert kb.get_task(conn, review_id).status == "ready"
    assert kb.request_review(conn, review_id, summary="ready for review") is True
    assert kb.complete_task(conn, review_id, summary="review approved") is True


def test_exact_duplicate_is_idempotent_and_changed_reuse_rejects(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    first = _record(
        conn,
        task_id,
        "OUTBOUND",
        1,
        idempotency_key="idem-exact",
        payload={"attempt": 1},
    )
    duplicate = _record(
        conn,
        task_id,
        "OUTBOUND",
        1,
        idempotency_key="idem-exact",
        payload={"attempt": 1},
    )
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.envelope.id == first.envelope.id
    assert len(kb.list_controller_envelopes(conn, task_id)) == 1
    projected = [
        event for event in kb.list_events(conn, task_id)
        if event.kind == "controller_envelope"
    ]
    assert len(projected) == 1

    with pytest.raises(kb.ControllerEnvelopeError, match="different envelope"):
        _record(
            conn,
            task_id,
            "OUTBOUND",
            1,
            idempotency_key="idem-exact",
            payload={"attempt": 2},
        )


def test_order_receipt_staleness_mismatch_reuse_and_post_terminal_fail_closed(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)

    with pytest.raises(kb.ControllerEnvelopeError, match="out of order"):
        _record(
            conn,
            task_id,
            "TRANSITION",
            1,
            ace_receipt={"receipt_id": "r"},
        )
    _record(conn, task_id, "OUTBOUND", 1)
    with pytest.raises(kb.ControllerEnvelopeError, match="requires ace_receipt"):
        _record(conn, task_id, "TRANSITION", 2)
    with pytest.raises(kb.ControllerEnvelopeError, match="correlation mismatch"):
        _record(
            conn,
            task_id,
            "TRANSITION",
            2,
            correlation="wrong",
            ace_receipt={"receipt_id": "r"},
        )
    with pytest.raises(kb.ControllerEnvelopeError, match="Ace identity mismatch"):
        kb.record_controller_envelope(
            conn,
            task_id,
            event_type="TRANSITION",
            idempotency_key="idem-wrong-ace",
            occurred_at="2026-08-13T12:00:02Z",
            correlation_id="corr-1",
            ace_identity="ace:other",
            ace_receipt={"receipt_id": "r"},
        )
    with pytest.raises(kb.ControllerEnvelopeError, match="stale"):
        _record(
            conn,
            task_id,
            "TRANSITION",
            1,
            ace_receipt={"receipt_id": "r"},
        )
    _record(
        conn,
        task_id,
        "TRANSITION",
        2,
        ace_receipt={"receipt_id": "r"},
    )
    with pytest.raises(kb.ControllerEnvelopeError, match="already recorded"):
        _record(
            conn,
            task_id,
            "TRANSITION",
            3,
            ace_receipt={"receipt_id": "new"},
        )
    with pytest.raises(kb.ControllerEnvelopeError, match="requires terminal_receipt"):
        _record(conn, task_id, "RETURN", 3)
    _record(
        conn,
        task_id,
        "RETURN",
        3,
        terminal_receipt={"receipt_id": "terminal"},
        vector_ack={"ack_id": "vector"},
    )
    with pytest.raises(kb.ControllerEnvelopeError, match="terminal"):
        _record(conn, task_id, "ESCALATE", 4, payload={"late": True})


def test_only_the_v1_event_permutation_is_accepted(conn) -> None:
    """Small exhaustive state-machine check without a property-test dependency."""
    kinds = ("OUTBOUND", "TRANSITION", "RETURN")
    for index, ordering in enumerate(permutations(kinds), start=1):
        task_id = _task(conn)
        correlation = f"corr-permutation-{index}"
        _opt_in(conn, task_id, correlation=correlation)
        accepted = True
        for step, kind in enumerate(ordering, start=1):
            kwargs = {}
            if kind == "TRANSITION":
                kwargs["ace_receipt"] = {"receipt_id": f"receipt-{index}"}
            elif kind == "RETURN":
                kwargs["terminal_receipt"] = {"receipt_id": f"terminal-{index}"}
                kwargs["vector_ack"] = {"ack_id": f"ack-{index}"}
            try:
                _record(
                    conn,
                    task_id,
                    kind,
                    step,
                    correlation=correlation,
                    **kwargs,
                )
            except kb.ControllerEnvelopeError:
                accepted = False
                break
        assert accepted is (ordering == kinds)


def test_init_recreates_controller_schema_for_an_existing_board(conn) -> None:
    db_path = conn.execute("PRAGMA database_list").fetchone()[2]
    conn.execute("DROP TABLE controller_envelopes")
    conn.execute("DROP TABLE controller_bindings")
    conn.commit()
    kb._INITIALIZED_PATHS.discard(str(Path(db_path).resolve()))

    kb.init_db()
    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    assert {"controller_bindings", "controller_envelopes"} <= tables


def test_assignee_drift_fails_closed_but_legacy_cards_are_unchanged(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    _terminal_flow(conn, task_id)
    assert kb.assign_task(conn, task_id, "other-controller") is True
    assert kb.complete_task(conn, task_id, summary="wrong controller") is False
    assert kb.request_review(conn, task_id, summary="wrong controller") is False

    exact_retry = _record(
        conn,
        task_id,
        "RETURN",
        3,
        terminal_receipt={"receipt_id": "terminal-r-1"},
        vector_ack={"ack_id": "vector-a-1"},
    )
    assert exact_retry.duplicate is True
    with pytest.raises(kb.ControllerEnvelopeError, match="no longer matches"):
        kb.record_controller_envelope(
            conn,
            task_id,
            event_type="RETURN",
            idempotency_key="idem-late-duplicate",
            occurred_at="2026-08-13T12:00:03Z",
            correlation_id="corr-1",
            ace_identity="ace:operator-1",
            terminal_receipt={"receipt_id": "terminal-r-1"},
            vector_ack={"ack_id": "vector-a-1"},
        )

    legacy_done = _task(conn, assignee="builder")
    assert kb.complete_task(conn, legacy_done, summary="legacy complete") is True
    legacy_review = _task(conn, assignee="builder")
    assert kb.request_review(conn, legacy_review, summary="legacy review") is True


def test_receipts_payload_and_projected_event_are_redacted(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    secret = "ghp_" + "A" * 40
    _record(conn, task_id, "OUTBOUND", 1, payload={"token": secret})
    envelope = kb.list_controller_envelopes(conn, task_id)[0]
    assert secret not in json.dumps(envelope.payload)
    projected = [
        event for event in kb.list_events(conn, task_id)
        if event.kind == "controller_envelope"
    ][0]
    assert secret not in json.dumps(projected.payload)


def test_cli_ingestion_round_trip_uses_real_sqlite(conn) -> None:
    task_id = _task(conn)
    opt = cli.run_slash(
        "controller-opt-in "
        f"{task_id} --controller-assignee ace-controller "
        "--correlation-id corr-cli --ace-identity ace:cli"
    )
    assert "ace.controller.v1" in opt
    outbound = cli.run_slash(
        "controller-event "
        f"{task_id} OUTBOUND --idempotency-key cli-1 "
        "--occurred-at 2026-08-13T12:00:01Z "
        "--correlation-id corr-cli --ace-identity ace:cli "
        f"--payload {shlex.quote(json.dumps({'source': 'cli'}))}"
    )
    assert "OUTBOUND recorded" in outbound
    shown = json.loads(cli.run_slash(f"show {task_id} --json"))
    assert shown["controller"]["status_projection"][0]["status"] == "SENT"
    assert shown["controller"]["status_projection"][0]["reached"] is True
