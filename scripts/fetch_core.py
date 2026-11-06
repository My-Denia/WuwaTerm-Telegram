"""Fetch the exact Core artifact named by the reviewed local lock."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import BadZipFile, ZipFile
from io import BytesIO
from datetime import datetime, timezone

from core_lock import LockError, REPOSITORY, load_lock, timestamp
from verify_wheels import check_wheel_bytes

API = f"https://api.github.com/repos/{REPOSITORY}"


class SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise LockError("artifact redirect is not HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urlsplit(req.full_url).hostname != urlsplit(newurl).hostname:
            for headers in (redirected.headers, redirected.unredirected_hdrs):
                for name in tuple(headers):
                    if name.casefold() == "authorization":
                        del headers[name]
        return redirected


def fetch(url: str, *, json_response: bool = False, limit: int = 128 * 1024 * 1024):
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and urlsplit(url).hostname == "api.github.com":
        headers["Authorization"] = f"Bearer {token}"
    try:
        with build_opener(SafeRedirect()).open(Request(url, headers=headers), timeout=30) as response:
            payload = response.read(limit + 1)
        if len(payload) > limit:
            raise LockError("Core artifact response exceeds size limit")
        return json.loads(payload) if json_response else payload
    except HTTPError as exc:
        # Status alone is enough to distinguish inaccessible cross-repository
        # artifacts without including response bodies, request headers or URLs.
        raise LockError(f"Core producer request failed (HTTP {exc.code})") from exc
    except (URLError, OSError, ValueError) as exc:
        raise LockError("Core producer request failed or was inaccessible") from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise LockError(message)


def obj(value: object) -> dict:
    require(isinstance(value, dict), "malformed Core producer response")
    return value


def actions_wheel(lock: dict) -> bytes:
    run_id, attempt, artifact_id = (lock[k] for k in ("run_id", "run_attempt", "artifact_id"))
    require(timestamp(lock["expires_at"]) > datetime.now(timezone.utc), "producer artifact has expired")
    run = obj(fetch(f"{API}/actions/runs/{run_id}/attempts/{attempt}", json_response=True))
    require(run.get("id") == run_id and run.get("run_attempt") == attempt, "producer run or attempt differs")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "producer run did not succeed")
    repository = obj(run.get("repository"))
    head_repository = obj(run.get("head_repository"))
    require(repository.get("full_name") == REPOSITORY and repository.get("id") == lock["repository_id"], "producer repository differs")
    require(head_repository.get("id") == lock["repository_id"], "producer head repository differs")
    require(run.get("head_sha") == lock["run_head_sha"] and run.get("event") == lock["event"], "producer run head or event differs")
    require(run.get("workflow_id") == lock["workflow_id"] and str(run.get("path", "")).split("@", 1)[0] == lock["workflow"], "producer workflow differs")
    workflow = obj(fetch(f"{API}/actions/workflows/{lock['workflow_id']}", json_response=True))
    require(workflow.get("id") == lock["workflow_id"] and workflow.get("path") == lock["workflow"], "producer workflow identity differs")
    # The run refers to its local reusable packaging workflow at the actual
    # checkout SHA. The PR head SHA is a different GitHub API field.
    references = run.get("referenced_workflows")
    require(isinstance(references, list) and any(
        isinstance(ref, dict) and ref.get("sha") == lock["checkout_sha"]
        and ref.get("path") == f"{REPOSITORY}/{lock['package_workflow']}@{lock['checkout_sha']}"
        for ref in references
    ), "producer checkout workflow differs")
    commit = obj(fetch(f"{API}/git/commits/{lock['checkout_sha']}", json_response=True))
    require(commit.get("sha") == lock["checkout_sha"] and obj(commit.get("tree")).get("sha") == lock["checkout_tree"], "producer checkout commit or tree differs")
    artifact = obj(fetch(f"{API}/actions/artifacts/{artifact_id}", json_response=True))
    require(artifact.get("id") == artifact_id and artifact.get("expired") is False, "producer artifact is missing or expired")
    require(artifact.get("name") == lock["artifact_name"] and artifact.get("size_in_bytes") == lock["artifact_bytes"], "producer artifact name or size differs")
    require(artifact.get("digest") == f"sha256:{lock['artifact_sha256']}", "producer artifact digest differs")
    require(timestamp(artifact.get("expires_at")) == timestamp(lock["expires_at"]), "producer artifact expiry differs")
    ref = obj(artifact.get("workflow_run"))
    require(ref.get("id") == run_id and ref.get("head_sha") == lock["run_head_sha"] and ref.get("repository_id") == lock["repository_id"] and ref.get("head_repository_id") == lock["repository_id"], "artifact run, head or repository differs")
    try:
        started = timestamp(run["run_started_at"])
        ended = timestamp(run["updated_at"])
        created = timestamp(artifact["created_at"])
        require(started <= created <= ended, "artifact was not created during the pinned attempt")
    except (KeyError, TypeError) as exc:
        raise LockError("producer attempt timestamps are invalid") from exc
    archive = fetch(f"{API}/actions/artifacts/{artifact_id}/zip", limit=lock["artifact_bytes"])
    require(len(archive) == lock["artifact_bytes"] and hashlib.sha256(archive).hexdigest() == lock["artifact_sha256"], "producer archive size or digest differs")
    try:
        with ZipFile(BytesIO(archive)) as z:
            names = [info.filename for info in z.infolist()]
            expected = {lock["filename"], lock["sdist_filename"], "SHA256SUMS", "source-manifest.json"}
            require(len(names) == len(expected) and set(names) == expected, "producer artifact contents differ")
            def one(name: str, limit: int) -> bytes:
                info = z.getinfo(name)
                require(not info.is_dir() and info.file_size <= limit, "producer artifact contents differ")
                return z.read(info)
            wheel = one(lock["filename"], 100 * 1024 * 1024)
            sdist = one(lock["sdist_filename"], 100 * 1024 * 1024)
            checksums = one("SHA256SUMS", 1024 * 1024).decode("ascii").splitlines()
            manifest = obj(json.loads(one("source-manifest.json", 1024 * 1024)))
    except (BadZipFile, ValueError, OSError, UnicodeError, KeyError) as exc:
        raise LockError("producer artifact archive is invalid") from exc
    require(manifest.get("schema") == 1 and manifest.get("source_commit") == lock["checkout_sha"] and manifest.get("source_tree") == lock["checkout_tree"] and manifest.get("version") == lock["version"], "producer manifest source, tree or version differs")
    expected_hashes = {lock["filename"]: lock["sha256"], lock["sdist_filename"]: lock["sdist_sha256"]}
    require(manifest.get("sha256") == expected_hashes, "producer manifest digests differ")
    require(set(checksums) == {f"{digest}  {name}" for name, digest in expected_hashes.items()} and len(checksums) == 2, "producer checksums differ")
    require(hashlib.sha256(wheel).hexdigest() == lock["sha256"] and hashlib.sha256(sdist).hexdigest() == lock["sdist_sha256"], "producer wheel or sdist digest differs")
    require(timestamp(lock["expires_at"]) > datetime.now(timezone.utc), "producer artifact has expired")
    return wheel


def release_wheel(lock: dict) -> bytes:
    tag = quote(lock["tag"], safe="")
    release = fetch(f"{API}/releases/tags/{tag}", json_response=True)
    require(release.get("tag_name") == lock["tag"] and release.get("draft") is False, "Core release differs")
    assets = [a for a in release.get("assets", []) if a.get("name") == lock["filename"]]
    require(len(assets) == 1 and assets[0].get("browser_download_url") == lock["url"], "Core release asset differs")
    ref = fetch(f"{API}/git/ref/tags/{tag}", json_response=True).get("object", {})
    for _ in range(4):
        if ref.get("type") != "tag":
            break
        ref = fetch(f"{API}/git/tags/{ref.get('sha', '')}", json_response=True).get("object", {})
    require(ref.get("type") == "commit" and ref.get("sha") == lock["source_commit"], "Core release tag source differs")
    return fetch(lock["url"])


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--lock", type=Path, required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    args = p.parse_args()
    try:
        lock = load_lock(args.lock)
        wheel = actions_wheel(lock) if lock["kind"] == "github-actions-artifact" else release_wheel(lock)
        # Do not leave a failed download at the output path.
        require(hashlib.sha256(wheel).hexdigest() == lock["sha256"], "Core wheel SHA-256 differs")
        check_wheel_bytes(wheel, lock["sha256"], "wuwaterm", lock["version"])
        args.output_dir.mkdir(parents=True, exist_ok=True)
        path = args.output_dir / lock["filename"]
        if path.exists():
            raise LockError("Core output file already exists")
        path.write_bytes(wheel)
    except (LockError, KeyError, TypeError) as exc:
        raise SystemExit(f"Core verification failed: {exc}") from exc
    print(f"Verified {lock['filename']}")


if __name__ == "__main__":
    main()
