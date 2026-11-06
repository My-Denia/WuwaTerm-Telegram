"""Resolve runtime state locations without reading or changing state files."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable


@dataclass(frozen=True)
class StatePaths:
    effective: Path
    legacy: Path
    migration_target: Path | None


def state_paths(
    db_path: str | Path,
    *,
    filename: str,
    explicit_path: str | Path | None,
    state_dir: str | None,
    resolve: Callable[[Path], Path] | None = None,
) -> StatePaths:
    """Match the bot's existing explicit-path and state-dir precedence."""
    resolve = resolve or (lambda path: path.resolve(strict=False))
    legacy = resolve(Path(db_path)).parent / filename
    target = Path(state_dir).expanduser() / filename if state_dir else None
    if explicit_path:
        explicit = Path(explicit_path).expanduser()
        if target is not None and (
            resolve(explicit) == resolve(legacy)
            or resolve(explicit) == resolve(target)
        ):
            return StatePaths(target, legacy, target)
        return StatePaths(explicit, legacy, None)
    if target is None:
        return StatePaths(legacy, legacy, None)
    return StatePaths(target, legacy, target)
