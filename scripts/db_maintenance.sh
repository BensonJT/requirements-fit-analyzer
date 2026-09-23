#!/bin/bash
# ============================================================================
# jobsearch — disk maintenance: compact the DuckDB file, sweep scratch files,
# prune worktrees and stale in-repo backups, report what only Windows can free.
#
#   bash scripts/db_maintenance.sh            # menu
#   bash scripts/db_maintenance.sh --auto     # unattended: what launch.sh runs after every overnight preset
#   bash scripts/db_maintenance.sh --report   # numbers only, changes nothing
#
# WHY. This box runs out of disk on C:, and three things on the WSL side feed
# it: the DuckDB file only ever grows (freed blocks are reused, never returned
# -- 2026-09-23: 2.1 GB with 35% free blocks), migration backups and worktrees
# get left behind, and WSL's ext4.vhdx keeps every byte the guest ever used
# until Windows compacts it (2026-09-23: 130.9 GB on C: for 94 GB in use).
# The first two are fixed here. The third is the user's own ~/scripts/compress_disk.sh
# (caches, fstrim, then `wsl --shutdown` + the Windows compactor): it is NEVER
# called from here because it ends every WSL session, launcher included; this
# script only measures the slack and says when it is worth running by hand.
#
# --auto never touches the external backup drive and never deletes anything
# a person has not seen deleted before: it compacts the live DB (verified,
# swapped), removes orphan DuckDB scratch files, prunes worktrees git no
# longer knows, deletes merged local branches, and removes IN-REPO db backups
# older than BACKUP_KEEP_DAYS (the same backups exist on the external drive).
# Everything it does is logged to logs/maintenance_<stamp>.log.
# ============================================================================

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1
PY=".venv/bin/python"
DB="db/jobsearch.duckdb"
BACKUP_KEEP_DAYS="${JOBSEARCH_BACKUP_KEEP_DAYS:-7}"          # in-repo db/jobsearch.duckdb.* backups
EXTERNAL_BACKUP_DIR="${JOBSEARCH_BACKUP_DIR:-/mnt/e/backups/jobsearch}"
EXTERNAL_KEEP="${JOBSEARCH_EXTERNAL_BACKUP_KEEP:-3}"        # menu option only, never --auto
VHDX_SLACK_WARN_GB=10
mkdir -p logs
STAMP="$(date +%Y%m%d_%H%M)"
LOG="logs/maintenance_${STAMP}.log"

say() { echo "$*" | tee -a "$LOG"; }
run() { say "  \$ $*"; "$@" 2>&1 | sed 's/^/    /' | tee -a "$LOG"; return "${PIPESTATUS[0]}"; }
mb() { du -sm "$1" 2>/dev/null | cut -f1; }

db_busy() {
  pgrep -af 'sweep_ats\.py|finder\.py|judge2 run' | grep -v pgrep | grep -v "db_maintenance" | head -3
}

# ── steps ──────────────────────────────────────────────────────────────────
step_report() {
  say "── report $(date '+%Y-%m-%d %H:%M:%S')"
  run "$PY" scripts/db_maintenance.py report --db "$DB"
  say "disk     : WSL ext4 $(df -h "$ROOT" | awk 'NR==2{print $4" free of "$2" ("$5" used)"}')"
  if [ -d /mnt/c ]; then
    say "disk     : C: $(df -h /mnt/c 2>/dev/null | awk 'NR==2{print $4" free of "$2" ("$5" used)"}')"
    local vhdx
    local lad
    lad="$(wslpath -u "$(/mnt/c/Windows/System32/cmd.exe /c 'echo %LOCALAPPDATA%' 2>/dev/null | tr -d '\r')" 2>/dev/null)"
    vhdx="$( [ -d "$lad/Packages" ] && find "$lad/Packages" -maxdepth 3 -name ext4.vhdx 2>/dev/null | head -1)"
    if [ -n "$vhdx" ]; then
      local vhdx_gb used_gb slack
      vhdx_gb=$(( $(stat -c %s "$vhdx") / 1073741824 ))
      used_gb=$(( $(df -B1 "$ROOT" | awk 'NR==2{print $3}') / 1073741824 ))
      slack=$(( vhdx_gb - used_gb ))
      say "vhdx     : ${vhdx_gb} GB on C: for ${used_gb} GB used inside WSL  (slack ${slack} GB)"
      if [ "$slack" -ge "$VHDX_SLACK_WARN_GB" ]; then
        say "           -> ${slack} GB is only recoverable by compacting the vhdx (shuts WSL down; by hand, never overnight):"
        say "              ~/scripts/compress_disk.sh --check   then   ~/scripts/compress_disk.sh"
      fi
    fi
  fi
  say "in-repo  : db/ $(mb db) MB total; backups: $(ls db/jobsearch.duckdb.* 2>/dev/null | grep -v '\.wal$' | xargs -r du -ch 2>/dev/null | tail -1 | cut -f1 || echo 0)"
  if [ -d "$EXTERNAL_BACKUP_DIR" ]; then
    say "external : $EXTERNAL_BACKUP_DIR $(ls "$EXTERNAL_BACKUP_DIR" | wc -l) files, $(du -sh "$EXTERNAL_BACKUP_DIR" | cut -f1) (menu option only)"
  fi
  say "worktrees: $(git worktree list | wc -l) registered; stray dirs: $(ls -d "$HOME"/jobsearch_wt_* 2>/dev/null | wc -l)"
  say "cache    : ~/.cache $(du -sh "$HOME/.cache" 2>/dev/null | cut -f1) (huggingface $(du -sh "$HOME/.cache/huggingface" 2>/dev/null | cut -f1)) -- not touched here"
}

step_compact() {
  say "── compact"
  local busy; busy="$(db_busy)"
  if [ -n "$busy" ]; then say "  skipped: database held by:"; say "$busy"; return 2; fi
  run "$PY" scripts/db_maintenance.py compact --db "$DB" "$@"
}

step_scratch() {
  say "── scratch files"
  local busy; busy="$(db_busy)"
  # Orphan DuckDB side files next to the live DB: only when nothing holds it (a live WAL is not an orphan).
  if [ -z "$busy" ]; then
    for f in db/*.duckdb.compacting db/*.duckdb.compacting.wal db/*.duckdb.tmp; do
      [ -e "$f" ] && { say "  rm $f ($(mb "$f") MB)"; rm -f "$f"; }
    done
  else
    say "  database busy -- leaving db/ side files alone"
  fi
  # pytest's per-user temp trees and any scratch DuckDB files under /tmp older than a day.
  find /tmp -maxdepth 1 -user "$(id -un)" \( -name 'pytest-of-*' -o -name '*.duckdb' -o -name '*.duckdb.wal' \) \
       -mtime +1 -print 2>/dev/null | while read -r f; do say "  rm -rf $f"; rm -rf "$f"; done
  find db -maxdepth 1 -name '*.duckdb.wal' -size 0 -mtime +1 -print 2>/dev/null | while read -r f; do say "  rm $f (empty, stale)"; rm -f "$f"; done
  say "  done"
}

step_worktrees() {
  say "── worktrees + branches"
  run git worktree prune -v
  local d
  for d in "$HOME"/jobsearch_wt_*; do
    [ -d "$d" ] || continue
    if git worktree list --porcelain | grep -qx "worktree $d"; then
      say "  keep $d (registered worktree; remove with: git worktree remove $d)"
    else
      say "  rm -rf $d (not a registered worktree, $(mb "$d") MB)"; rm -rf "$d"
    fi
  done
  local b
  for b in $(git branch --merged main --format='%(refname:short)' | grep -vx main); do
    say "  git branch -d $b (merged into main)"; git branch -d "$b" >>"$LOG" 2>&1
  done
  say "  done"
}

step_repo_backups() {
  say "── in-repo db backups older than ${BACKUP_KEEP_DAYS} days"
  find db -maxdepth 1 -type f -name 'jobsearch.duckdb.*' ! -name '*.wal' ! -name '*.compacting' -mtime +"$BACKUP_KEEP_DAYS" -print 2>/dev/null \
    | while read -r f; do say "  rm $f ($(mb "$f") MB)"; rm -f "$f"; done
  say "  done"
}

step_external_backups() {  # menu only: keeps the newest $EXTERNAL_KEEP, lists the rest, asks
  say "── external backups: $EXTERNAL_BACKUP_DIR (keep newest $EXTERNAL_KEEP)"
  [ -d "$EXTERNAL_BACKUP_DIR" ] || { say "  not mounted"; return; }
  local victims
  victims="$(ls -t "$EXTERNAL_BACKUP_DIR"/*duckdb* 2>/dev/null | tail -n +"$((EXTERNAL_KEEP + 1))")"
  [ -n "$victims" ] || { say "  nothing older than the newest $EXTERNAL_KEEP"; return; }
  echo "$victims" | while read -r f; do say "  would rm $f ($(mb "$f") MB)"; done
  read -r -p "Delete these? [y/N] " OK
  case "$OK" in y|Y|yes|YES) echo "$victims" | while read -r f; do rm -f "$f"; say "  rm $f"; done ;; *) say "  kept" ;; esac
}

# ── modes ──────────────────────────────────────────────────────────────────
MODE="${1:-menu}"
case "$MODE" in
  --report) step_report; exit 0 ;;
  --auto)
    say "db_maintenance --auto $(date '+%Y-%m-%d %H:%M:%S')"
    T0=$(date +%s)
    step_report
    step_compact; RC=$?
    step_scratch
    step_worktrees
    step_repo_backups
    say "── after"
    run "$PY" scripts/db_maintenance.py report --db "$DB" | grep -E "database|blocks"
    say "db_maintenance --auto done in $(( $(date +%s) - T0 ))s (compact rc=$RC); log $LOG"
    exit 0 ;;
  menu|"") ;;
  *) echo "usage: $0 [--auto|--report]" >&2; exit 1 ;;
esac

echo "═══════════════════════════════════════════════════════════════════════"
echo " jobsearch disk maintenance"
echo "═══════════════════════════════════════════════════════════════════════"
step_report
echo
echo "  1) everything --auto does (compact if >=10% free, scratch, worktrees, in-repo backups >${BACKUP_KEEP_DAYS}d)"
echo "  2) compact the database now (--force, even under the threshold)"
echo "  3) scratch files + worktrees + merged branches only"
echo "  4) prune external backups to the newest ${EXTERNAL_KEEP} (asks before deleting)"
echo "  q) quit"
read -r -p "Choice: " CHOICE
case "$CHOICE" in
  1) step_compact; step_scratch; step_worktrees; step_repo_backups ;;
  2) step_compact --force ;;
  3) step_scratch; step_worktrees ;;
  4) step_external_backups ;;
  *) echo "Nothing done." ;;
esac
say "log: $LOG"
