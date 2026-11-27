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
_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?")
_SPEC_CLAUSE_RE = re.compile(r"^(==|>=|<=|>|<)([0-9]+(?:\.[0-9]+)*)\Z")


class LockError(ValueError):
    pass


def _version_tuple(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.match(value.strip())
    if match is None:
        raise LockError(f"invalid Core version: {value!r}")
    return (
        int(match.group(1)),
        int(match.group(2) or 0),
        int(match.group(3) or 0),
    )


def version_satisfies_spec(version: str, spec: str) -> bool:
    """Evaluate a simple PEP 440 comma-separated version specifier."""
    parsed = _version_tuple(version)
    clauses = [part.strip() for part in spec.split(",") if part.strip()]
    if not clauses:
        raise LockError("Core dependency specifier must not be empty")
    for clause in clauses:
        match = _SPEC_CLAUSE_RE.fullmatch(clause)
        if match is None:
            raise LockError(f"unsupported Core version clause: {clause!r}")
        op, target_raw = match.groups()
        target = _version_tuple(target_raw)
        if op == "==" and not (parsed == target):
            return False
        if op == ">=" and not (parsed >= target):
            return False
        if op == "<=" and not (parsed <= target):
            return False
        if op == ">" and not (parsed > target):
            return False
        if op == "<" and not (parsed < target):
            return False
    return True


def product_core_spec(root: Path = ROOT) -> str:
    """Return the `wuwaterm` dependency version specifier from pyproject.toml."""
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    dependencies = project["project"]["dependencies"]
    specs = [
        s.removeprefix("wuwaterm").strip()
        for s in dependencies
        if re.match(r"^wuwaterm(?:==|>=|<=|>|<)", s)
    ]
    if len(specs) != 1:
        raise LockError("project must specify one wuwaterm Core dependency")
    return specs[0]


def product_core_version(root: Path = ROOT) -> str:
    """Return the locked Core release version and verify it fits pyproject.toml."""
    spec = product_core_spec(root)
    source = ast.parse((root / "src/wuwaterm_telegram/core_compat.py").read_text(encoding="utf-8"))
    constants = [
        n.value.value
        for n in source.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "CORE_VERSION" for t in n.targets)
        and isinstance(n.value, ast.Constant)
        and isinstance(n.value.value, str)
    ]
    if len(constants) != 1:
        raise LockError("runtime Core version constant is missing")
    core_version = constants[0]
    if not version_satisfies_spec(core_version, spec):
        raise LockError("runtime and package Core versions disagree")
    return core_version


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
        spec = product_core_spec()
        lock_version = lock.get("version")
        if (
            not isinstance(lock_version, str)
            or lock_version != version
            or not version_satisfies_spec(lock_version, spec)
        ):
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
