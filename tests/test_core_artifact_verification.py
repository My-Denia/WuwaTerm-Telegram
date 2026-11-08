"""The consumer lock binds independent producer records before installation."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import hashlib
from io import BytesIO
import json
from pathlib import Path
import sys
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import core_lock  # noqa: E402
import fetch_core  # noqa: E402
import install_verified  # noqa: E402


def artifact_lock() -> dict:
    """A synthetic Actions-artifact lock; the committed lock pins a release."""
    version = core_lock.product_core_version()
    return {
        "schema": 2,
        "kind": "github-actions-artifact",
        "repository": "My-Denia/WuwaTerm",
        "repository_id": 101,
        "workflow": ".github/workflows/ci.yml",
        "workflow_id": 102,
        "package_workflow": ".github/workflows/python-package.yml",
        "event": "pull_request",
        "run_id": 103,
        "run_attempt": 1,
        "run_head_sha": "a" * 40,
        "checkout_sha": "b" * 40,
        "checkout_tree": "c" * 40,
        "artifact_id": 104,
        "artifact_name": "python-package",
        "artifact_sha256": "0" * 64,
        "artifact_bytes": 1,
        "expires_at": "2999-01-01T00:00:00Z",
        "version": version,
        "filename": f"wuwaterm-{version}-py3-none-any.whl",
        "sha256": "0" * 64,
        "sdist_filename": f"wuwaterm-{version}.tar.gz",
        "sdist_sha256": "0" * 64,
    }


@pytest.fixture()
def producer(monkeypatch, tmp_path):
    lock_file = tmp_path / "artifact-lock.json"
    lock_file.write_text(json.dumps(artifact_lock()))
    lock = core_lock.load_lock(lock_file)
    wheel = b"synthetic wheel for transport verification"
    sdist = b"synthetic sdist for transport verification"
    lock["sha256"] = hashlib.sha256(wheel).hexdigest()
    lock["sdist_sha256"] = hashlib.sha256(sdist).hexdigest()
    digests = {lock["filename"]: lock["sha256"], lock["sdist_filename"]: lock["sdist_sha256"]}
    manifest = {"schema": 1, "source_commit": lock["checkout_sha"],
                "source_tree": lock["checkout_tree"], "version": lock["version"], "sha256": digests}
    data = BytesIO()
    with ZipFile(data, "w") as z:
        z.writestr(lock["filename"], wheel)
        z.writestr(lock["sdist_filename"], sdist)
        z.writestr("SHA256SUMS", "".join(f"{digest}  {name}\n" for name, digest in digests.items()))
        z.writestr("source-manifest.json", json.dumps(manifest))
    archive = data.getvalue()
    lock["artifact_sha256"] = hashlib.sha256(archive).hexdigest()
    lock["artifact_bytes"] = len(archive)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(minutes=2)).isoformat()
    end = (now + timedelta(minutes=2)).isoformat()
    created = now.isoformat()
    run = {
        "id": lock["run_id"], "run_attempt": lock["run_attempt"], "status": "completed",
        "conclusion": "success", "event": lock["event"], "head_sha": lock["run_head_sha"],
        "workflow_id": lock["workflow_id"], "path": lock["workflow"],
        "repository": {"id": lock["repository_id"], "full_name": lock["repository"]},
        "head_repository": {"id": lock["repository_id"]},
        "referenced_workflows": [{"sha": lock["checkout_sha"], "path":
            f"{lock['repository']}/{lock['package_workflow']}@{lock['checkout_sha']}"}],
        "run_started_at": start, "updated_at": end,
    }
    artifact = {
        "id": lock["artifact_id"], "name": lock["artifact_name"], "expired": False,
        "size_in_bytes": len(archive), "digest": f"sha256:{lock['artifact_sha256']}",
        "expires_at": lock["expires_at"], "created_at": created,
        "workflow_run": {"id": lock["run_id"], "head_sha": lock["run_head_sha"],
            "repository_id": lock["repository_id"], "head_repository_id": lock["repository_id"]},
    }
    records = {"run": run, "workflow": {"id": lock["workflow_id"], "path": lock["workflow"]},
               "commit": {"sha": lock["checkout_sha"], "tree": {"sha": lock["checkout_tree"]}},
               "artifact": artifact, "archive": archive}
    calls = []

    def fake_fetch(url, **kwargs):
        calls.append(url)
        if "/attempts/" in url:
            return records["run"]
        if "/actions/workflows/" in url:
            return records["workflow"]
        if "/git/commits/" in url:
            return records["commit"]
        if url.endswith("/zip"):
            return records["archive"]
        if "/actions/artifacts/" in url:
            return records["artifact"]
        raise AssertionError("unexpected producer request")

    monkeypatch.setattr(fetch_core, "fetch", fake_fetch)
    return lock, records, calls, wheel


def test_separate_run_head_and_checkout_are_verified(producer):
    lock, _, calls, wheel = producer
    assert lock["run_head_sha"] != lock["checkout_sha"]
    assert fetch_core.actions_wheel(lock) == wheel
    assert len(calls) == 5
    assert any("/git/commits/" + lock["checkout_sha"] in url for url in calls)


@pytest.mark.parametrize(("record", "key", "bad"), [
    ("run", "id", 1), ("run", "run_attempt", 2), ("run", "status", "in_progress"),
    ("run", "conclusion", "failure"), ("run", "head_sha", "0" * 40),
    ("run", "event", "push"), ("run", "workflow_id", 1),
    ("run", "repository", {"id": 1, "full_name": "My-Denia/WuwaTerm"}),
    ("run", "head_repository", {"id": 1}),
    ("run", "referenced_workflows", []), ("workflow", "path", ".github/workflows/other.yml"),
    ("commit", "sha", "0" * 40), ("commit", "tree", {"sha": "0" * 40}),
    ("artifact", "id", 1), ("artifact", "name", "other"), ("artifact", "expired", True),
    ("artifact", "size_in_bytes", 1), ("artifact", "digest", "sha256:" + "0" * 64),
    ("artifact", "expires_at", "2001-01-01T00:00:00Z"),
    ("artifact", "workflow_run", {"id": 1}),
])
def test_mismatched_producer_fails_before_artifact_output(producer, record, key, bad):
    lock, records, calls, _ = producer
    records[record][key] = bad
    with pytest.raises(core_lock.LockError):
        fetch_core.actions_wheel(lock)
    assert not any(url.endswith("/zip") for url in calls)


def test_expired_lock_fails_without_network(producer):
    lock, _, calls, _ = producer
    lock["expires_at"] = "2001-01-01T00:00:00Z"
    with pytest.raises(core_lock.LockError, match="expired"):
        fetch_core.actions_wheel(lock)
    assert calls == []


def test_corrupted_archive_fails_before_wheel_output(producer):
    lock, records, _, _ = producer
    records["archive"] = records["archive"][:-1] + b"x"
    with pytest.raises(core_lock.LockError, match="archive"):
        fetch_core.actions_wheel(lock)


def test_manifest_checkout_mismatch_cannot_become_output(producer):
    lock, records, _, _ = producer
    # Alter an archive while keeping its metadata consistent to exercise the
    # independent trusted checkout/manifest comparison after archive validation.
    old = ZipFile(BytesIO(records["archive"]))
    manifest = json.loads(old.read("source-manifest.json"))
    manifest["source_commit"] = lock["run_head_sha"]
    out = BytesIO()
    with old, ZipFile(out, "w") as new:
        for name in old.namelist():
            new.writestr(name, json.dumps(manifest) if name == "source-manifest.json" else old.read(name))
    records["archive"] = out.getvalue()
    lock["artifact_bytes"] = len(records["archive"])
    lock["artifact_sha256"] = hashlib.sha256(records["archive"]).hexdigest()
    records["artifact"]["size_in_bytes"] = lock["artifact_bytes"]
    records["artifact"]["digest"] = f"sha256:{lock['artifact_sha256']}"
    with pytest.raises(core_lock.LockError, match="manifest"):
        fetch_core.actions_wheel(lock)


def test_http_denial_reports_status_without_sensitive_body(monkeypatch):
    class Opener:
        def open(self, request, timeout):
            raise HTTPError(request.full_url, 403, "private body", {}, None)
    monkeypatch.setattr(fetch_core, "build_opener", lambda *_: Opener())
    monkeypatch.setenv("GITHUB_TOKEN", "fake-test-token")
    with pytest.raises(core_lock.LockError) as error:
        fetch_core.fetch("https://api.github.com/fixture")
    assert str(error.value) == "Core producer request failed (HTTP 403)"
    assert "fake-test-token" not in str(error.value)
    assert "private body" not in str(error.value)


def test_same_host_https_redirect_retains_authorization():
    token = "synthetic-redirect-credential"
    request = Request("https://api.github.com/first", headers={"Authorization": token})
    result = fetch_core.SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://api.github.com/second"
    )
    assert result.get_header("Authorization") == token
    assert token not in result.full_url


def test_different_host_https_redirect_removes_authorization_from_both_header_stores():
    token = "synthetic-redirect-credential"
    request = Request("https://api.github.com/first")
    request.headers["aUtHoRiZaTiOn"] = token
    request.unredirected_hdrs["AUTHorization"] = token
    result = fetch_core.SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://objects.example.invalid/file"
    )
    assert all(name.casefold() != "authorization" for name in result.headers)
    assert all(name.casefold() != "authorization" for name in result.unredirected_hdrs)
    assert token not in result.full_url


def test_cross_host_redirect_strips_mixed_case_in_both_returned_header_stores(monkeypatch):
    token = "synthetic-redirect-credential"
    request = Request("https://api.github.com/first")

    def base_redirect(self, req, fp, code, msg, headers, newurl):
        result = Request(newurl)
        result.headers["aUtHoRiZaTiOn"] = token
        result.unredirected_hdrs["AUTHorization"] = token
        return result

    monkeypatch.setattr(HTTPRedirectHandler, "redirect_request", base_redirect)
    result = fetch_core.SafeRedirect().redirect_request(
        request, None, 302, "Found", {}, "https://objects.example.invalid/file"
    )
    assert all(name.casefold() != "authorization" for name in result.headers)
    assert all(name.casefold() != "authorization" for name in result.unredirected_hdrs)


def test_http_redirect_is_rejected_without_credential_in_error():
    token = "synthetic-redirect-credential"
    request = Request("https://api.github.com/first", headers={"Authorization": token})
    with pytest.raises(core_lock.LockError) as error:
        fetch_core.SafeRedirect().redirect_request(
            request, None, 302, "Found", {}, "http://objects.example.invalid/file"
        )
    assert str(error.value) == "artifact redirect is not HTTPS"
    assert token not in str(error.value)


def test_artifact_lock_expiry_rejected_before_installation(tmp_path):
    lock = artifact_lock()
    lock["expires_at"] = "2001-01-01T00:00:00Z"
    path = tmp_path / "expired.json"
    path.write_text(json.dumps(lock))
    with pytest.raises(core_lock.LockError, match="expired"):
        core_lock.load_lock(path)


def test_committed_lock_pins_the_stable_core_release():
    lock = core_lock.load_lock(ROOT / "core-lock.json")
    assert lock["kind"] == "github-release"
    assert lock["version"] == core_lock.product_core_version()
    assert lock["filename"] == f"wuwaterm-{lock['version']}-py3-none-any.whl"
    assert lock["tag"] == f"v{lock['version']}"


def release_fetch(lock: dict, wheel: bytes, source: str, calls: list[str]):
    def fake_fetch(url, **kwargs):
        calls.append(url)
        if "/releases/tags/" in url:
            return {"tag_name": lock["tag"], "draft": False,
                    "assets": [{"name": lock["filename"], "browser_download_url": lock["url"]}]}
        if "/git/ref/tags/" in url:
            return {"object": {"type": "commit", "sha": source}}
        if url == lock["url"]:
            return wheel
        raise AssertionError("unexpected release request")
    return fake_fetch


def test_release_wheel_verifies_tag_asset_and_download(monkeypatch):
    lock = core_lock.load_lock(ROOT / "core-lock.json")
    wheel = b"synthetic release wheel"
    calls: list[str] = []
    monkeypatch.setattr(fetch_core, "fetch", release_fetch(lock, wheel, lock["source_commit"], calls))
    assert fetch_core.release_wheel(lock) == wheel
    assert calls == [f"{fetch_core.API}/releases/tags/{lock['tag']}",
                     f"{fetch_core.API}/git/ref/tags/{lock['tag']}", lock["url"]]


def test_release_tag_moved_away_from_the_pinned_source_is_refused(monkeypatch):
    lock = core_lock.load_lock(ROOT / "core-lock.json")
    monkeypatch.setattr(fetch_core, "fetch", release_fetch(lock, b"must not be used", "0" * 40, []))
    with pytest.raises(core_lock.LockError, match="tag source"):
        fetch_core.release_wheel(lock)


def test_wrong_wheel_hash_never_starts_pip(tmp_path, monkeypatch):
    lock_path = Path(__file__).resolve().parents[1] / "core-lock.json"
    lock = json.loads(lock_path.read_text())
    core = tmp_path / lock["filename"]
    core.write_bytes(b"wrong Core bytes")
    telegram = tmp_path / "telegram.whl"
    telegram.write_bytes(b"not reached")
    monkeypatch.setattr(sys, "argv", ["install_verified", "--lock", str(lock_path),
                        "--core", str(core), "--telegram", str(telegram),
                        "--telegram-sha256", "0" * 64])
    def no_pip(*args, **kwargs):
        raise AssertionError("pip must not start after a hash failure")
    monkeypatch.setattr(install_verified.subprocess, "run", no_pip)
    with pytest.raises(SystemExit, match="SHA-256 mismatch"):
        install_verified.main()


def test_denied_download_creates_no_wheel(tmp_path, monkeypatch):
    lock_path = Path(__file__).resolve().parents[1] / "core-lock.json"
    output = tmp_path / "artifacts"
    monkeypatch.setattr(sys, "argv", ["fetch_core", "--lock", str(lock_path),
                                      "--output-dir", str(output)])
    def denied(*args, **kwargs):
        raise core_lock.LockError("Core producer request failed (HTTP 403)")
    monkeypatch.setattr(fetch_core, "fetch", denied)
    with pytest.raises(SystemExit, match="HTTP 403"):
        fetch_core.main()
    assert not output.exists()


def test_artifact_lock_is_not_mutated(producer):
    lock, records, _, _ = producer
    before = copy.deepcopy(lock), copy.deepcopy(records)
    fetch_core.actions_wheel(lock)
    assert (lock, records) == before
