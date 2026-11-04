# postgres:16 image already ships the PGDG apt source (pgdg.list), so pgaudit installs directly.
FROM postgres:16

RUN apt-get update \
    && apt-get install -y --no-install-recommends postgresql-16-pgaudit \
    && rm -rf /var/lib/apt/lists/*
