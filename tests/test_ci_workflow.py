"""`ci.yml` runs once per change; a stale run only cancels its own pull request.

The workflow is read as text: no YAML parser is installed in the test
environment, and the trigger and concurrency lines are exactly what decides how
many full builds one batch of commits starts.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"


def top_block(text: str, key: str) -> list[str]:
    """The indented lines of one top-level `key:` block."""
    lines = text.splitlines()
    try:
        start = lines.index(f"{key}:")
    except ValueError:
        raise AssertionError(f"no top-level `{key}:` in the workflow") from None
    block: list[str] = []
    for line in lines[start + 1:]:
        if line and not line.startswith(" "):
            break
        block.append(line)
    return block


def triggers(text: str) -> dict[str, list[str]]:
    """`on:` entries, each with the settings nested under it."""
    found: dict[str, list[str]] = {}
    current: str | None = None
    for line in top_block(text, "on"):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if line.startswith("    "):
            assert current is not None
            found[current].append(stripped)
        else:
            current = stripped.rstrip(":")
            found[current] = []
    return found


def setting(block: list[str], name: str) -> str:
    prefix = f"{name}:"
    for line in block:
        stripped = line.strip()
        if stripped.startswith(prefix):
            return stripped[len(prefix):].strip()
    raise AssertionError(f"no `{name}:` in the block")


def test_ci_runs_on_pull_requests_main_and_when_the_release_calls_it():
    found = triggers(CI.read_text(encoding="utf-8"))
    assert set(found) == {"push", "pull_request", "workflow_call"}
    # A tag starts release.yml only, and release.yml calls this workflow itself,
    # so one tagged commit must not start a second, standalone copy of these jobs.
    assert found["push"] == ["branches: [main]"]
    # No path or branch filter may silently drop a check the PR is judged by.
    assert found["pull_request"] == [] and found["workflow_call"] == []


def test_only_a_superseded_pull_request_run_is_cancelled():
    block = top_block(CI.read_text(encoding="utf-8"), "concurrency")
    group = setting(block, "group")
    # One group per pull request cancels its own earlier run; everything else
    # (a main push, a reusable-workflow run) takes its own run id, so no run can
    # cancel another and none cancels itself.
    assert "github.event.pull_request.number" in group
    assert "github.run_id" in group
    assert setting(block, "cancel-in-progress") == "${{ github.event_name == 'pull_request' }}"


def test_a_tag_push_starts_the_release_workflow_only():
    found = triggers(RELEASE.read_text(encoding="utf-8"))
    assert list(found) == ["push"] and found["push"] == ["tags: ['v*']"]
    # ...and that workflow runs these jobs itself, once per tagged commit.
    assert "uses: ./.github/workflows/ci.yml" in RELEASE.read_text(encoding="utf-8")
