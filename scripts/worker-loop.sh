#!/usr/bin/env sh
set -eu
: "${AUREMGRID_DB:?set AUREMGRID_DB}"
: "${AUREMGRID_ORGANIZATION_ID:?set AUREMGRID_ORGANIZATION_ID}"
exec python -m auremgrid worker-loop --db "$AUREMGRID_DB" --organization "$AUREMGRID_ORGANIZATION_ID" ${AUREMGRID_WORKSPACE:+--workspace "$AUREMGRID_WORKSPACE"} --worker-id "${AUREMGRID_WORKER_ID:-worker-$(hostname)}"
