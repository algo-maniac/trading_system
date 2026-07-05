#!/usr/bin/env bash
# Reset the replay namespace so an accelerated replay starts from a clean paper book:
#   - rebuild trading_replay with the CANONICAL schema (alembic migrations — every
#     PK/unique/hypertable, so the trader's ON CONFLICT upserts work), then copy only
#     the market DATA (instruments, ohlcv, features, ingestion_runs) from trading_db;
#   - the paper ledger + news/universe/report tables are created empty;
#   - flush Redis DB 1 (replay streams, consumer groups, budgets).
#
# Why not `pg_dump trading_db | psql`? A full dump/restore silently dropped every
# constraint into the fresh DB (psql continues past errors), so the trader crashed on
# `ON CONFLICT (sleeve, ts)` with no matching unique constraint. Building the schema
# from migrations is the robust path; only bulk data is copied.
#
# Run from the repo root with the project venv active (needs `alembic` on PATH).
# The live stack can stay up — this only reads trading_db.
#
# Usage: scripts/replay_reset.sh
set -euo pipefail

PG=trading_system-timescaledb-1
REDIS=trading_system-redis-1
DBUSER=trading
REPLAY_DB=trading_replay
REPLAY_COMPOSE=docker-compose.replay.yml
# Copy ONLY the market data the replay pipeline actually reads. The strategist's day
# sleeves consume v3 features; the 5-min gap-ORB sleeve consumes intraday_v2. v1/v2/
# intraday_v1 (~60M unused rows) are skipped — copying all 86M feature rows in one
# COPY crashed the server connection and bloated every reset. Set REPLAY_INTRADAY=1
# to also copy the 5-minute data (adds ~34M intraday_v2 rows; day-only is the default).
REPLAY_INTRADAY="${REPLAY_INTRADAY:-0}"
if [ "$REPLAY_INTRADAY" = "1" ]; then
  OHLCV_INTERVALS="'day','5minute'"
  FEATURE_VERSIONS="v3 intraday_v2"
else
  OHLCV_INTERVALS="'day'"
  FEATURE_VERSIONS="v3"
fi

if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi

# Replay consumers run restart:unless-stopped against trading_replay — if left up they
# reconnect the instant dropdb --force bounces them and block the rebuild. Stop first.
echo ">> stopping replay consumers (if running)"
$DC -f "$REPLAY_COMPOSE" stop strategist trader reporter 2>/dev/null || true

echo ">> dropping + recreating $REPLAY_DB"
docker exec "$PG" dropdb --if-exists --force -U "$DBUSER" "$REPLAY_DB"
docker exec "$PG" createdb -U "$DBUSER" "$REPLAY_DB"
docker exec "$PG" psql -q -U "$DBUSER" -d "$REPLAY_DB" \
  -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"

echo ">> building schema from alembic migrations"
DATABASE_URL="postgresql://$DBUSER:trading_pass@localhost:5432/$REPLAY_DB" alembic upgrade head

# Per-table COPY, not pg_dump -t: ohlcv/features are hypertables whose PARENT holds
# zero rows directly (data lives in chunks), so `pg_dump --data-only -t ohlcv` copies
# nothing. `COPY (SELECT ...) TO STDOUT` reads through every chunk, and `COPY ... FROM
# STDIN` routes rows back into chunks. ON_ERROR_STOP=1 = loud fail.
copy_query() {  # $1 = dest table, $2 = SELECT ... query on trading_db
  docker exec "$PG" bash -c "
    set -euo pipefail
    psql -q -U $DBUSER -d trading_db -c \"COPY ($2) TO STDOUT\" \
      | psql -q -v ON_ERROR_STOP=1 -U $DBUSER -d $REPLAY_DB -c \"COPY $1 FROM STDIN\"
  "
}

echo ">> copying market data (intervals: $OHLCV_INTERVALS · features: $FEATURE_VERSIONS)"
echo "   .. instruments"
copy_query instruments "SELECT * FROM instruments"
echo "   .. ingestion_runs"
copy_query ingestion_runs "SELECT * FROM ingestion_runs"
echo "   .. ohlcv"
copy_query ohlcv "SELECT * FROM ohlcv WHERE interval IN ($OHLCV_INTERVALS)"
for v in $FEATURE_VERSIONS; do
  echo "   .. features ($v)"
  copy_query features "SELECT * FROM features WHERE feature_set_version = '$v'"
done

echo ">> flushing Redis DB 1 (replay namespace)"
docker exec "$REDIS" redis-cli -n 1 FLUSHDB > /dev/null

echo ">> sanity: row counts in $REPLAY_DB"
docker exec "$PG" psql -U "$DBUSER" -d "$REPLAY_DB" -t -c "
  SELECT 'ohlcv: '        || count(*) FROM ohlcv
  UNION ALL SELECT 'features: '   || count(*) FROM features
  UNION ALL SELECT 'instruments: '|| count(*) FROM instruments
  UNION ALL SELECT 'live_orders: '|| count(*) FROM live_orders;
"
echo ">> replay namespace ready — start consumers, THEN the replayer:"
echo "   $DC -f $REPLAY_COMPOSE up -d strategist trader reporter"
echo "   $DC -f $REPLAY_COMPOSE run --rm replayer --from YYYY-MM-DD --to YYYY-MM-DD"
