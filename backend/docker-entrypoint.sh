#!/bin/sh
set -e

case "$1" in
  api)
    export STEWARD_COMPONENT=api
    exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --workers 2
    ;;
  worker)
    # One container per specialist: WORKER_NAME selects the domain, WORKER_QUEUES what it consumes.
    exec celery -A app.celery_app worker --loglevel=INFO --concurrency="${WORKER_CONCURRENCY:-2}" \
      -n "${WORKER_NAME:-default}@%h" -Q "${WORKER_QUEUES:-run.default}"
    ;;
  scheduler)
    export STEWARD_COMPONENT=scheduler
    exec python -m app.scheduler
    ;;
  migrate)
    exec alembic upgrade head
    ;;
  manage)
    shift
    exec python -m app.manage "$@"
    ;;
  mockcc)
    exec uvicorn mockcc.app:app --host 0.0.0.0 --port 8080 --workers 1
    ;;
  *)
    exec "$@"
    ;;
esac
