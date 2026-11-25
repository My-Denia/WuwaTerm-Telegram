"""Telegram wrapper cleanup survives Core 0.13.0's preparation change.

Core #318 stopped deleting screenshot markers inside generic text preparation
and exposes `wuwaterm.normalize.strip_telegram_wrapper_noise` instead, so the
adapter has to own the cleanup. Core 0.12.0 folded it into
`normalize_user_text` with wider marker patterns. The pin is Core 0.13.0, so
every assertion here runs against the narrower 0.13.0 patterns and none is
conditional on the installed Core.
"""
from __future__ import annotations

import importlib

import pytest

from wuwaterm_telegram import core_compat
from wuwaterm_telegram.core_compat import (
    CoreCompatibilityError,
    strip_telegram_wrapper_noise,
    verify_core_compatibility,
    wrapper_strip_is_native,
)


def test_wrapper_markers_are_stripped():
    cases = [
        ("[spolier] (WW 3.4) 这是一个测试。", "这是一个测试。"),
        ("[spoiler] 这是一个测试。", "这是一个测试。"),
        ("【剧透】这是一个测试。", "这是一个测试。"),
        ("(WW 3.4) 这是一个测试。", "这是一个测试。"),
        ("[Wuthering Waves 3.7]\n这是一个测试。", "这是一个测试。"),
        ("[Wuthering Waves 3.7]\n\n这是一个测试。", "这是一个测试。"),
        ("> 这是一个测试。", "这是一个测试。"),
        ("| 这是一个测试。", "这是一个测试。"),
        ("spoiler: hidden text", "hidden text"),
    ]
    for source, expected in cases:
        assert strip_telegram_wrapper_noise(source) == expected


def test_wrapper_strip_is_idempotent():
    once = strip_telegram_wrapper_noise("[Wuthering Waves 3.7]\n[spoiler]\n> 今汐登场")
    assert once == "今汐登场"
    assert strip_telegram_wrapper_noise(once) == once


def test_keep_paragraphs_preserves_blank_line_separators():
    wrapped = "(WW 3.4)\n[spoiler]\n> 第一段\n\n> 第二段"
    assert (
        strip_telegram_wrapper_noise(wrapped, keep_paragraphs=True)
        == "第一段\n\n第二段"
    )
    assert strip_telegram_wrapper_noise(wrapped) == "第一段\n第二段"


def _legacy_core_normalize(value: str) -> str:
    """Stand-in for Core 0.12.0: markers out, every line collapsed."""
    for marker in ("(WW 3.4)", "[spoiler]", ">"):
        value = value.replace(marker, "")
    lines = (line.strip() for line in value.splitlines())
    return "\n".join(line for line in lines if line)


def test_legacy_core_fallback_also_keeps_paragraphs(monkeypatch):
    """The pre-0.13.0 path must behave the same on the paragraph boundary."""
    normalize = importlib.import_module("wuwaterm.normalize")
    monkeypatch.delattr(normalize, "strip_telegram_wrapper_noise", raising=False)
    monkeypatch.setattr(normalize, "normalize_user_text", _legacy_core_normalize)
    assert not wrapper_strip_is_native()
    wrapped = "(WW 3.4)\n[spoiler]\n> 第一段\n\n> 第二段"
    assert (
        strip_telegram_wrapper_noise(wrapped, keep_paragraphs=True)
        == "第一段\n\n第二段"
    )
    assert strip_telegram_wrapper_noise(wrapped) == "第一段\n第二段"


def test_prose_that_only_looks_like_a_marker_is_preserved():
    cases = [
        ">50% Energy Regen",
        "#spoilersafe Please do not reveal the ending.",
        "#spoiler-free guide for players",
        "#剧透警告 今汐",
        "Wuthering Waves 2.1 brings a new region.",
    ]
    for source in cases:
        assert strip_telegram_wrapper_noise(source) == source


def test_version_compare_gates_the_symbol_requirement():
    assert core_compat._version_at_least("0.13.0", "0.13.0")
    assert core_compat._version_at_least("0.13.1", "0.13.0")
    assert not core_compat._version_at_least("0.12.0", "0.13.0")
    assert not core_compat._version_at_least("0.9.0", "0.13.0")


def test_pinned_core_ships_the_native_wrapper_strip_symbol():
    assert core_compat._version_at_least(
        core_compat.CORE_VERSION, core_compat.WRAPPER_STRIP_CORE_VERSION
    )
    assert wrapper_strip_is_native()
    verify_core_compatibility()


def test_missing_wrapper_strip_symbol_fails_compatibility(monkeypatch):
    normalize = importlib.import_module("wuwaterm.normalize")
    monkeypatch.delattr(normalize, "strip_telegram_wrapper_noise")
    assert not wrapper_strip_is_native()
    with pytest.raises(CoreCompatibilityError) as raised:
        verify_core_compatibility()
    assert "strip_telegram_wrapper_noise" in str(raised.value.__cause__)
