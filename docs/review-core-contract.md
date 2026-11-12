# `/review`: what it relies on in Core

`/review` is built for Core 0.8.2 (ADR 0018 and 0019, `docs/segmentation-v1.md`,
`contracts/semantic-review-v1/`). All Core access is in `review_backend.py`;
`core_compat.REVIEW_CORE_SYMBOLS` is the symbol list that
`verify_core_compatibility` requires at start-up.

## What the adapter uses (checked against Core v0.8.2, commit 21c061d)

- `semantic_review_async(service, translator, SemanticReviewJob, *, request_id,
  before_llm_call, offload)` returns a `SemanticReviewOutcome` whose `body` is
  the published response and whose `error_code` is one of the translation error
  codes. The adapter builds the job (no precedents; the previous segment as
  `ContextBefore`), runs it off the event loop and turns `error_code` or an
  `LLMTranslationError` into a `ReviewBackendError`.
- `semantic_review_enabled()`: semantic review is experimental and **off by
  default** (`WUWATERM_SEMANTIC_REVIEW_ENABLED`). With the flag off, or no model
  configured, `/review` runs the dictionary track only and says semantic issues
  are not enabled; no model is called and the `/tr` review button is not shown.
- `wuwaterm.segmentation`: `SEGMENT_LIMIT` and `pair_segments(source, target,
  limit)`; statuses `ready`, `needs_manual_split`, `untranslated`,
  `unpaired_target`. A ready pair is re-checked against its limit before any
  request.
- `review_pair(..., review_version="review-v2")` for the dictionary track;
  `direction` is the language of the translation.
- Long-text `/tr`: Telegram no longer passes a `splitter` (Core ignores it).
  Core cuts by segmentation-v1, keeps the sender's separators and carries the
  previous segment as context.

## Not done

- `before_llm_call` is not passed: the budget guard of `/tr` is not applied to
  `/review`. Authorization is re-checked between segments and before delivery.
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
