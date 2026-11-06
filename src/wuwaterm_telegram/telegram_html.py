"""Telegram HTML validation and stripping helpers."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
import re

from wuwaterm.application import (
    LLMTranslationError,
    ProtectedStructure,
    ProtectedText,
    TRANSLATION_UNAVAILABLE_NOTICE,
)


# Telegram's HTML subset: https://core.telegram.org/bots/api#html-style
ALLOWED_TAGS = frozenset(
    {
        "b",
        "strong",
        "i",
        "em",
        "u",
        "ins",
        "s",
        "strike",
        "del",
        "a",
        "code",
        "pre",
        "blockquote",
        "span",
        "tg-spoiler",
        "tg-emoji",
    }
)

_ENTITY_RE = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);"
)

# Retry without formatting only for unusable model content, never for quota,
# network, authorization or freshness failures.
HTML_CONTENT_FAILURE_REASONS = frozenset({"invalid_response", "html_integrity"})


class TelegramHTMLIntegrityError(ValueError):
    """Translated HTML no longer contains the protected source structure."""


def _validate_restored_html(translated: ProtectedText, source: ProtectedText) -> str:
    # Inspect model-produced text separately: valid new tags or entities are
    # still additions, even when the final string would parse as valid HTML.
    if not isinstance(translated, ProtectedText) or translated.structures != source.structures:
        raise TelegramHTMLIntegrityError("translated HTML structure changed")
    for segment in translated.visible_segments:
        if "<" in segment or _structural_tokens(segment):
            raise TelegramHTMLIntegrityError("translated text added HTML structure")
    restored = translated.render()
    if _structural_tokens(restored) != tuple(s.original for s in translated.structures):
        raise TelegramHTMLIntegrityError("restored HTML structure changed")
    if not validate_telegram_html(restored):
        raise TelegramHTMLIntegrityError("restored HTML is invalid")
    return restored


def _tag_end(text: str, start: int) -> int | None:
    quote: str | None = None
    for index in range(start + 1, len(text)):
        char = text[index]
        if quote is not None:
            if char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
        elif char == ">":
            return index + 1
    return None


def _structural_spans(html_text: str) -> tuple[tuple[int, int], ...]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(html_text):
        if html_text[index] == "<":
            end = _tag_end(html_text, index)
            if end is not None:
                spans.append((index, end))
                index = end
                continue
        if html_text[index] == "&":
            match = _ENTITY_RE.match(html_text, index)
            if match is not None:
                spans.append((index, match.end()))
                index = match.end()
                continue
        index += 1
    return tuple(spans)


def _structural_tokens(html_text: str) -> tuple[str, ...]:
    return tuple(html_text[start:end] for start, end in _structural_spans(html_text))


def protect_telegram_html(html_text: str) -> ProtectedText:
    """Supply exact tags and entities as opaque Core structures."""

    segments: list[str] = []
    structures: list[ProtectedStructure] = []
    cursor = 0
    for start, end in _structural_spans(html_text):
        segments.append(html_text[cursor:start])
        raw = html_text[start:end]
        structures.append(ProtectedStructure(raw, unescape(raw) if raw.startswith("&") else ""))
        cursor = end
    segments.append(html_text[cursor:])
    return ProtectedText(tuple(segments), tuple(structures))


def _legacy_failure(exc: Exception) -> Exception:
    """Keep the historic HTML failure reason and its safe diagnostic aligned."""
    if isinstance(exc, LLMTranslationError) and exc.reason != "protected_text_integrity":
        return exc
    return LLMTranslationError(TRANSLATION_UNAVAILABLE_NOTICE, reason="html_integrity")


def translate_html(translator, html_text: str, *, to_chinese: bool = False) -> str:
    """Translate the visible segments via Core's protected-text API."""
    protected = protect_telegram_html(html_text)
    try:
        translated = translator.translate_protected(protected, to_chinese=to_chinese)
        return _validate_restored_html(translated, protected)
    except (LLMTranslationError, TelegramHTMLIntegrityError) as exc:
        mapped = _legacy_failure(exc)
        if mapped is exc:
            raise
        raise mapped from exc


async def translate_html_async(
    translator, html_text: str, *, to_chinese: bool = False, before_llm_call=None
) -> str:
    protected = protect_telegram_html(html_text)
    try:
        translated = await translator.translate_protected_async(
            protected, to_chinese=to_chinese, before_llm_call=before_llm_call
        )
        return _validate_restored_html(translated, protected)
    except (LLMTranslationError, TelegramHTMLIntegrityError) as exc:
        mapped = _legacy_failure(exc)
        if mapped is exc:
            raise
        raise mapped from exc


class _TelegramHTMLValidator(HTMLParser):
    """Conservative validator: false negatives only cost formatting."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.valid = True
        self.stack: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS or not self._attrs_allowed(tag, dict(attrs)):
            self.valid = False
            return
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.valid = False
            return
        self.stack.pop()

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        # None of Telegram's subset is void; self-closing tags are invalid.
        self.valid = False

    def handle_comment(self, data: str) -> None:
        self.valid = False

    def handle_decl(self, decl: str) -> None:
        self.valid = False

    def handle_pi(self, data: str) -> None:
        self.valid = False

    def unknown_decl(self, data: str) -> None:
        self.valid = False

    @staticmethod
    def _attrs_allowed(tag: str, attrs: dict[str, str | None]) -> bool:
        if tag == "a":
            return set(attrs) == {"href"} and bool(attrs.get("href"))
        if tag == "span":
            return set(attrs) == {"class"} and attrs.get("class") == "tg-spoiler"
        if tag == "tg-emoji":
            return set(attrs) == {"emoji-id"} and bool(attrs.get("emoji-id"))
        if tag == "code":
            if not attrs:
                return True
            return set(attrs) == {"class"} and str(attrs.get("class") or "").startswith(
                "language-"
            )
        if tag == "blockquote":
            return not attrs or set(attrs) == {"expandable"}
        return not attrs


class _TelegramHTMLStripper(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def validate_telegram_html(html_text: str) -> bool:
    validator = _TelegramHTMLValidator()
    try:
        validator.feed(html_text)
        validator.close()
    except Exception:  # noqa: BLE001 - any parser failure means "not valid"
        return False
    return validator.valid and not validator.stack


def strip_telegram_html(html_text: str) -> str:
    stripper = _TelegramHTMLStripper()
    try:
        stripper.feed(html_text)
        stripper.close()
    except Exception:  # noqa: BLE001 - must never raise; plain send is safe
        return html_text
    return "".join(stripper.parts)
