"""Fail before polling when the installed Core or database is incompatible."""

from __future__ import annotations

from contextlib import closing
from importlib import import_module, metadata
from inspect import signature
from pathlib import Path
import sqlite3


CORE_VERSION = "0.7.0.dev1"
DATABASE_SCHEMA_VERSION = "2"


class CoreCompatibilityError(RuntimeError):
    """The installed Core does not support this adapter."""


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
        if "before_llm_call" not in signature(app.translate_request_async).parameters:
            raise AttributeError("application translation guard")
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
