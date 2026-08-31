"""Unit tests for agent/package_contract.py — the canonical package contract.

One envelope, four family payloads (plugin / skill / mcp / dashboard).
Each rule the validator enforces gets a focused test with a synthetic
package, plus round-trip determinism checks. Repo-wide enforcement lives in
tests/test_package_inventory.py; this file covers the library itself.
"""

import json

import pytest
import yaml

from agent.package_contract import (
    CONTRACT_VERSION,
    KNOWN_PLUGIN_MANIFEST_FIELDS,
    MCP_MANIFEST_SUPPORTED,
    PLUGIN_MANIFEST_SUPPORTED,
    SKILL_SCHEMA_SUPPORTED,
    Finding,
    PackageRecord,
    enumerate_source_packages,
    parse_dashboard_manifest,
    parse_mcp_manifest,
    parse_plugin_manifest,
    parse_skill_frontmatter,
    record_to_dict,
    validate_cross_package,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _rules(findings):
    return sorted(f.rule for f in findings)


def _errors(findings):
    return [f for f in findings if f.severity == "error"]


PLUGIN_OK = {
    "name": "sample",
    "version": "1.0.0",
    "description": "A sample plugin.",
    "author": "NousResearch",
    "kind": "standalone",
    "hooks": ["pre_tool_call"],
}

SKILL_OK = {
    "name": "sample-skill",
    "description": "Does one sample thing.",
    "version": "1.0.0",
    "author": "NousResearch",
    "license": "MIT",
    "platforms": ["linux", "macos", "windows"],
    "metadata": {"hermes": {"tags": ["Sample"]}},
}

MCP_OK = {
    "manifest_version": 1,
    "name": "sample-mcp",
    "description": "A sample MCP entry.",
    "source": "https://example.com",
    "transport": {"type": "http", "url": "https://example.com/mcp"},
}

DASHBOARD_OK = {
    "name": "sample-dash",
    "label": "Sample",
    "description": "A dashboard extension.",
    "version": "1.0.0",
    "tab": {"path": "/sample", "position": "end"},
    "entry": "dist/index.js",
}


# ---------------------------------------------------------------------------
# Plugin manifests
# ---------------------------------------------------------------------------


class TestParsePluginManifest:
    def test_valid_manifest_no_errors(self):
        rec, findings = parse_plugin_manifest(PLUGIN_OK, key="sample", path="plugins/sample")
        assert not _errors(findings)
        assert rec.family == "plugin"
        assert rec.id == "sample"
        assert rec.name == "sample"
        assert rec.version == "1.0.0"
        assert rec.kind == "standalone"
        assert rec.schema_version == 1

    def test_nested_key_is_identity(self):
        rec, _ = parse_plugin_manifest(
            dict(PLUGIN_OK, name="fal"), key="image_gen/fal", path="plugins/image_gen/fal"
        )
        assert rec.id == "image_gen/fal"

    def test_manifest_version_2_parses(self):
        rec, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, manifest_version=2, license="MIT", tags=["x"]),
            key="sample",
            path="plugins/sample",
        )
        assert rec.schema_version == 2
        assert rec.license == "MIT"
        assert not _errors(findings)

    def test_newer_manifest_version_warns_not_errors(self):
        _, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, manifest_version=PLUGIN_MANIFEST_SUPPORTED + 1),
            key="sample",
            path="plugins/sample",
        )
        assert not _errors(findings)
        assert "manifest-version-newer" in _rules(findings)

    def test_unknown_field_reported(self):
        _, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, mystery_field=True), key="sample", path="plugins/sample"
        )
        assert "unknown-field" in _rules(findings)
        assert not _errors(findings)  # unknown fields are ignored, never fatal

    def test_known_field_census_matches_loader(self):
        # The canonical census and the loader census must be the same object
        # (single source of truth) — hermes_cli.plugins re-exports it.
        from hermes_cli.plugins import _KNOWN_MANIFEST_FIELDS

        assert _KNOWN_MANIFEST_FIELDS is KNOWN_PLUGIN_MANIFEST_FIELDS

    def test_missing_name_is_error(self):
        d = dict(PLUGIN_OK)
        del d["name"]
        _, findings = parse_plugin_manifest(d, key="sample", path="plugins/sample")
        assert "missing-name" in [f.rule for f in _errors(findings)]

    def test_invalid_version_is_error(self):
        _, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, version="not-a-version"), key="sample", path="plugins/sample"
        )
        assert "invalid-version" in [f.rule for f in _errors(findings)]

    def test_missing_version_is_error(self):
        d = dict(PLUGIN_OK)
        del d["version"]
        _, findings = parse_plugin_manifest(d, key="sample", path="plugins/sample")
        assert "missing-version" in [f.rule for f in _errors(findings)]

    def test_unknown_kind_warns(self):
        _, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, kind="mystery"), key="sample", path="plugins/sample"
        )
        assert "unknown-kind" in _rules(findings)
        assert not _errors(findings)  # loader coerces to standalone; not fatal

    def test_requires_env_both_shapes_accepted(self):
        simple, f1 = parse_plugin_manifest(
            dict(PLUGIN_OK, requires_env=["API_KEY"]), key="s", path="plugins/s"
        )
        rich, f2 = parse_plugin_manifest(
            dict(PLUGIN_OK, requires_env=[{"name": "API_KEY", "prompt": "Key"}]),
            key="s",
            path="plugins/s",
        )
        assert not _errors(f1) and not _errors(f2)
        assert simple.dependencies["env"] == ["API_KEY"]
        assert rich.dependencies["env"] == ["API_KEY"]

    def test_malformed_requires_env_entry_is_error(self):
        _, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, requires_env=[{"prompt": "no name"}]),
            key="s",
            path="plugins/s",
        )
        assert "invalid-env-declaration" in [f.rule for f in _errors(findings)]

    def test_requires_plugins_normalized(self):
        rec, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, requires_plugins=["other", {"id": "x", "version_range": ">=1"}]),
            key="s",
            path="plugins/s",
        )
        assert not _errors(findings)
        assert rec.dependencies["plugins"] == [
            {"id": "other", "version_range": None},
            {"id": "x", "version_range": ">=1"},
        ]

    def test_pip_dependencies_is_a_compat_alias(self):
        rec, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, pip_dependencies=["requests>=2,<3"]),
            key="s",
            path="plugins/s",
        )
        assert not _errors(findings)
        assert rec.dependencies["python"] == ["requests>=2,<3"]

    def test_python_dependencies_wins_over_the_alias(self):
        rec, _ = parse_plugin_manifest(
            dict(PLUGIN_OK, python_dependencies=["new>=1"], pip_dependencies=["old>=1"]),
            key="s",
            path="plugins/s",
        )
        assert rec.dependencies["python"] == ["new>=1"]

    def test_explicit_empty_python_dependencies_does_not_fall_back(self):
        # Declaring the v2 key at all is explicit: an empty list means "none",
        # it must not silently resurrect the v1 alias.
        rec, _ = parse_plugin_manifest(
            dict(PLUGIN_OK, python_dependencies=[], pip_dependencies=["old>=1"]),
            key="s",
            path="plugins/s",
        )
        assert rec.dependencies["python"] == []

    def test_non_list_dependencies_warns_and_ignores(self):
        rec, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, python_dependencies="requests"), key="s", path="plugins/s"
        )
        assert "invalid-dependency-declaration" in _rules(findings)
        assert not _errors(findings)
        assert rec.dependencies["python"] == []

    def test_unknown_capability_warns(self):
        _, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, capabilities=["not.a.capability"]),
            key="s",
            path="plugins/s",
        )
        assert "unknown-capability" in _rules(findings)

    def test_known_capability_ok(self):
        rec, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, capabilities=["tools.override"]),
            key="s",
            path="plugins/s",
        )
        assert "unknown-capability" not in _rules(findings)
        assert rec.capabilities == ["tools.override"]

    def test_unknown_platform_is_error(self):
        _, findings = parse_plugin_manifest(
            dict(PLUGIN_OK, platforms=["amiga"]), key="s", path="plugins/s"
        )
        assert "unknown-platform" in [f.rule for f in _errors(findings)]


# ---------------------------------------------------------------------------
# Skill frontmatter
# ---------------------------------------------------------------------------


class TestParseSkillFrontmatter:
    def test_valid_frontmatter_no_errors(self):
        rec, findings = parse_skill_frontmatter(
            SKILL_OK, skill_dir="skills/sample/sample-skill", tier="bundled"
        )
        assert not _errors(findings)
        assert rec.family == "skill"
        assert rec.id == "sample-skill"
        assert rec.schema_version == SKILL_SCHEMA_SUPPORTED
        assert rec.license == "MIT"
        assert rec.platforms == ["linux", "macos", "windows"]

    def test_name_dir_mismatch_is_error(self):
        _, findings = parse_skill_frontmatter(
            dict(SKILL_OK, name="wrong"), skill_dir="skills/sample/sample-skill", tier="bundled"
        )
        assert "name-dir-mismatch" in [f.rule for f in _errors(findings)]

    def test_missing_required_fields_are_errors(self):
        d = {"name": "sample-skill", "description": "x."}
        _, findings = parse_skill_frontmatter(
            d, skill_dir="skills/sample/sample-skill", tier="bundled"
        )
        rules = [f.rule for f in _errors(findings)]
        assert "missing-version" in rules
        assert "missing-author" in rules
        assert "missing-license" in rules
        assert "missing-platforms" in rules

    def test_description_over_60_chars_is_error(self):
        _, findings = parse_skill_frontmatter(
            dict(SKILL_OK, description="x" * 61 + "."),
            skill_dir="skills/sample/sample-skill",
            tier="bundled",
        )
        assert "description-too-long" in [f.rule for f in _errors(findings)]

    def test_top_level_tags_category_accepted_as_alias(self):
        d = dict(SKILL_OK)
        d.pop("metadata")
        d["tags"] = ["Sample"]
        d["category"] = "sample"
        rec, findings = parse_skill_frontmatter(
            d, skill_dir="skills/sample/sample-skill", tier="bundled"
        )
        assert not _errors(findings)
        assert rec.tags == ["Sample"]
        assert rec.category == "sample"

    def test_unknown_top_level_field_reported(self):
        _, findings = parse_skill_frontmatter(
            dict(SKILL_OK, mystery=1), skill_dir="skills/sample/sample-skill", tier="bundled"
        )
        assert "unknown-field" in _rules(findings)

    def test_consumed_long_tail_fields_are_known(self):
        # Fields with real consumers must not be flagged unknown:
        # prerequisites/setup/required_environment_variables/
        # required_credential_files (tools/skills_tool.py), environments
        # (agent/skill_utils.py), compatibility (skills_tool.py),
        # dependencies (declared pip deps, mirrored by the contract).
        d = dict(
            SKILL_OK,
            prerequisites={"env_vars": ["K"], "commands": ["jq"]},
            setup={"help": "h", "collect_secrets": []},
            required_environment_variables=["K"],
            required_credential_files=["cred.json"],
            environments=["docker"],
            compatibility="needs docker",
            dependencies=["requests"],
        )
        rec, findings = parse_skill_frontmatter(
            d, skill_dir="skills/sample/sample-skill", tier="bundled"
        )
        assert "unknown-field" not in _rules(findings)
        assert rec.dependencies["python"] == ["requests"]
        assert rec.dependencies["env"] == ["K"]
        assert rec.dependencies["commands"] == ["jq"]

    def test_unknown_platform_is_error(self):
        _, findings = parse_skill_frontmatter(
            dict(SKILL_OK, platforms=["beos"]),
            skill_dir="skills/sample/sample-skill",
            tier="bundled",
        )
        assert "unknown-platform" in [f.rule for f in _errors(findings)]


# ---------------------------------------------------------------------------
# MCP catalog manifests
# ---------------------------------------------------------------------------


class TestParseMcpManifest:
    def test_valid_manifest_no_errors(self):
        rec, findings = parse_mcp_manifest(MCP_OK, name="sample-mcp", path="optional-mcps/sample-mcp")
        assert not _errors(findings)
        assert rec.family == "mcp"
        assert rec.id == "sample-mcp"
        assert rec.schema_version == 1

    def test_schema_version_agrees_with_the_catalog_reader(self):
        """The contract owns the value; the catalog reader mirrors it.

        ``hermes_cli/mcp_catalog.py`` is a documented adapter, not a
        contract importer: it is the fail-closed supply-chain boundary for
        installing MCP servers, so it deliberately keeps its own literal
        rather than pulling the skills/plugin import chain into that file.
        Drift is impossible because this test fails.
        """
        from hermes_cli.mcp_catalog import _MANIFEST_VERSION

        assert _MANIFEST_VERSION == MCP_MANIFEST_SUPPORTED

    def test_unsupported_manifest_version_is_error(self):
        _, findings = parse_mcp_manifest(
            dict(MCP_OK, manifest_version=MCP_MANIFEST_SUPPORTED + 1),
            name="sample-mcp",
            path="optional-mcps/sample-mcp",
        )
        assert "manifest-version-unsupported" in [f.rule for f in _errors(findings)]

    def test_stdio_requires_command(self):
        _, findings = parse_mcp_manifest(
            dict(MCP_OK, transport={"type": "stdio"}),
            name="sample-mcp",
            path="optional-mcps/sample-mcp",
        )
        assert "unresolved-entrypoint" in [f.rule for f in _errors(findings)]

    def test_http_requires_url(self):
        _, findings = parse_mcp_manifest(
            dict(MCP_OK, transport={"type": "http"}),
            name="sample-mcp",
            path="optional-mcps/sample-mcp",
        )
        assert "unresolved-entrypoint" in [f.rule for f in _errors(findings)]


# ---------------------------------------------------------------------------
# Dashboard manifests
# ---------------------------------------------------------------------------


class TestParseDashboardManifest:
    def test_valid_manifest_no_errors(self):
        rec, findings = parse_dashboard_manifest(
            DASHBOARD_OK, key="sample-dash", path="plugins/sample-dash"
        )
        assert not _errors(findings)
        assert rec.family == "plugin"
        assert rec.kind == "dashboard"
        assert rec.entrypoints["entry"] == "dist/index.js"

    def test_absolute_api_path_is_error(self):
        _, findings = parse_dashboard_manifest(
            dict(DASHBOARD_OK, api="/etc/passwd"), key="d", path="plugins/d"
        )
        assert "unsafe-path" in [f.rule for f in _errors(findings)]

    def test_traversal_entry_path_is_error(self):
        _, findings = parse_dashboard_manifest(
            dict(DASHBOARD_OK, entry="../../evil.js"), key="d", path="plugins/d"
        )
        assert "unsafe-path" in [f.rule for f in _errors(findings)]

    def test_windows_drive_and_backslash_traversal_are_errors(self):
        _, f1 = parse_dashboard_manifest(
            dict(DASHBOARD_OK, api="C:\\evil\\api.py"), key="d", path="plugins/d"
        )
        assert "unsafe-path" in [f.rule for f in _errors(f1)]
        _, f2 = parse_dashboard_manifest(
            dict(DASHBOARD_OK, entry="..\\..\\evil.js"), key="d", path="plugins/d"
        )
        assert "unsafe-path" in [f.rule for f in _errors(f2)]

    def test_backslash_relative_path_normalized_not_rejected(self):
        rec, findings = parse_dashboard_manifest(
            dict(DASHBOARD_OK, entry="dist\\index.js"), key="d", path="plugins/d"
        )
        assert not _errors(findings)
        assert rec.entrypoints["entry"] == "dist/index.js"


# ---------------------------------------------------------------------------
# Cross-package validation
# ---------------------------------------------------------------------------


class TestCrossPackage:
    def _rec(self, family, id_, **kw):
        data = dict(PLUGIN_OK, name=id_.rsplit("/", 1)[-1])
        rec, _ = parse_plugin_manifest(data, key=id_, path=f"plugins/{id_}")
        return rec

    def test_duplicate_ids_error(self):
        a = self._rec("plugin", "dup")
        b = self._rec("plugin", "dup")
        findings = validate_cross_package([a, b])
        assert "duplicate-id" in [f.rule for f in _errors(findings)]

    def test_distinct_ids_ok(self):
        a = self._rec("plugin", "image_gen/fal")
        b = self._rec("plugin", "video_gen/fal")
        findings = validate_cross_package([a, b])
        assert "duplicate-id" not in _rules(findings)

    def test_skill_duplicate_name_across_tiers_errors(self):
        a, _ = parse_skill_frontmatter(
            SKILL_OK, skill_dir="skills/sample/sample-skill", tier="bundled"
        )
        b, _ = parse_skill_frontmatter(
            SKILL_OK, skill_dir="optional-skills/other/sample-skill", tier="optional"
        )
        findings = validate_cross_package([a, b])
        assert "duplicate-id" in [f.rule for f in _errors(findings)]

    def test_unresolved_requires_plugins_warns(self):
        data = dict(PLUGIN_OK, requires_plugins=["nonexistent-plugin"])
        rec, _ = parse_plugin_manifest(data, key="sample", path="plugins/sample")
        findings = validate_cross_package([rec])
        assert "unresolved-dependency" in _rules(findings)
        assert not _errors(findings)  # advisory, matches loader semantics


# ---------------------------------------------------------------------------
# Enumerator (synthetic tree)
# ---------------------------------------------------------------------------


@pytest.fixture
def synthetic_repo(tmp_path):
    root = tmp_path

    # plugin family
    flat = root / "plugins" / "flat-plugin"
    flat.mkdir(parents=True)
    (flat / "plugin.yaml").write_text(yaml.safe_dump(dict(PLUGIN_OK, name="flat-plugin")), encoding="utf-8")
    (flat / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")

    nested = root / "plugins" / "image_gen" / "sample"
    nested.mkdir(parents=True)
    (nested / "plugin.yaml").write_text(
        yaml.safe_dump(dict(PLUGIN_OK, name="sample", kind="backend")),
        encoding="utf-8",
    )
    (nested / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")

    dash = root / "plugins" / "dash-app" / "dashboard"
    dash.mkdir(parents=True)
    (dash / "manifest.json").write_text(json.dumps(dict(DASHBOARD_OK, name="dash-app")), encoding="utf-8")
    (dash / "dist").mkdir()
    (dash / "dist" / "index.js").write_text("//x\n", encoding="utf-8")

    # skill family
    sk = root / "skills" / "samplecat" / "sample-skill"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(SKILL_OK) + "---\n\n# Sample Skill\n\nBody.\n",
        encoding="utf-8",
    )
    (root / "skills" / "samplecat" / "DESCRIPTION.md").write_text(
        "---\ndescription: Sample category.\n---\n",
        encoding="utf-8",
    )

    osk = root / "optional-skills" / "othercat" / "other-skill"
    osk.mkdir(parents=True)
    (osk / "SKILL.md").write_text(
        "---\n"
        + yaml.safe_dump(dict(SKILL_OK, name="other-skill"))
        + "---\n\n# Other Skill\n\nBody.\n",
        encoding="utf-8",
    )

    # mcp family
    mcp = root / "optional-mcps" / "sample-mcp"
    mcp.mkdir(parents=True)
    (mcp / "manifest.yaml").write_text(yaml.safe_dump(MCP_OK), encoding="utf-8")

    return root


class TestEnumerator:
    def test_classifies_each_package_exactly_once(self, synthetic_repo):
        records, findings = enumerate_source_packages(synthetic_repo)
        ids = sorted((r.family, r.id) for r in records)
        assert ids == [
            ("mcp", "sample-mcp"),
            ("plugin", "dash-app"),
            ("plugin", "flat-plugin"),
            ("plugin", "image_gen/sample"),
            ("skill", "other-skill"),
            ("skill", "sample-skill"),
        ]
        assert not _errors(findings)

    def test_orphan_directory_reported(self, synthetic_repo):
        orphan = synthetic_repo / "plugins" / "mystery-dir"
        orphan.mkdir()
        (orphan / "code.py").write_text("x = 1\n", encoding="utf-8")
        _, findings = enumerate_source_packages(synthetic_repo)
        assert "orphan-package" in [f.rule for f in _errors(findings)]

    def test_build_residue_is_not_an_orphan(self, synthetic_repo):
        """Cache dirs and git's leftover empty dirs are not packages.

        Switching branches leaves ``__pycache__`` and empty directories
        behind after tracked files move away. Reporting those as
        orphan-package turns a clean tree's inventory red for something no
        author can fix, and differs from CI's fresh checkout.
        """
        stale = synthetic_repo / "plugins" / "moved-away"
        (stale / "__pycache__").mkdir(parents=True)
        (stale / "__pycache__" / "old.cpython-311.pyc").write_bytes(b"\x00")
        (synthetic_repo / "skills" / "samplecat" / "emptied").mkdir(parents=True)
        nested = synthetic_repo / "skills" / "samplecat" / "gone" / "scripts"
        (nested / "__pycache__").mkdir(parents=True)

        records, findings = enumerate_source_packages(synthetic_repo)

        assert not [f for f in findings if f.rule == "orphan-package"]
        # Residue is skipped, never mistaken for a package.
        assert not [r for r in records if r.id in {"moved-away", "emptied", "gone"}]

    def test_orphan_still_reported_when_residue_dir_holds_real_content(
        self, synthetic_repo
    ):
        """The residue guard must not swallow genuinely broken packages."""
        broken = synthetic_repo / "skills" / "samplecat" / "half-authored"
        (broken / "__pycache__").mkdir(parents=True)
        (broken / "README.md").write_text("wip\n", encoding="utf-8")
        _, findings = enumerate_source_packages(synthetic_repo)
        assert "orphan-package" in [f.rule for f in _errors(findings)]

    def test_plugin_without_entrypoint_reported(self, synthetic_repo):
        broken = synthetic_repo / "plugins" / "no-entry"
        broken.mkdir()
        (broken / "plugin.yaml").write_text(
            yaml.safe_dump(dict(PLUGIN_OK, name="no-entry")),
            encoding="utf-8",
        )
        _, findings = enumerate_source_packages(synthetic_repo)
        assert "unresolved-entrypoint" in [f.rule for f in _errors(findings)]

    def test_model_provider_without_register_hook_ok(self, synthetic_repo):
        # model-provider entrypoint is module-level register_provider(), not register(ctx)
        mp = synthetic_repo / "plugins" / "model-providers" / "sample"
        mp.mkdir(parents=True)
        (mp / "plugin.yaml").write_text(
            yaml.safe_dump(dict(PLUGIN_OK, name="sample-provider", kind="model-provider")),
            encoding="utf-8",
        )
        (mp / "__init__.py").write_text(
            "from providers import register_provider\n",
            encoding="utf-8",
        )
        _, findings = enumerate_source_packages(synthetic_repo)
        assert "unresolved-entrypoint" not in [f.rule for f in _errors(findings)]

    def test_deterministic_output(self, synthetic_repo):
        a = enumerate_source_packages(synthetic_repo)
        b = enumerate_source_packages(synthetic_repo)
        assert [record_to_dict(r) for r in a[0]] == [record_to_dict(r) for r in b[0]]
        assert [(f.rule, f.package, f.message) for f in a[1]] == [
            (f.rule, f.package, f.message) for f in b[1]
        ]

    def test_skill_body_required(self, synthetic_repo):
        empty = synthetic_repo / "skills" / "samplecat" / "empty-skill"
        empty.mkdir()
        (empty / "SKILL.md").write_text(
            "---\n" + yaml.safe_dump(dict(SKILL_OK, name="empty-skill")) + "---\n\n",
            encoding="utf-8",
        )
        _, findings = enumerate_source_packages(synthetic_repo)
        assert "unresolved-entrypoint" in [f.rule for f in _errors(findings)]


# ---------------------------------------------------------------------------
# Serialization round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_record_to_dict_is_json_safe_and_stable(self):
        rec, _ = parse_plugin_manifest(PLUGIN_OK, key="sample", path="plugins/sample")
        d1 = record_to_dict(rec)
        d2 = record_to_dict(rec)
        assert d1 == d2
        json.dumps(d1)  # must not raise
        assert d1["contract_version"] == CONTRACT_VERSION
        assert d1["family"] == "plugin"
        assert d1["id"] == "sample"
