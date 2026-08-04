"""Behavioral coverage for compacted-transcript visibility.

In-place compression (``SessionDB.archive_and_compact``) soft-archives the
pre-compaction turns as ``active=0, compacted=1``. The contract this file
pins down:

- the model-fed history (default projections) drops the archived rows and
  reloads only the compacted set;
- the user-visible display projections (``include_ancestors=True``,
  ``include_compacted=True``, ``get_ancestor_display_prefix``) keep the
  archived rows visible;
- rewound rows (``active=0, compacted=0``) stay hidden from the display
  projection — "user took it back" is not "summarized away".

These are the exact projections gateway/platforms/api_server.py and
hermes_cli/web_routers/sessions.py serve transcripts from; before this file
the ``active = 1 OR compacted = 1`` display clause had no behavioral test.
"""

import pytest

from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return SessionDB(db_path=tmp_path / "state.db")


def _seed_conversation(db, session_id):
    db.create_session(session_id, source="cli")
    db.append_message(session_id, "user", "first question")
    db.append_message(session_id, "assistant", "first answer")
    db.append_message(session_id, "user", "second question")
    db.append_message(session_id, "assistant", "second answer")


def _contents(messages):
    return [m.get("content") for m in messages]


def test_archive_and_compact_keeps_archives_visible_in_display(db):
    sid = "sess-compact"
    _seed_conversation(db, sid)

    active_count = db.archive_and_compact(
        sid, [{"role": "assistant", "content": "summary of the first two turns"}]
    )
    assert active_count == 1

    # Model-fed history: only the compacted set.
    model_view = db.get_messages_as_conversation(sid)
    assert _contents(model_view) == ["summary of the first two turns"]

    # Display transcript: archived turns stay visible alongside the summary.
    display_view = db.get_messages_as_conversation(sid, include_ancestors=True)
    display_contents = _contents(display_view)
    assert "first question" in display_contents
    assert "second answer" in display_contents
    assert "summary of the first two turns" in display_contents
    assert len(display_view) == len(model_view) + 4


def test_get_messages_include_compacted_flag(db):
    sid = "sess-flag"
    _seed_conversation(db, sid)
    db.archive_and_compact(sid, [{"role": "assistant", "content": "the summary"}])

    default_rows = db.get_messages(sid)
    assert _contents(default_rows) == ["the summary"]

    with_archives = db.get_messages(sid, include_compacted=True)
    contents = _contents(with_archives)
    assert "first question" in contents
    assert "the summary" in contents
    assert len(with_archives) == len(default_rows) + 4


def test_display_prefix_contains_compacted_rows(db):
    sid = "sess-prefix"
    _seed_conversation(db, sid)
    db.archive_and_compact(sid, [{"role": "assistant", "content": "prefix summary"}])

    prefix = db.get_ancestor_display_prefix(sid)
    prefix_contents = _contents(prefix)
    assert "first question" in prefix_contents
    assert "second answer" in prefix_contents
    # The live model history (the summary) is NOT part of the display prefix.
    assert "prefix summary" not in prefix_contents


def test_rewound_rows_stay_hidden_from_display(db):
    sid = "sess-rewind"
    _seed_conversation(db, sid)
    rows = db.get_messages(sid)
    second_user = next(r for r in rows if r.get("content") == "second question")
    # Rewind to the second user turn: it and later rows flip to
    # active=0, compacted=0 ("user took it back").
    db.rewind_to_message(sid, second_user["id"])

    display_view = db.get_messages_as_conversation(sid, include_ancestors=True)
    display_contents = _contents(display_view)
    assert "first question" in display_contents
    assert "first answer" in display_contents
    assert "second question" not in display_contents
    assert "second answer" not in display_contents


def test_archived_rows_survive_and_stay_searchable(db):
    sid = "sess-search"
    _seed_conversation(db, sid)
    db.archive_and_compact(sid, [{"role": "assistant", "content": "condensed"}])

    hits = db.search_messages("second question")
    assert any(h.get("session_id") == sid for h in hits), hits
