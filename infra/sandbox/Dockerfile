# aegis-sandbox: victim dependencies preinstalled at build time (SPEC §5.3).
# No credentials, no secrets, no network at runtime (the container runs on sandbox_net, internal: true).
# Versions match uv.lock.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN pip install \
        "fastapi==0.162.0" \
        "sqlalchemy==2.1.6" \
        "psycopg[binary]==3.3.6" \
        "faker==40.44.0" \
        "pydantic==2.14.2" \
        "pytest==9.2.0" \
        "pytest-json-report==1.5.0" \
    && rm -rf /root/.cache

# Fixed non-root uid (SPEC §5.3 user="10001:10001").
RUN groupadd --gid 10001 runner \
    && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin runner

WORKDIR /workspace
USER 10001:10001
