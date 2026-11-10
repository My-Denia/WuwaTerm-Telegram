# `/review`: what it assumes from Core

`/review` is written against the published Core contract: ADR 0018 and 0019,
`docs/segmentation-v1.md` and the fixtures in `contracts/semantic-review-v1/`
(Core commit `7122ea7`; copies in `tests/fixtures/semantic-review-v1/`). Core
0.8.0 had not been released when this was written, and its engine (C2) was not
merged. Everything that touches Core goes through `review_backend.py`, so the
points below are the only places to re-check.

## Assumptions to verify once Core's engine is merged

1. `wuwaterm.application.semantic_review_async(service, translator, request)`
   exists, takes the published request body as a dict, awaits to the published
   response body as a mapping, and raises `LLMTranslationError` (with
   `reason`) or an error with a `.code` for failures. If it instead takes
   a typed request, returns an object, or accepts `before_llm_call`, only
   `LiveReviewBackend.semantic_review` changes. A `before_llm_call` hook would
   let the budget and authorization be re-checked right before each request;
   today the check runs between segments.
2. It shares the process-wide LLM budget, concurrency limit and per-device
   limit with translation. The bot's own per-chat limiter spends one slot per
   `/review` that starts a run, however many segments it has (at most 12).
3. `wuwaterm.segmentation` provides `SEGMENT_LIMIT`, `segment_text` and
   `pair_segments(source, target, limit)` returning objects with `index`,
   `status`, `hard_cut` and `source`/`target` regions (`start`, `end`,
   `core_start`, `core_end`). The list is `REVIEW_CORE_SYMBOLS` in
   `core_compat.py`.
4. Telegram sends no `precedents` (there is no manuscript) and sends
   `context.before` as the tail (at most 400 scalars) of the previous segment.
5. `direction` is the language of the translation, for both `review_pair`
   (verified on Core 0.7.0) and `semantic_review_async`.
6. Long-text `/tr`: the bot still passes its own UTF-16 `splitter` to
   `translate_request_async`. If Core 0.8.0 keeps honouring that argument, long
   Telegram texts will not get segmentation-v1 and the previous-segment
   context. Decide whether to stop passing it when the lock moves.

## Moving the lock to Core 0.8.0

One change, after Core 0.8.0 is released. Nothing here can be prepared
earlier: the lock needs the released wheel's SHA-256, source commit and URL.

- `core-lock.json`: version, filename, sha256, source_commit, tag, url, read
  back from the Core release.
- `pyproject.toml`: `wuwaterm==0.8.0` and the Telegram version `0.2.0`.
- `core_compat.py`: `CORE_VERSION = "0.8.0"`, and require `REVIEW_CORE_SYMBOLS`
  in `verify_core_compatibility`.
- `tests/test_review_backend.py`: the two tests that skip or branch on the
  installed Core become unconditional.
- README: the Core version sentence.

## Limits and costs

- Over 2,000 characters a side, a public member is refused; owner and group
  admins may send up to 12 segments (21,600 characters) per command.
- One model request at a time; a rate limit or an exhausted budget stops the
  run with "已完成 x/y 段", and the same content may be submitted again.
- A segment answer that does not meet the contract (a span whose text is not
  the submitted slice, an unknown category or `term_effect`, a suggestion that
  is also withheld) is discarded and counted as not reviewed.
- Accepting a suggestion re-checks that the translation still holds the span's
  full text and that accepted ranges do not overlap.
