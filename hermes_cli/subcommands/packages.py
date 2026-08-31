"""``hermes packages`` subcommand parser.

Handler injected to avoid importing ``main`` (same pattern as the other
extracted subcommand parsers).
"""

from __future__ import annotations

from typing import Callable


def build_packages_parser(subparsers, *, cmd_packages: Callable) -> None:
    """Attach the ``packages`` subcommand to ``subparsers``."""
    packages_parser = subparsers.add_parser(
        "packages",
        help="Inventory, lint, and migrate source-owned plugin/skill/MCP packages",
        description=(
            "Deterministic tooling over the canonical package contract "
            "(agent/package_contract.py): census every source-owned package "
            "under plugins/, skills/, optional-skills/, and optional-mcps/, "
            "lint them against the contract, and apply mechanical migrations."
        ),
    )
    packages_subparsers = packages_parser.add_subparsers(dest="packages_action")

    inventory = packages_subparsers.add_parser(
        "inventory",
        help="Machine-readable census of every source-owned package",
    )
    inventory.add_argument(
        "--json", action="store_true", help="Emit the full inventory as JSON"
    )
    inventory.add_argument(
        "--root", help="Repo root to scan (default: this checkout)"
    )

    lint = packages_subparsers.add_parser(
        "lint",
        help="Validate every source-owned package against the contract",
    )
    lint.add_argument(
        "--strict", action="store_true",
        help="Exit non-zero on warnings too (default: errors only)",
    )
    lint.add_argument(
        "--root", help="Repo root to scan (default: this checkout)"
    )

    migrate = packages_subparsers.add_parser(
        "migrate",
        help="Apply the documented mechanical contract migrations",
    )
    migrate.add_argument(
        "--check", action="store_true",
        help="Report what would change without writing (exit 1 if anything would)",
    )
    migrate.add_argument(
        "--root", help="Repo root to scan (default: this checkout)"
    )

    packages_parser.set_defaults(func=cmd_packages)
