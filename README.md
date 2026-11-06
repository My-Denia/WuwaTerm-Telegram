<img src="assets/icon.svg" width="64" height="64" alt="">

# WuwaTerm Telegram

A standalone Telegram bot for WuwaTerm's Chinese–English game terminology. It uses the WuwaTerm Core dictionary and translation library directly; no HTTP API process is required. It supports `/tr` and `/term`, `/sentence` and `/sent`, group access settings, and linked-channel translation.

This repository is a development candidate. Its exact Core dependency is `0.7.0.dev1`; `core-lock.json` records a verified Core CI artifact, not a stable release. Download access from another repository's default CI token remains unverified. No stable release installation URL is available yet.

## Requirements

- Python 3.11 or later.
- A schema-2 WuwaTerm terms database, built or obtained separately with WuwaTerm Core.
- A Telegram bot token. A configured OpenAI-compatible model is optional; without it dictionary lookup remains available.

## Installation from verified artifacts

With access to the locked Core artifact and a verified Telegram wheel, use `core-lock.json` and obtain the Telegram wheel's SHA-256 from a trusted checksum record. From a checkout of this repository:

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

It uses the bot's environment and path precedence, reports each file by kind and says whether an existing legacy file would be copied or an existing target validated. It does not copy, validate, lock or change the files; results describe a moment in time and must be rechecked when the bot is stopped. For a container, a caller can supply `--mount` more than once, each as a JSON object with absolute `container` and `host` paths and a boolean `read_only`. The command then maps legacy and target paths, rejects an unmapped or read-only target and checks host-side access without writing. The caller must supply the actual mounts and perform the check with the runtime's effective user; this is not a guarantee against later filesystem or permission changes.

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

An optional `WUWATERM_SOURCE_REVISION` build argument may be supplied as a full lowercase commit SHA after verifying the wheel provenance. The image records it as an unverified claim. Compose keeps API Web credentials out of the bot container even when a shared `.env` contains them.

Place the database at `data/terms.db`; Compose mounts `data/` read-only and `state/` read-write. The prepared Docker context contains only the lock, wheel artifacts and verification code, not the workspace or `.env`. Stop the old bot before starting a replacement against the same state.

## Source and license

The extraction sources are recorded in [PROVENANCE.md](PROVENANCE.md). The code is available under the [MIT License](LICENSE). Wuthering Waves and its game terminology belong to their respective owners; this independent fan project is not affiliated with or endorsed by Kuro Games.
