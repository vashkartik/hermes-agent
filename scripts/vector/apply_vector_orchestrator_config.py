#!/usr/bin/env python3
"""Apply the VECTOR daily-orchestrator config profile to a Hermes config.yaml.

Enables the orchestrator capability set (Kanban, delegation, cron, web,
media, computer-use, voice) on the ``cli`` and ``telegram`` platforms while
enforcing the silence invariants (no unattended audio). Idempotent; preserves
YAML comments and key order via ruamel round-trip; writes a timestamped
backup next to the config before modifying it.

Usage:
  apply_vector_orchestrator_config.py --config ~/.hermes/profiles/vector/config.yaml \
      [--dry-run] [--report out.json]
"""

from __future__ import annotations

import argparse
import datetime
import io
import json
import shutil
import sys
from pathlib import Path

from ruamel.yaml import YAML

# Toolsets the daily orchestrator needs, on top of whatever the profile
# already enables. Names must exist in toolsets.TOOLSETS. `web` covers both
# web_search and web_extract; `video`/`video_gen` are default-off and must be
# listed explicitly.
ORCHESTRATOR_TOOLSETS = (
    "browser",        # computer-use: drive a real browser
    "computer_use",   # computer-use: desktop-level control (needs cua-driver)
    "cronjob",        # schedule jobs
    "delegation",     # delegate_task subagents
    "image_gen",      # media: generate images
    "kanban",         # multi-agent board
    "tts",            # voice output tools (on-demand only; see invariants)
    "video",          # media: video handling
    "video_gen",      # media: generate video
    "vision",         # media: analyze images
    "web",            # web search + extract
)
PLATFORMS = ("cli", "telegram")

# The kanban orchestrator gate reads the TOP-LEVEL toolsets list, not
# platform_toolsets (kanban_tools._profile_has_kanban_toolset).
TOP_LEVEL_TOOLSETS = ("kanban",)

# Dotted-path invariants. Voice capability stays ON (stt enabled, tts tools
# available) but nothing may speak unattended. Note: per-chat /voice modes
# persisted in gateway_voice_mode.json override voice.auto_tts — audit that
# file separately for a hard no-unattended-audio guarantee.
INVARIANTS = {
    "voice.auto_tts": False,            # never speak replies automatically
    "voice.beep_enabled": False,        # no unattended beeps
    "wake_word.enabled": False,         # no always-on microphone
    "discord.voice_fx.enabled": False,  # no ambient/ack audio
    "stt.enabled": True,                # voice input stays available
    "kanban.dispatch_in_gateway": True,
    "delegation.orchestrator_enabled": True,
}


def _get_path(config: dict, dotted: str):
    node = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(config: dict, dotted: str, value) -> None:
    parts = dotted.split(".")
    node = config
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value


def plan_changes(config: dict) -> dict:
    """Compute the delta this run would apply. Pure; does not mutate."""
    plan: dict = {
        "toolsets_added": {},
        "top_level_toolsets_added": [],
        "disabled_toolsets_removed": [],
        "settings_set": {},
    }
    platform_toolsets = config.get("platform_toolsets") or {}
    for platform in PLATFORMS:
        existing = [str(t) for t in (platform_toolsets.get(platform) or [])]
        missing = [t for t in ORCHESTRATOR_TOOLSETS if t not in existing]
        if missing:
            plan["toolsets_added"][platform] = missing

    top_level = [str(t) for t in (config.get("toolsets") or [])]
    plan["top_level_toolsets_added"] = [
        t for t in TOP_LEVEL_TOOLSETS if t not in top_level
    ]

    # agent.disabled_toolsets silently wins over platform_toolsets — an
    # orchestrator toolset left there would never take effect.
    disabled = [str(t) for t in (_get_path(config, "agent.disabled_toolsets") or [])]
    plan["disabled_toolsets_removed"] = [
        t for t in disabled if t in ORCHESTRATOR_TOOLSETS or t in TOP_LEVEL_TOOLSETS
    ]

    for dotted, wanted in INVARIANTS.items():
        if _get_path(config, dotted) != wanted:
            plan["settings_set"][dotted] = wanted
    return plan


def apply_changes(config: dict, plan: dict) -> None:
    platform_toolsets = config.setdefault("platform_toolsets", {})
    for platform, missing in plan["toolsets_added"].items():
        existing = platform_toolsets.setdefault(platform, [])
        for toolset in missing:
            existing.append(toolset)
    if plan["top_level_toolsets_added"]:
        top_level = config.setdefault("toolsets", [])
        for toolset in plan["top_level_toolsets_added"]:
            top_level.append(toolset)
    if plan["disabled_toolsets_removed"]:
        disabled = _get_path(config, "agent.disabled_toolsets")
        for toolset in plan["disabled_toolsets_removed"]:
            disabled.remove(toolset)
    for dotted, value in plan["settings_set"].items():
        _set_path(config, dotted, value)


def run(config_path: Path, dry_run: bool = False, now: datetime.datetime | None = None) -> dict:
    yaml = YAML()  # round-trip: preserves comments, order, anchors
    yaml.preserve_quotes = True
    yaml.width = 4096
    with config_path.open("r", encoding="utf-8") as fh:
        config = yaml.load(fh)
    if config is None:
        raise SystemExit(f"empty or unreadable config: {config_path}")

    plan = plan_changes(config)
    changed = bool(
        plan["toolsets_added"]
        or plan["top_level_toolsets_added"]
        or plan["disabled_toolsets_removed"]
        or plan["settings_set"]
    )
    plan["changed"] = changed
    plan["dry_run"] = dry_run
    if not changed or dry_run:
        return plan

    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.bak-orchestrator-{stamp}")
    shutil.copy2(config_path, backup)
    plan["backup"] = str(backup)

    apply_changes(config, plan)
    buf = io.StringIO()
    yaml.dump(config, buf)
    config_path.write_text(buf.getvalue(), encoding="utf-8")
    return plan


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args(argv)

    plan = run(args.config.expanduser(), dry_run=args.dry_run)
    output = json.dumps(plan, indent=2)
    if args.report:
        args.report.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
