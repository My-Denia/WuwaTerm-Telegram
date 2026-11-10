"""Read what a user typed after `/review`. Pure text handling, no Telegram types.

`context.args` is not used for this: it splits on whitespace and loses the
line breaks that paragraph pairing depends on. The text is read from the raw
message instead.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

COMMAND_PREFIX_RE = re.compile(r"^/[A-Za-z0-9_]+(?:@[A-Za-z0-9_]+)?[ \t\r\n]*")
# A line holding only `---` (three or more hyphens), tolerating surrounding
# whitespace and CRLF. Hyphens inside a sentence never match.
SEPARATOR_RE = re.compile(r"(?m)^[ \t]*-{3,}[ \t]*\r?$")

KIND_PAIR = "pair"  # source and target are both in the message
KIND_REPLY_TARGET = "reply_target"  # replied message is the source; the body is the target
KIND_REPLY_TRANSLATION = "reply_translation"  # reply to a bot translation, no body
KIND_LAST = "last"
KIND_USAGE = "usage"
KIND_BAD_SEPARATOR = "bad_separator"


@dataclass(frozen=True)
class ReviewInput:
    kind: str
    source: str = ""
    target: str = ""


def command_body(message_text: str | None) -> str:
    """The text after the command, with its line breaks intact."""
    text = message_text or ""
    return COMMAND_PREFIX_RE.sub("", text, count=1)


def parse_review_input(body: str, *, replied_text: str | None, replied_is_bot: bool) -> ReviewInput:
    stripped = body.strip()
    if stripped.lower() == "last":
        return ReviewInput(KIND_LAST)
    matches = list(SEPARATOR_RE.finditer(body))
    if matches:
        if len(matches) > 1:
            return ReviewInput(KIND_BAD_SEPARATOR)
        sep = matches[0]
        source, target = body[: sep.start()].strip(), body[sep.end() :].strip()
        if not source or not target:
            return ReviewInput(KIND_BAD_SEPARATOR)
        return ReviewInput(KIND_PAIR, source, target)
    if stripped and replied_text and replied_text.strip():
        return ReviewInput(KIND_REPLY_TARGET, replied_text.strip(), stripped)
    if not stripped and replied_is_bot:
        return ReviewInput(KIND_REPLY_TRANSLATION)
    return ReviewInput(KIND_USAGE)
