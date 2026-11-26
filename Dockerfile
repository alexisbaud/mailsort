# syntax=docker/dockerfile:1
#
# Image de mailsort : le programme, ses dépendances et labels.yaml, sans aucun secret ni
# donnée. Les secrets (.env, token.json) et la base arrivent à l'exécution, dans /data
# (voir deploy/compose.yml).
#
#   docker build -t mailsort .
#   docker run --rm mailsort labels check

ARG PYTHON_IMAGE=python:3.12.14-slim-trixie

# ---------------------------------------------------------------- construction
FROM ${PYTHON_IMAGE} AS build
COPY --from=ghcr.io/astral-sh/uv:0.12.10 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Les dépendances d'abord : cette étape reste en cache tant que uv.lock ne change pas.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

# -------------------------------------------------------------------- exécution
FROM ${PYTHON_IMAGE}
# tzdata : les heures du journal sont celles de Paris.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --user-group --no-create-home --home-dir /nonexistent \
       --shell /usr/sbin/nologin mailsort
COPY --from=build /app/.venv /app/.venv
COPY labels.yaml /app/labels.yaml

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Paris \
    MAILSORT_LABELS=/app/labels.yaml \
    MAILSORT_DB=/data/mailsort.db \
    MAILSORT_LOG_DIR=/data/logs \
    GOOGLE_TOKEN_FILE=/data/token.json \
    GOOGLE_CLIENT_SECRET_FILE=/data/credentials.json

# /data : le seul dossier où le programme écrit (base, jeton renouvelé, journal, CSV).
WORKDIR /data
USER 10001:10001
ENTRYPOINT ["mailsort"]
CMD ["--help"]
