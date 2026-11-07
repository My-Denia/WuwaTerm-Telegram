FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
# The reviewed lock is absent from an explicit local-Core context (see below).
COPY pyproject.toml core-lock.json* ./
COPY scripts ./scripts
COPY src/wuwaterm_telegram/core_compat.py ./src/wuwaterm_telegram/core_compat.py
COPY wheels ./wheels

# Optional for a build supplied with an offline dependency wheelhouse.
ARG PIP_NO_INDEX
ARG PIP_FIND_LINKS
ARG WUWATERM_SOURCE_REVISION=""
# Empty: the reviewed core-lock.json decides the Core wheel. Non-empty: an
# explicit local Core whose bytes must hash to this value; the context then has
# no lock. Set by scripts/prepare_docker_context.py --local-core-wheel.
ARG CORE_WHEEL_SHA256=""

# A build-supplied source revision is a claim, not proof tying the wheel to it.
RUN python -c 'import pathlib,re,sys; r=sys.argv[1]; assert not r or re.fullmatch("[0-9a-f]{40}", r), "invalid source revision"; pathlib.Path("/app/.build-revision").write_text(r, encoding="ascii")' "$WUWATERM_SOURCE_REVISION"
LABEL org.opencontainers.image.revision="$WUWATERM_SOURCE_REVISION"

# The context builder verifies both wheels. Repeat the checks inside the image
# before pip sees either artifact, then let pip resolve their dependencies.
RUN core="$(cat wheels/core.filename)" && telegram="$(cat wheels/telegram.filename)" && \
    telegram_sha="$(cat wheels/telegram.sha256)" && \
    if [ -n "$CORE_WHEEL_SHA256" ]; then \
      { test ! -e core-lock.json || { echo "a local Core build must not carry core-lock.json" >&2; exit 1; }; } && \
      python scripts/install_verified.py --local-core-wheel "wheels/$core" \
        --local-core-sha256 "$CORE_WHEEL_SHA256" \
        --telegram "wheels/$telegram" --telegram-sha256 "$telegram_sha"; \
    else \
      python scripts/install_verified.py --lock core-lock.json --core "wheels/$core" \
        --telegram "wheels/$telegram" --telegram-sha256 "$telegram_sha"; \
    fi && \
    python -m pip check

# Keep this the last RUN: it may only add the report, so a consumer can prove from
# the image layers that nothing it describes changed afterwards. The probe itself
# was copied above (/app/scripts/install_probe.py), in an earlier layer.
RUN python -I -B scripts/install_probe.py --product telegram \
    --dist wuwaterm --dist wuwaterm-telegram \
    --out /usr/local/share/wuwaterm/install-probe.json

ENTRYPOINT ["wuwaterm-telegram"]
CMD ["--db", "/app/data/terms.db"]
