<img src="assets/icon.svg" width="64" height="64" alt="">

# WuwaTerm Telegram

A standalone Telegram adapter for WuwaTerm's Chinese–English game terminology, centered on **linked-channel auto-translation (P0)** while fully preserving **group/private translation, terminology lookup, and `/review` (P1)**.

- **Core compatibility & service decoupling**: Compatible with WuwaTerm Core `>=0.16.0,<1.0` via semantic version range + runtime capability probing. `core-lock.json` pins the reviewed Core `0.16.0` release wheel and SHA-256 for deterministic CI and container builds. Recommended deployment: **pure HTTP Core mode** (`WUWATERM_CORE_MODE=http`, or `auto` with `WUWATERM_CORE_API_URL` set, which is what Core `deploy/setup.sh --with-telegram` writes; optional `WUWATERM_CORE_API_TOKEN`): Telegram consumes an independently deployed Core HTTP API via `HttpCoreBackend` (with `GET /v1/capabilities` negotiation and hot-swap awareness) and completely skips local `terms.db` verification and in-process translator initialization. The embedded `LocalCoreBackend` remains available as a compatibility fallback (`WUWATERM_CORE_MODE=local`, or `auto` without `WUWATERM_CORE_API_URL`).

## Channel auto-translation (P0)

When the bot is authorized in a linked discussion group (`WUWATERM_CHANNEL_AUTOTRANSLATE=1`), it automatically translates channel posts in-thread and syncs edits in place:

- **Cross-segment context for long posts**: Posts over 2,000 characters use Core's `segmentation-v1` and pass `TranslationContext(before=ContextBefore(...))` across segments to keep character, skill, and mechanic translations consistent while preserving paragraph boundaries and blank lines.
- **Paragraph-aware rich HTML preservation**: Both long inputs (>2,000 chars) and long outputs (>4,096 UTF-16 units) preserve balanced Telegram HTML across chunks (bold, italic, links, quotes, spoilers, custom emojis, timestamps) and fall back to plain text only when tag validation or Telegram API delivery cannot preserve markup.
- **PTB 22.8 / Bot API 10.0 rich message compatibility**: Supports `blockquote` and `blockquote expandable`, `<tg-emoji emoji-id="...">`, `<tg-time unix="..." format="...">` (with attribute validation), and fenced code blocks rendered as `<pre><code class="{language}">` as well as `language-xxx`.
- **Zero-cost deterministic output checks**: Before publishing or editing a channel reply, the adapter runs Core `0.16.0`'s deterministic `check_segment_structure` (`structure-check-v1`) plus leaked-placeholder and severe-truncation checks with zero model cost. Critical defects (empty output or leaked internal placeholders) block delivery and trigger safe retry; structural warnings record sanitized counters without leaking post text.
- **Bounded, recoverable retry queue**: Capacity rejections (`queue_full`, `llm_budget`) and transient LLM/gateway failures enqueue the post into a bounded `ChannelTaskQueue` (`pending` → `in_flight` → `completed` / exponential-backoff `pending` / `failed` / `expired`) instead of silently dropping it. Newer edits overwrite pending queued tasks for the same post, and extra-chunk deletion (`delete_message`) is protected by flood-wait retry. By default the queue is pure in-memory; optional disk persistence (`WUWATERM_CHANNEL_QUEUE_PERSIST=1`) writes with `0600` permissions, enforces TTL and max-size caps, wipes post text immediately on completion/failure/expiration, and never writes post text to logs or `/status`.

## Commands (P1)

`/help` and `/start` print the same list inside Telegram, including who may use each command. A chat the owner adds the bot to is authorized automatically; if someone else adds it, the bot leaves unless the owner authorizes that chat with `/grant`. Translate commands in a group are admin-only until a group admin runs `/public on`.

| Command | What it does |
| --- | --- |
| `/tr [--to en\|zh] <text>` | Dictionary-first translation. Replying to a message with `/tr` translates that message. |
| `/term` | Same as `/tr`. |
| `/sentence [--to en\|zh] <sentence>` | Whole-sentence input; same pipeline as `/tr`. Replying with `/sentence` translates that message. |
| `/sent` | Same as `/sentence`. |
| `/review` (alias `/check`) | Reviews a translation against its source; see [Reviewing a translation](#reviewing-a-translation). `/review last` returns to the previous result. |
| `/about` | Dictionary source, pinned commit, term count, rate limit and whether a model is configured. |
| `/status` | Owner-only runtime status (including sanitized channel queue and Core backend stats). Other callers get no reply. |
| `/public on\|off\|status` | Group admins only, in groups: open the translate commands to every member, or restore admins-only. |
| `/grant`, `/revoke` | Owner-only group authorization: `/grant` authorizes the current group, `/grant <chat_id>` by id, `/grant list` lists them; `/revoke` removes the current group and leaves it. |

Translation in a private chat is owner-only.

Without `--to`, `/tr`, `/term` and `/sentence` pick the direction themselves: text with no CJK character goes to Chinese, and text with CJK characters goes to Chinese only when its Latin letters outnumber the CJK characters more than four to one (an English paragraph with one Chinese term is translated into Chinese). Screenshot wrapper markers (`[spoiler]`, `(WW 3.4)`, `[Wuthering Waves 3.7]`, quote bars) are removed via Core's native `strip_telegram_wrapper_noise` on plain paths; a formatted message's HTML is translated as written, markers included. Prose that only looks like a marker, such as `>50% Energy Regen` or `#spoilersafe ...`, is kept.

## Requirements

- Python 3.11 or later.
- A running WuwaTerm Core HTTP API service (`WUWATERM_CORE_MODE=http` + `WUWATERM_CORE_API_URL`), **or** (legacy embedded fallback) a local schema-2 `terms.db` with `WUWATERM_CORE_MODE=local` (or `auto` with `WUWATERM_CORE_API_URL` unset).
- WuwaTerm Core `>=0.16.0,<1.0` installed (used for shared text/HTML helpers and optional embedded fallback).
- A Telegram bot token. In pure HTTP mode, LLM credentials live on the Core service; in local fallback mode, an OpenAI-compatible model is optional (without it dictionary lookup remains available).

## Installation from verified artifacts

Download the pinned Core wheel through the lock and the Telegram wheel from its release, and obtain the Telegram wheel's SHA-256 from that release's `SHA256SUMS`. From a checkout of this repository:

```sh
python3 -m venv .venv
python3 scripts/fetch_core.py --lock core-lock.json --output-dir artifacts
.venv/bin/python scripts/install_verified.py --lock core-lock.json \
  --core artifacts/<core-wheel> --telegram artifacts/<telegram-wheel> \
  --telegram-sha256 <verified-telegram-sha256>
.venv/bin/python -m pip check
```

Both wheel hashes and metadata are checked before pip starts. Pip installs both wheels together using its normal dependency resolver. The lock and the release checksum record are the trust roots; a checksum taken only from the same untrusted download does not establish provenance. See [artifact verification](docs/artifact-verification.md).

Set the environment variables needed by your deployment using [the example](deploy/env.example), then start the bot:

```sh
# Pure HTTP Core mode (recommended; no local terms.db required):
WUWATERM_CORE_MODE=http WUWATERM_CORE_API_URL=http://127.0.0.1:8000 \
  WUWATERM_STATE_DIR=state .venv/bin/wuwaterm-telegram

# Legacy embedded local-DB fallback:
WUWATERM_CORE_MODE=local .venv/bin/wuwaterm-telegram --db data/terms.db
```

`TELEGRAM_BOT_TOKEN` is required. `OWNER_USER_ID` controls owner-only commands and private translation access; without it, private translation is refused. `WUWATERM_CORE_MODE` is fail-closed (`auto` | `http` | `local`, default `auto`): `http` fails at startup if `WUWATERM_CORE_API_URL` is unset or invalid so deployments never silently fall back to an embedded database, and `local` fails if `WUWATERM_CORE_API_URL` is also set. In pure HTTP mode the bot skips local `terms.db` verification and in-process translator initialization; in local mode it checks the database before polling. Keep credentials out of the repository and logs.

## Reviewing a translation

`/review` (alias `/check`) checks a translation against its source. Give it the pair in one of three ways:

1. Reply to the source message and write the translation after the command.
2. Put the source, a line containing only `---`, and the translation in one message.
3. Reply to a recent `/tr` result (kept in memory for 30 minutes).

The result has two separate parts: dictionary findings (deterministic; a "surface match" only says that an official form appears) and, when enabled, semantic issues from the model (experimental, can be wrong; shown with the reason and a suggested change inside an expandable quote). Semantic review is off by default in Core (`WUWATERM_SEMANTIC_REVIEW_ENABLED`) and needs a configured model; while it is off `/review` gives the dictionary findings and says semantic issues are not enabled. Buttons under the result step through the issues, accept a suggestion and build a revised translation. `/review last` points back to your own previous result, or to the newest one in the chat if you have none. After a model translation, `/tr` adds a "审校这段译文" button when the model track is on.

Texts over 2,000 characters are paired by [segmentation-v1](https://github.com/My-Denia/WuwaTerm/blob/main/docs/segmentation-v1.md) and reviewed one segment at a time, at most 12 per command. When the two sides cannot be paired the bot says so and asks for a blank line at matching places; it never guesses. Long `/tr` texts follow the same segmentation in Core, keep the sender's blank lines and carry the previous segment as context. Review state is memory-only (30 minutes, five per chat). Content already under review is not reviewed twice: another member who submits the same pair is queued and receives her own result and buttons when that review finishes. Each result keeps its own 30 minutes for its buttons, while a computed report is reused for at most 30 minutes from when the model answered. If a result cannot be sent, its notice carries a button that sends the stored result again without a new review. Only a send that provably never reached Telegram is retried automatically: a connection that never opened is retried once, while a timeout or a connection that broke after the request went out is not retried, because Telegram may already have posted it. Those notices say the outcome is unknown and that resending may show the result twice. Semantic review sends both texts to the configured model. Channel auto-translation has no interactive `/review` track (it uses zero-cost deterministic structural checks instead). Core assumptions and limits: [docs/review-core-contract.md](docs/review-core-contract.md).

## State and recovery

The bot reads the terms database and writes its settings and channel reply index separately. By default, `chat_settings.json` and `channel_replies.json` live beside the database; `WUWATERM_STATE_DIR` puts both in that directory, and `WUWATERM_SETTINGS_PATH` and `WUWATERM_CHANNEL_REPLY_INDEX_PATH` select individual paths. If a state directory is configured, an existing DB-adjacent legacy file is copied to the new location without replacing an existing target. An invalid existing state file is handled according to the application's fail-closed persistence rules. The channel retry queue is pure in-memory by default and only persists to `channel_queue.json` (mode `0600`) when `WUWATERM_CHANNEL_QUEUE_PERSIST=1` is explicitly set.

Before moving or upgrading, stop the bot and back up the database and both state files, including any explicitly configured paths. Restore those files to their original locations when rolling back. Keep the old release and its matching Core artifact available. Do not overwrite a current state file with an old backup without first preserving the current file.

To validate a state file without starting the bot, pass its actual path and kind:

```sh
python -m wuwaterm_telegram.state_validation /path/to/settings.json chat-settings
python -m wuwaterm_telegram.state_validation /path/to/replies.json channel-replies
```

The command reads the specified file without modifying it. It exits with 0 for valid state, 1 for a missing, unreadable or invalid file, and 2 for incorrect command usage. The kind is required even when using a default filename.

The offline operations command can report the effective state paths before migration:

```sh
python -m wuwaterm_telegram.operations state-plan --db data/terms.db
```

It uses the bot's environment and path precedence, reports each file by kind and says whether an existing legacy file would be copied or an existing target validated. It does not copy, validate file contents, lock or change the files; results describe a moment in time and must be rechecked when the bot is stopped. Without mounts the command only plans paths. For a container, supply `--mount` for each actual mount as a JSON object with absolute `container` and `host` paths and a boolean `read_only`. Host paths must be the backing directories visible to the checking process. Run the check with the runtime's environment, effective user and working directory, so relative paths have the same meaning.

The check uses the most specific container mount for direct paths. It follows links within that mount using container path semantics, but reports `unsupported mount traversal` when a link or `..` crosses a declared mount boundary, escapes a mount, loops, or cannot be established safely. It checks existing target readability, the actual replacement directory and the settings lock; a legacy file must be readable only when it would be copied, and a dangling destination symlink is refused when a copy is needed. Metadata checks do not guarantee a later write or prevent filesystem and permission changes.

`python -m wuwaterm_telegram.operations identity` reports installed Telegram and Core versions. In the supplied container image it also checks the retained wheels against the supplied lock/checksum and reports their digests. These checks do not establish that the lock or checksum is trustworthy: verify them against the intended release. A build-supplied source revision is reported as a claim, not proof that a wheel came from that source. The command does not test whether a running bot is ready.

For a container, set `WUWATERM_POLL_HEALTH=1` to enable polling health. Compose supplies a private tmpfs at `/run/wuwaterm-telegram` for the transient record. From the same container and user, run:

```sh
docker compose -f deploy/docker-compose.yml exec -T wuwaterm-telegram \
  python -B -m wuwaterm_telegram.operations health
```

In local mode, `operations health` checks local SDK polling evidence without making any network connection, succeeding only after a completed poll (including an empty response) within `--max-age` (default 90 seconds, range 1–3600). In pure HTTP Core mode (`WUWATERM_CORE_MODE=http`, or `auto` with `WUWATERM_CORE_API_URL` set), `operations health` always probes Core HTTP `/v1/meta` (and optional `/v1/capabilities` / `/v1/dictionary`) without requiring a local `terms.db`, reporting `"poll_checked": false` when `WUWATERM_POLL_HEALTH` is off; when `WUWATERM_POLL_HEALTH=1` is set, it also verifies local SDK polling evidence and reports `"poll_checked": true`. A stopped poller or stale, missing or invalid record fails whenever polling health is checked. Poll health needs Linux `/proc` and the writable tmpfs when enabled.

## Container

The image contains only the bot runtime and installs the same two verified wheels. Prepare a minimal build context with the reviewed lock and verified artifacts:

```sh
python3 scripts/prepare_docker_context.py --lock core-lock.json \
  --core artifacts/<core-wheel> --telegram artifacts/<telegram-wheel> \
  --telegram-sha256 <verified-telegram-sha256> --output-dir artifacts/docker-context
cp deploy/env.example .env
# Fill in the required values without committing .env.
docker compose -f deploy/docker-compose.yml up --build -d
```

For an offline or local build, pass one explicit Core wheel instead of the lock: `--local-core-wheel <wheel> --local-core-sha256 <sha256>` replaces `--lock`/`--core`. The wheel must match that hash and satisfy the `wuwaterm` semver range in `pyproject.toml`; the committed lock is not read or changed, and the context carries no lock. Build it with the printed `--build-arg CORE_WHEEL_SHA256=<sha256>`; the image then records no reviewed Core lock.

The final image layer only adds `/usr/local/share/wuwaterm/install-probe.json`, a build-time report of the installed files and their SHA-256 values for `wuwaterm` and `wuwaterm-telegram`.

An optional `WUWATERM_SOURCE_REVISION` build argument may be supplied as a full lowercase commit SHA after verifying the wheel provenance. The image records it as an unverified claim. Compose keeps API Web credentials out of the bot container even when a shared `.env` contains them.

`deploy/docker-compose.yml` mounts `state/` read-write and keeps the `data/` read-only mount so existing local-DB deployments and Core's `deploy/setup.sh --telegram-dir` remain compatible. In pure HTTP Core mode (`WUWATERM_CORE_MODE=http` and a container-reachable `WUWATERM_CORE_API_URL` such as a shared Docker network address, or `http://127.0.0.1:8788` with `network_mode: host`), Telegram ignores `data/terms.db` and does not require a local database file. For legacy embedded local-DB fallback, set `WUWATERM_CORE_MODE=local`, leave `WUWATERM_CORE_API_URL` unset, and place the database at `data/terms.db`. The prepared Docker context contains only the lock, wheel artifacts and verification code, not the workspace or `.env`. Stop the old bot before starting a replacement against the same state.

## Dependency audit

CI audits the dependency set the container installs for critical and high advisories, on changes to the resolver inputs and weekly. See [dependency advisory audit](docs/dependency-audit.md) for what is audited, what fails the run, what it does not cover and how to run it locally.

## Source and license

The extraction sources are recorded in [PROVENANCE.md](PROVENANCE.md). The code is available under the [MIT License](LICENSE). Wuthering Waves and its game terminology belong to their respective owners; this independent fan project is not affiliated with or endorsed by Kuro Games.
