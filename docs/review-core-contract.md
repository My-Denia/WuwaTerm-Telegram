# `/review`: what it relies on in Core

`/review` is built for Core 0.13.0 (ADR 0018 and 0019, `docs/segmentation-v1.md`,
`contracts/semantic-review-v1/`); the review contract is the one Core 0.12.0
introduced, and Core 0.13.0 leaves it unchanged. All Core access is in
`review_backend.py`; `core_compat.REVIEW_CORE_SYMBOLS` is the symbol list that
`verify_core_compatibility` requires at start-up.

## What the adapter uses (checked against Core v0.13.0, commit e5b7f1a8)

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
  gated on `len(plain) <= LLM_INPUT_CHAR_LIMIT`), keeping plain single-segment,
  HTML fallback, and long split chunks bounded to their reserved admission calls.
- Long-text `/tr`: Telegram no longer passes a `splitter` (Core ignores it).
  Core cuts by segmentation-v1, keeps the sender's separators and carries the
  previous segment as context. Core's guard reads a per-line share ratio,
  `(answer line / answer total) / (source line / source total)`, and looks for a
  merge and a split over the whole segment at any distance and in either order;
  an answer is regrouped into the source's paragraphs only when neither appears,
  so a distant merge-and-split that shifts a line across a paragraph boundary is
  refused, and the model's own blank lines are kept (`MY-359`).
- Telegram wrapper cleanup: the adapter, not Core, removes screenshot markers
  (version tags, spoiler labels, quote bars). Core `0.13.0` provides
  `wuwaterm.normalize.strip_telegram_wrapper_noise(..., keep_paragraphs=True)`
  and stops deleting these markers during generic preparation. The pin is
  `0.13.0`, so `verify_core_compatibility` requires the symbol at start-up;
  `core_compat.strip_telegram_wrapper_noise` still degrades to
  `normalize_user_text` for a Core older than `WRAPPER_STRIP_CORE_VERSION`,
  which the compatibility check no longer accepts. `/tr` strips the plain text
  Core reads (dictionary stage, automatic direction, size gate and plain
  fallback) on every path; a rendered-HTML payload stays as written, because
  Core's markup translator works on it alone. Linked-channel posts
  keep a second text, `pipeline_plain`: it is what the dictionary lookup, the
  direction check, the call reservation, the over-2,000-character split and
  every plain stage (plain-only post, the retry after a failed HTML attempt)
  use, while the HTML attempt is translated exactly as written, because
  stripping there would desync the markup its placeholders must be restored
  into. The marker patterns on `0.13.0` are narrower than on `0.12.0`: a
  line-leading comparator (`>50% Energy Regen`) and a hashtag that only starts
  with a marker word (`#spoilersafe ...`, `#剧透警告 今汐`) are content and
  survive; `0.12.0` stripped them. The tests for these cases run on the pin
  with no skip.
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
