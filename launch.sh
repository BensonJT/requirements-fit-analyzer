#!/bin/bash
# ============================================================================
# jobsearch — interactive pipeline launcher, with an optional scheduled start.
#
#   bash launch.sh              # menu, start now
#   bash launch.sh 02:00        # menu, then wake + start at 02:00 (the NEXT 02:00)
#   bash launch.sh 00:00        # midnight start: what FULL + GOLD SCORE (~7-7.5 h) needs to finish by ~7:30
#   bash launch.sh "2026-09-24 02:00"
#
# Modelled on Meridian's scripts/launch.sh, which has run its nightly batch this
# way for weeks. The menu prints the resolved commands before anything runs.
#
# HOW THE NIGHT WORKS (WSL). A Windows scheduled task with WakeToRun fires five
# minutes before the start (scripts/windows/wake_task.sh), so the laptop may
# SLEEP through the wait and wake itself; the Windows "DisableSleep" task then
# holds it awake for the run, and on exit this script deletes its own wake tasks
# and runs "RestoreSleep" -- unless a keep-awake lease is present, which nothing
# automated may revoke. LEAVE THIS TERMINAL WINDOW OPEN: the launcher is the
# process that waits; closing the window cancels the run (and the EXIT trap
# still cleans up the wake task).
#
# WHY NOT CRON. A sleeping laptop never fires a cron slot and vanilla cron has no
# catch-up, so a missed night is lost with no error anywhere (Meridian proved it
# on 2026-07-25). A wake task is the fix.
#
# Pre-flight runs TWICE: when you confirm (so a problem shows up before you go to
# bed) and again at the start time. Everything is logged to
# logs/launch_<stamp>.log; per-preset timings go to logs/launch_timings.jsonl
# and are shown on the menu next time.
# ============================================================================

set -uo pipefail

# ── snapshot-and-re-exec ───────────────────────────────────────────────────
# bash reads a script incrementally by byte offset, so editing this file while a
# scheduled launch is waiting would corrupt the waiting run (Meridian, 2026-08-20).
# Run from a private copy instead; SELF_ORIG keeps the real path.
if [ -n "${JOBSEARCH_SELF_COPY:-}" ]; then
  SELF_ORIG="${JOBSEARCH_SELF_ORIG:-$0}"
  rm -f "$JOBSEARCH_SELF_COPY" 2>/dev/null || true
  unset JOBSEARCH_SELF_COPY JOBSEARCH_SELF_ORIG
else
  SELF_ORIG="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/$(basename "$0")"
  _copy="$(mktemp 2>/dev/null || true)"
  if [ -n "$_copy" ] && cat "$SELF_ORIG" >"$_copy" 2>/dev/null; then
    export JOBSEARCH_SELF_COPY="$_copy" JOBSEARCH_SELF_ORIG="$SELF_ORIG"
    exec bash "$_copy" ${1+"$@"}
  fi
fi

ROOT="$(cd "$(dirname "$SELF_ORIG")" && pwd)"
cd "$ROOT" || exit 1
PY=".venv/bin/python"
mkdir -p logs

PLATFORM=linux
if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
  PLATFORM=wsl
fi

# ── presets ────────────────────────────────────────────────────────────────
# A preset is a list of steps joined by '|':
#   retrain            finder.py retrain (gated + ledgered; promotes AND rescreens only if a model passes)
#   sweep:<args>       sweep_ats.py <args> -- sweep, JD fetch, tracker sync, screen, coverage,
#                      required-embed, [Gemma judge if --llm-top N], Jobs_Found, snapshots
#                      (with --skip-sweep --no-screen it is only a JD fetch: the directional backfill preset;
#                      the next FULL run screens those rows because their JD is newer than their screen)
#   sync               finder.py sync (mirror Application_Tracker.md)
#   top                finder.py top (the END-of-pipeline Top_Jobs file)
#   judge2run          finder.py judge2 run --eval-set: Gemma judges every blind human-graded gold row
#                      (vw_report_feedback_blind, read at start time, so rows graded today count tonight);
#                      rows already judged under the current prompt_version are served from cache
#   judge2eval         finder.py judge2 eval: catch / agree against the gold labels + the line report;
#                      the numbers land in the launch log (bar: catch >= 70%, agree >= 85%)
#   @dryrun            pre-flight + plan only
# --llm-top N and the judge2* steps turn on the Gemma second judge with the user's approved fact sheet
# (see judge_env below). The top-100 judge and the gold eval set barely overlap (3 of 100 on 9/23), so
# scoring Gemma against gold needs its own step; that is what FULL + GOLD SCORE adds.
LABELS=(
  "Overnight FULL            sweep + screen + coverage + Gemma judge (top 100) + Top_Jobs"
  "Overnight FULL + GOLD SCORE  as FULL, then Gemma judges the gold eval set and scores it (start at 00:00 to finish by ~7:30)"
  "Overnight FULL + RETRAIN  retrain models on new labels first, then as FULL"
  "Overnight LIGHT           sweep + screen + coverage + Top_Jobs; no Gemma judge"
  "Gold score only           Gemma judges the gold eval set + scores it; no sweep (~3.5 h per 110 rows)"
  "JD backfill: directional  fetch JDs for the vw_jd_missing 'directional' tier (wider than the prefilter); no sweep, no screen"
  "Report only               tracker sync + Top_Jobs (minutes)"
  "Dry run                   pre-flight checks + the plan; schedules and writes nothing"
)
ESTIMATES=("~3.75 h (9/23 measured)" "~7-7.5 h (3.75 h FULL + ~3.5 h eval set)" "~5-5.5 h (estimate)" "~1.5 h (estimate)" "~3.5 h (estimate)" "~8 min per 2,000 JDs" "~2 min" "seconds")
PRESETS=(
  "sweep:--llm-top 100|top"
  "sweep:--llm-top 100|judge2run|judge2eval|top"
  "retrain|sweep:--llm-top 100|top"
  "sweep:|top"
  "judge2run|judge2eval"
  "sweep:--skip-sweep --detail-pattern directional --detail-budget 2000 --no-screen"
  "sync|top"
  "@dryrun"
)

timing_for() {  # last completed run of this exact preset, from logs/launch_timings.jsonl
  "$PY" - "$1" <<'EOF' 2>/dev/null
import json, sys
last = None
try:
    for line in open("logs/launch_timings.jsonl", encoding="utf-8"):
        r = json.loads(line)
        if r.get("preset") == sys.argv[1] and r.get("status") == 0:
            last = r
except OSError:
    pass
if last:
    s = int(last["seconds"])
    print(f"last run: {s // 3600}h {s % 3600 // 60:02d}m on {last['finished'][:16]}")
EOF
}

echo "═══════════════════════════════════════════════════════════════════════"
echo " jobsearch pipeline launcher                platform: $PLATFORM"
echo "═══════════════════════════════════════════════════════════════════════"
i=0
while [ "$i" -lt "${#LABELS[@]}" ]; do
  T="$(timing_for "${PRESETS[$i]}")"
  printf "  %d) %s\n     %s\n" "$((i + 1))" "${LABELS[$i]}" "${T:-typical: ${ESTIMATES[$i]}}"
  i=$((i + 1))
done
echo "  c) custom — type your own sweep_ats.py arguments"
echo "  q) quit"
echo
read -r -p "Choice: " CHOICE
case "$CHOICE" in
  q|Q) echo "Nothing launched."; exit 0 ;;
  c|C)
    read -r -p "sweep_ats.py arguments (e.g. --llm-top 50 --full-screen): " CUSTOM
    PRESET="sweep:${CUSTOM}|top"
    ;;
  ''|*[!0-9]*) echo "launch: not a valid choice." >&2; exit 1 ;;
  *)
    IDX=$((CHOICE - 1))
    if [ "$IDX" -lt 0 ] || [ "$IDX" -ge "${#PRESETS[@]}" ]; then
      echo "launch: choice out of range." >&2; exit 1
    fi
    PRESET="${PRESETS[$IDX]}"
    ;;
esac

DRY_RUN=false
[ "$PRESET" = "@dryrun" ] && { DRY_RUN=true; PRESET="sweep:--llm-top 100|top"; }
IFS='|' read -r -a STEPS <<<"$PRESET"
USES_JUDGE=false
case "$PRESET" in *--llm-top*|*judge2*) USES_JUDGE=true ;; esac

# ── start time ─────────────────────────────────────────────────────────────
START_SPEC="${1:-now}"
if [ "$START_SPEC" = "now" ]; then
  START_EPOCH=$(date +%s)
else
  case "$START_SPEC" in
    [0-9]:[0-9][0-9] | [0-9][0-9]:[0-9][0-9] | \
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]" "[0-9]:[0-9][0-9] | \
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]" "[0-9][0-9]:[0-9][0-9])
      START_EPOCH=$(date -d "$START_SPEC" +%s 2>/dev/null) ;;
    *) START_EPOCH="" ;;
  esac
  if [ -z "${START_EPOCH:-}" ]; then
    echo "launch: could not parse '$START_SPEC'. Use HH:MM (e.g. 02:00) or 'YYYY-MM-DD HH:MM'." >&2
    exit 1
  fi
  # A bare HH:MM already past today means the NEXT one, never one in the past.
  [ "$START_EPOCH" -lt "$(date +%s)" ] && START_EPOCH=$((START_EPOCH + 86400))
fi
WAIT_SECS=$((START_EPOCH - $(date +%s)))
[ "$WAIT_SECS" -lt 0 ] && WAIT_SECS=0

step_cmd() {  # the literal command line a step runs
  case "$1" in
    retrain) echo "$PY -u finder.py retrain" ;;
    sync)    echo "$PY -u finder.py sync" ;;
    top)     echo "$PY -u finder.py top" ;;
    sweep:*) echo "$PY -u sweep_ats.py ${1#sweep:}" ;;
    judge2run)  echo "$PY -u finder.py judge2 run --eval-set --background file --background-file $JUDGE_BG --i-have-approval" ;;
    judge2eval) echo "$PY -u finder.py judge2 eval --background file --background-file $JUDGE_BG" ;;
    *)       echo "" ;;
  esac
}

# The judge sends ONLY the user-approved fact sheet (judge2_background.local.md, gitignored); never the rubric.
# Defined before step_cmd is first called (the plan printout below); the eval steps name it on the command line
# because `finder.py judge2` defaults --background to public and does not read JUDGE2_BACKGROUND from the env.
JUDGE_BG="$ROOT/judge2_background.local.md"
judge_env() {
  export JUDGE2_LIVE_OK=1 JUDGE2_BACKGROUND=file JUDGE2_BACKGROUND_FILE="$JUDGE_BG"
}

# ── pre-flight ─────────────────────────────────────────────────────────────
# Each check prints one line; returns non-zero if the run should not start.
preflight() {
  local ok=0
  if [ -f .env ]; then set -a; . ./.env; set +a; else echo "  FAIL  .env missing"; ok=1; fi
  if "$PY" finder.py setup-check >/dev/null 2>&1; then echo "  ok    setup-check"; else echo "  FAIL  finder.py setup-check (run it by hand to see why)"; ok=1; fi
  local busy
  busy="$(pgrep -af 'sweep_ats\.py|finder\.py' | grep -v pgrep | grep -v "launch.sh" | head -3)"
  if [ -n "$busy" ]; then echo "  WAIT  another jobsearch process holds the database:"; echo "$busy" | sed 's/^/          /'; ok=2
  elif "$PY" -c "import duckdb; duckdb.connect('db/jobsearch.duckdb', read_only=True).close()" >/dev/null 2>&1; then echo "  ok    database free"
  else echo "  WAIT  database locked by another process"; ok=2; fi
  if [ "$USES_JUDGE" = true ]; then
    if [ -r "$JUDGE_BG" ]; then echo "  ok    judge fact sheet ($(basename "$JUDGE_BG"))"; else echo "  FAIL  judge fact sheet missing: $JUDGE_BG"; ok=1; fi
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 20 \
      "https://generativelanguage.googleapis.com/v1beta/models?key=${GEMINI_API_KEY:-none}" 2>/dev/null)"
    if [ "$code" = "200" ]; then echo "  ok    Gemini API reachable"
    else echo "  WARN  Gemini API returned HTTP ${code:-none} (VPN on? key?) -- the judge stage would be skipped, the rest still runs"; fi
  fi
  if curl -s -o /dev/null --max-time 20 https://boards-api.greenhouse.io/v1/boards/stripe/jobs 2>/dev/null; then echo "  ok    network"; else echo "  FAIL  no network"; ok=1; fi
  return $ok
}

echo
echo "───────────────────────────────────────────────────────────────────────"
for s in "${STEPS[@]}"; do echo "  step    : $(step_cmd "$s")"; done
[ "$USES_JUDGE" = true ] && echo "  judge   : Gemma second judge ON, fact sheet $(basename "$JUDGE_BG")"
if [ "$WAIT_SECS" -gt 0 ]; then
  printf "  starts  : %s  (in %dh %dm)\n" "$(date -d "@$START_EPOCH" '+%a %Y-%m-%d %H:%M')" \
    "$((WAIT_SECS / 3600))" "$(((WAIT_SECS % 3600) / 60))"
  [ "$PLATFORM" = "wsl" ] && [ "$DRY_RUN" = false ] && echo "  wake    : a Windows task fires 5 min earlier; the laptop may sleep until then"
  echo "  window  : LEAVE THIS TERMINAL OPEN until the run finishes"
else
  echo "  starts  : immediately"
fi
echo "───────────────────────────────────────────────────────────────────────"
echo "Pre-flight (now):"
preflight; PF=$?
if [ "$DRY_RUN" = true ]; then
  echo
  echo "Dry run: nothing scheduled, nothing written. Pre-flight status: $PF (0 = ready)."
  exit "$PF"
fi
if [ "$PF" = 1 ]; then
  echo "launch: pre-flight failed -- fix the FAIL line(s) above first." >&2
  exit 1
fi
[ "$PF" = 2 ] && [ "$WAIT_SECS" -eq 0 ] && { echo "launch: the database is busy right now -- not starting." >&2; exit 1; }
[ "$PF" = 2 ] && echo "  (the database only has to be free at the start time; it is checked again then)"
read -r -p "Proceed? [y/N] " OK
case "$OK" in y|Y|yes|YES) ;; *) echo "Cancelled."; exit 0 ;; esac

# ── Windows availability ───────────────────────────────────────────────────
# One EXIT trap owns ALL cleanup (bash replaces a trap, it does not stack them); INT/TERM just exit so
# the EXIT trap fires -- a handler that only cleaned up would let the wait loop carry on (Meridian 2026-08-07).
if [ "$PLATFORM" = "wsl" ]; then
  # shellcheck source=scripts/windows/wake_task.sh
  . "$ROOT/scripts/windows/wake_task.sh"
  cleanup() { cleanup_all_wake_tasks; restore_sleep; }
  trap cleanup EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  cleanup_all_wake_tasks
  if [ "$WAIT_SECS" -gt 0 ]; then
    create_wake_task "$START_EPOCH" "$ROOT/scripts/windows/wake_stub.sh" || {
      echo "launch: wake task failed -- holding the machine awake instead" >&2
      /mnt/c/Windows/System32/schtasks.exe /run /tn DisableSleep >/dev/null 2>&1 || true
    }
  fi
fi

# ── wait ───────────────────────────────────────────────────────────────────
if [ "$WAIT_SECS" -gt 0 ]; then
  echo "launch: waiting for $(date -d "@$START_EPOCH" '+%H:%M') … (Ctrl+C cancels and removes the wake task)"
  while [ "$(date +%s)" -lt "$START_EPOCH" ]; do
    REMAIN=$((START_EPOCH - $(date +%s)))
    printf "\r  %02d:%02d:%02d remaining " $((REMAIN / 3600)) $(((REMAIN % 3600) / 60)) $((REMAIN % 60))
    # Poll, never one long sleep: a laptop that slept and resumed would otherwise oversleep.
    if [ "$REMAIN" -gt 120 ]; then sleep 60; else sleep 5; fi
  done
  printf "\r%*s\r" 32 ""
fi
# Held awake for the run itself; the wake task only covers waking up.
[ "$PLATFORM" = "wsl" ] && /mnt/c/Windows/System32/schtasks.exe /run /tn DisableSleep >/dev/null 2>&1 || true

# ── run ────────────────────────────────────────────────────────────────────
STAMP="$(date +%Y%m%d_%H%M)"
LOG="logs/launch_${STAMP}.log"
exec > >(tee -a "$LOG") 2>&1
echo "launch: $(date '+%Y-%m-%d %H:%M:%S')  preset: $PRESET"
echo "Pre-flight (start time):"
# The database may still be held (a late session, a long judge run): wait up to an hour for it.
for attempt in 1 2 3 4 5 6 7 8 9 10 11 12 13; do
  preflight; PF=$?
  [ "$PF" != 2 ] && break
  [ "$attempt" = 13 ] && { echo "launch: database still busy after an hour -- giving up."; PF=1; break; }
  echo "  database busy -- checking again in 5 minutes"; sleep 300
done
if [ "$PF" = 1 ]; then
  echo "launch: pre-flight failed at the start time -- nothing run."
  exit 1
fi
export PYTHONUNBUFFERED=1
[ "$USES_JUDGE" = true ] && judge_env

RUN_STARTED=$(date +%s)
STATUS=0
for s in "${STEPS[@]}"; do
  CMD="$(step_cmd "$s")"
  echo
  echo "════ $(date '+%H:%M:%S')  $CMD"
  T0=$(date +%s)
  # shellcheck disable=SC2086
  $CMD
  RC=$?
  printf "════ %s  exit %d after %dm\n" "$(date '+%H:%M:%S')" "$RC" $(( ($(date +%s) - T0) / 60 ))
  if [ "$RC" -ne 0 ]; then
    STATUS=$RC
    # A failed retrain or sweep still leaves a usable database: keep going so Top_Jobs is written.
    [ "$s" = "top" ] || echo "launch: step failed -- continuing so the report is still written"
  fi
done

SECS=$(( $(date +%s) - RUN_STARTED ))
echo
echo "launch: finished $(date '+%Y-%m-%d %H:%M:%S') after $((SECS / 3600))h $((SECS % 3600 / 60))m, status $STATUS"
NEWEST="$(ls -t "${JOBSEARCH_VAULT_DIR:-/nonexistent}"/Search_Results/Top_Jobs_*.md 2>/dev/null | head -1)"
[ -n "$NEWEST" ] && echo "launch: newest report: $NEWEST"
echo "launch: full log: $ROOT/$LOG"
"$PY" - "$PRESET" "$SECS" "$STATUS" <<'EOF' 2>/dev/null || true
import json, sys, datetime
with open("logs/launch_timings.jsonl", "a", encoding="utf-8") as f:
    f.write(json.dumps({"preset": sys.argv[1], "seconds": int(sys.argv[2]), "status": int(sys.argv[3]),
                        "finished": datetime.datetime.now().isoformat(timespec="minutes")}) + "\n")
EOF
exit "$STATUS"
