# Source provenance

The Telegram adapter was extracted and adapted from `My-Denia/WuwaTerm` commit `4d5e02b408ffd9193ce1277c37a5f77df80e6e15`. These are Git blob object IDs at that commit, not hashes of the adapted destination files:

| Original path | Destination | Source blob |
| --- | --- | --- |
| `src/wuwaterm/bot.py` | `src/wuwaterm_telegram/bot.py` | `ee3d7d808c2bd3192baec63fd18c6d754fc9eec4` |
| `src/wuwaterm/channel.py` | `src/wuwaterm_telegram/channel.py` | `68892a23c643bb930df4488fdb70cececd324e45` |
| `src/wuwaterm/channel_reply_index.py` | `src/wuwaterm_telegram/channel_reply_index.py` | `6163da69ac23c01f2ad73fa789f47895e311d4dd` |
| `src/wuwaterm/channel_reply_schema.py` | `src/wuwaterm_telegram/channel_reply_schema.py` | `85e4f3582004992162c0cf2582677c6688ca1f72` |
| `src/wuwaterm/channel_runtime.py` | `src/wuwaterm_telegram/channel_runtime.py` | `8858fb84138bff414a42e24edb9757409b57aeaf` |
| `src/wuwaterm/runtime_keys.py` | `src/wuwaterm_telegram/runtime_keys.py` | `088b33e753b6cfefa8f1f9b16abd9decc8574d70` |
| `src/wuwaterm/settings.py` | `src/wuwaterm_telegram/settings.py` | `0177bfa38397c9d0907b6e069f03829f5f5d96d9` |
| `src/wuwaterm/telegram_html.py` | `src/wuwaterm_telegram/telegram_html.py` | `0462d53b7b2479c9e75da1aee7fad6384a574024` |
| `src/wuwaterm/telegram_text.py` | `src/wuwaterm_telegram/telegram_text.py` | `e1128019deea10d568673daa735afbaa96e5b831` |
| `src/wuwaterm/cli.py` | `src/wuwaterm_telegram/cli.py` (standalone adaptation) | `2f5d87a5ab7d4748bd42c0153165cf2f55c8b9e3` |
| `deploy/env.example` | `deploy/env.example` (Telegram-only adaptation) | `5224de7c7d6b79b6ed37a47f99796ea84bbc7716` |
| `LICENSE` | `LICENSE` | `5664cf89870e1a70fbfe1e55095f1585c792e15e` |


## Test source mappings

These test files were copied or adapted from the same Core commit. Each destination retains the original path; the blob IDs identify the original versions, not the adapted files.

| Original and destination path | Source blob |
| --- | --- |
| `tests/conftest.py` | `52d7c84f3c05174a9772e8217408fd4c9e8b94e9` |
| `tests/test_bot.py` | `a259d829a7d124066f2d2c64ebfe7f475ffcb5ff` |
| `tests/test_channel.py` | `c4078196e023742c2cf324423e65066f94616f0a` |
| `tests/test_channel_reply_schema.py` | `790ef795a5331cd3ad9066b520a3479448e2569f` |
| `tests/test_channel_runtime.py` | `b175254ee73f556c9ec5073532dadc75562c2f44` |
| `tests/test_settings.py` | `7f008ec47bc5bf01e0ab32930bd5cea2ecfbb083` |
| `tests/test_telegram_text.py` | `c16905167bd7d2f6a0b692cc46b4e5a27477e9a4` |

The loopback runtime, state migration edge and isolation tests, together with the isolated test runner and socket guard, are new files in this repository; some use helpers and fixtures from the adapted tests above.

New package entry, compatibility, packaging, container and verification files are maintained in this repository. The Core release identity is a separate artifact-lock record and is not implied by the source mapping above.
