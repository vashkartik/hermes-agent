"""Tests for the ``hermes packages`` CLI (hermes_cli/packages_cmd.py).

Covers the three verbs against a synthetic tree: deterministic JSON
inventory, lint exit codes (errors vs --strict warnings), and the
mechanical migrations (idempotent; --check mode reports without writing).
"""

import argparse
import json

import pytest
import yaml

from hermes_cli.packages_cmd import (
    cmd_packages_inventory,
    cmd_packages_lint,
    cmd_packages_migrate,
)


PLUGIN_OK = {
    "name": "sample",
    "version": "1.0.0",
    "description": "A sample plugin.",
    "author": "NousResearch",
    "kind": "standalone",
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


def _args(**kw):
    return argparse.Namespace(**kw)


@pytest.fixture
def tree(tmp_path):
    plugin = tmp_path / "plugins" / "sample"
    plugin.mkdir(parents=True)
    (plugin / "plugin.yaml").write_text(yaml.safe_dump(PLUGIN_OK), encoding="utf-8")
    (plugin / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")

    skill = tmp_path / "skills" / "cat" / "sample-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\n" + yaml.safe_dump(SKILL_OK) + "---\n\n# Sample Skill\n\nBody.\n",
        encoding="utf-8",
    )
    return tmp_path


class TestInventory:
    def test_json_deterministic(self, tree, capsys):
        assert cmd_packages_inventory(_args(root=str(tree), json=True)) == 0
        first = capsys.readouterr().out
        assert cmd_packages_inventory(_args(root=str(tree), json=True)) == 0
        second = capsys.readouterr().out
        assert first == second
        payload = json.loads(first)
        assert payload["counts"] == {"mcp": 0, "plugin": 1, "skill": 1, "total": 2}
        assert [p["id"] for p in payload["packages"]] == ["sample", "sample-skill"]

    def test_table_output(self, tree, capsys):
        assert cmd_packages_inventory(_args(root=str(tree), json=False)) == 0
        out = capsys.readouterr().out
        assert "2 source-owned packages" in out
        assert "sample-skill" in out


class TestLint:
    def test_clean_tree_exits_zero(self, tree, capsys):
        assert cmd_packages_lint(_args(root=str(tree), strict=False)) == 0
        assert "satisfy the package contract" in capsys.readouterr().out

    def test_error_exits_one(self, tree, capsys):
        broken = tree / "plugins" / "broken"
        broken.mkdir()
        (broken / "plugin.yaml").write_text(
            yaml.safe_dump(dict(PLUGIN_OK, name="broken", version="not-semver")),
            encoding="utf-8",
        )
        (broken / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
        assert cmd_packages_lint(_args(root=str(tree), strict=False)) == 1
        assert "invalid-version" in capsys.readouterr().out

    def test_warning_needs_strict(self, tree, capsys):
        d = dict(PLUGIN_OK, name="warny")
        del d["author"]
        warny = tree / "plugins" / "warny"
        warny.mkdir()
        (warny / "plugin.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
        (warny / "__init__.py").write_text("def register(ctx):\n    pass\n", encoding="utf-8")
        assert cmd_packages_lint(_args(root=str(tree), strict=False)) == 0
        capsys.readouterr()
        assert cmd_packages_lint(_args(root=str(tree), strict=True)) == 1
        assert "missing-author" in capsys.readouterr().out


class TestMigrate:
    def _mismatched_memory_plugin(self, tree):
        mem = tree / "plugins" / "memory" / "sample-mem"
        mem.mkdir(parents=True)
        (mem / "plugin.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "sample-mem",
                    "version": "1.0.0",
                    "description": "A memory provider.",
                    "author": "NousResearch",
                }
            ),
            encoding="utf-8",
        )
        (mem / "__init__.py").write_text("x = 1\n", encoding="utf-8")
        return mem

    def test_check_reports_without_writing(self, tree, capsys):
        mem = self._mismatched_memory_plugin(tree)
        before = (mem / "plugin.yaml").read_text(encoding="utf-8")
        assert cmd_packages_migrate(_args(root=str(tree), check=True)) == 1
        assert "would migrate" in capsys.readouterr().out
        assert (mem / "plugin.yaml").read_text(encoding="utf-8") == before

    def test_migrates_kind_and_is_idempotent(self, tree, capsys):
        mem = self._mismatched_memory_plugin(tree)
        assert cmd_packages_migrate(_args(root=str(tree), check=False)) == 0
        out = capsys.readouterr().out
        assert "declared kind: exclusive" in out
        manifest = yaml.safe_load((mem / "plugin.yaml").read_text(encoding="utf-8"))
        assert manifest["kind"] == "exclusive"
        # Second run: nothing left to do.
        assert cmd_packages_migrate(_args(root=str(tree), check=False)) == 0
        assert "nothing to migrate" in capsys.readouterr().out

    def test_drops_dead_title_field(self, tree, capsys):
        skill_md = tree / "skills" / "cat" / "sample-skill" / "SKILL.md"
        fm = dict(SKILL_OK, title="A Dead Title")
        skill_md.write_text(
            "---\n" + yaml.safe_dump(fm) + "---\n\n# Sample Skill\n\nBody.\n",
            encoding="utf-8",
        )
        assert cmd_packages_migrate(_args(root=str(tree), check=False)) == 0
        assert "dropped dead field 'title'" in capsys.readouterr().out
        assert "title:" not in skill_md.read_text(encoding="utf-8")
        # Frontmatter still parses and the skill still validates.
        assert cmd_packages_lint(_args(root=str(tree), strict=True)) == 0

    def test_renames_upstream_skill(self, tree, capsys):
        skill_md = tree / "skills" / "cat" / "sample-skill" / "SKILL.md"
        fm = dict(SKILL_OK)
        fm["metadata"] = {
            "hermes": {"tags": ["Sample"], "upstream_skill": "https://example.com"}
        }
        skill_md.write_text(
            "---\n" + yaml.safe_dump(fm) + "---\n\n# Sample Skill\n\nBody.\n",
            encoding="utf-8",
        )
        assert cmd_packages_migrate(_args(root=str(tree), check=False)) == 0
        assert "renamed upstream_skill" in capsys.readouterr().out
        text = skill_md.read_text(encoding="utf-8")
        assert "upstream_skill:" not in text
        assert "upstream:" in text
        assert cmd_packages_lint(_args(root=str(tree), strict=True)) == 0

    def test_non_mechanical_findings_reported_manual(self, tree, capsys):
        skill_md = tree / "skills" / "cat" / "sample-skill" / "SKILL.md"
        fm = dict(SKILL_OK, triggers=["do a thing"])
        skill_md.write_text(
            "---\n" + yaml.safe_dump(fm) + "---\n\n# Sample Skill\n\nBody.\n",
            encoding="utf-8",
        )
        assert cmd_packages_migrate(_args(root=str(tree), check=False)) == 0
        out = capsys.readouterr().out
        assert "Needs manual migration" in out
        assert "triggers" in out
