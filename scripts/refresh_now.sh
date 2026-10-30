#!/usr/bin/env bash
# On-demand refresh, the same three steps a pipeline run ends with:
#   1. finder.py apply-queue   queued decisions (feedback applet) -> DuckDB
#   2. scripts/pg_snapshot.py  DuckDB -> Postgres jobs.fit_snapshot
#   3. POST_SNAPSHOT_CMD       (.env) rebuilds the reporting views, workbook and focus report
# Used by the feedback applet's "Apply queue + refresh" button; safe to run by hand.
# Exit 0 done · 2 a pipeline run holds the DuckDB (no snapshot; the report pages are still rebuilt, since their
# views read queued calls and tracker rows live, 2026-10-30) · 1 other error.
# Log: logs/refresh_now.log
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
mkdir -p logs
exec > >(tee -a logs/refresh_now.log) 2>&1
echo "== $(date '+%F %T') refresh_now"

# Locked: rebuild just the focus and bridge pages from Postgres (a second or so), then exit 2.
pages_only() {
  local dir="${JOBSEARCH_VAULT_DIR:-}/Tools/reporting" s
  [ -n "${JOBSEARCH_VAULT_DIR:-}" ] || { echo "  (JOBSEARCH_VAULT_DIR unset: pages not rebuilt)"; exit 2; }
  for s in build_focus_report.py build_bridge_report.py; do
    (cd "$dir" && python3 "$s") || echo "  page rebuild failed: $s"
  done
  echo "PAGES: report pages rebuilt at $(date '+%-I:%M %p') with your queued calls; the snapshot waits for the run."
  exit 2
}

# A launch.sh run (or a sweep) owns the database for hours: say so and stop before touching anything.
if pgrep -f "finder\.py (top|screen|rescreen-all|retrain|report)|sweep_ats\.py|scripts/pg_snapshot\.py" >/dev/null; then
  echo "LOCKED: a pipeline run is using the database. Nothing changed; its own snapshot will include your queued decisions."
  pages_only
fi

out=$(.venv/bin/python finder.py apply-queue 2>&1); rc=$?
echo "$out"
if [ $rc -ne 0 ]; then
  if echo "$out" | grep -qi "locked"; then
    echo "LOCKED: the DuckDB is held by another process. Nothing changed; try again when it finishes."
    pages_only
  fi
  echo "FAILED: apply-queue exited $rc"; exit 1
fi

out=$(timeout 900 .venv/bin/python -u scripts/pg_snapshot.py 2>&1); rc=$?
echo "$out"
if [ $rc -ne 0 ]; then
  if echo "$out" | grep -qi "locked"; then
    echo "LOCKED: the DuckDB was taken by another process before the snapshot. Queued decisions are applied; the snapshot waits."
    pages_only
  fi
  echo "FAILED: pg_snapshot exited $rc"; exit 1
fi

if [ -n "${POST_SNAPSHOT_CMD:-}" ]; then
  bash -c "$POST_SNAPSHOT_CMD" || { echo "FAILED: report rebuild (POST_SNAPSHOT_CMD)"; exit 1; }
fi
echo "DONE: queue applied, snapshot taken, report rebuilt at $(date '+%-I:%M %p')."
