"""A fake Core for `/review` tests: scripted answers, no model, no network."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Mapping

from wuwaterm_telegram.review_backend import ReviewBackendError, TermReview
from wuwaterm_telegram.review_model import (
    PAIR_NEEDS_MANUAL_SPLIT,
    PAIR_READY,
    PairInfo,
)

FIXTURES = Path(__file__).parent / "fixtures" / "semantic-review-v1"


def load_fixture(name: str) -> tuple[dict, dict]:
    request = json.loads((FIXTURES / f"{name}-request.json").read_text(encoding="utf-8"))
    response = json.loads((FIXTURES / f"{name}-response.json").read_text(encoding="utf-8"))
    return request, response


def pair(index: int, source: str, target: str, s0: int, t0: int, *, status=PAIR_READY, hard_cut=False, limit=1800) -> PairInfo:
    return PairInfo(
        index=index, status=status, hard_cut=hard_cut,
        source_start=s0, source_end=s0 + len(source), source_core_start=s0, source_core_end=s0 + len(source),
        target_start=t0, target_end=t0 + len(target), target_core_start=t0, target_core_end=t0 + len(target),
        limit=limit,
    )


def wire_response(source: str, target: str, *, find: str, replacement: str | None = "REPL", confidence="high") -> dict:
    """A contract-shaped answer that flags the first `find` in `target`."""
    start = target.index(find)
    span = {"start": start, "end": start + len(find), "text": find}
    return {
        "request_id": "fake", "review_version": "semantic-review-v1",
        "source_revision": "0" * 64, "target_revision": "1" * 64,
        "term_basis": {"dictionary_revision": "2" * 64, "matcher_revision": "3" * 64},
        "status": "complete", "status_reason": None, "dropped_unanchored": 0,
        "checked": ["omission"],
        "issues": [{
            "id": "a" * 31 + str(start % 10), "fingerprint": "b" * 32, "category": "meaning_shift",
            "origin": "model", "confidence": confidence,
            "source_span": None, "target_span": span,
            "source_excerpt": None, "target_excerpt": find,
            "rationale": "译文与原文意思不一致。",
            "suggestion": (
                {"target_span": span, "replacement": replacement, "term_effect": "none"}
                if replacement is not None else None
            ),
            "suggestion_withheld": None, "alternatives": [],
        }],
        "truncated": False, "truncated_reason": None, "omitted_issues": 0,
    }


class FakeBackend:
    """Pairs on blank lines; answers each segment with `answer(source, target)`."""

    def __init__(self, *, semantic=True, answer=None, plan=None, errors=None, delay=0.0):
        self.semantic = semantic
        self.answer = answer
        self.plan = plan
        self.errors = dict(errors or {})  # call number (1-based) -> error code
        self.delay = delay
        self.calls: list[dict[str, Any]] = []
        self.term_calls = 0
        self._in_flight = 0
        self.max_in_flight = 0

    def semantic_available(self) -> bool:
        return self.semantic

    def plan_pairs(self, source: str, target: str) -> list[PairInfo]:
        if self.plan is not None:
            return list(self.plan(source, target))
        s_parts, t_parts = source.split("\n\n"), target.split("\n\n")
        if len(s_parts) != len(t_parts):
            return [pair(0, source, target, 0, 0, status=PAIR_NEEDS_MANUAL_SPLIT)]
        pairs, s0, t0 = [], 0, 0
        for i, (s, t) in enumerate(zip(s_parts, t_parts)):
            pairs.append(pair(i, s, t, s0, t0))
            s0 += len(s) + 2
            t0 += len(t) + 2
        return pairs

    async def term_review(self, source: str, target: str, direction: str) -> TermReview:
        self.term_calls += 1
        return TermReview()

    async def semantic_review(self, source, target, *, direction, explain_language, context_before) -> Mapping[str, Any]:
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            self.calls.append({"source": source, "target": target, "direction": direction, "context_before": context_before})
            if self.delay:
                await asyncio.sleep(self.delay)
            code = self.errors.get(len(self.calls))
            if code is not None:
                raise ReviewBackendError(code)
            if self.answer is not None:
                return self.answer(source, target)
            return wire_response(source, target, find=target.split()[0] if target.split() else target)
        finally:
            self._in_flight -= 1
