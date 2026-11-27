"""Fail before polling when the installed Core or database is incompatible."""

from __future__ import annotations

from contextlib import closing
from importlib import import_module, metadata
from inspect import signature
from pathlib import Path
import re
import sqlite3


# Locked Core release used by deterministic CI/container builds, and the
# runtime semantic version range accepted by the embedded compatibility layer.
CORE_VERSION = "0.16.0"
MIN_CORE_VERSION = "0.16.0"
MAX_CORE_VERSION_EXCLUSIVE = "1.0.0"
CORE_VERSION_SPEC = ">=0.16.0,<1.0"
DATABASE_SCHEMA_VERSION = "2"

_PEP440_FINAL_RE = re.compile(
    r"^(?:(?P<epoch>\d+)!)?(?P<release>\d+(?:\.\d+)*)"
    r"(?:\.post(?P<post>\d+))?"
    r"(?:\+(?P<local>[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*))?\Z"
)

try:
    from packaging.specifiers import SpecifierSet as _SpecifierSet
    from packaging.version import InvalidVersion as _InvalidVersion, Version as _Version
except ImportError:  # pragma: no cover - stdlib fallback
    _SpecifierSet = None
    _InvalidVersion = ValueError
    _Version = None


class CoreCompatibilityError(RuntimeError):
    """The installed Core does not support this adapter."""


def _stdlib_final_version_key(version: str) -> tuple[int, tuple[int, ...], int]:
    match = _PEP440_FINAL_RE.fullmatch(version.strip())
    if match is None:
        raise ValueError(f"unsupported or pre-release version: {version!r}")
    epoch = int(match.group("epoch") or 0)
    parts = [int(p) for p in match.group("release").split(".")]
    while len(parts) < 3:
        parts.append(0)
    post = int(match.group("post")) if match.group("post") is not None else -1
    return (epoch, tuple(parts), post)


def parse_version_tuple(version: str) -> tuple[int, int, int]:
    """Extract the (major, minor, patch) numeric tuple of a final PEP 440 version."""
    _epoch, parts, _post = _stdlib_final_version_key(version)
    return (parts[0], parts[1], parts[2])


def _version_at_least(version: str, minimum: str) -> bool:
    if _Version is not None:
        try:
            return _Version(version) >= _Version(minimum)
        except _InvalidVersion:
            return False
    try:
        return _stdlib_final_version_key(version) >= _stdlib_final_version_key(minimum)
    except ValueError:
        return False


def is_compatible_core_version(
    version: str,
    *,
    spec: str = CORE_VERSION_SPEC,
) -> bool:
    """True when ``version`` satisfies the PEP 440 Core version specifier."""
    if _SpecifierSet is not None and _Version is not None:
        try:
            parsed = _Version(version)
            return _SpecifierSet(spec).contains(parsed, prereleases=False)
        except (_InvalidVersion, ValueError):
            return False
    try:
        parsed_key = _stdlib_final_version_key(version)
    except ValueError:
        return False
    for clause in (part.strip() for part in spec.split(",") if part.strip()):
        for op in ("==", ">=", "<=", ">", "<"):
            if clause.startswith(op):
                target_key = _stdlib_final_version_key(clause[len(op) :])
                if op == "==" and not (parsed_key == target_key):
                    return False
                if op == ">=" and not (parsed_key >= target_key):
                    return False
                if op == "<=" and not (parsed_key <= target_key):
                    return False
                if op == ">" and not (parsed_key > target_key):
                    return False
                if op == "<" and not (parsed_key < target_key):
                    return False
                break
        else:
            return False
    return True


def wrapper_strip_is_native() -> bool:
    """True when Core ships `strip_telegram_wrapper_noise`."""
    try:
        normalize = import_module("wuwaterm.normalize")
    except ImportError:
        return False
    return hasattr(normalize, "strip_telegram_wrapper_noise")


def strip_telegram_wrapper_noise(value: str, *, keep_paragraphs: bool = False) -> str:
    """Strip Telegram screenshot/wrapper markers using Core's native helper.

    Telegram-facing text only: version tags (`[Wuthering Waves 3.7]`), spoiler
    labels (`[spoiler]`), and quote bars are screenshot furniture, not content.
    Core >= 0.16.0 always provides `wuwaterm.normalize.strip_telegram_wrapper_noise`.
    """
    try:
        normalize = import_module("wuwaterm.normalize")
    except ImportError as exc:  # pragma: no cover - Core is a hard dependency
        raise CoreCompatibilityError(
            "installed WuwaTerm Core lacks Telegram wrapper cleanup"
        ) from exc
    strip = getattr(normalize, "strip_telegram_wrapper_noise", None)
    if strip is None:
        raise CoreCompatibilityError(
            "installed WuwaTerm Core lacks wuwaterm.normalize.strip_telegram_wrapper_noise"
        )
    return strip(value, keep_paragraphs=keep_paragraphs)


# What `/review` needs from the Core runtime (semantic-review-v1, segmentation-v1).
# `verify_core_compatibility` probes all of them at start-up.
REVIEW_CORE_SYMBOLS: dict[str, tuple[str, ...]] = {
    "wuwaterm.application": (
        "ContextBefore",
        "SemanticReviewJob",
        "TranslationContext",
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


def probe_local_core_capabilities() -> frozenset[str]:
    """Probe optional runtime capabilities exposed by an installed local Core."""
    caps: set[str] = set()
    try:
        app = import_module("wuwaterm.application")
    except ImportError:
        return frozenset()
    service_caps = getattr(app, "service_capabilities", None)
    if callable(service_caps):
        try:
            caps.update(str(item) for item in service_caps())
        except Exception:  # noqa: BLE001 - capability probe must stay safe
            pass
    if hasattr(app, "TranslationContext") and hasattr(app, "ContextBefore"):
        caps.add("translation-context-v1")
    try:
        structural = import_module("wuwaterm.structural_checks")
        if hasattr(structural, "check_segment_structure"):
            caps.add("structure-check-v1")
    except ImportError:
        pass
    try:
        protected_sig = signature(app.SentenceTranslator.translate_protected_async)
        if "context" in protected_sig.parameters:
            caps.add("protected-text-context-v1")
    except (AttributeError, TypeError, ValueError):
        pass
    return frozenset(caps)


def verify_core_compatibility() -> None:
    """Validate installed Core semver range and runtime capabilities."""
    try:
        version = metadata.version("wuwaterm")
    except metadata.PackageNotFoundError as exc:
        raise CoreCompatibilityError("WuwaTerm Core is not installed") from exc
    if not is_compatible_core_version(version):
        raise CoreCompatibilityError(
            f"WuwaTerm Core {CORE_VERSION_SPEC} is required; found {version}"
        )
    try:
        app = import_module("wuwaterm.application")
        required = (
            "ContextBefore",
            "DEFAULT_LLM_MAX_CONCURRENCY",
            "DEFAULT_LLM_TIMEOUT_SECONDS",
            "LLM_INPUT_CHAR_LIMIT",
            "LLMTranslationError",
            "MarkupTranslation",
            "OperationExecutionBudget",
            "ProtectedStructure",
            "ProtectedText",
            "SentenceTranslator",
            "SlidingWindowRateLimiter",
            "TermService",
            "TranslationContext",
            "TranslationJob",
            "TranslationOutcome",
            "TRANSLATION_UNAVAILABLE_NOTICE",
            "build_term_service",
            "build_translator",
            "count_cjk",
            "count_latin",
            "error_code_for_llm_reason",
            "llm_configured",
            "translate_request",
            "translate_request_async",
        )
        for name in required:
            getattr(app, name)
        protected_async = app.SentenceTranslator.translate_protected_async
        if "before_llm_call" not in signature(protected_async).parameters:
            raise AttributeError("protected translation guard")
        if "execution_budget" not in signature(protected_async).parameters:
            raise AttributeError("protected execution budget")
        translate_async_params = signature(app.SentenceTranslator.translate_async).parameters
        if "execution_budget" not in translate_async_params:
            raise AttributeError("sentence execution budget")
        if "context" not in translate_async_params:
            raise AttributeError("sentence translation context")
        if "before_llm_call" not in signature(app.translate_request_async).parameters:
            raise AttributeError("application translation guard")
        if "execution_budget" not in signature(app.translate_request_async).parameters:
            raise AttributeError("application execution budget")
        missing = missing_review_symbols()
        if missing:
            raise AttributeError("review symbols: " + ", ".join(missing))
        if not wrapper_strip_is_native():
            raise AttributeError(
                "wuwaterm.normalize.strip_telegram_wrapper_noise"
            )
        if "execution_budget" not in signature(app.semantic_review_async).parameters:
            raise AttributeError("review execution budget")
        structural = import_module("wuwaterm.structural_checks")
        getattr(structural, "check_segment_structure")
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
