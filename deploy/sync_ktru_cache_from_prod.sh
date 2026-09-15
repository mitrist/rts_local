#!/bin/bash
# Sync KTRU PP template cache from co-hosted prod (rts-tender) to rts-local.
# Run on the Yandex VM after first deploy or when prod cache was refreshed.
set -euo pipefail

SRC="${RTS_KTRU_CACHE_SRC:-/var/lib/rts_tender/ktru_rts_cache}"
DST="${RTS_KTRU_CACHE_DST:-/var/lib/rts_local/ktru_rts_cache}"
OWNER="${APP_USER:-mitrist12}"

if [[ ! -d "$SRC" ]]; then
  echo "ERROR: prod cache not found: $SRC" >&2
  exit 1
fi

src_count=$(ls "$SRC" | wc -l)
echo "source=$SRC count=$src_count"

sudo mkdir -p "$DST"
sudo rsync -a --delete "$SRC/" "$DST/"
sudo chown -R "$OWNER:$OWNER" "$DST"

dst_count=$(ls "$DST" | wc -l)
echo "dest=$DST count=$dst_count"
echo "OK: ktru_rts_cache synced (no service restart required)"
