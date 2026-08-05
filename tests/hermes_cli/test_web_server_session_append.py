"""Focused tests for ``POST /api/sessions/{session_id}/messages``.

The canonical client is the Ace desktop shell's cron→home-chat delivery
(ace-coder#178): it POSTs ``{role, content, source}`` with the dashboard
session token, treats 404 as "home session gone" (drops its pin), 405 as
"append-unsupported sidecar" (fails closed), and any other failure as
ambiguous — reconciled by re-reading the transcript for the marker embedded
in ``content`` before any second append. These tests pin that wire contract
plus the server-side retry dedupe, profile scoping, and compression-tip
resolution the route adds on top of it.
"""

import sqlite3

import pytest


def _state_db_path():
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "state.db"


def _open_db(db_path=None):
    from hermes_state import SessionDB

    if db_path is None:
        db_path = _state_db_path()
    return SessionDB(db_path=db_path)


def _seed_session(session_id, *, db_path=None, source="cli", messages=("hello",)):
    db = _open_db(db_path)
    try:
        db.create_session(session_id=session_id, source=source)
        for content in messages:
            db.append_message(session_id=session_id, role="user", content=content)
    finally:
        db.close()


# The exact body shape ace-coder#178's delivery transport sends: content
# carries the run output plus a per-(job, run) marker line the client later
# greps the transcript for when an ambiguous failure forces reconciliation.
_ACE_MARKER = "[ace-cron-delivery job-nightly/run-1]"
_ACE_BODY = {
    "role": "user",
    "content": f"Cron result: Nightly brief\n\nAll clear.\n\n{_ACE_MARKER}",
    "source": "ace-cron-home-delivery",
}


class TestSessionMessageAppend:
    @pytest.fixture(autouse=True)
    def _setup_test_client(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_cli.web_server import _SESSION_HEADER_NAME, _SESSION_TOKEN, app

        monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", _state_db_path())

        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def _visible_messages(self, session_id, **params):
        resp = self.client.get(f"/api/sessions/{session_id}/messages", params=params)
        assert resp.status_code == 200
        return resp.json()["messages"]

    # -- auth ---------------------------------------------------------------

    def test_post_without_token_is_401(self):
        """The append route sits behind the same session-token gate as every
        other /api route — an unauthenticated POST must never write."""
        from starlette.testclient import TestClient

        from hermes_cli.web_server import app

        _seed_session("append-auth")
        bare = TestClient(app)
        resp = bare.post("/api/sessions/append-auth/messages", json=dict(_ACE_BODY))
        assert resp.status_code == 401
        assert len(self._visible_messages("append-auth")) == 1

    # -- the Ace wire contract ---------------------------------------------

    def test_ace_shaped_append_lands_and_is_visible(self):
        _seed_session("append-ace")
        resp = self.client.post(
            "/api/sessions/append-ace/messages", json=dict(_ACE_BODY)
        )
        assert resp.status_code == 200
        payload = resp.json()
        assert payload["ok"] is True
        assert payload["session_id"] == "append-ace"
        assert payload["deduplicated"] is False
        assert isinstance(payload["message_id"], int)

        messages = self._visible_messages("append-ace")
        assert len(messages) == 2
        appended = messages[-1]
        assert appended["role"] == "user"
        # The marker embedded in content is the client's reconciliation
        # arbiter — it must round-trip verbatim through GET.
        assert _ACE_MARKER in appended["content"]
        assert appended["display_metadata"] == {"source": "ace-cron-home-delivery"}

    def test_identical_retry_is_deduplicated(self):
        """POSTing the same body twice yields ONE visible message: the retry
        answers with the original row (``deduplicated: true``)."""
        _seed_session("append-retry")
        first = self.client.post(
            "/api/sessions/append-retry/messages", json=dict(_ACE_BODY)
        )
        second = self.client.post(
            "/api/sessions/append-retry/messages", json=dict(_ACE_BODY)
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["deduplicated"] is True
        assert second.json()["message_id"] == first.json()["message_id"]
        contents = [m["content"] for m in self._visible_messages("append-retry")]
        assert contents.count(_ACE_BODY["content"]) == 1

    def test_distinct_content_appends_normally(self):
        _seed_session("append-distinct")
        body2 = dict(_ACE_BODY, content="Cron result: other run\n\n[marker run-2]")
        r1 = self.client.post("/api/sessions/append-distinct/messages", json=dict(_ACE_BODY))
        r2 = self.client.post("/api/sessions/append-distinct/messages", json=body2)
        assert r1.json()["deduplicated"] is False
        assert r2.json()["deduplicated"] is False
        assert len(self._visible_messages("append-distinct")) == 3

    def test_source_mismatch_defeats_keyless_dedupe(self):
        """Identical text from a DIFFERENT writer is a new message, not a
        retry — the provenance tag is part of the dedupe identity."""
        _seed_session("append-src")
        self.client.post("/api/sessions/append-src/messages", json=dict(_ACE_BODY))
        other = dict(_ACE_BODY, source="other-writer")
        resp = self.client.post("/api/sessions/append-src/messages", json=other)
        assert resp.json()["deduplicated"] is False
        assert len(self._visible_messages("append-src")) == 3

    def test_retry_outside_window_appends(self):
        """The keyless dedupe is a retry guard, not a permanent content
        uniqueness constraint: after the window a re-send is a new message."""
        _seed_session("append-window")
        self.client.post("/api/sessions/append-window/messages", json=dict(_ACE_BODY))
        # Age the appended row past the 15-minute window.
        conn = sqlite3.connect(_state_db_path())
        try:
            conn.execute(
                "UPDATE messages SET timestamp = timestamp - 3600 "
                "WHERE session_id = 'append-window'"
            )
            conn.commit()
        finally:
            conn.close()
        resp = self.client.post(
            "/api/sessions/append-window/messages", json=dict(_ACE_BODY)
        )
        assert resp.json()["deduplicated"] is False
        assert len(self._visible_messages("append-window")) == 3

    def test_rewound_twin_does_not_dedupe(self):
        """A retry whose twin row was rewound (active=0, invisible) must
        append fresh — pointing at a hidden row would leave the transcript
        without the message the client is ensuring exists."""
        _seed_session("append-rewound")
        self.client.post("/api/sessions/append-rewound/messages", json=dict(_ACE_BODY))
        conn = sqlite3.connect(_state_db_path())
        try:
            conn.execute(
                "UPDATE messages SET active = 0 "
                "WHERE session_id = 'append-rewound' AND content LIKE '%ace-cron%'"
            )
            conn.commit()
        finally:
            conn.close()
        resp = self.client.post(
            "/api/sessions/append-rewound/messages", json=dict(_ACE_BODY)
        )
        assert resp.json()["deduplicated"] is False
        contents = [m["content"] for m in self._visible_messages("append-rewound")]
        assert contents.count(_ACE_BODY["content"]) == 1

    def test_retry_dedupes_past_trailing_invisible_row(self):
        """The dedupe target is the last VISIBLE row: an unrelated rewound
        (active=0) row inserted after the delivery must not blind the retry
        into appending a duplicate."""
        _seed_session("append-trail")
        self.client.post("/api/sessions/append-trail/messages", json=dict(_ACE_BODY))
        # An unrelated message lands after the delivery and is then rewound.
        db = _open_db()
        try:
            db.append_message(
                session_id="append-trail", role="user", content="unrelated, rewound"
            )
        finally:
            db.close()
        conn = sqlite3.connect(_state_db_path())
        try:
            conn.execute(
                "UPDATE messages SET active = 0 "
                "WHERE session_id = 'append-trail' AND content = 'unrelated, rewound'"
            )
            conn.commit()
        finally:
            conn.close()
        resp = self.client.post(
            "/api/sessions/append-trail/messages", json=dict(_ACE_BODY)
        )
        assert resp.json()["deduplicated"] is True
        contents = [m["content"] for m in self._visible_messages("append-trail")]
        assert contents.count(_ACE_BODY["content"]) == 1

    def test_archived_guard_holds_inside_write_txn(self):
        """The route's archived pre-check has a transactional twin: an archive
        landing between the pre-check and the INSERT is caught inside the
        write txn (SessionArchivedError → 409), while default writers (live
        agent flushes) deliberately keep appending to archived sessions."""
        from hermes_state import SessionArchivedError

        db = _open_db()
        try:
            db.create_session(session_id="append-txn-arch", source="cli")
            db.set_session_archived("append-txn-arch", True)
            with pytest.raises(SessionArchivedError):
                db.append_message(
                    session_id="append-txn-arch",
                    role="user",
                    content="raced append",
                    reject_archived=True,
                )
            # Opt-out (the default) preserves existing writer behavior.
            db.append_message(
                session_id="append-txn-arch", role="user", content="agent flush"
            )
            assert [m["content"] for m in db.get_messages("append-txn-arch")] == [
                "agent flush"
            ]
        finally:
            db.close()

    # -- dedupe_key idempotency ----------------------------------------------

    def test_dedupe_key_is_idempotent_even_with_changed_content(self):
        _seed_session("append-keyed")
        first = self.client.post(
            "/api/sessions/append-keyed/messages",
            json={"role": "user", "content": "keyed body", "dedupe_key": "job/run-1"},
        )
        retry = self.client.post(
            "/api/sessions/append-keyed/messages",
            json={"role": "user", "content": "keyed body CHANGED", "dedupe_key": "job/run-1"},
        )
        assert retry.json()["deduplicated"] is True
        assert retry.json()["message_id"] == first.json()["message_id"]
        assert len(self._visible_messages("append-keyed")) == 2

    def test_dedupe_key_is_scoped_per_session(self):
        _seed_session("append-key-a")
        _seed_session("append-key-b")
        body = {"role": "user", "content": "shared", "dedupe_key": "job/run-9"}
        ra = self.client.post("/api/sessions/append-key-a/messages", json=body)
        rb = self.client.post("/api/sessions/append-key-b/messages", json=body)
        assert ra.json()["deduplicated"] is False
        assert rb.json()["deduplicated"] is False

    # -- session resolution ---------------------------------------------------

    def test_unknown_session_is_404(self):
        resp = self.client.post(
            "/api/sessions/no-such-session/messages", json=dict(_ACE_BODY)
        )
        assert resp.status_code == 404

    def test_ambiguous_prefix_is_404(self):
        _seed_session("ambig-prefix-one")
        _seed_session("ambig-prefix-two")
        resp = self.client.post(
            "/api/sessions/ambig-prefix/messages", json=dict(_ACE_BODY)
        )
        assert resp.status_code == 404

    def test_unique_prefix_resolves_like_get(self):
        _seed_session("prefix-resolve-xyz")
        resp = self.client.post(
            "/api/sessions/prefix-resolve/messages", json=dict(_ACE_BODY)
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "prefix-resolve-xyz"

    def test_archived_session_is_409(self):
        _seed_session("append-archived")
        assert (
            self.client.patch(
                "/api/sessions/append-archived", json={"archived": True}
            ).status_code
            == 200
        )
        resp = self.client.post(
            "/api/sessions/append-archived/messages", json=dict(_ACE_BODY)
        )
        assert resp.status_code == 409
        assert len(self._visible_messages("append-archived")) == 1

    # -- compression chain ----------------------------------------------------

    def test_append_follows_compression_tip(self):
        """A POST addressed to a compressed-away parent lands on the live
        continuation — the same tip GET resolves — so the message is visible
        in the conversation the desktop actually reopens."""
        db = _open_db()
        try:
            db.create_session(session_id="compress-parent", source="cli")
            db.append_message(
                session_id="compress-parent", role="user", content="old turn"
            )
            db.end_session("compress-parent", end_reason="compression")
            db.create_session(
                session_id="compress-child",
                source="cli",
                parent_session_id="compress-parent",
            )
            db.append_message(
                session_id="compress-child", role="user", content="continuation turn"
            )
        finally:
            db.close()

        resp = self.client.post(
            "/api/sessions/compress-parent/messages", json=dict(_ACE_BODY)
        )
        assert resp.status_code == 200
        assert resp.json()["session_id"] == "compress-child"

        # GET through the parent id resolves the same tip and shows the
        # appended message on top of the intact continuation history.
        messages = self._visible_messages("compress-parent")
        contents = [m["content"] for m in messages]
        assert contents[-1] == _ACE_BODY["content"]
        assert "continuation turn" in contents

    def test_compression_closed_session_without_child_is_409(self):
        """A compression-ended session with no continuation yet: the write
        guard refuses (no write happened) and the client retries later."""
        db = _open_db()
        try:
            db.create_session(session_id="compress-stub", source="cli")
            db.append_message(
                session_id="compress-stub", role="user", content="pre-compression"
            )
            db.end_session("compress-stub", end_reason="compression")
        finally:
            db.close()
        resp = self.client.post(
            "/api/sessions/compress-stub/messages", json=dict(_ACE_BODY)
        )
        assert resp.status_code == 409
        assert len(self._visible_messages("compress-stub")) == 1

    def test_compression_in_progress_is_503(self):
        _seed_session("compress-locked")
        db = _open_db()
        try:
            assert db.try_acquire_compression_lock(
                "compress-locked", holder="another-writer"
            )
            resp = self.client.post(
                "/api/sessions/compress-locked/messages", json=dict(_ACE_BODY)
            )
            assert resp.status_code == 503
            db.release_compression_lock("compress-locked", "another-writer")
        finally:
            db.close()
        assert len(self._visible_messages("compress-locked")) == 1

    # -- validation -----------------------------------------------------------

    @pytest.mark.parametrize("role", ["tool", "system", "operator", ""])
    def test_disallowed_roles_are_400(self, role):
        _seed_session("append-roles")
        resp = self.client.post(
            "/api/sessions/append-roles/messages",
            json={"role": role, "content": "x"},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("content", ["", "   \n\t "])
    def test_blank_content_is_400(self, content):
        _seed_session("append-blank")
        resp = self.client.post(
            "/api/sessions/append-blank/messages",
            json={"role": "user", "content": content},
        )
        assert resp.status_code == 400

    def test_oversized_content_is_400(self):
        _seed_session("append-huge")
        resp = self.client.post(
            "/api/sessions/append-huge/messages",
            json={"role": "user", "content": "x" * 1_000_001},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("source", ["", "-leading-dash", "has space", "x" * 121])
    def test_invalid_source_is_400(self, source):
        _seed_session("append-badsrc")
        resp = self.client.post(
            "/api/sessions/append-badsrc/messages",
            json={"role": "user", "content": "x", "source": source},
        )
        assert resp.status_code == 400

    @pytest.mark.parametrize("key", ["", "   ", "k" * 201])
    def test_invalid_dedupe_key_is_400(self, key):
        _seed_session("append-badkey")
        resp = self.client.post(
            "/api/sessions/append-badkey/messages",
            json={"role": "user", "content": "x", "dedupe_key": key},
        )
        assert resp.status_code == 400

    def test_missing_fields_are_422(self):
        _seed_session("append-shape")
        assert (
            self.client.post(
                "/api/sessions/append-shape/messages", json={"content": "x"}
            ).status_code
            == 422
        )
        assert (
            self.client.post(
                "/api/sessions/append-shape/messages", json={"role": "user"}
            ).status_code
            == 422
        )

    # -- profile scoping -------------------------------------------------------

    def test_unknown_profile_is_404_and_writes_nothing(self):
        _seed_session("append-prof-miss")
        resp = self.client.post(
            "/api/sessions/append-prof-miss/messages",
            json=dict(_ACE_BODY, profile="no-such-profile"),
        )
        assert resp.status_code == 404
        assert "profile" in resp.json()["detail"].lower()
        assert len(self._visible_messages("append-prof-miss")) == 1

    def test_conflicting_profile_query_and_body_is_400(self):
        _seed_session("append-prof-conflict")
        resp = self.client.post(
            "/api/sessions/append-prof-conflict/messages?profile=alpha",
            json=dict(_ACE_BODY, profile="beta"),
        )
        assert resp.status_code == 400

    def test_profile_scoped_append_writes_only_that_store(self):
        """A ``profile``-addressed POST opens THAT profile's state.db; the
        default store — even one holding a same-id session — is untouched."""
        from hermes_cli import profiles as profiles_mod

        profiles_mod.create_profile("scout", no_alias=True, no_skills=True)
        scout_db_path = profiles_mod.get_profile_dir("scout") / "state.db"
        _seed_session("shared-id", db_path=scout_db_path, messages=("scout turn",))
        _seed_session("shared-id", messages=("default turn",))

        resp = self.client.post(
            "/api/sessions/shared-id/messages",
            json=dict(_ACE_BODY, profile="scout"),
        )
        assert resp.status_code == 200

        scout_db = _open_db(scout_db_path)
        try:
            scout_contents = [
                m["content"] for m in scout_db.get_messages("shared-id")
            ]
        finally:
            scout_db.close()
        assert _ACE_BODY["content"] in scout_contents
        default_contents = [m["content"] for m in self._visible_messages("shared-id")]
        assert _ACE_BODY["content"] not in default_contents

    # -- GET intact ------------------------------------------------------------

    def test_get_contract_unchanged_after_append(self):
        """The pre-existing GET shape (session_id/messages/pagination) is
        exactly what ace-coder#178's readers parse — pin it."""
        _seed_session("append-get-shape")
        self.client.post(
            "/api/sessions/append-get-shape/messages", json=dict(_ACE_BODY)
        )
        resp = self.client.get("/api/sessions/append-get-shape/messages")
        payload = resp.json()
        assert set(payload.keys()) == {"session_id", "messages", "pagination"}
        assert payload["pagination"]["returned"] == 2
