"""Read the reviewed Core artifact lock; never infer an artifact from 'latest'."""
from __future__ import annotations

import ast
from datetime import datetime, timezone
import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPOSITORY = "My-Denia/WuwaTerm"
HEX40 = re.compile(r"[0-9a-f]{40}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class LockError(ValueError):
    pass


def product_core_version() -> str:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    dependencies = project["project"]["dependencies"]
    pins = [s.removeprefix("wuwaterm==") for s in dependencies if s.startswith("wuwaterm==")]
    if len(pins) != 1:
        raise LockError("project must specify one exact Core dependency")
    source = ast.parse((ROOT / "src/wuwaterm_telegram/core_compat.py").read_text())
    constants = [n.value.value for n in source.body if isinstance(n, ast.Assign)
                 and any(isinstance(t, ast.Name) and t.id == "CORE_VERSION" for t in n.targets)
                 and isinstance(n.value, ast.Constant)]
    if constants != pins:
        raise LockError("runtime and package Core versions disagree")
    return pins[0]


def timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise LockError("invalid producer timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LockError("invalid producer timestamp") from exc
    if parsed.tzinfo is None:
        raise LockError("producer timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def load_lock(path: Path) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(lock, dict) or lock.get("schema") != 2:
            raise LockError("unsupported Core lock schema")
        if lock.get("repository") != REPOSITORY:
            raise LockError("unexpected Core repository")
        version = product_core_version()
        if lock.get("version") != version:
            raise LockError("Core lock version differs from package dependency")
        if lock.get("filename") != f"wuwaterm-{version}-py3-none-any.whl":
            raise LockError("unexpected Core wheel filename")
        if not isinstance(lock.get("sha256"), str) or not HEX64.fullmatch(lock["sha256"]):
            raise LockError("invalid Core wheel digest")
        if lock.get("kind") == "github-actions-artifact":
            for key in ("repository_id", "workflow_id", "run_id", "run_attempt", "artifact_id", "artifact_bytes"):
                if type(lock.get(key)) is not int or lock[key] < 1:
                    raise LockError(f"invalid {key}")
            for key in ("run_head_sha", "checkout_sha", "checkout_tree"):
                if not isinstance(lock.get(key), str) or not HEX40.fullmatch(lock[key]):
                    raise LockError(f"invalid {key}")
            for key in ("artifact_sha256", "sdist_sha256"):
                if not isinstance(lock.get(key), str) or not HEX64.fullmatch(lock[key]):
                    raise LockError(f"invalid {key}")
            if lock.get("sdist_filename") != f"wuwaterm-{version}.tar.gz":
                raise LockError("unexpected Core source distribution filename")
            for key in ("workflow", "package_workflow"):
                if not isinstance(lock.get(key), str) or not re.fullmatch(
                    r"\.github/workflows/[A-Za-z0-9_.-]+\.ya?ml", lock[key]
                ):
                    raise LockError(f"invalid {key}")
            if lock.get("event") != "pull_request" or lock.get("artifact_name") != "python-package":
                raise LockError("unexpected producer event or artifact name")
            if timestamp(lock.get("expires_at")) <= datetime.now(timezone.utc):
                raise LockError("Core artifact lock has expired")
        elif lock.get("kind") == "github-release":
            if not isinstance(lock.get("source_commit"), str) or not HEX40.fullmatch(lock["source_commit"]):
                raise LockError("invalid Core release source commit")
            tag = lock.get("tag")
            if not isinstance(tag, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", tag):
                raise LockError("invalid Core release tag")
            expected = f"https://github.com/{REPOSITORY}/releases/download/{tag}/{lock['filename']}"
            if lock.get("url") != expected:
                raise LockError("unexpected Core release URL")
        else:
            raise LockError("unsupported Core producer kind")
        return lock
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise LockError("missing or invalid Core artifact lock") from exc
