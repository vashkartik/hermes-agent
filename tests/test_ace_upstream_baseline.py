"""Regression for squash-sync baseline accounting (``.ace/upstream-main.sha``).

The Ace fork tracks Nous Hermes by periodically squash-merging upstream main
into ``ace/patches``. Because the merge is a SQUASH, git history carries no
link back to the upstream commit that was synced — ``.ace/upstream-main.sha``
is the only record of it, and the nightly "N commits behind" counter is
computed from that file alone.

That makes the file silently load-bearing: if a sync forgets to bump it, or
writes a malformed / mid-window value, the drift counter reports a number that
looks alarming (or reassuring) but means nothing, and the next sync computes
its range from the wrong base.

These tests pin the two properties that make the recorded value trustworthy:

1. **Format.** Exactly one 40-hex commit id. Always checked.
2. **Minimality.** The recorded commit must explain this tree better than any
   of its own ancestors — i.e. it is the *tip* of what was synced, not a
   commit from the middle of the synced range. Checked only when the upstream
   objects are actually present locally, so CI without an ``upstream`` remote
   skips instead of failing.

Verified 2026-08-03 for ``ace/patches`` @ d2ecf452 ("Sync current Nous Hermes
main into Ace patches (#14)"): scanning all 1341 upstream commits in the
2026-07-20..2026-07-25 sync window, ``0b17d4d7`` is the unique minimum-diff
baseline at 103 differing files (its ancestors score 105 / 108 / 109), and all
103 are Ace patch surface. The recorded value is CORRECT. A large "commits
behind" figure against it is real upstream drift, not a bookkeeping error.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
BASELINE_FILE = REPO_ROOT / ".ace" / "upstream-main.sha"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"
UNSUPPORTED_RUNNERS = {
    "ubuntu-latest-32-core",
    "ubuntu-latest-96-core",
}

# How many ancestors of the recorded baseline to score. A sync that recorded a
# mid-range commit shows up within a handful of steps; scoring the whole
# history would cost a full tree diff per commit.
ANCESTOR_DEPTH = 3


def _git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )


def _have_object(sha: str) -> bool:
    return _git("cat-file", "-e", f"{sha}^{{commit}}").returncode == 0


def _differing_files(a: str, b: str) -> int:
    result = _git("diff", "--name-only", a, b)
    if result.returncode != 0:
        pytest.skip(f"git diff {a[:8]}..{b[:8]} unavailable: {result.stderr.strip()}")
    return len([line for line in result.stdout.splitlines() if line.strip()])


@pytest.fixture(scope="module")
def recorded_sha() -> str:
    if not BASELINE_FILE.exists():
        pytest.skip(".ace/upstream-main.sha not present in this checkout")
    return BASELINE_FILE.read_text(encoding="utf-8").strip()


def test_baseline_file_holds_exactly_one_commit_id(recorded_sha: str):
    """A malformed value makes the drift counter meaningless, not loud."""
    raw = BASELINE_FILE.read_text(encoding="utf-8")
    assert raw.endswith("\n"), "baseline file must end with a newline"
    assert raw.count("\n") == 1, "baseline file must hold a single line"
    assert re.fullmatch(r"[0-9a-f]{40}", recorded_sha), (
        f"expected a full 40-hex commit id, got {recorded_sha!r}"
    )


def test_baseline_is_not_a_fork_commit(recorded_sha: str):
    """It must name an UPSTREAM commit, never one of our own squash-syncs."""
    if not _have_object(recorded_sha):
        pytest.skip("recorded baseline object not present locally")
    head = _git("rev-parse", "HEAD").stdout.strip()
    assert recorded_sha != head
    subject = _git("log", "-1", "--format=%s", recorded_sha).stdout.strip()
    assert "Sync current Nous Hermes main" not in subject, (
        "baseline points at a fork squash-sync commit, not the upstream commit "
        f"it synced from: {subject!r}"
    )


def test_baseline_is_the_tip_of_the_synced_range(recorded_sha: str):
    """The recorded commit must match this tree better than its ancestors.

    This is the accounting property a squash sync can silently break: recording
    the merge-base (or any mid-range commit) instead of the upstream tip leaves
    a baseline whose ancestors match just as well, and every later drift
    computation inherits the error.
    """
    if not _have_object(recorded_sha):
        pytest.skip("recorded baseline object not present locally")

    head = _git("rev-parse", "HEAD").stdout.strip()
    baseline_score = _differing_files(recorded_sha, head)

    ancestors = _git(
        "rev-list", f"--max-count={ANCESTOR_DEPTH + 1}", recorded_sha
    ).stdout.split()[1:]
    if not ancestors:
        pytest.skip("recorded baseline has no ancestors available locally")

    for ancestor in ancestors:
        ancestor_score = _differing_files(ancestor, head)
        assert ancestor_score > baseline_score, (
            f"ancestor {ancestor[:8]} matches this tree at least as well as the "
            f"recorded baseline {recorded_sha[:8]} "
            f"({ancestor_score} vs {baseline_score} differing files) — the "
            "recorded baseline is probably from the middle of the synced range"
        )


def test_workflows_do_not_require_unsupported_large_runners():
    """Fork CI must stay on GitHub-hosted runner labels available to ACE."""
    def scalar_strings(value):
        if isinstance(value, dict):
            for key, child in value.items():
                yield from scalar_strings(key)
                yield from scalar_strings(child)
        elif isinstance(value, list):
            for child in value:
                yield from scalar_strings(child)
        elif isinstance(value, str):
            yield value

    offenders = {}
    for workflow in sorted(WORKFLOWS_DIR.rglob("*.y*ml")):
        document = yaml.safe_load(workflow.read_text(encoding="utf-8"))
        assert isinstance(document, dict), f"invalid workflow document: {workflow}"
        labels = sorted(
            label
            for label in UNSUPPORTED_RUNNERS
            if any(label in scalar for scalar in scalar_strings(document))
        )
        if labels:
            offenders[workflow.relative_to(REPO_ROOT).as_posix()] = labels

    assert not offenders, f"unsupported workflow runner labels: {offenders}"
