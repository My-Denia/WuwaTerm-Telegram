"""CI and the release hand the bundle to the pinned Core, not to a claim about it.

`build_delivery.py` can say a bundle is well formed, and it does self-check with
Core's `verify-bundle`. A bundle is only delivered when the consumer's own
`component_artifact.py` acquires and verifies it, from the release tag the lock
names. These tests read the workflows as text: the commands that do that work
have to stay in them.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import subprocess
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]
CI = ROOT / ".github" / "workflows" / "ci.yml"
RELEASE = ROOT / ".github" / "workflows" / "release.yml"
JOB = re.compile(r"^  ([A-Za-z0-9_-]+):$")
PUBLIC = ("bundle.json", "install-probe.json", "image.oci.tar", "source.tar")


def jobs(text: str) -> dict[str, str]:
    """The workflow body of each top-level two-space job key."""
    found: dict[str, str] = {}
    current: str | None = None
    body: list[str] = []
    for line in text.splitlines():
        match = JOB.match(line)
        if match:
            if current is not None:
                found[current] = "\n".join(body)
            current, body = match.group(1), []
        elif current is not None:
            body.append(line)
    if current is not None:
        found[current] = "\n".join(body)
    return found


def commands(body: str) -> list[str]:
    """One entry per shell command, with backslash continuations joined."""
    out: list[str] = []
    pending = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        pending = (pending + " " + line).strip()
        if pending.endswith("\\"):
            pending = pending[:-1].rstrip()
            continue
        out.append(" ".join(pending.split()))
        pending = ""
    if pending:
        out.append(" ".join(pending.split()))
    return out


def only(body: str, fragment: str) -> str:
    """The single command carrying a fragment; a missing or duplicated one fails."""
    matches = [line for line in commands(body) if fragment in line]
    assert len(matches) == 1, (fragment, matches)
    return matches[0]


def test_the_verifier_is_the_release_the_lock_names() -> None:
    lock = json.loads((ROOT / "core-lock.json").read_text(encoding="utf-8"))
    assert lock["repository"] == "My-Denia/WuwaTerm"
    assert lock["kind"] == "github-release"
    for path in (CI, RELEASE):
        text = " ".join(commands(path.read_text(encoding="utf-8")))
        # The repository is checked, not echoed: a lock naming another owner
        # would otherwise be cloned and trusted.
        assert 'assert lock["repository"] == "My-Denia/WuwaTerm"' in text
        assert "refs/tags/$CORE_TAG" in text
        assert 'test "$(git -C "$RUNNER_TEMP/core-source" rev-parse HEAD)" = "$CORE_COMMIT"' in text


def test_ci_builds_a_bundle_and_hands_it_to_the_pinned_core() -> None:
    handoff = jobs(CI.read_text(encoding="utf-8"))["delivery-handoff"]
    assert "needs: package" in handoff
    assert "contents: read" in handoff
    assert "actions: read" in handoff
    assert "write" not in handoff
    text = " ".join(commands(handoff))
    assert 'fetch_core.py --lock core-lock.json --output-dir "$RUNNER_TEMP/core-wheel"' in text
    assert '--core-tool "$RUNNER_TEMP/core-source/scripts/component_artifact.py" 9>&-' in text
    assert "docker pull python:3.11-slim" in text
    acquire = only(handoff, '"$CORE_TOOL" acquire')
    assert '--bundle "${attempts[0]}/bundle/bundle.json" --root "$RUNNER_TEMP/deploy-store" 9>&-' in acquire
    verify = only(handoff, '"$CORE_TOOL" verify')
    assert '--receipt "$RUNNER_TEMP/deploy-store/.deployments/artifacts/acquisitions/$acquisition.json"' in verify


def test_release_seals_with_the_pinned_core_and_proves_the_public_files_acquire() -> None:
    image = jobs(RELEASE.read_text(encoding="utf-8"))["image-bundle"]
    text = " ".join(commands(image))
    assert '--core-tool "$RUNNER_TEMP/core-source/scripts/component_artifact.py" 9>&-' in text
    # Only the six published files rebuild the attempt: no wheels artifact, no
    # attempt directory, no workspace from the build.
    assert 'stage="$RUNNER_TEMP/public-handoff/attempts/$attempt_id"' in text
    assert "cp release-assets/bundle.json release-assets/install-probe.json " \
        "release-assets/image.oci.tar \"$stage/bundle/\"" in text
    assert 'cp release-assets/source.tar "$stage/inputs/"' in text
    assert 'cp dist/*.whl artifacts/*.whl "$stage/inputs/wheels/"' in text
    acquire = only(image, '"$CORE_TOOL" acquire')
    assert '--bundle "$stage/bundle/bundle.json" --root "$RUNNER_TEMP/public-store" 9>&-' in acquire
    verify = only(image, '"$CORE_TOOL" verify')
    assert '--receipt "$RUNNER_TEMP/public-store/.deployments/artifacts/acquisitions/$acquisition.json"' in verify


def test_the_release_publishes_one_sums_file_over_every_published_file() -> None:
    publish = jobs(RELEASE.read_text(encoding="utf-8"))["publish"]
    text = " ".join(commands(publish))
    assert "mkdir -p public" in text
    assert "cp dist/*.whl dist/*.tar.gz release-assets/bundle.json release-assets/install-probe.json " \
        "release-assets/image.oci.tar release-assets/source.tar public/" in text
    assert "sha256sum *.whl *.tar.gz bundle.json install-probe.json image.oci.tar source.tar " \
        "> SHA256SUMS" in text
    assert "(cd public && sha256sum -c SHA256SUMS)" in text
    assert 'test "$(wc -l < public/SHA256SUMS)" -eq 6' in text
    # Core's deploy/setup.sh greps a basename line and feeds it to sha256sum -c.
    # The double space is sha256sum's own format, so it is read from the raw body.
    assert 'test "$(grep -c "  $wheel\\$" public/SHA256SUMS)" -eq 1' in publish
    assert 'gh release create "$RELEASE_TAG" --verify-tag --generate-notes public/*' in text


@pytest.mark.parametrize("name", PUBLIC)
def test_a_published_file_is_copied_and_hashed_under_the_same_name(name: str) -> None:
    publish = jobs(RELEASE.read_text(encoding="utf-8"))["publish"]
    copy = only(publish, "cp dist/*.whl")
    sums = only(publish, "sha256sum *.whl")
    for line, label in ((copy, "copy"), (sums, "SHA256SUMS")):
        assert line.count(name) == 1, (label, name, line)


def test_the_published_sums_read_from_the_download_directory(tmp_path: Path) -> None:
    """Core's deploy/setup.sh greps the wheel line and feeds it to sha256sum -c.

    It fetches ``<release>/SHA256SUMS``, runs ``grep " <wheel>$" | sha256sum -c -``
    and awk-matches ``$2`` from the download directory, so the published file has
    to carry bare basenames in sha256sum's own two-space format.
    """
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    wheel, sdist = f"wuwaterm_telegram-{version}-py3-none-any.whl", f"wuwaterm_telegram-{version}.tar.gz"
    (tmp_path / "dist").mkdir()
    (tmp_path / "dist" / wheel).write_bytes(wheel.encode())
    (tmp_path / "dist" / sdist).write_bytes(sdist.encode())
    (tmp_path / "release-assets").mkdir()
    for name in PUBLIC:
        (tmp_path / "release-assets" / name).write_bytes(name.encode())
    command = commands(jobs(RELEASE.read_text(encoding="utf-8"))["publish"])
    for line in [line for line in command if line.startswith(("mkdir -p public",
                                                              "cp dist/*.whl",
                                                              "(cd public && sha256sum"))]:
        subprocess.run(["sh", "-c", line], cwd=tmp_path, check=True)
    entries = (tmp_path / "public" / "SHA256SUMS").read_text(encoding="ascii").splitlines()
    assert len(entries) == 6
    assert all(re.fullmatch(r"[0-9a-f]{64}  [^/]+\Z", entry) for entry in entries), entries
    grepped = [entry for entry in entries if entry.endswith("  " + wheel)]
    assert len(grepped) == 1
    assert subprocess.run(["sh", "-c", f'grep " {wheel}$" SHA256SUMS | sha256sum -c -'],
                          cwd=tmp_path / "public", capture_output=True, text=True).returncode == 0
    digest = subprocess.run(["sh", "-c", f"awk '$2 == \"{wheel}\" {{print $1}}' SHA256SUMS"],
                            cwd=tmp_path / "public", capture_output=True, text=True, check=True).stdout.strip()
    assert digest == hashlib.sha256(wheel.encode()).hexdigest()
