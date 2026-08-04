#!/usr/bin/env python3
"""Install mattpocock/skills into a Hermes profile skills dir, namespaced mp-*.

Copies every skill (a directory containing SKILL.md) from a checkout of
github.com/mattpocock/skills into ``<dest>/<prefix><name>/``, rewriting the
frontmatter ``name:`` to the prefixed name so the loaded skill name matches
its directory.

Dedupe rules, in order:
  1. A source skill whose *unprefixed* name is already visible anywhere in
     the profile's skill search path (profile skills dir + external dirs)
     is skipped — the existing skill wins and the mp- copy would double-load
     the same playbook under two names.
  2. A source skill whose *prefixed* name (mp-<name>) is already provided by
     a directory outside the install dest (e.g. an external vendored skills
     dir) is skipped, and any stale copy inside dest is removed — a duplicate
     name makes skill_view() fail as ambiguous, so exactly one provider may
     remain and the external one wins.
  3. Re-running against an already-installed mp- skill overwrites it only
     when content changed (idempotent update), otherwise it is left alone.
  4. After installing, the run fails (exit 1) if any skill name resolves to
     more than one directory across the search path.

Categories in ``--exclude-category`` (default: in-progress) are not
installed; the repo marks them as unfinished.

Usage:
  install_mp_skills.py --source /path/to/mattpocock-skills \
      --dest ~/.hermes/profiles/vector/skills/mattpocock \
      --search-dir ~/.hermes/profiles/vector/skills \
      --search-dir /Users/you/.agentskills \
      [--prefix mp-] [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import filecmp
import json
import shutil
import sys
from pathlib import Path

DEFAULT_PREFIX = "mp-"
DEFAULT_EXCLUDED_CATEGORIES = ("in-progress",)
MAX_SCAN_DEPTH = 3  # skills/<skill>/SKILL.md and skills/<category>/<skill>/SKILL.md


def read_frontmatter_name(skill_md: Path) -> str | None:
    """Return the frontmatter ``name:`` value, or None if unparseable."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip().strip("'\"") or None
    return None


def rewrite_frontmatter_name(skill_md: Path, new_name: str) -> None:
    text = skill_md.read_text(encoding="utf-8")
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{skill_md} has no frontmatter block")
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            break
        if line.startswith("name:"):
            lines[i] = f"name: {new_name}\n"
            skill_md.write_text("".join(lines), encoding="utf-8")
            return
    raise ValueError(f"{skill_md} frontmatter has no name: field")


def discover_source_skills(
    source_root: Path, excluded_categories: tuple[str, ...]
) -> list[tuple[str, str, Path]]:
    """Yield (category, name, skill_dir) for every skill in the source repo."""
    skills_root = source_root / "skills" if (source_root / "skills").is_dir() else source_root
    found = []
    for skill_md in sorted(skills_root.glob("*/*/SKILL.md")):
        skill_dir = skill_md.parent
        category = skill_dir.parent.name
        if category in excluded_categories:
            continue
        name = read_frontmatter_name(skill_md) or skill_dir.name
        found.append((category, name, skill_dir))
    # Also allow a flat layout (skills/<name>/SKILL.md).
    for skill_md in sorted(skills_root.glob("*/SKILL.md")):
        skill_dir = skill_md.parent
        if skill_dir.name in excluded_categories:
            continue
        name = read_frontmatter_name(skill_md) or skill_dir.name
        found.append(("", name, skill_dir))
    return found


def visible_skill_dirs(search_dirs: list[Path]) -> dict[str, list[Path]]:
    """Map every visible skill name -> the SKILL.md dirs providing it."""
    names: dict[str, list[Path]] = {}
    for root in search_dirs:
        if not root.is_dir():
            continue
        pattern_depths = ["*/SKILL.md", "*/*/SKILL.md", "*/*/*/SKILL.md"][: MAX_SCAN_DEPTH]
        seen_dirs: set[Path] = set()
        for pattern in pattern_depths:
            for skill_md in root.glob(pattern):
                skill_dir = skill_md.parent
                if skill_dir in seen_dirs:
                    continue
                seen_dirs.add(skill_dir)
                name = read_frontmatter_name(skill_md) or skill_dir.name
                names.setdefault(name, []).append(skill_dir)
    return names


def dirs_equal(a: Path, b: Path) -> bool:
    cmp = filecmp.dircmp(a, b)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(
        dirs_equal(a / sub, b / sub) for sub in cmp.common_dirs
    )


def install(
    source_root: Path,
    dest: Path,
    search_dirs: list[Path],
    prefix: str = DEFAULT_PREFIX,
    excluded_categories: tuple[str, ...] = DEFAULT_EXCLUDED_CATEGORIES,
    dry_run: bool = False,
) -> dict:
    """Install prefixed skills into dest; return a machine-readable report."""
    report: dict = {
        "installed": [],
        "updated": [],
        "unchanged": [],
        "skipped_existing_name": [],
        "skipped_existing_prefixed": [],
        "removed_duplicate": [],
        "skipped_category": sorted(excluded_categories),
        "collisions": [],
    }
    visible = visible_skill_dirs(search_dirs)

    def _outside_dest(d: Path) -> bool:
        try:
            return not d.resolve().is_relative_to(dest.resolve())
        except OSError:
            return True

    for _category, name, skill_dir in discover_source_skills(source_root, excluded_categories):
        target_name = f"{prefix}{name}"
        target_dir = dest / target_name

        providers = [
            d
            for d in visible.get(name, [])
            if not d.name.startswith(prefix) and _outside_dest(d)
        ]
        if providers:
            report["skipped_existing_name"].append(
                {"skill": name, "existing": [str(p) for p in providers]}
            )
            continue

        prefixed_providers = [
            d for d in visible.get(target_name, []) if _outside_dest(d)
        ]
        if prefixed_providers:
            report["skipped_existing_prefixed"].append(
                {"skill": target_name, "existing": [str(p) for p in prefixed_providers]}
            )
            if target_dir.is_dir():
                if not dry_run:
                    shutil.rmtree(target_dir)
                report["removed_duplicate"].append(target_name)
            continue

        if target_dir.is_dir():
            staged = _stage(skill_dir, target_name)
            try:
                if dirs_equal(staged, target_dir):
                    report["unchanged"].append(target_name)
                    continue
                if not dry_run:
                    shutil.rmtree(target_dir)
                    shutil.copytree(staged, target_dir)
                report["updated"].append(target_name)
            finally:
                shutil.rmtree(staged.parent, ignore_errors=True)
            continue

        if not dry_run:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copytree(skill_dir, target_dir)
            rewrite_frontmatter_name(target_dir / "SKILL.md", target_name)
        report["installed"].append(target_name)

    # Post-condition: no skill name may resolve twice across the search path.
    post = visible_skill_dirs(list(dict.fromkeys(search_dirs + [dest])))
    for name, dirs in sorted(post.items()):
        unique = sorted({str(d) for d in dirs})
        if len(unique) > 1:
            report["collisions"].append({"skill": name, "dirs": unique})
    return report


def _stage(skill_dir: Path, target_name: str) -> Path:
    """Copy a source skill to a temp dir with the prefixed name applied."""
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="mp-skill-stage-"))
    staged = tmp / target_name
    shutil.copytree(skill_dir, staged)
    rewrite_frontmatter_name(staged / "SKILL.md", target_name)
    return staged


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--dest", required=True, type=Path)
    parser.add_argument("--search-dir", action="append", default=[], type=Path)
    parser.add_argument("--prefix", default=DEFAULT_PREFIX)
    parser.add_argument(
        "--exclude-category", action="append", default=list(DEFAULT_EXCLUDED_CATEGORIES)
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    report = install(
        args.source.expanduser(),
        args.dest.expanduser(),
        [p.expanduser() for p in args.search_dir],
        prefix=args.prefix,
        excluded_categories=tuple(dict.fromkeys(args.exclude_category)),
        dry_run=args.dry_run,
    )
    output = json.dumps(report, indent=2)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 1 if report["collisions"] else 0


if __name__ == "__main__":
    sys.exit(main())
