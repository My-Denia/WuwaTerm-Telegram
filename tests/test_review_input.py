from __future__ import annotations

from wuwaterm_telegram.review_input import (
    KIND_BAD_SEPARATOR,
    KIND_LAST,
    KIND_PAIR,
    KIND_REPLY_TARGET,
    KIND_REPLY_TRANSLATION,
    KIND_USAGE,
    command_body,
    parse_review_input,
)


def parse(text, replied=None, bot=False):
    return parse_review_input(command_body(text), replied_text=replied, replied_is_bot=bot)


def test_command_body_keeps_line_breaks_and_strips_the_bot_suffix():
    assert command_body("/review@WuwaBot 甲\n\n乙") == "甲\n\n乙"
    assert command_body("/check\n甲") == "甲"


def test_separator_line_splits_source_and_target_and_keeps_paragraphs():
    got = parse("/review 第一段\n\n第二段\n---\nPara one\n\nPara two")
    assert (got.kind, got.source, got.target) == (KIND_PAIR, "第一段\n\n第二段", "Para one\n\nPara two")


def test_separator_tolerates_crlf_and_spaces():
    got = parse("/review 甲\r\n  ----  \r\nA")
    assert (got.kind, got.source, got.target) == (KIND_PAIR, "甲", "A")


def test_hyphens_inside_a_line_are_text_not_a_separator():
    got = parse("/review 甲 --- 乙", replied="原文")
    assert got.kind == KIND_REPLY_TARGET and got.target == "甲 --- 乙"


def test_two_separators_or_an_empty_side_is_refused():
    assert parse("/review a\n---\nb\n---\nc").kind == KIND_BAD_SEPARATOR
    assert parse("/review a\n---\n").kind == KIND_BAD_SEPARATOR
    assert parse("/review \n---\nb").kind == KIND_BAD_SEPARATOR


def test_reply_forms():
    assert parse("/review Hello", replied="你好").kind == KIND_REPLY_TARGET
    assert parse("/review", replied="Bot says", bot=True).kind == KIND_REPLY_TRANSLATION
    assert parse("/review", replied="a human wrote this").kind == KIND_USAGE
    assert parse("/review").kind == KIND_USAGE


def test_last_keyword_wins_over_a_reply():
    assert parse("/review LAST", replied="x").kind == KIND_LAST
