"""Tests for the read-only /api/memory/facts browser endpoint (Capella).

Covers the pure query helper against a real holographic ``MemoryStore``
temp db (so the FTS5 triggers/schema are the genuine article), the FTS
input sanitizer, the runtime db-path resolution, and the route handler
itself (called directly, matching test_web_server_session_search.py).
"""

import asyncio

import pytest

from hermes_cli import web_server
from hermes_cli.web_server import (
    _fts_match_expr,
    _holographic_db_path,
    _query_memory_facts,
)


@pytest.fixture
def fact_store(tmp_path):
    """A real holographic MemoryStore with a handful of facts."""
    from plugins.memory.holographic.store import MemoryStore

    db_path = tmp_path / "memory_store.db"
    store = MemoryStore(db_path=db_path)
    store.add_fact("User prefers dark editor themes", category="user_pref")
    store.add_fact("Deploy process uses GitHub Actions", category="project")
    store.add_fact("Capella desktop embeds the Hermes dashboard", category="project")
    store.add_fact("pytest is the preferred test runner", category="tool", tags="testing,python")
    store.close()
    return db_path


# ---------------------------------------------------------------------------
# _fts_match_expr — user input must never produce FTS5 syntax errors
# ---------------------------------------------------------------------------


def test_fts_match_expr_quotes_prefix_terms():
    assert _fts_match_expr("deploy proc") == '"deploy"* "proc"*'


def test_fts_match_expr_strips_fts_operators():
    # Quotes, NEAR, minus, parens — all reduced to plain quoted terms.
    expr = _fts_match_expr('de"ploy -x NEAR(y)')
    assert '"de"' in expr and '"ploy"' in expr
    assert "-" not in expr and "(" not in expr


def test_fts_match_expr_empty_input():
    assert _fts_match_expr("") == ""
    assert _fts_match_expr("  !!  ") == ""


# ---------------------------------------------------------------------------
# _query_memory_facts — pure helper over a temp sqlite store
# ---------------------------------------------------------------------------


def test_query_facts_browse_returns_all(fact_store):
    result = _query_memory_facts(fact_store)
    assert "error" not in result
    assert result["total"] == 4
    assert len(result["facts"]) == 4
    fact = result["facts"][0]
    assert set(fact) == {
        "id", "content", "category", "tags", "trust_score",
        "retrieval_count", "helpful_count", "created_at", "updated_at",
    }
    assert result["categories"] == {"user_pref": 1, "project": 2, "tool": 1}


def test_query_facts_search_matches_fts(fact_store):
    result = _query_memory_facts(fact_store, q="deploy")
    assert result["total"] == 1
    assert result["facts"][0]["content"] == "Deploy process uses GitHub Actions"
    # Categories histogram stays unfiltered so the UI chips keep their counts.
    assert result["categories"]["project"] == 2


def test_query_facts_search_prefix(fact_store):
    result = _query_memory_facts(fact_store, q="deplo")
    assert result["total"] == 1


def test_query_facts_search_never_bumps_retrieval_count(fact_store):
    # Browsing is read-only — unlike the plugin's search_facts tool path.
    _query_memory_facts(fact_store, q="deploy")
    result = _query_memory_facts(fact_store, q="deploy")
    assert result["facts"][0]["retrieval_count"] == 0


def test_query_facts_hostile_query_is_graceful(fact_store):
    result = _query_memory_facts(fact_store, q='"unbalanced NEAR( - OR')
    assert "error" not in result
    assert isinstance(result["facts"], list)


def test_query_facts_category_filter(fact_store):
    result = _query_memory_facts(fact_store, category="project")
    assert result["total"] == 2
    assert all(f["category"] == "project" for f in result["facts"])


def test_query_facts_search_and_category(fact_store):
    assert _query_memory_facts(fact_store, q="deploy", category="user_pref")["total"] == 0
    assert _query_memory_facts(fact_store, q="deploy", category="project")["total"] == 1


def test_query_facts_limit_offset(fact_store):
    page1 = _query_memory_facts(fact_store, limit=3)
    page2 = _query_memory_facts(fact_store, limit=3, offset=3)
    assert page1["total"] == 4 and page2["total"] == 4
    assert len(page1["facts"]) == 3 and len(page2["facts"]) == 1
    ids1 = {f["id"] for f in page1["facts"]}
    ids2 = {f["id"] for f in page2["facts"]}
    assert not ids1 & ids2


def test_query_facts_limit_clamped(fact_store):
    # max 500, min 1 — no sqlite error on absurd input.
    assert len(_query_memory_facts(fact_store, limit=10_000)["facts"]) == 4
    assert len(_query_memory_facts(fact_store, limit=-5)["facts"]) == 1


def test_query_facts_missing_store_reports_error(tmp_path):
    result = _query_memory_facts(tmp_path / "nope.db")
    assert "error" in result
    assert result["facts"] == [] and result["total"] == 0 and result["categories"] == {}


def test_query_facts_corrupt_store_reports_error(tmp_path):
    bogus = tmp_path / "bogus.db"
    bogus.write_text("this is not sqlite")
    result = _query_memory_facts(bogus)
    assert "error" in result
    assert result["facts"] == []


# ---------------------------------------------------------------------------
# _holographic_db_path — resolves like HolographicMemoryProvider.initialize
# ---------------------------------------------------------------------------


def test_db_path_defaults_to_home_store():
    from hermes_constants import get_hermes_home

    assert _holographic_db_path() == get_hermes_home() / "memory_store.db"


def test_db_path_honors_plugin_config_with_home_expansion(tmp_path):
    import yaml
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.dump({"plugins": {"hermes-memory-store": {"db_path": "$HERMES_HOME/sub/facts.db"}}})
    )
    assert _holographic_db_path() == home / "sub" / "facts.db"


# ---------------------------------------------------------------------------
# Route handler — direct call, same idiom as test_web_server_session_search
# ---------------------------------------------------------------------------


def test_get_memory_facts_handler_envelope(fact_store):
    import yaml
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(
        yaml.dump({
            "memory": {"provider": "holographic"},
            "plugins": {"hermes-memory-store": {"db_path": str(fact_store)}},
        })
    )

    result = asyncio.run(web_server.get_memory_facts(q="", category="", limit=100, offset=0))
    assert result["total"] == 4
    assert result["provider"] == "holographic"
    assert isinstance(result["db_mtime"], float)
    assert result["categories"]["project"] == 2


def test_get_memory_facts_handler_missing_store_is_graceful():
    result = asyncio.run(web_server.get_memory_facts(q="", category="", limit=100, offset=0))
    assert "error" in result
    assert result["facts"] == []
    assert result["db_mtime"] is None
