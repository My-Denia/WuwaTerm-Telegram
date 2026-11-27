# Core contract: channel auto-translation, `/tr`, and `/review`

This adapter targets WuwaTerm Core `>=0.16.0,<1.0` (verified against Core `0.16.0`, commit `8565b48b`, ADR 0018, 0019, and 0022, `docs/segmentation-v1.md`, `contracts/semantic-review-v1/`). Runtime startup checks the semantic version range plus runtime capability probing (`core_compat.py`) instead of rejecting newer compatible `0.x` Core releases. Service-consumption mode (`core_backend.py`, `HttpCoreBackend`) negotiates optional capabilities via `GET /v1/capabilities` without modifying `GET /v1/meta`'s exact-key contract.

## What the adapter uses (checked against Core v0.16.0, commit 8565b48b)

- `semantic_review_async(service, translator, SemanticReviewJob, *, request_id,
  before_llm_call, offload, execution_budget)` returns a
  `SemanticReviewOutcome` whose `body` is the published response and whose
  `error_code` is one of the translation error codes. The adapter creates one
  run-scoped `OperationExecutionBudget(max_total_calls=len(ready),
  max_corrective_calls=0, max_corrective_calls_per_segment=0)` in
  `review_flow.py`, cancels it on authorization abort or task cancellation,
  and maps `operation_budget` to `llm_budget_exhausted` and `cancelled` to
  `ReviewAborted`.
- `semantic_review_enabled()`: semantic review is experimental and **off by
  default** (`WUWATERM_SEMANTIC_REVIEW_ENABLED`). With the flag off, or no model
  configured, `/review` runs the dictionary track only and says semantic issues
  are not enabled; no model is called and the `/tr` review button is not shown.
- `wuwaterm.segmentation`: `SEGMENT_LIMIT` and `pair_segments(source, target,
  limit)`; statuses `ready`, `needs_manual_split`, `untranslated`,
  `unpaired_target`. A ready pair is re-checked against its limit before any
  request.
- `review_pair(..., review_version="review-v2")` for the dictionary track;
  `direction` is the language of the translation. The term findings come from
  Core's CJK matcher (`wuwaterm-cjk-matcher-v9` through Core 0.12.0,
  `wuwaterm-cjk-matcher-v10` on 0.13.0; only the revision label moved, the
  admission decisions are the same), so single-character CJK names are
  admitted only at plausible right boundaries and translation reference
  admission and `review-v2` share the same matcher helper.
- `/tr` & rich HTML: `translate_request_async` activates Core's single-segment
  `OperationExecutionBudget` (at most 2 upstream calls and 1 shared corrective
  slot across HTML-to-plain fallback, timeout fallback, and paragraph repair),
  so opt-in `WUWATERM_TRANSLATION_THINKING_EN_ZH=auto` cannot trigger a 3-call
  blowup; defaults stay `omit` (`en->zh`) and `disabled` (`zh->en`).
- Linked-channel posts (`channel.py`): `channel_post_handler` aligns an outer
  `OperationExecutionBudget` with `ChannelRuntime.reserve(...)` (`allow_plain_fallback`
  gated on `len(plain) <= LLM_INPUT_CHAR_LIMIT`). Long posts (>2,000 chars) use
  paragraph-aware balanced HTML segmentation when markup is present, or Core's
  `segmentation-v1` + `TranslationContext(before=ContextBefore(...))` for plain
  text, preserving paragraph breaks and carrying prior segment context across
  chunks. Long HTML replies (>4,096 UTF-16 units) split into balanced HTML
  chunks rather than stripping all markup, falling back to plain text only when
  tag validation or Telegram delivery fails. Before delivery, zero-cost
  `wuwaterm.structural_checks.check_segment_structure` (`structure-check-v1`)
  verifies numbers, paragraphs, placeholders, and non-empty output. Capacity
  rejections and transient LLM failures enqueue into a bounded recoverable
  `ChannelTaskQueue` (`channel_queue.py`) with exponential backoff and edit
  superseding.
- Telegram HTML compatibility (`telegram_html.py`): supports PTB 22.8 / Bot API
  10.0 rich entities including `blockquote`/`expandable`, `<tg-emoji>`,
  `<tg-time unix="..." format="...">` (validated attributes), and
  `<pre><code class="{language}">` alongside `language-xxx`.
- Telegram wrapper cleanup: the adapter owns screenshot marker removal via Core's
  native `wuwaterm.normalize.strip_telegram_wrapper_noise(..., keep_paragraphs=True)`.
  The pre-0.13.0 `normalize_user_text` fallback has been removed (`MY-360`
  documentation aligned). `/tr` and linked-channel plain routes strip markers;
  HTML payloads stay untouched so placeholders remain aligned. Line-leading
  comparators (`>50% Energy Regen`) and non-marker hashtags (`#spoilersafe ...`,
  `#剧透警告 今汐`) are preserved.
- Automatic direction (Core 0.13.0): with no `--to`, Core reads the direction
  from the normalized user text, and text containing CJK characters goes to
  Chinese only when its Latin letters outnumber the CJK characters more than
  four to one (`0.12.0` translated to English whenever any CJK character was
  present). Wrapper markers left in the text count as Latin letters, so the
  adapter strips them from the text Core reads (including a formatted
  message's plain text), and linked-channel posts pass their own direction to
  Core.

Telegram `0.5.0` aligns `/tr`, `channel.py`, and `/review` with Core `0.12.0`'s
`OperationExecutionBudget`, cancellation boundaries, and failure taxonomy while
preserving Telegram's shared-pointer delivery, resend, and pre-send-only retry
behavior (`0.4.1`).

Telegram `0.6.0` moves the pin to Core `0.13.0`: the adapter owns the wrapper
cleanup and the marker patterns are the narrower 0.13.0 ones (above), and
automatic direction follows Core's new rule. No environment variable the bot
reads changed.

## Not done

- `before_llm_call` is not passed on `/review`: per-segment concurrency/slot
  accounting runs inside Core, while `review_flow.py` passes the run-scoped
  `execution_budget` and re-checks authorization between segments and before
  delivery.
- No precedents are sent (there is no manuscript in Telegram).
- A real model was never called from this repository's tests; the engine runs
  against a mock HTTP model.

## Limits and costs

- Over 2,000 characters a side, a public member is refused; owner and group
  admins may send up to 12 segments (21,600 characters) per command.
- A command that starts a run spends one slot of the chat's rate limit however
  many segments it has. Requests go one at a time; a rate limit or an exhausted
  budget stops the run with "已完成 x/y 段", and the same content may be
  submitted again.
- A segment answer that does not meet the contract (a span whose text is not
  the submitted slice, an unknown category or `term_effect`, a suggestion that
  is also withheld) is discarded and counted as not reviewed.
- Accepting a suggestion re-checks that the translation still holds the span's
  full text and that accepted ranges do not overlap.
