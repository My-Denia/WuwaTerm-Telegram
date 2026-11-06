"""Reject development versions and mismatched stable release tags."""
from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path

from core_lock import ROOT, LockError, load_lock


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--tag", required=True)
    p.add_argument("--lock", type=Path, required=True)
    args = p.parse_args()
    try:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
        version = project["version"]
        if not re.fullmatch(r"\d+\.\d+\.\d+", version) or args.tag != f"v{version}":
            raise LockError("release tag must match a stable Telegram version")
        lock = load_lock(args.lock)
        if lock["kind"] != "github-release" or not re.fullmatch(r"\d+\.\d+\.\d+", lock["version"]):
            raise LockError("stable release requires a pinned stable Core release")
    except (OSError, KeyError, LockError) as exc:
        raise SystemExit(f"Release preflight failed: {exc}") from exc
    print("Release version and Core lock match")


if __name__ == "__main__":
    main()
