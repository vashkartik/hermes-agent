"""Behavior contract for opt-in ACE controller envelopes v1."""

from __future__ import annotations

import json
import shlex
from dataclasses import asdict
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
    if assignee == "ace-controller":
        # Manual-opt-in fixtures deliberately predate controller-profile
        # provisioning, matching an upgraded board. Avoid the automatic
        # authorized-assignee path until _opt_in provisions the capability.
        task_id = kb.create_task(conn, title="controller card")
        conn.execute("UPDATE tasks SET assignee = ? WHERE id = ?", (assignee, task_id))
        conn.commit()
        return task_id
    return kb.create_task(conn, title="controller card", assignee=assignee)


def _authorize_controller(profile: str = "ace-controller") -> Path:
    skill = (
        Path.home()
        / ".hermes"
        / "profiles"
        / profile
        / "skills"
        / "orchestration"
        / "vector-controller"
        / "SKILL.md"
    )
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text("---\nname: vector-controller\n---\n", encoding="utf-8")
    return skill


def _opt_in(conn, task_id: str, *, correlation="corr-1") -> dict:
    _authorize_controller()
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


def _subscribe_receiver(conn, task_id: str) -> None:
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform="tui",
        chat_id="vector-session",
        notifier_profile="vector",
    )


def _ack_return(conn, task_id: str) -> kb.ControllerEnvelopeResult:
    kb.claim_unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="tui",
        chat_id="vector-session",
        kinds=["controller_envelope"],
    )
    return_event = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? "
        "AND kind = 'controller_envelope' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert return_event is not None
    return kb.acknowledge_controller_return(
        conn,
        task_id,
        return_event_id=return_event["id"],
        platform="tui",
        chat_id="vector-session",
    )


def _terminal_flow(conn, task_id: str, *, correlation="corr-1") -> None:
    _subscribe_receiver(conn, task_id)
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
    )
    _ack_return(conn, task_id)


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

    terminal = _task(conn, assignee="builder")
    assert kb.complete_task(conn, terminal, summary="already done") is True
    conn.execute(
        "UPDATE tasks SET assignee = 'ace-controller' WHERE id = ?", (terminal,)
    )
    conn.commit()
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
    _subscribe_receiver(conn, task_id)
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
    )
    projection = kb.controller_status_projection(conn, task_id)
    assert projection is not None and projection["terminal"] is False
    assert [stage["status"] for stage in projection["status_projection"]] == [
        "SENT",
        "ACE ACCEPTED",
        "RESPONSE RECEIVED",
        "VECTOR ACKNOWLEDGED",
    ]
    assert [stage["reached"] for stage in projection["status_projection"]] == [
        True, True, True, False,
    ]
    assert projection["status_projection"][1]["evidence"] == {
        "receipt_id": "ace-r-1"
    }
    assert projection["status_projection"][2]["evidence"] == {
        "receipt_id": "terminal-r-1"
    }
    assert projection["status_projection"][3]["evidence"] is None
    assert kb.complete_task(conn, task_id, summary="delivery not acknowledged") is False
    with pytest.raises(kb.ControllerEnvelopeError, match="receiver-owned"):
        kb.record_controller_envelope(
            conn,
            task_id,
            event_type="RETURN",
            idempotency_key="sender-self-ack",
            occurred_at="2026-08-13T12:00:04Z",
            correlation_id="corr-1",
            ace_identity="ace:operator-1",
            terminal_receipt={"receipt_id": "terminal-r-1"},
            vector_ack={"ack_id": "forged"},
        )
    retry = _record(
        conn,
        task_id,
        "RETURN",
        4,
        terminal_receipt={"receipt_id": "terminal-r-1"},
    )
    assert retry.duplicate is True
    ack = _ack_return(conn, task_id)
    assert ack.duplicate is False
    assert ack.envelope.vector_ack["receiver"] == {
        "platform": "tui",
        "notifier_profile": "vector",
    }
    assert _ack_return(conn, task_id).duplicate is True
    projection = kb.controller_status_projection(conn, task_id)
    assert projection is not None and projection["terminal"] is True
    assert all(stage["reached"] for stage in projection["status_projection"])
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


def test_controller_human_block_requires_typed_escalation(conn) -> None:
    missing = _task(conn)
    _opt_in(conn, missing, correlation="corr-missing-escalate")
    _record(conn, missing, "OUTBOUND", 1, correlation="corr-missing-escalate")
    _record(
        conn,
        missing,
        "TRANSITION",
        2,
        correlation="corr-missing-escalate",
        ace_receipt={"receipt_id": "accepted"},
    )

    with pytest.raises(kb.ControllerEnvelopeError, match="typed ESCALATE"):
        kb.block_task(
            conn,
            missing,
            reason="pretty comment is not a receipt",
            kind="needs_input",
        )
    missing_task = kb.get_task(conn, missing)
    assert missing_task is not None
    assert missing_task.status == "ready"

    escalated = _task(conn)
    _opt_in(conn, escalated, correlation="corr-escalated")
    _record(conn, escalated, "OUTBOUND", 1, correlation="corr-escalated")
    _record(
        conn,
        escalated,
        "TRANSITION",
        2,
        correlation="corr-escalated",
        ace_receipt={"receipt_id": "accepted"},
    )
    _record(
        conn,
        escalated,
        "ESCALATE",
        3,
        correlation="corr-escalated",
        payload={"reason": "owner decision required"},
    )
    assert kb.block_task(
        conn,
        escalated,
        reason="owner decision required",
        kind="needs_input",
    ) is True
    escalated_task = kb.get_task(conn, escalated)
    assert escalated_task is not None
    assert escalated_task.status == "blocked"
    assert kb.unblock_task(conn, escalated) is True
    with pytest.raises(kb.ControllerEnvelopeError, match="fresh typed ESCALATE"):
        kb.block_task(
            conn,
            escalated,
            reason="same stale escalation must not authorize another block",
            kind="needs_input",
        )

    legacy = _task(conn, assignee="builder")
    assert kb.block_task(
        conn,
        legacy,
        reason="legacy behavior remains unchanged",
        kind="needs_input",
    ) is True


def test_acknowledged_controller_return_can_recover_historical_triage(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id, correlation="corr-triage-recovery")
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform="tui",
        chat_id="receiver-session",
    )
    _record(conn, task_id, "OUTBOUND", 1, correlation="corr-triage-recovery")
    _record(
        conn,
        task_id,
        "TRANSITION",
        2,
        correlation="corr-triage-recovery",
        ace_receipt={"receipt_id": "accepted"},
    )
    _record(
        conn,
        task_id,
        "ESCALATE",
        3,
        correlation="corr-triage-recovery",
        payload={"reason": "owner decision required"},
    )
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status = 'triage' WHERE id = ?", (task_id,))
    _record(
        conn,
        task_id,
        "RETURN",
        4,
        correlation="corr-triage-recovery",
        terminal_receipt={"state": "request-changes"},
    )
    return_event_id = [
        ev.id
        for ev in kb.list_events(conn, task_id)
        if ev.kind == "controller_envelope"
        and (ev.payload or {}).get("event_type") == "RETURN"
    ][-1]
    _, _, claimed = kb.claim_unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="tui",
        chat_id="receiver-session",
        kinds=("controller_envelope",),
    )
    assert return_event_id in {ev.id for ev in claimed}
    kb.acknowledge_controller_return(
        conn,
        task_id,
        return_event_id=return_event_id,
        platform="tui",
        chat_id="receiver-session",
    )

    assert kb.complete_task(
        conn,
        task_id,
        summary="Recovered acknowledged controller return",
    ) is True
    recovered = kb.get_task(conn, task_id)
    assert recovered is not None
    assert recovered.status == "done"


def test_acknowledgment_requires_exact_subscription_and_claimed_return(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    kb.add_notify_sub(
        conn,
        task_id=task_id,
        platform="tui",
        chat_id="unclaimed-receiver",
        notifier_profile="vector",
    )
    _record(conn, task_id, "OUTBOUND", 1)
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
        "RETURN",
        3,
        terminal_receipt={"receipt_id": "terminal-r-1"},
    )
    return_event = conn.execute(
        "SELECT id FROM task_events WHERE task_id = ? "
        "AND kind = 'controller_envelope' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    assert return_event is not None

    with pytest.raises(kb.ControllerEnvelopeError, match="exact subscribed receiver"):
        kb.acknowledge_controller_return(
            conn,
            task_id,
            return_event_id=return_event["id"],
            platform="tui",
            chat_id="not-subscribed",
        )
    with pytest.raises(kb.ControllerEnvelopeError, match="has not claimed"):
        kb.acknowledge_controller_return(
            conn,
            task_id,
            return_event_id=return_event["id"],
            platform="tui",
            chat_id="unclaimed-receiver",
        )

    kb.claim_unseen_events_for_sub(
        conn,
        task_id=task_id,
        platform="tui",
        chat_id="unclaimed-receiver",
        kinds=["controller_envelope"],
    )
    acknowledged = kb.acknowledge_controller_return(
        conn,
        task_id,
        return_event_id=return_event["id"],
        platform="tui",
        chat_id="unclaimed-receiver",
    )
    assert acknowledged.envelope.vector_ack is not None


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


@pytest.mark.parametrize("carrier", ["title", "body", "metadata", "assignee"])
def test_documented_opt_in_carriers_bind_only_authorized_controller_profiles(
    conn, carrier,
) -> None:
    _authorize_controller("vector-controller")
    kwargs = {
        "title": "ordinary controller task",
        "assignee": "vector-controller",
    }
    if carrier == "title":
        kwargs["title"] = "run ace.controller.v1 lifecycle"
    elif carrier == "body":
        kwargs["body"] = "Protocol: ace.controller.v1"
    elif carrier == "metadata":
        kwargs["metadata"] = {
            "controller": {
                "protocol": "ace.controller.v1",
                "correlation_id": "corr-metadata",
                "ace_identity": "ace:metadata",
            },
            "unrelated": "preserved",
        }

    task_id = kb.create_task(conn, **kwargs)
    projection = kb.controller_status_projection(conn, task_id)
    assert projection is not None
    assert projection["opt_in_source"] == (
        "authorized_assignee" if carrier == "assignee" else carrier
    )
    if carrier == "metadata":
        assert projection["correlation_id"] == "corr-metadata"
        assert projection["ace_identity"] == "ace:metadata"
        assert kb.get_task(conn, task_id).metadata["unrelated"] == "preserved"


def test_v1_migration_backfills_existing_qualified_rows_and_legacy_stays_legacy(
    conn,
) -> None:
    legacy = kb.create_task(conn, title="ordinary", assignee="builder")
    migrating = kb.create_task(conn, title="old card", assignee="builder")
    malformed = kb.create_task(conn, title="old malformed", assignee="builder")
    conn.execute(
        "UPDATE tasks SET title = ?, assignee = ? WHERE id = ?",
        ("old ace.controller.v1 card", "vector-controller", migrating),
    )
    conn.execute(
        "UPDATE tasks SET metadata = ? WHERE id = ?",
        (json.dumps({"controller": {"protocol": "wrong"}}), malformed),
    )
    conn.commit()
    _authorize_controller("vector-controller")
    conn.execute(
        "DELETE FROM kanban_migrations WHERE name = 'controller_auto_opt_in_v1'"
    )
    conn.commit()

    db_path = Path(conn.execute("PRAGMA database_list").fetchone()[2])
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))
    kb.init_db(db_path)

    assert kb.controller_status_projection(conn, migrating)["opt_in_source"] == "title"
    assert kb.controller_status_projection(conn, legacy) is None
    assert kb.complete_task(conn, legacy, summary="legacy unchanged") is True
    assert kb.complete_task(conn, malformed, summary="must fail closed") is False
    assert kb.request_review(conn, malformed, summary="must fail closed") is False


def test_first_envelope_lazily_binds_a_post_migration_content_opt_in(conn) -> None:
    task_id = kb.create_task(conn, title="old card", assignee="builder")
    conn.execute(
        "UPDATE tasks SET title = ?, assignee = ? WHERE id = ?",
        ("old ace.controller.v1 card", "vector-controller", task_id),
    )
    conn.commit()
    _authorize_controller("vector-controller")
    assert kb.controller_status_projection(conn, task_id) is None

    result = kb.record_controller_envelope(
        conn,
        task_id,
        event_type="OUTBOUND",
        idempotency_key="lazy-outbound",
        occurred_at="2026-08-13T12:00:01Z",
        correlation_id=f"kanban:{task_id}",
        ace_identity=f"ace:pending:{task_id}",
    )

    assert result.duplicate is False
    projection = kb.controller_status_projection(conn, task_id)
    assert projection["opt_in_source"] == "title"
    assert projection["status_projection"][0]["status"] == "SENT"
    assert projection["status_projection"][0]["reached"] is True


def test_explicit_v1_marker_rejects_unauthorized_assignee_without_partial_task(conn) -> None:
    before = {task.id for task in kb.list_tasks(conn, include_archived=True)}
    with pytest.raises(kb.ControllerEnvelopeError, match="authorized controller"):
        kb.create_task(
            conn,
            title="ace.controller.v1 control card",
            assignee="ordinary-worker",
        )
    assert {task.id for task in kb.list_tasks(conn, include_archived=True)} == before


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
        )

    legacy_done = _task(conn, assignee="builder")
    assert kb.complete_task(conn, legacy_done, summary="legacy complete") is True
    legacy_review = _task(conn, assignee="builder")
    assert kb.request_review(conn, legacy_review, summary="legacy review") is True


def test_controller_capability_removal_rejects_new_events_but_preserves_exact_retry(
    conn,
) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    outbound = _record(conn, task_id, "OUTBOUND", 1)
    _authorize_controller().unlink()

    assert _record(conn, task_id, "OUTBOUND", 1).duplicate is True
    assert outbound.duplicate is False
    with pytest.raises(kb.ControllerEnvelopeError, match="no longer an authorized"):
        _record(
            conn,
            task_id,
            "TRANSITION",
            2,
            ace_receipt={"receipt_id": "ace-r-1"},
        )
    assert kb.complete_task(conn, task_id, summary="not authorized") is False
    assert kb.request_review(conn, task_id, summary="not authorized") is False


def test_receipts_payload_and_projected_event_are_redacted(conn) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    secret = "ghp_" + "A" * 40
    secret_key = "ghp_" + "B" * 40
    nested = {"items": [{secret_key: {"token": secret}}]}
    _record(conn, task_id, "OUTBOUND", 1, payload=nested)
    _record(conn, task_id, "TRANSITION", 2, ace_receipt=nested)
    _record(
        conn,
        task_id,
        "RETURN",
        3,
        terminal_receipt=nested,
    )

    durable = json.dumps(
        [asdict(envelope) for envelope in kb.list_controller_envelopes(conn, task_id)]
    )
    projected = json.dumps([
        asdict(event)
        for event in kb.list_events(conn, task_id)
        if event.kind == "controller_envelope"
    ])
    projection = json.dumps(kb.controller_status_projection(conn, task_id))
    for serialized in (durable, projected, projection):
        assert secret not in serialized
        assert secret_key not in serialized


@pytest.mark.parametrize(
    "invalid_payload",
    [
        {"nested": [float("nan")]},
        {"nested": {"value": float("inf")}},
        {float("-inf"): "non-finite key"},
    ],
    ids=("nan-value", "infinite-value", "infinite-key"),
)
def test_non_finite_controller_json_rejects_without_durable_side_effects(
    conn, invalid_payload,
) -> None:
    task_id = _task(conn)
    _opt_in(conn, task_id)
    events_before = kb.list_events(conn, task_id)

    with pytest.raises(kb.ControllerEnvelopeError, match="JSON serializable"):
        _record(conn, task_id, "OUTBOUND", 1, payload=invalid_payload)

    assert kb.list_controller_envelopes(conn, task_id) == []
    assert kb.list_events(conn, task_id) == events_before


def test_cli_ingestion_round_trip_uses_real_sqlite(conn) -> None:
    task_id = _task(conn)
    _authorize_controller()
    opt = cli.run_slash(
        "controller-opt-in "
        f"{task_id} --controller-assignee ace-controller "
        f"--correlation-id kanban:{task_id} --ace-identity ace:pending:{task_id}"
    )
    assert "ace.controller.v1" in opt
    outbound = cli.run_slash(
        "controller-event "
        f"{task_id} OUTBOUND --idempotency-key cli-1 "
        "--occurred-at 2026-08-13T12:00:01Z "
        f"--correlation-id kanban:{task_id} --ace-identity ace:pending:{task_id} "
        f"--payload {shlex.quote(json.dumps({'source': 'cli'}))}"
    )
    assert "OUTBOUND recorded" in outbound
    shown = json.loads(cli.run_slash(f"show {task_id} --json"))
    assert shown["controller"]["status_projection"][0]["status"] == "SENT"
    assert shown["controller"]["status_projection"][0]["reached"] is True
