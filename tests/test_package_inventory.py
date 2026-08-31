"""Repo-wide package inventory enforcement (the executable completeness proof).

Runs the canonical enumerator (agent/package_contract.py) over the real
tree and asserts that every source-owned package — native plugins,
dashboard-app plugins, bundled + optional skills, MCP catalog entries — is
classified exactly once and passes the contract: no duplicate identities,
no orphan directories, no unknown fields, no invalid versions, no unsafe
paths, no unresolved entrypoints or dependencies, no incompatible
capability/platform declarations.

This is deliberately a zero-findings gate for SOURCE-OWNED packages (an
authoring/CI boundary). Runtime loaders keep their documented
warn-and-continue behavior for user-installed packages — see the native
plugin compatibility contract.

Regenerate the machine-readable inventory with:
    hermes packages inventory --json
"""

from pathlib import Path

from agent.package_contract import (
    Finding,
    enumerate_source_packages,
    record_to_dict,
)

REPO = Path(__file__).resolve().parents[1]


def _run():
    return enumerate_source_packages(REPO)


def _fmt(findings):
    return "\n".join(
        f"{f.severity}: [{f.rule}] {f.package}: {f.message}" for f in findings
    )


def test_population_sanity():
    """The enumerator actually finds the trees (not a count snapshot)."""
    records, _ = _run()
    families = {r.family for r in records}
    assert families == {"plugin", "skill", "mcp"}
    ownerships = {r.ownership for r in records}
    assert "bundled" in ownerships and "optional" in ownerships
    # Every category discovery system is represented.
    plugin_categories = {r.category for r in records if r.family == "plugin"}
    for expected in (
        "browser", "cron_providers", "dashboard_auth", "image_gen", "memory",
        "model-providers", "platforms", "video_gen", "web",
    ):
        assert expected in plugin_categories, f"no packages found under plugins/{expected}/"


def test_every_package_classified_exactly_once():
    records, _ = _run()
    refs = [r.ref for r in records]
    assert len(refs) == len(set(refs)), "a package was classified twice"
    paths = [(r.family, r.path) for r in records]
    assert len(paths) == len(set(paths)), "a package directory was classified twice"


def test_zero_contract_findings():
    """Source-owned packages carry no contract violations — errors or warnings.

    Covers, via the validator rules exercised against the real tree:
    duplicate-id, orphan-package, unknown-field/unknown-metadata,
    invalid-version/missing-version, unsafe-path, unresolved-entrypoint,
    unresolved-dependency (error severity), invalid-env-declaration,
    unknown-capability, unknown-platform, kind-family-mismatch,
    manifest-unreadable.
    """
    _, findings = _run()
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, "contract errors:\n" + _fmt(errors)
    warnings = [f for f in findings if f.severity == "warning"]
    assert not warnings, (
        "contract warnings (fix the package or, for a new consumed field, "
        "add it to the census in agent/package_contract.py):\n" + _fmt(warnings)
    )


def test_inventory_round_trips_deterministically():
    a_records, a_findings = _run()
    b_records, b_findings = _run()
    assert [record_to_dict(r) for r in a_records] == [
        record_to_dict(r) for r in b_records
    ]
    assert a_findings == b_findings


def test_identity_is_public_id():
    """Spot-check that identities are the preserved public IDs."""
    records, _ = _run()
    by_ref = {r.ref: r for r in records}
    # plugin key (nested + flat), skill directory name, mcp catalog name
    assert "plugin:disk-cleanup" in by_ref
    assert "plugin:image_gen/openai" in by_ref
    assert "plugin:kanban" in by_ref  # dashboard-app package
    assert by_ref["plugin:kanban"].kind == "dashboard"
    # Skill identity is the directory name, never the category path.
    assert "skill:github" in by_ref
    assert by_ref["skill:github"].path == "skills/software-development/github"
    assert "mcp:figma" in by_ref
    # name duplication across categories is legal; identity is the key
    assert "plugin:image_gen/fal" in by_ref and "plugin:video_gen/fal" in by_ref
