"""Fail before polling when the installed Core or database is incompatible."""

from __future__ import annotations

from contextlib import closing
from importlib import import_module, metadata
from inspect import signature
from pathlib import Path
import re
import sqlite3


CORE_VERSION = "0.12.0"
DATABASE_SCHEMA_VERSION = "2"
# Core #318 moved Telegram wrapper cleanup out of generic text preparation into
# `wuwaterm.normalize.strip_telegram_wrapper_noise` and stopped `prepare_text`
# from deleting screenshot markers. Core 0.12.0 still folds that cleanup into
# `normalize_user_text`, so the adapter resolves whichever symbol exists and
# keeps working on both; the symbol becomes mandatory with the 0.13.0 pin.
WRAPPER_STRIP_CORE_VERSION = "0.13.0"

_PARAGRAPH_BREAK_RE = re.compile(r"\n[ \t]*\n+")


class CoreCompatibilityError(RuntimeError):
    """The installed Core does not support this adapter."""


def _version_at_least(version: str, minimum: str) -> bool:
    def parts(value: str) -> tuple[int, ...]:
        numbers: list[int] = []
        for chunk in value.split("."):
            digits = "".join(char for char in chunk if char.isdigit())
            if not digits:
                break
            numbers.append(int(digits))
        return tuple(numbers)

    return parts(version) >= parts(minimum)


def wrapper_strip_is_native() -> bool:
    """True when Core ships `strip_telegram_wrapper_noise` (>= 0.13.0)."""
    try:
        normalize = import_module("wuwaterm.normalize")
    except ImportError:
        return False
    return hasattr(normalize, "strip_telegram_wrapper_noise")


def strip_telegram_wrapper_noise(value: str, *, keep_paragraphs: bool = False) -> str:
    """Strip Telegram screenshot/wrapper markers with the installed Core.

    Telegram-facing text only: version tags (`[Wuthering Waves 3.7]`), spoiler
    labels (`[spoiler]`), and quote bars are screenshot furniture, not content.
    Core >= 0.13.0 exposes the cleanup as its own function; Core 0.12.0 only
    strips the same markers inside `normalize_user_text`, so that is the
    fallback until the pin moves.
    """
    try:
        normalize = import_module("wuwaterm.normalize")
    except ImportError as exc:  # pragma: no cover - Core is a hard dependency
        raise CoreCompatibilityError(
            "installed WuwaTerm Core lacks Telegram wrapper cleanup"
        ) from exc
    strip = getattr(normalize, "strip_telegram_wrapper_noise", None)
    if strip is not None:
        return strip(value, keep_paragraphs=keep_paragraphs)
    legacy = getattr(normalize, "normalize_user_text", None)
    if legacy is None:  # pragma: no cover - every supported Core has one
        raise CoreCompatibilityError(
            "installed WuwaTerm Core lacks Telegram wrapper cleanup"
        )
    if not keep_paragraphs:
        return legacy(value)
    parts = (legacy(part) for part in _PARAGRAPH_BREAK_RE.split(value))
    return "\n\n".join(part for part in parts if part)


# What `/review` needs from Core 0.12.0 (semantic-review-v1, segmentation-v1).
# `verify_core_compatibility` requires all of them: the adapter is built for
# exactly this Core. Whether the model track is *on* is a separate, run-time
# matter (`semantic_review_enabled()` and a configured model).
REVIEW_CORE_SYMBOLS: dict[str, tuple[str, ...]] = {
    "wuwaterm.application": (
        "ContextBefore",
        "SemanticReviewJob",
        "semantic_review_async",
        "semantic_review_enabled",
    ),
    "wuwaterm.segmentation": ("SEGMENT_LIMIT", "pair_segments", "segment_text"),
}


def missing_review_symbols() -> tuple[str, ...]:
    """Names from `REVIEW_CORE_SYMBOLS` the installed Core does not provide."""
    missing: list[str] = []
    for module_name, names in REVIEW_CORE_SYMBOLS.items():
        try:
            module = import_module(module_name)
        except ImportError:
            missing.extend(f"{module_name}.{name}" for name in names)
            continue
        missing.extend(
            f"{module_name}.{name}" for name in names if not hasattr(module, name)
        )
    return tuple(missing)


def verify_core_compatibility() -> None:
    try:
        version = metadata.version("wuwaterm")
    except metadata.PackageNotFoundError as exc:
        raise CoreCompatibilityError("WuwaTerm Core is not installed") from exc
    if version != CORE_VERSION:
        raise CoreCompatibilityError(
            f"WuwaTerm Core {CORE_VERSION} is required; found {version}"
        )
    try:
        app = import_module("wuwaterm.application")
        required = (
            "DEFAULT_LLM_MAX_CONCURRENCY", "DEFAULT_LLM_TIMEOUT_SECONDS",
            "LLM_INPUT_CHAR_LIMIT", "LLMTranslationError", "MarkupTranslation",
            "OperationExecutionBudget",
            "ProtectedStructure", "ProtectedText", "SentenceTranslator",
            "SlidingWindowRateLimiter", "TermService", "TranslationJob",
            "TranslationOutcome", "TRANSLATION_UNAVAILABLE_NOTICE",
            "build_term_service", "build_translator", "count_cjk", "count_latin",
            "error_code_for_llm_reason", "llm_configured", "translate_request",
            "translate_request_async",
        )
        for name in required:
            getattr(app, name)
        protected_async = app.SentenceTranslator.translate_protected_async
        if "before_llm_call" not in signature(protected_async).parameters:
            raise AttributeError("protected translation guard")
        if "execution_budget" not in signature(protected_async).parameters:
            raise AttributeError("protected execution budget")
        if "execution_budget" not in signature(app.SentenceTranslator.translate_async).parameters:
            raise AttributeError("sentence execution budget")
        if "before_llm_call" not in signature(app.translate_request_async).parameters:
            raise AttributeError("application translation guard")
        if "execution_budget" not in signature(app.translate_request_async).parameters:
            raise AttributeError("application execution budget")
        missing = missing_review_symbols()
        if missing:
            raise AttributeError("review symbols: " + ", ".join(missing))
        if _version_at_least(CORE_VERSION, WRAPPER_STRIP_CORE_VERSION) and not (
            wrapper_strip_is_native()
        ):
            raise AttributeError(
                "wuwaterm.normalize.strip_telegram_wrapper_noise"
            )
        if "execution_budget" not in signature(app.semantic_review_async).parameters:
            raise AttributeError("review execution budget")
        llm_transport = import_module("wuwaterm.llm_transport")
        getattr(llm_transport, "current_execution_budget")
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise CoreCompatibilityError(
            "installed WuwaTerm Core lacks the required Telegram adapter capabilities"
        ) from exc


def verify_dictionary(db_path: str | Path) -> None:
    """Inspect schema and terms read-only without creating a missing database."""
    try:
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True)) as connection:
            connection.execute("PRAGMA query_only=ON")
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if row is None or row[0] != DATABASE_SCHEMA_VERSION:
                raise CoreCompatibilityError(
                    f"terms database schema {DATABASE_SCHEMA_VERSION} is required"
                )
            connection.execute("SELECT 1 FROM terms LIMIT 1").fetchone()
    except (OSError, sqlite3.Error, ValueError) as exc:
        raise CoreCompatibilityError("terms database could not be read") from exc
