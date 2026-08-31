import importlib.util
import json
from pathlib import Path

import pytest

repo_root = Path(__file__).parent.parent.parent
script_path = repo_root / "scripts" / "vector" / "apply_vector_orchestrator_config.py"

spec = importlib.util.spec_from_file_location(
    "apply_vector_orchestrator_config", str(script_path)
)
applier = importlib.util.module_from_spec(spec)
spec.loader.exec_module(applier)

BASE_CONFIG = """\
# top comment must survive
model:
  provider: openai-codex
toolsets:
  - hermes-cli
agent:
  disabled_toolsets:
    - kanban
voice:
  auto_tts: true
  beep_enabled: true
stt:
  enabled: true
platform_toolsets:
  cli:
    - terminal
    - file
  telegram:
    - terminal
# trailing comment must survive
"""


@pytest.fixture
def config_file(tmp_path):
    path = tmp_path / "config.yaml"
    path.write_text(BASE_CONFIG, encoding="utf-8")
    return path


def test_adds_orchestrator_toolsets_and_keeps_existing(config_file):
    plan = applier.run(config_file)

    assert plan["changed"] is True
    text = config_file.read_text(encoding="utf-8")
    from ruamel.yaml import YAML

    config = YAML().load(text)
    for platform in ("cli", "telegram"):
        enabled = list(config["platform_toolsets"][platform])
        assert "terminal" in enabled
        for toolset in applier.ORCHESTRATOR_TOOLSETS:
            assert toolset in enabled, (platform, toolset)
    # kanban orchestrator gate reads the top-level toolsets list
    top_level = list(config["toolsets"])
    assert "hermes-cli" in top_level
    assert "kanban" in top_level
    # a disabled_toolsets entry would silently veto the enablement
    assert "kanban" not in list(config["agent"]["disabled_toolsets"])


def test_silence_invariants_enforced(config_file):
    applier.run(config_file)

    from ruamel.yaml import YAML

    config = YAML().load(config_file.read_text(encoding="utf-8"))
    assert config["voice"]["auto_tts"] is False
    assert config["voice"]["beep_enabled"] is False
    assert config["wake_word"]["enabled"] is False
    assert config["discord"]["voice_fx"]["enabled"] is False
    assert config["stt"]["enabled"] is True
    assert config["kanban"]["dispatch_in_gateway"] is True
    assert config["delegation"]["orchestrator_enabled"] is True


def test_comments_survive_and_backup_written(config_file):
    plan = applier.run(config_file)

    text = config_file.read_text(encoding="utf-8")
    assert "# top comment must survive" in text
    assert "# trailing comment must survive" in text
    backup = Path(plan["backup"])
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == BASE_CONFIG


def test_idempotent_second_run_changes_nothing(config_file):
    applier.run(config_file)
    after_first = config_file.read_text(encoding="utf-8")

    plan = applier.run(config_file)

    assert plan["changed"] is False
    assert "backup" not in plan
    assert config_file.read_text(encoding="utf-8") == after_first


def test_dry_run_reports_but_does_not_write(config_file):
    plan = applier.run(config_file, dry_run=True)

    assert plan["changed"] is True
    assert plan["dry_run"] is True
    assert config_file.read_text(encoding="utf-8") == BASE_CONFIG
    assert set(plan["toolsets_added"]) == {"cli", "telegram"}
    assert plan["settings_set"]["voice.auto_tts"] is False


def test_orchestrator_toolsets_exist_in_toolsets_registry():
    import sys

    sys.path.insert(0, str(repo_root))
    try:
        import toolsets

        for name in applier.ORCHESTRATOR_TOOLSETS:
            assert name in toolsets.TOOLSETS, name
    finally:
        sys.path.remove(str(repo_root))


def test_cli_report(config_file, tmp_path, capsys):
    report_path = tmp_path / "plan.json"

    rc = applier.main(["--config", str(config_file), "--dry-run", "--report", str(report_path)])

    assert rc == 0
    saved = json.loads(report_path.read_text(encoding="utf-8"))
    assert saved["dry_run"] is True
    printed = json.loads(capsys.readouterr().out)
    assert printed["changed"] is True
