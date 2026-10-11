#!/bin/sh
# Runs once, when the Postgres volume is first created.
# POSTGRES_DB (n8n's database) is created by the image itself; this adds the
# app database. Its tables are created by pp-tools at startup (schema.sql),
# so schema changes also reach databases that already exist.
set -eu

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<EOSQL
CREATE DATABASE "${APP_DB_NAME}";
EOSQL

echo "created database ${APP_DB_NAME}"
