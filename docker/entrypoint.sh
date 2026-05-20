#!/bin/sh
set -eu

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
  echo "[entrypoint] Running alembic upgrade head..."
  alembic upgrade head
fi

exec "$@"
