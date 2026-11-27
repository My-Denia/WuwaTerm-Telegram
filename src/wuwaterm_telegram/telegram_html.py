"""Telegram HTML validation, balanced splitting, and stripping helpers."""

from __future__ import annotations

from html import unescape
from html.parser import HTMLParser
from inspect import signature
import re
from typing import Callable

from wuwaterm.application import (
    LLMTranslationError,
    ProtectedStructure,
    ProtectedText,
    TRANSLATION_UNAVAILABLE_NOTICE,
)

from .telegram_text import TELEGRAM_TEXT_MESSAGE_LIMIT, telegram_text_units


# Telegram's HTML subset (including Bot API 10.0 / PTB 22.8 tg-time):
# https://core.telegram.org/bots/api#html-style
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
        "tg-time",
    }
)

# Tags whose contents should not be split across chunk boundaries.
_ATOMIC_TAGS = frozenset({"tg-emoji", "tg-time"})

_ENTITY_RE = re.compile(
    r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);"
)
_TAG_NAME_RE = re.compile(r"^<\s*(/?)\s*([A-Za-z][A-Za-z0-9-]*)")
_HTML_TAG_PRESENT_RE = re.compile(r"</?[A-Za-z][^>]*>")
_CODE_LANG_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+#.-]{1,64}\Z")
_TG_TIME_UNIX_RE = re.compile(r"^-?[0-9]{1,16}\Z")
_TG_TIME_FORMAT_RE = re.compile(r"^(?:|r|(?=.+)w?[dD]?[tT]?)\Z")
_CODE_OPEN_CLASS_RE = re.compile(
    r'^<code\s+class=(?P<q>["\'])(?P<cls>[^"\']+)(?P=q)\s*>\Z',
    re.IGNORECASE,
)
_SPLIT_BOUNDARY_CHARS = frozenset(" \t\r\n.,;:!?)]}、，。！？；：）】》")


def _is_valid_tg_time_format(fmt: str) -> bool:
    """Validate Telegram Bot API Date-Time format string.

    Allowed: empty string (default format), ``"r"`` (relative), or a non-empty
    subset strictly in canonical order ``w?`` + ``[dD]?`` + ``[tT]?``.
    """
    return _TG_TIME_FORMAT_RE.fullmatch(fmt) is not None


def normalize_telegram_html(html_text: str) -> str:
    """Normalize PTB 22.8 HTML quirks to strict Telegram Bot API / TDLib HTML.

    PTB 22.8 emits ``<pre><code class="python">`` without the ``language-``
    prefix that Telegram/TDLib requires for syntax highlighting. Rewrites bare
    language classes inside ``<pre>`` to ``class="language-{lang}"`` while
    leaving all other tags, attributes, entities, and text untouched.
    """
    if "<code" not in html_text or "<pre" not in html_text:
        return html_text
    parts: list[str] = []
    stack: list[str] = []
    cursor = 0
    for start, end in _structural_spans(html_text):
        if start > cursor:
            parts.append(html_text[cursor:start])
        raw = html_text[start:end]
        if raw.startswith("&"):
            parts.append(raw)
        else:
            match = _TAG_NAME_RE.match(raw)
            if match is None:
                parts.append(raw)
            else:
                closing, name = match.group(1), match.group(2).lower()
                if closing:
                    if stack and stack[-1] == name:
                        stack.pop()
                    parts.append(raw)
                else:
                    parent = stack[-1] if stack else None
                    if name == "code" and parent == "pre":
                        cls_match = _CODE_OPEN_CLASS_RE.fullmatch(raw)
                        if cls_match is not None:
                            cls = cls_match.group("cls")
                            if (
                                not cls.startswith("language-")
                                and _CODE_LANG_TOKEN_RE.fullmatch(cls)
                            ):
                                quote = cls_match.group("q")
                                raw = f"<code class={quote}language-{cls}{quote}>"
                    stack.append(name)
                    parts.append(raw)
        cursor = end
    if cursor < len(html_text):
        parts.append(html_text[cursor:])
    return "".join(parts)


def translator_supports_protected_context(translator) -> bool:
    """True iff ``translator.translate_protected_async`` accepts a ``context`` arg."""
    fn = getattr(translator, "translate_protected_async", None)
    if fn is None:
        return False
    try:
        return "context" in signature(fn).parameters
    except (TypeError, ValueError):
        return False

# Retry without formatting only for unusable model content, never for quota,
# network, authorization or freshness failures.
HTML_CONTENT_FAILURE_REASONS = frozenset({"invalid_response", "html_integrity"})


class TelegramHTMLIntegrityError(ValueError):
    """Translated HTML no longer contains the protected source structure."""


def telegram_html_has_tags(html_text: str | None) -> bool:
    """True when ``html_text`` contains at least one HTML tag."""
    return bool(html_text and _HTML_TAG_PRESENT_RE.search(html_text))


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

    normalized = normalize_telegram_html(html_text)
    segments: list[str] = []
    structures: list[ProtectedStructure] = []
    cursor = 0
    for start, end in _structural_spans(normalized):
        segments.append(normalized[cursor:start])
        raw = normalized[start:end]
        structures.append(ProtectedStructure(raw, unescape(raw) if raw.startswith("&") else ""))
        cursor = end
    segments.append(normalized[cursor:])
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
    translator,
    html_text: str,
    *,
    to_chinese: bool = False,
    before_llm_call=None,
    execution_budget=None,
    context=None,
) -> str:
    protected = protect_telegram_html(html_text)
    try:
        kwargs = {"to_chinese": to_chinese, "before_llm_call": before_llm_call}
        if execution_budget is not None:
            kwargs["execution_budget"] = execution_budget
        if context is not None and translator_supports_protected_context(translator):
            kwargs["context"] = context
        translated = await translator.translate_protected_async(
            protected, **kwargs
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
        parent = self.stack[-1] if self.stack else None
        if tag not in ALLOWED_TAGS or not self._attrs_allowed(tag, dict(attrs), parent=parent):
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
    def _attrs_allowed(
        tag: str, attrs: dict[str, str | None], *, parent: str | None = None
    ) -> bool:
        if tag == "a":
            return set(attrs) == {"href"} and bool(attrs.get("href"))
        if tag == "span":
            return set(attrs) == {"class"} and attrs.get("class") == "tg-spoiler"
        if tag == "tg-emoji":
            return set(attrs) == {"emoji-id"} and bool(attrs.get("emoji-id"))
        if tag == "tg-time":
            if not set(attrs) <= {"unix", "format"} or "unix" not in attrs:
                return False
            unix_val = attrs.get("unix")
            if not isinstance(unix_val, str) or not _TG_TIME_UNIX_RE.fullmatch(unix_val):
                return False
            if "format" in attrs:
                fmt_val = attrs.get("format")
                if fmt_val is None or not _is_valid_tg_time_format(fmt_val):
                    return False
            return True
        if tag == "code":
            if not attrs:
                return True
            if set(attrs) != {"class"}:
                return False
            cls = attrs.get("class")
            if not isinstance(cls, str) or not cls:
                return False
            if cls.startswith("language-"):
                lang = cls.removeprefix("language-")
                return bool(lang and _CODE_LANG_TOKEN_RE.fullmatch(lang))
            # PTB 22.8 renders MessageEntity.PRE with a language as
            # `<pre><code class="{language}">...</code></pre>`.
            if parent == "pre":
                return bool(_CODE_LANG_TOKEN_RE.fullmatch(cls))
            return False
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


# --------------------------------------------------------------------------
# Paragraph-aware, tag-balanced Telegram HTML chunking
# --------------------------------------------------------------------------

def _tokenize_html(html_text: str) -> list[tuple[str, str, str]]:
    """Tokenize validated Telegram HTML into (kind, raw, tag_name_or_visible).

    Kinds:
    - ``"open"``: raw open tag, tag_name (lowercased)
    - ``"close"``: raw close tag, tag_name (lowercased)
    - ``"atomic"``: indivisible visible unit (entity, or atomic tag like tg-emoji / tg-time)
    - ``"text"``: plain text run without tags or entities
    """
    raw_tokens: list[tuple[str, str, str]] = []
    cursor = 0
    for start, end in _structural_spans(html_text):
        if start > cursor:
            raw_tokens.append(("text", html_text[cursor:start], html_text[cursor:start]))
        raw = html_text[start:end]
        if raw.startswith("&"):
            raw_tokens.append(("atomic", raw, unescape(raw)))
        else:
            match = _TAG_NAME_RE.match(raw)
            if match is None:
                raw_tokens.append(("text", raw, raw))
            else:
                closing, name = match.group(1), match.group(2).lower()
                raw_tokens.append(("close" if closing else "open", raw, name))
        cursor = end
    if cursor < len(html_text):
        raw_tokens.append(("text", html_text[cursor:], html_text[cursor:]))

    # Collapse atomic tags (<tg-emoji>...</tg-emoji>, <tg-time>...</tg-time>)
    # into single indivisible tokens so a split never cuts inside them.
    merged: list[tuple[str, str, str]] = []
    index = 0
    while index < len(raw_tokens):
        kind, raw, extra = raw_tokens[index]
        if kind == "open" and extra in _ATOMIC_TAGS:
            target = extra
            raw_parts = [raw]
            visible_parts: list[str] = []
            depth = 1
            index += 1
            while index < len(raw_tokens) and depth > 0:
                k, r, ex = raw_tokens[index]
                raw_parts.append(r)
                if k == "open" and ex == target:
                    depth += 1
                elif k == "close" and ex == target:
                    depth -= 1
                elif k in {"text", "atomic"}:
                    visible_parts.append(ex)
                index += 1
            merged.append(("atomic", "".join(raw_parts), "".join(visible_parts)))
            continue
        merged.append((kind, raw, extra))
        index += 1
    return merged


def _best_text_cut(
    text: str,
    limit_units: int,
    unit_counter: Callable[[str], int],
) -> int:
    """Choose a clean cut index in ``text`` fitting within ``limit_units``."""
    units = 0
    hard_cut = 0
    para_cut = 0
    para_units = 0
    line_cut = 0
    line_units = 0
    boundary_cut = 0
    boundary_units = 0
    min_para_units = max(1, int(limit_units * 0.25))
    min_line_units = max(1, int(limit_units * 0.5))
    min_boundary_units = max(1, int(limit_units * 0.7))

    for index, char in enumerate(text):
        char_units = unit_counter(char)
        if units + char_units > limit_units:
            break
        units += char_units
        pos = index + 1
        hard_cut = pos
        if char == "\n":
            line_cut = pos
            line_units = units
            if index > 0 and text[index - 1] == "\n":
                para_cut = pos
                para_units = units
        if char.isspace() or char in _SPLIT_BOUNDARY_CHARS:
            boundary_cut = pos
            boundary_units = units
    else:
        return len(text)

    if para_cut > 0 and para_units >= min_para_units:
        return para_cut
    if line_cut > 0 and line_units >= min_line_units:
        return line_cut
    if boundary_cut > 0 and boundary_units >= min_boundary_units:
        return boundary_cut
    return hard_cut


def _strip_empty_tag_shells(chunk: str) -> str:
    """Remove empty adjacent open/close pairs introduced at chunk boundaries."""
    # Only strips truly empty pairs created at boundaries, never altering visible text.
    pattern = re.compile(
        r"<([A-Za-z][A-Za-z0-9-]*)(?:\s[^<>]*)?></\1>"
    )
    previous = None
    current = chunk
    while current != previous:
        previous = current
        current = pattern.sub("", current)
    return current


def _split_text_at_paragraph_boundaries(text: str) -> list[str]:
    """Break a text token into paragraph-ending pieces while preserving exact bytes."""
    if "\n" not in text:
        return [text]
    pieces: list[str] = []
    start = 0
    length = len(text)
    index = 0
    while index < length:
        if text[index] == "\n":
            end = index + 1
            while end < length and text[end] == "\n":
                end += 1
            pieces.append(text[start:end])
            start = end
            index = end
        else:
            index += 1
    if start < length:
        pieces.append(text[start:])
    return pieces


def split_telegram_html(
    html_text: str,
    *,
    limit: int = TELEGRAM_TEXT_MESSAGE_LIMIT,
    unit_counter: Callable[[str], int] = telegram_text_units,
    max_raw_units: int | None = None,
) -> list[str] | None:
    """Split valid Telegram HTML into balanced chunks within ``limit`` visible units.

    Prefers paragraph boundaries (``\\n\\n``), then line breaks, then punctuation
    or whitespace. Any tags open across a cut are closed cleanly at the end of
    the chunk and reopened with their original attributes at the start of the
    next chunk. When ``max_raw_units`` is set, each chunk's raw serialized HTML
    also stays within ``max_raw_units``. Returns ``None`` if ``html_text`` is
    invalid or cannot be split safely (callers should then fall back to
    plain-text splitting).
    """
    if limit < 1 or not validate_telegram_html(html_text):
        return None
    if max_raw_units is not None and max_raw_units < 1:
        return None
    original_visible = strip_telegram_html(html_text)
    if unit_counter(original_visible) <= limit and (
        max_raw_units is None or unit_counter(html_text) <= max_raw_units
    ):
        return [html_text]

    tokens = _tokenize_html(html_text)
    # Expand plain text runs along newline boundaries so paragraph breaks
    # become natural decision points in the token stream.
    stream: list[tuple[str, str, str]] = []
    for kind, raw, extra in tokens:
        if kind == "text":
            for piece in _split_text_at_paragraph_boundaries(raw):
                stream.append(("text", piece, piece))
        else:
            stream.append((kind, raw, extra))

    chunks: list[str] = []
    open_stack: list[tuple[str, str]] = []
    buf: list[str] = []
    buf_units = 0
    buf_raw_units = 0
    # Best rollback point inside ``buf``: (buf_len, buf_units, stack_snapshot, priority)
    # Priority 2 = paragraph break (\n\n), 1 = line break (\n).
    best_break: tuple[int, int, list[tuple[str, str]], int] | None = None

    def close_tags(stack: list[tuple[str, str]]) -> str:
        return "".join(f"</{name}>" for name, _ in reversed(stack))

    def open_tags(stack: list[tuple[str, str]]) -> str:
        return "".join(raw_open for _, raw_open in stack)

    def close_units(stack: list[tuple[str, str]]) -> int:
        return unit_counter(close_tags(stack)) if stack else 0

    def remaining_budget(stack: list[tuple[str, str]]) -> int:
        vis_rem = limit - buf_units
        if max_raw_units is None:
            return vis_rem
        raw_rem = max_raw_units - buf_raw_units - close_units(stack)
        return min(vis_rem, raw_rem)

    def reset_buf(stack: list[tuple[str, str]]) -> None:
        nonlocal buf, buf_units, buf_raw_units, best_break
        opened = open_tags(stack)
        buf = [opened] if opened else []
        buf_units = 0
        buf_raw_units = unit_counter(opened) if opened else 0
        best_break = None

    def emit_chunk(parts: list[str], closing_stack: list[tuple[str, str]]) -> None:
        raw_chunk = _strip_empty_tag_shells("".join(parts) + close_tags(closing_stack))
        if raw_chunk:
            chunks.append(raw_chunk)

    def record_break_if_applicable(piece: str) -> None:
        nonlocal best_break
        if buf_units <= 0:
            return
        if piece.endswith("\n\n"):
            best_break = (len(buf), buf_units, list(open_stack), 2)
        elif piece.endswith("\n"):
            if best_break is None or best_break[3] <= 1:
                best_break = (len(buf), buf_units, list(open_stack), 1)

    pending: list[tuple[str, str, str]] = list(reversed(stream))
    safety_steps = 0
    max_steps = max(len(stream) * 20, 10_000)

    while pending:
        safety_steps += 1
        if safety_steps > max_steps:
            return None
        kind, raw, extra = pending.pop()

        if kind == "open":
            extra_raw = unit_counter(raw) + unit_counter(f"</{extra}>")
            if (
                max_raw_units is not None
                and buf_units > 0
                and buf_raw_units + extra_raw + close_units(open_stack) > max_raw_units
            ):
                emit_chunk(buf, open_stack)
                reset_buf(open_stack)
            buf.append(raw)
            buf_raw_units += unit_counter(raw)
            open_stack.append((extra, raw))
            continue

        if kind == "close":
            if not open_stack or open_stack[-1][0] != extra:
                return None
            buf.append(raw)
            buf_raw_units += unit_counter(raw)
            open_stack.pop()
            continue

        if kind == "atomic":
            token_units = unit_counter(extra)
            token_raw_units = unit_counter(raw)
            if token_units > limit:
                return None
            if max_raw_units is not None and token_raw_units > max_raw_units:
                return None
            fits_visible = buf_units + token_units <= limit
            fits_raw = (
                max_raw_units is None
                or buf_raw_units + token_raw_units + close_units(open_stack) <= max_raw_units
            )
            if not (fits_visible and fits_raw):
                if buf_units == 0:
                    return None
                if (
                    best_break is not None
                    and best_break[1]
                    >= max(1, int(limit * (0.25 if best_break[3] == 2 else 0.5)))
                ):
                    cut_len, _cut_units, cut_stack, _prio = best_break
                    head = buf[:cut_len]
                    tail_items = buf[cut_len:]
                    tail_stack = list(open_stack)
                    emit_chunk(head, cut_stack)
                    opened = open_tags(cut_stack)
                    buf = ([opened] if opened else []) + tail_items
                    joined_buf = "".join(buf)
                    buf_raw_units = unit_counter(joined_buf)
                    buf_units = unit_counter(
                        strip_telegram_html(joined_buf + close_tags(tail_stack))
                    )
                    best_break = None
                    if (
                        buf_units + token_units > limit
                        or (
                            max_raw_units is not None
                            and buf_raw_units + token_raw_units + close_units(tail_stack)
                            > max_raw_units
                        )
                    ):
                        emit_chunk(buf, tail_stack)
                        reset_buf(tail_stack)
                else:
                    emit_chunk(buf, open_stack)
                    reset_buf(open_stack)
            buf.append(raw)
            buf_units += token_units
            buf_raw_units += token_raw_units
            continue

        # kind == "text"
        text_units = unit_counter(raw)
        rem = remaining_budget(open_stack)
        if text_units <= rem:
            buf.append(raw)
            buf_units += text_units
            buf_raw_units += text_units
            record_break_if_applicable(raw)
            continue

        if (
            buf_units > 0
            and best_break is not None
            and best_break[1] >= max(1, int(limit * (0.25 if best_break[3] == 2 else 0.5)))
        ):
            cut_len, _cut_units, cut_stack, _prio = best_break
            head = buf[:cut_len]
            tail_items = buf[cut_len:]
            tail_stack = list(open_stack)
            emit_chunk(head, cut_stack)
            opened = open_tags(cut_stack)
            buf = ([opened] if opened else []) + tail_items
            joined_buf = "".join(buf)
            buf_raw_units = unit_counter(joined_buf)
            buf_units = unit_counter(
                strip_telegram_html(joined_buf + close_tags(tail_stack))
            )
            best_break = None
            pending.append((kind, raw, extra))
            continue

        if buf_units > 0 and text_units <= limit and rem < int(limit * 0.25):
            emit_chunk(buf, open_stack)
            reset_buf(open_stack)
            pending.append((kind, raw, extra))
            continue

        cut = _best_text_cut(raw, rem, unit_counter) if rem > 0 else 0
        if cut <= 0:
            if buf_units == 0:
                return None
            emit_chunk(buf, open_stack)
            reset_buf(open_stack)
            pending.append((kind, raw, extra))
            continue

        head_text = raw[:cut]
        tail_text = raw[cut:]
        head_units = unit_counter(head_text)
        buf.append(head_text)
        buf_units += head_units
        buf_raw_units += head_units
        emit_chunk(buf, open_stack)
        reset_buf(open_stack)
        if tail_text:
            pending.append(("text", tail_text, tail_text))

    if buf:
        emit_chunk(buf, open_stack)

    if not chunks:
        return None
    # Strict post-condition verification: every chunk must be valid Telegram HTML,
    # within limit (and max_raw_units if set), and preserve exact visible text.
    rebuilt_visible_parts: list[str] = []
    for chunk in chunks:
        if not validate_telegram_html(chunk):
            return None
        if max_raw_units is not None and unit_counter(chunk) > max_raw_units:
            return None
        visible = strip_telegram_html(chunk)
        if unit_counter(visible) > limit:
            return None
        rebuilt_visible_parts.append(visible)
    if "".join(rebuilt_visible_parts) != original_visible:
        return None
    return chunks


def _extract_outer_whitespace(html_chunk: str) -> tuple[str, str, str]:
    """Split pure leading/trailing top-level whitespace outside any tags."""
    length = len(html_chunk)
    start = 0
    while start < length and html_chunk[start] in " \t\r\n":
        start += 1
    end = length
    while end > start and html_chunk[end - 1] in " \t\r\n":
        end -= 1
    return html_chunk[:start], html_chunk[start:end], html_chunk[end:]


def _extract_boundary_whitespace(html_chunk: str) -> tuple[str, str, str]:
    """Extract leading/trailing whitespace outside tags AND at spanning tag edges.

    When a single tag such as ``<b>...\\n\\n...</b>`` spans a segment split
    boundary, ``split_telegram_html`` emits ``<b>para1\\n\\n</b>`` and
    ``<b>para2</b>``. Extracting the ``\\n\\n`` sitting immediately before the
    trailing closing tags (or right after leading opening tags) preserves
    inter-segment blank lines verbatim even if the translator strips edge
    whitespace from translated segments.
    """
    outer_lead, middle, outer_trail = _extract_outer_whitespace(html_chunk)
    if not middle:
        return outer_lead, "", outer_trail

    tokens = _tokenize_html(middle)
    if not tokens:
        return outer_lead, middle, outer_trail

    inner_lead = ""
    inner_trail = ""

    # Leading whitespace immediately after pure opening tags (e.g. <b>\n\nText).
    lead_idx = 0
    while (
        lead_idx < len(tokens)
        and tokens[lead_idx][0] == "open"
        and tokens[lead_idx][2] not in _ATOMIC_TAGS
    ):
        lead_idx += 1
    if lead_idx < len(tokens) and tokens[lead_idx][0] == "text":
        raw_text = tokens[lead_idx][1]
        ws_end = 0
        while ws_end < len(raw_text) and raw_text[ws_end] in " \t\r\n":
            ws_end += 1
        if ws_end > 0:
            inner_lead = raw_text[:ws_end]
            rest = raw_text[ws_end:]
            tokens[lead_idx] = ("text", rest, rest)

    # Trailing whitespace immediately before pure closing tags (e.g. Text\n\n</b>).
    trail_idx = len(tokens) - 1
    while (
        trail_idx >= 0
        and tokens[trail_idx][0] == "close"
        and tokens[trail_idx][2] not in _ATOMIC_TAGS
    ):
        trail_idx -= 1
    if trail_idx >= 0 and tokens[trail_idx][0] == "text":
        raw_text = tokens[trail_idx][1]
        ws_start = len(raw_text)
        while ws_start > 0 and raw_text[ws_start - 1] in " \t\r\n":
            ws_start -= 1
        if ws_start < len(raw_text):
            inner_trail = raw_text[ws_start:]
            rest = raw_text[:ws_start]
            tokens[trail_idx] = ("text", rest, rest)

    if not inner_lead and not inner_trail:
        return outer_lead, middle, outer_trail

    core = "".join(raw for _kind, raw, _extra in tokens if raw)
    lead = outer_lead + inner_lead
    trail = inner_trail + outer_trail
    if not strip_telegram_html(core):
        return lead, "", trail
    if not validate_telegram_html(core):
        return outer_lead, middle, outer_trail
    if lead + strip_telegram_html(core) + trail != strip_telegram_html(html_chunk):
        return outer_lead, middle, outer_trail
    return lead, core, trail


def split_telegram_html_segments(
    html_text: str,
    *,
    limit: int,
) -> list[tuple[str, str, str]] | None:
    """Split long HTML for translation, isolating inter-segment whitespace.

    Returns ``[(leading_ws, core_html, trailing_ws), ...]`` so callers can
    translate ``core_html`` and reattach the exact original blank lines and
    separators verbatim even if the model strips trailing newlines.
    """
    normalized = normalize_telegram_html(html_text)
    chunks = split_telegram_html(normalized, limit=limit, unit_counter=len)
    if not chunks:
        return None
    result: list[tuple[str, str, str]] = []
    for chunk in chunks:
        lead, core, trail = _extract_boundary_whitespace(chunk)
        if core and not validate_telegram_html(core):
            return None
        result.append((lead, core, trail))
    return result
