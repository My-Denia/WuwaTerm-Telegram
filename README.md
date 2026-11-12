<img src="assets/icon.svg" width="64" height="64" alt="">

# WuwaTerm Telegram

A standalone Telegram bot for WuwaTerm's Chinese–English game terminology. It uses the WuwaTerm Core dictionary and translation library directly; no HTTP API process is required. It supports `/tr` and `/term`, `/sentence` and `/sent`, group access settings, and linked-channel translation.

Telegram `0.2.2` builds against the published WuwaTerm Core `0.8.1` release. `core-lock.json` pins that release's wheel and SHA-256, so the Core wheel is a public release download rather than a cross-repository CI artifact.

## Commands

`/help` and `/start` print the same list inside Telegram, including who may use each command. A chat the owner adds the bot to is authorized automatically; if someone else adds it, the bot leaves unless the owner authorizes that chat with `/grant`. Translate commands in a group are admin-only until a group admin runs `/public on`.

| Command | What it does |
| --- | --- |
| `/tr [--to en\|zh] <text>` | Dictionary-first translation. Replying to a message with `/tr` translates that message. |
| `/term` | Same as `/tr`. |
| `/sentence [--to en\|zh] <sentence>` | Whole-sentence input; same pipeline as `/tr`. Replying with `/sentence` translates that message. |
| `/sent` | Same as `/sentence`. |
| `/review` (alias `/check`) | Reviews a translation against its source; see [Reviewing a translation](#reviewing-a-translation). `/review last` returns to the previous result. |
| `/about` | Dictionary source, pinned commit, term count, rate limit and whether a model is configured. |
| `/status` | Owner-only runtime status. Other callers get no reply. |
| `/public on\|off\|status` | Group admins only, in groups: open the translate commands to every member, or restore admins-only. |
| `/grant`, `/revoke` | Owner-only group authorization: `/grant` authorizes the current group, `/grant <chat_id>` by id, `/grant list` lists them; `/revoke` removes the current group and leaves it. |

Translation in a private chat is owner-only.

## Requirements

- Python 3.11 or later.
- A schema-2 WuwaTerm terms database, built or obtained separately with WuwaTerm Core.
- A Telegram bot token. A configured OpenAI-compatible model is optional; without it dictionary lookup remains available.

## Installation from verified artifacts

Download the pinned Core wheel through the lock and the Telegram `v0.2.2` wheel from its release, and obtain the Telegram wheel's SHA-256 from that release's `SHA256SUMS`. From a checkout of this repository:

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
.venv/bin/wuwaterm-telegram --db data/terms.db
```

`TELEGRAM_BOT_TOKEN` is required. `OWNER_USER_ID` controls owner-only commands and private translation access; without it, private translation is refused. The bot checks the installed Core version, required capabilities and the database before polling. Keep credentials out of the repository and logs.

## Reviewing a translation

`/review` (alias `/check`) checks a translation against its source. Give it the pair in one of three ways:

1. Reply to the source message and write the translation after the command.
2. Put the source, a line containing only `---`, and the translation in one message.
3. Reply to a recent `/tr` result (kept in memory for 30 minutes).

The result has two separate parts: dictionary findings (deterministic; a "surface match" only says that an official form appears) and, when enabled, semantic issues from the model (experimental, can be wrong; shown with the reason and a suggested change inside an expandable quote). Semantic review is off by default in Core (`WUWATERM_SEMANTIC_REVIEW_ENABLED`) and needs a configured model; while it is off `/review` gives the dictionary findings and says semantic issues are not enabled. Buttons under the result step through the issues, accept a suggestion and build a revised translation. `/review last` points back to the previous result. After a model translation, `/tr` adds a "审校这段译文" button when the model track is on.

Texts over 2,000 characters are paired by [segmentation-v1](https://github.com/My-Denia/WuwaTerm/blob/main/docs/segmentation-v1.md) and reviewed one segment at a time, at most 12 per command. When the two sides cannot be paired the bot says so and asks for a blank line at matching places; it never guesses. Long `/tr` texts follow the same segmentation in Core, keep the sender's blank lines and carry the previous segment as context. Review state is memory-only (30 minutes, five per chat). Semantic review sends both texts to the configured model. Channel auto-translation has no review. Core assumptions and limits: [docs/review-core-contract.md](docs/review-core-contract.md).

## State and recovery

The bot reads the terms database and writes its settings and channel reply index separately. By default, `chat_settings.json` and `channel_replies.json` live beside the database; `WUWATERM_STATE_DIR` puts both in that directory, and `WUWATERM_SETTINGS_PATH` and `WUWATERM_CHANNEL_REPLY_INDEX_PATH` select individual paths. If a state directory is configured, an existing DB-adjacent legacy file is copied to the new location without replacing an existing target. An invalid existing state file is handled according to the application's fail-closed persistence rules.

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

The command succeeds only after the SDK has completed a poll, including an empty response, within the last 90 seconds. `--max-age` accepts 1–3600 seconds. It checks the running process and boot identity without contacting Telegram. A stopped poller or stale, missing or invalid record fails. This indicates recent polling only; it does not verify handlers or translation. Poll health is disabled unless explicitly enabled, and needs Linux `/proc` and the writable tmpfs.

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

For an offline or local build, pass one explicit Core wheel instead of the lock: `--local-core-wheel <wheel> --local-core-sha256 <sha256>` replaces `--lock`/`--core`. The wheel must match that hash and the exact Core version; the committed lock is not read or changed, and the context carries no lock. Build it with the printed `--build-arg CORE_WHEEL_SHA256=<sha256>`; the image then records no reviewed Core lock.

The final image layer only adds `/usr/local/share/wuwaterm/install-probe.json`, a build-time report of the installed files and their SHA-256 values for `wuwaterm` and `wuwaterm-telegram`.

An optional `WUWATERM_SOURCE_REVISION` build argument may be supplied as a full lowercase commit SHA after verifying the wheel provenance. The image records it as an unverified claim. Compose keeps API Web credentials out of the bot container even when a shared `.env` contains them.

Place the database at `data/terms.db`; Compose mounts `data/` read-only and `state/` read-write. The prepared Docker context contains only the lock, wheel artifacts and verification code, not the workspace or `.env`. Stop the old bot before starting a replacement against the same state.

## Dependency audit

CI audits the dependency set the container installs for critical and high advisories, on changes to the resolver inputs and weekly. See [dependency advisory audit](docs/dependency-audit.md) for what is audited, what fails the run, what it does not cover and how to run it locally.

## Source and license

The extraction sources are recorded in [PROVENANCE.md](PROVENANCE.md). The code is available under the [MIT License](LICENSE). Wuthering Waves and its game terminology belong to their respective owners; this independent fan project is not affiliated with or endorsed by Kuro Games.
