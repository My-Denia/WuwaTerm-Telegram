FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY core-lock.json pyproject.toml ./
COPY scripts ./scripts
COPY src/wuwaterm_telegram/core_compat.py ./src/wuwaterm_telegram/core_compat.py
COPY wheels ./wheels

# Optional for a build supplied with an offline dependency wheelhouse.
ARG PIP_NO_INDEX
ARG PIP_FIND_LINKS

# The context builder verifies both wheels. Repeat the checks inside the image
# before pip sees either artifact, then let pip resolve their dependencies.
RUN core="$(cat wheels/core.filename)" && telegram="$(cat wheels/telegram.filename)" && \
    telegram_sha="$(cat wheels/telegram.sha256)" && \
    python scripts/install_verified.py --lock core-lock.json --core "wheels/$core" \
      --telegram "wheels/$telegram" --telegram-sha256 "$telegram_sha" && \
    python -m pip check

ENTRYPOINT ["wuwaterm-telegram"]
CMD ["--db", "/app/data/terms.db"]
