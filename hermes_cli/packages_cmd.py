"""``hermes packages`` — deterministic inventory / lint / migrate over the
source-owned package trees (plugins/, skills/, optional-skills/,
optional-mcps/).

Thin CLI over the canonical package contract
(:mod:`agent.package_contract`). All three verbs are deterministic: stable
ordering, stable JSON (sorted keys), no timestamps.

- ``inventory`` — machine-readable census of every source-owned package
  (the contract-gap matrix: counts, ownership, family, manifest shape,
  entrypoints, dependencies, unknown fields).
- ``lint``      — contract findings; exit 1 when errors are present
  (``--strict``: warnings fail too). This is the fail-closed authoring
  boundary; runtime loaders keep their documented warn-and-continue
  behavior.
- ``migrate``   — apply the documented mechanical migrations (see
  ``_MIGRATIONS``); everything non-mechanical is reported for manual
  migration. Idempotent; ``--check`` reports without writing (exit 1 when
  changes would be made).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from agent.package_contract import (
    CONTRACT_VERSION,
    Finding,
    PLUGIN_CATEGORY_KINDS,
    enumerate_source_packages,
    record_to_dict,
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _print_findings(findings: List[Finding]) -> None:
    for f in findings:
        badge = "✗" if f.severity == "error" else "⚠"
        print(f"{badge} [{f.rule}] {f.package}: {f.message}")


def cmd_packages_inventory(args) -> int:
    root = Path(getattr(args, "root", None) or _repo_root())
    records, findings = enumerate_source_packages(root)
    if getattr(args, "json", False):
        payload = {
            "contract_version": CONTRACT_VERSION,
            "root": str(root),
            "counts": _counts(records),
            "packages": [record_to_dict(r) for r in records],
            "findings": [f.to_dict() for f in findings],
        }
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
        return 0
    counts = _counts(records)
    print(f"Package contract v{CONTRACT_VERSION} — {counts['total']} source-owned packages")
    for family in ("plugin", "skill", "mcp"):
        print(f"  {family:7} {counts[family]}")
    print()
    for rec in records:
        line = f"{rec.family:7} {rec.id:44} v{rec.version or '?':10} {rec.kind or '-':14} {rec.path}"
        print(line.rstrip())
    if findings:
        print()
        _print_findings(findings)
    return 0


def _counts(records) -> dict:
    counts = {"plugin": 0, "skill": 0, "mcp": 0}
    for rec in records:
        counts[rec.family] = counts.get(rec.family, 0) + 1
    counts["total"] = len(records)
    return counts


def cmd_packages_lint(args) -> int:
    root = Path(getattr(args, "root", None) or _repo_root())
    _, findings = enumerate_source_packages(root)
    errors = [f for f in findings if f.severity == "error"]
    warnings = [f for f in findings if f.severity == "warning"]
    _print_findings(findings)
    if not findings:
        print("✓ all source-owned packages satisfy the package contract "
              f"(v{CONTRACT_VERSION})")
    if errors:
        return 1
    if warnings and getattr(args, "strict", False):
        return 1
    return 0


# ── migrate ───────────────────────────────────────────────────────────────
#
# Mechanical migrations only: transforms whose target value is derivable
# from the tree itself. Anything needing human judgment (author credit,
# folding dead trigger lists into the body) is reported, not guessed.
# Text-level line edits — never a YAML re-dump — so manifest formatting,
# comments, and key order survive.


def _migrate_plugin_kind(root: Path, rec_path: str, expected: str) -> Optional[str]:
    """Append/declare the family-derived ``kind:`` in a plugin.yaml."""
    manifest = root / rec_path / "plugin.yaml"
    text = manifest.read_text(encoding="utf-8")
    if any(line.startswith("kind:") for line in text.splitlines()):
        return None  # declared but mismatched — not mechanical, report only
    new = text.rstrip("\n") + f"\nkind: {expected}\n"
    manifest.write_text(new, encoding="utf-8")
    return f"{rec_path}/plugin.yaml: declared kind: {expected}"


def _migrate_skill_rename(
    root: Path, rec_path: str, old_key: str, new_key: str
) -> Optional[str]:
    """Rename a frontmatter key in-place when the line shape is unambiguous."""
    manifest = root / rec_path / "SKILL.md"
    text = manifest.read_text(encoding="utf-8")
    head, sep, tail = text.partition("\n---")
    needle = f"{old_key}:"
    lines = head.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.lstrip().startswith(needle)]
    if len(hits) != 1:
        return None
    i = hits[0]
    lines[i] = lines[i].replace(needle, f"{new_key}:", 1)
    manifest.write_text("".join(lines) + sep + tail, encoding="utf-8")
    return f"{rec_path}/SKILL.md: renamed {old_key} → {new_key}"


def _migrate_skill_drop(root: Path, rec_path: str, key: str) -> Optional[str]:
    """Drop a single-line dead frontmatter field."""
    manifest = root / rec_path / "SKILL.md"
    text = manifest.read_text(encoding="utf-8")
    head, sep, tail = text.partition("\n---")
    lines = head.splitlines(keepends=True)
    hits = [
        i for i, line in enumerate(lines)
        if line.startswith(f"{key}:")
    ]
    if len(hits) != 1:
        return None
    i = hits[0]
    # Only drop when the value is inline (single line) — block values need
    # human judgment about where the content should live.
    if i + 1 < len(lines) and (lines[i + 1].startswith((" ", "\t", "-"))):
        return None
    del lines[i]
    manifest.write_text("".join(lines) + sep + tail, encoding="utf-8")
    return f"{rec_path}/SKILL.md: dropped dead field {key!r}"


def cmd_packages_migrate(args) -> int:
    root = Path(getattr(args, "root", None) or _repo_root())
    check = bool(getattr(args, "check", False))
    records, findings = enumerate_source_packages(root)
    path_by_ref = {rec.ref: rec.path for rec in records}

    planned: List[Tuple[str, Callable[[], Optional[str]]]] = []
    manual: List[Finding] = []
    for f in findings:
        pkg_path = path_by_ref.get(f.package)
        if f.rule == "kind-family-mismatch" and pkg_path:
            category = pkg_path.split("/", 2)[1] if pkg_path.count("/") >= 1 else ""
            expected = PLUGIN_CATEGORY_KINDS.get(category)
            if expected:
                planned.append(
                    (f"{pkg_path}: declare kind: {expected}",
                     lambda p=pkg_path, e=expected: _migrate_plugin_kind(root, p, e))
                )
                continue
        if f.rule == "unknown-metadata" and "upstream_skill" in f.message and pkg_path:
            planned.append(
                (f"{pkg_path}: rename upstream_skill → upstream",
                 lambda p=pkg_path: _migrate_skill_rename(
                     root, p, "upstream_skill", "upstream"))
            )
            continue
        if f.rule == "unknown-field" and pkg_path and (
            "'title'" in f.message or "'authors'" in f.message
        ):
            key = "title" if "'title'" in f.message else "authors"
            planned.append(
                (f"{pkg_path}: drop dead field {key}",
                 lambda p=pkg_path, k=key: _migrate_skill_drop(root, p, k))
            )
            continue
        manual.append(f)

    if not planned and not manual:
        print("✓ nothing to migrate — all source-owned packages satisfy the "
              f"package contract (v{CONTRACT_VERSION})")
        return 0

    rc = 0
    for label, apply in planned:
        if check:
            print(f"would migrate: {label}")
            rc = 1
        else:
            result = apply()
            print(f"migrated: {result}" if result else f"skipped (not mechanical): {label}")
    if manual:
        print("\nNeeds manual migration (see docs/…/package-contract.md):")
        _print_findings(manual)
        rc = max(rc, 1 if check else 0)
    return rc


def cmd_packages(args) -> int:
    action = getattr(args, "packages_action", None)
    if action == "inventory":
        return cmd_packages_inventory(args)
    if action == "lint":
        return cmd_packages_lint(args)
    if action == "migrate":
        return cmd_packages_migrate(args)
    print("usage: hermes packages {inventory|lint|migrate} [--json|--strict|--check]",
          file=sys.stderr)
    return 2
