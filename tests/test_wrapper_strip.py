"""Telegram wrapper cleanup and Core >=0.16.0,<1.0 compatibility checks."""
from __future__ import annotations

import importlib

import pytest

from wuwaterm_telegram import core_compat
from wuwaterm_telegram.core_compat import (
    CoreCompatibilityError,
    is_compatible_core_version,
    probe_local_core_capabilities,
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


def test_missing_wrapper_strip_symbol_raises_instead_of_legacy_fallback(monkeypatch):
    """Pre-0.13.0 legacy fallback is gone; missing symbol fails loudly."""
    normalize = importlib.import_module("wuwaterm.normalize")
    monkeypatch.delattr(normalize, "strip_telegram_wrapper_noise", raising=False)
    assert not wrapper_strip_is_native()
    with pytest.raises(CoreCompatibilityError, match="strip_telegram_wrapper_noise"):
        strip_telegram_wrapper_noise("[spoiler] 今汐登场")


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


def test_semver_range_accepts_newer_compatible_core_and_rejects_out_of_range(monkeypatch):
    assert is_compatible_core_version("0.16.0")
    assert is_compatible_core_version("0.16.1")
    assert is_compatible_core_version("0.17.0")
    assert is_compatible_core_version("0.99.9")
    assert not is_compatible_core_version("0.15.9")
    assert not is_compatible_core_version("0.13.0")
    assert not is_compatible_core_version("1.0.0")
    assert not is_compatible_core_version("not-a-version")

    # A newer compatible 0.x Core passes runtime compatibility checks.
    monkeypatch.setattr(core_compat.metadata, "version", lambda _name: "0.17.2")
    verify_core_compatibility()

    # An older Core or breaking 1.0.0 Core is rejected.
    monkeypatch.setattr(core_compat.metadata, "version", lambda _name: "0.15.0")
    with pytest.raises(CoreCompatibilityError, match=">=0.16.0,<1.0"):
        verify_core_compatibility()

    monkeypatch.setattr(core_compat.metadata, "version", lambda _name: "1.0.0")
    with pytest.raises(CoreCompatibilityError, match=">=0.16.0,<1.0"):
        verify_core_compatibility()


def test_installed_core_ships_native_wrapper_strip_and_capabilities():
    assert core_compat.CORE_VERSION == "0.16.0"
    assert wrapper_strip_is_native()
    verify_core_compatibility()
    caps = probe_local_core_capabilities()
    assert "translation-context-v1" in caps
    assert "structure-check-v1" in caps


def test_missing_wrapper_strip_symbol_fails_compatibility(monkeypatch):
    normalize = importlib.import_module("wuwaterm.normalize")
    monkeypatch.delattr(normalize, "strip_telegram_wrapper_noise")
    assert not wrapper_strip_is_native()
    with pytest.raises(CoreCompatibilityError) as raised:
        verify_core_compatibility()
    assert "strip_telegram_wrapper_noise" in str(raised.value.__cause__)
