import importlib.util
import json
from pathlib import Path

repo_root = Path(__file__).parent.parent.parent
script_path = repo_root / "scripts" / "vector" / "install_mp_skills.py"

spec = importlib.util.spec_from_file_location("install_mp_skills", str(script_path))
install_mp_skills = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install_mp_skills)


def make_skill(root: Path, category: str, name: str, body: str = "Do the thing.") -> Path:
    skill_dir = root / "skills" / category / name if category else root / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {name} skill.\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


def make_existing(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: pre-existing.\n---\n\n# {name}\n",
        encoding="utf-8",
    )
    return skill_dir


def test_installs_with_prefix_and_rewrites_frontmatter(tmp_path):
    src = tmp_path / "src"
    make_skill(src, "engineering", "tdd")
    dest = tmp_path / "profile" / "skills" / "mattpocock"

    report = install_mp_skills.install(src, dest, [tmp_path / "profile" / "skills"])

    assert report["installed"] == ["mp-tdd"]
    installed_md = dest / "mp-tdd" / "SKILL.md"
    assert installed_md.is_file()
    assert install_mp_skills.read_frontmatter_name(installed_md) == "mp-tdd"
    assert report["collisions"] == []


def test_excluded_categories_are_not_installed(tmp_path):
    src = tmp_path / "src"
    make_skill(src, "engineering", "tdd")
    make_skill(src, "in-progress", "wizard")
    dest = tmp_path / "dest"

    report = install_mp_skills.install(src, dest, [])

    assert report["installed"] == ["mp-tdd"]
    assert not (dest / "mp-wizard").exists()


def test_dedupe_skips_skill_already_visible_unprefixed(tmp_path):
    src = tmp_path / "src"
    make_skill(src, "engineering", "tdd")
    make_skill(src, "engineering", "research")
    external = tmp_path / "external-skills"
    make_existing(external, "tdd")
    dest = tmp_path / "dest"

    report = install_mp_skills.install(src, dest, [external])

    assert report["installed"] == ["mp-research"]
    skipped = {entry["skill"] for entry in report["skipped_existing_name"]}
    assert skipped == {"tdd"}
    assert not (dest / "mp-tdd").exists()
    assert report["collisions"] == []


def test_rerun_is_idempotent_and_updates_changed_content(tmp_path):
    src = tmp_path / "src"
    skill_dir = make_skill(src, "engineering", "tdd")
    dest = tmp_path / "dest"

    first = install_mp_skills.install(src, dest, [])
    assert first["installed"] == ["mp-tdd"]

    second = install_mp_skills.install(src, dest, [])
    assert second["installed"] == []
    assert second["unchanged"] == ["mp-tdd"]

    (skill_dir / "SKILL.md").write_text(
        "---\nname: tdd\ndescription: tdd skill.\n---\n\n# tdd\n\nNew content.\n",
        encoding="utf-8",
    )
    third = install_mp_skills.install(src, dest, [])
    assert third["updated"] == ["mp-tdd"]
    assert "New content." in (dest / "mp-tdd" / "SKILL.md").read_text(encoding="utf-8")
    assert install_mp_skills.read_frontmatter_name(dest / "mp-tdd" / "SKILL.md") == "mp-tdd"


def test_dedupe_skips_when_prefixed_name_exists_externally(tmp_path):
    src = tmp_path / "src"
    make_skill(src, "engineering", "tdd")
    external = tmp_path / "vendored-skills"
    make_existing(external, "mp-tdd")
    dest = tmp_path / "dest"

    report = install_mp_skills.install(src, dest, [external])

    assert report["installed"] == []
    assert [e["skill"] for e in report["skipped_existing_prefixed"]] == ["mp-tdd"]
    assert not (dest / "mp-tdd").exists()
    assert report["collisions"] == []


def test_dedupe_removes_stale_local_copy_shadowed_by_external(tmp_path):
    src = tmp_path / "src"
    make_skill(src, "engineering", "tdd")
    external = tmp_path / "vendored-skills"
    make_existing(external, "mp-tdd")
    dest = tmp_path / "dest"
    make_existing(dest, "mp-tdd")  # stale copy from an earlier install

    report = install_mp_skills.install(src, dest, [external, dest])

    assert report["removed_duplicate"] == ["mp-tdd"]
    assert not (dest / "mp-tdd").exists()
    assert report["collisions"] == []


def test_collision_detection_reports_duplicate_names(tmp_path):
    search_a = tmp_path / "a"
    search_b = tmp_path / "b"
    make_existing(search_a, "dupe")
    make_existing(search_b, "dupe")
    src = tmp_path / "src"
    (src / "skills").mkdir(parents=True)
    dest = tmp_path / "dest"

    report = install_mp_skills.install(src, dest, [search_a, search_b])

    assert [c["skill"] for c in report["collisions"]] == ["dupe"]


def test_cli_writes_report_and_exit_codes(tmp_path, capsys):
    src = tmp_path / "src"
    make_skill(src, "engineering", "tdd")
    dest = tmp_path / "dest"
    report_path = tmp_path / "report.json"

    rc = install_mp_skills.main(
        [
            "--source", str(src),
            "--dest", str(dest),
            "--search-dir", str(dest),
            "--report", str(report_path),
        ]
    )

    assert rc == 0
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["installed"] == ["mp-tdd"]
    printed = json.loads(capsys.readouterr().out)
    assert printed["installed"] == ["mp-tdd"]


def test_nested_and_flat_source_layouts(tmp_path):
    src = tmp_path / "src"
    make_skill(src, "engineering", "tdd")
    make_skill(src, "", "flat-skill")
    dest = tmp_path / "dest"

    report = install_mp_skills.install(src, dest, [])

    assert sorted(report["installed"]) == ["mp-flat-skill", "mp-tdd"]
