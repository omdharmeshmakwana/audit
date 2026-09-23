#!/usr/bin/env bash
# Phase 1 demo (spec §9): seed, ingest the synthetic chain as events, verify-log passes,
# rebuild reproduces the derived tables, tamper one row, verify-log fails on it.
# Runs against a throwaway database so the real log is never touched.
set -euo pipefail
cd "$(dirname "$0")/../server"

DB=faible_demo
PY=.venv/bin/python
export FAIBLE_DATABASE_URL="postgresql+psycopg:///$DB"
FAIBLE_DATA_DIR="$(mktemp -d)"
export FAIBLE_DATA_DIR

step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }

step "fresh database $DB"
dropdb --if-exists "$DB"
createdb "$DB"
$PY -m alembic upgrade head 2>&1 | grep -v "^INFO" || true

step "seed (users, DEMO project, shots, characters, synthetic chain)"
$PY -m faible.cli seed

step "seed again (idempotent: every event is a duplicate)"
$PY -m faible.cli seed

step "verify-log"
$PY -m faible.cli verify-log

step "rebuild (derived checksum before == after)"
$PY -m faible.cli rebuild

step "the log refuses a normal UPDATE"
OUT=$(psql -q -d "$DB" -c "UPDATE events SET source = 'x' WHERE seq = 1" 2>&1 || true)
if grep -q "append-only" <<<"$OUT"; then
  echo "blocked by trigger: $OUT"
else
  echo "FAIL: UPDATE was not blocked"; exit 1
fi

SEQ=$(psql -tA -d "$DB" -c "SELECT seq FROM events WHERE type = 'generation.requested' ORDER BY seq LIMIT 1")
step "tamper with event seq $SEQ (superuser bypasses the trigger) and change its prompt"
psql -q -d "$DB" -c "SET session_replication_role = replica;
  UPDATE events SET payload = jsonb_set(payload, '{prompt}', '\"a different prompt\"') WHERE seq = $SEQ;"

step "verify-log (must fail at seq $SEQ)"
if $PY -m faible.cli verify-log; then
  echo "FAIL: tampering was not detected"; exit 1
fi

dropdb "$DB"
rm -rf "$FAIBLE_DATA_DIR"
printf '\n\033[1mPhase 1 demo passed.\033[0m\n'
