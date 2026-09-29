#!/usr/bin/env bash
# Gold reassessment: re-run BOTH second appraisers on the blind gold set and score them, so the results can be
# read and compared in the morning. First written 2026-09-28 after the splitter Layer 1 change (SPLITTER_VERSION
# 2026-09-28.1); reusable whenever the splitter, the fact sheet or a prompt changes.
#
#   bash scripts/gold_redo.sh [--tag TAG] [--skip-gemma] [--skip-jev] [--dry-run] [--detach]
#
#   --tag TAG     judge2 run tag for the Gemma re-ask (default gold<MMDD>). judge2's prompt_version does NOT
#                 include the splitter, so a plain `judge2 run --eval-set` would serve cached answers; this
#                 re-asks under <pv>:TAG and keeps the old rows for the before/after compare.
#   --skip-gemma  Jev only.       --skip-jev  Gemma only.
#   --dry-run     pre-flight + the plan + both dry runs; no live call, nothing written.
#   --detach      re-launch itself with setsid/nohup so closing the terminal does not stop it.
#
# Order: Jev live passes first (~15-25 min), then Gemma (~3.5 h on the free tier), then every eval, so the
# final `jev eval` compares Jev with the Gemma rows this same run produced. Each step is logged; a failed step
# is recorded and the rest still run. Output: logs/gold_redo_<stamp>/ (untracked; *.log is gitignored) with
# one log per step and SUMMARY.log, the file to read in the morning.
set -uo pipefail

cd "$(dirname "$0")/.." || exit 1
ROOT="$(pwd)"
PY="$ROOT/.venv/bin/python"
BG="$ROOT/judge2_background.local.md"
TAG="gold$(date +%m%d)"
DO_GEMMA=true; DO_JEV=true; DRY=false; DETACH=false
while [ $# -gt 0 ]; do
  case "$1" in
    --tag) TAG="$2"; shift 2 ;;
    --skip-gemma) DO_GEMMA=false; shift ;;
    --skip-jev) DO_JEV=false; shift ;;
    --dry-run) DRY=true; shift ;;
    --detach) DETACH=true; shift ;;
    -h|--help) sed -n 2,20p "$0"; exit 0 ;;
    *) echo "gold_redo: unknown argument $1 (see --help)" >&2; exit 2 ;;
  esac
done

STAMP="$(date +%Y%m%d_%H%M)"
OUT="$ROOT/logs/gold_redo_$STAMP"
if [ "$DETACH" = true ]; then
  mkdir -p "$OUT"
  args=(--tag "$TAG"); [ "$DO_GEMMA" = false ] && args+=(--skip-gemma); [ "$DO_JEV" = false ] && args+=(--skip-jev)
  [ "$DRY" = true ] && args+=(--dry-run)
  setsid nohup bash "$0" "${args[@]}" > "$OUT/console.log" 2>&1 < /dev/null &
  echo "gold_redo: detached (pid $!). Follow it with:  tail -f $OUT/console.log"
  echo "           the morning read is: $OUT/SUMMARY.log (written as each step finishes)"
  exit 0
fi
mkdir -p "$OUT"
SUMMARY="$OUT/SUMMARY.log"
say() { echo "$*" | tee -a "$SUMMARY"; }

# ---------------------------------------------------------------- pre-flight
say "gold_redo $STAMP  tag=$TAG  gemma=$DO_GEMMA  jev=$DO_JEV  dry_run=$DRY"
fail=0
if [ -f .env ]; then set -a; . ./.env; set +a; say "  ok    .env loaded"; else say "  FAIL  .env missing"; fail=1; fi
[ -r "$BG" ] && say "  ok    fact sheet $(basename "$BG")" || { say "  FAIL  fact sheet missing: $BG"; fail=1; }
busy="$(pgrep -af 'sweep_ats\.py|finder\.py' | grep -v pgrep | grep -v gold_redo | head -3)"
if [ -n "$busy" ]; then say "  FAIL  another jobsearch process holds the database:"; say "$busy"; fail=1
elif "$PY" -c "import duckdb; duckdb.connect('db/jobsearch.duckdb', read_only=True).close()" >/dev/null 2>&1; then
  say "  ok    database free"
else say "  FAIL  database locked by another process"; fail=1; fi
if [ "$DO_GEMMA" = true ]; then
  [ -n "${GEMINI_API_KEY:-}" ] || { say "  FAIL  GEMINI_API_KEY not set"; fail=1; }
  code="$(printf 'x-goog-api-key: %s\n' "${GEMINI_API_KEY:-none}" | curl -s -o /dev/null -w '%{http_code}' \
    --max-time 20 -H @- https://generativelanguage.googleapis.com/v1beta/models 2>/dev/null)"
  [ "$code" = "200" ] && say "  ok    Gemini API reachable" || { say "  FAIL  Gemini API HTTP ${code:-none} (VPN on? key?)"; fail=1; }
fi
if [ "$DO_JEV" = true ]; then
  [ -n "${TYPESAFE_API_KEY:-}" ] || { say "  FAIL  TYPESAFE_API_KEY not set"; fail=1; }
  code="$(printf 'Authorization: Bearer %s\n' "${TYPESAFE_API_KEY:-none}" | curl -s -o /dev/null -w '%{http_code}' \
    --max-time 20 -H @- https://api.typesafe.ai/v1/models 2>/dev/null)"
  [ "$code" = "200" ] && say "  ok    TypeSafe API reachable" || { say "  FAIL  TypeSafe API HTTP ${code:-none}"; fail=1; }
fi
[ "$fail" = 0 ] || { say "gold_redo: pre-flight failed; nothing was run."; exit 1; }

# The prompt versions this run reads and writes (computed, never hard-coded, so the script stays reusable).
J2PV="$("$PY" -c "from backend.finder import judge2 as J; print(J.prompt_version(J.get_background('file', path='$BG')))")"
JEVPV="$("$PY" -c "from backend.finder import jev; print(jev.prompt_version(jev.load_facts('$BG'), jev.endpoint_from_env()))")"
SENT_TAG="sent$(date +%m%d)"
say "  judge2 prompt_version $J2PV  (re-ask stored as $J2PV:$TAG)"
say "  jev    prompt_version $JEVPV (canonical; repeats r2, r3; injection inj; sentinel $SENT_TAG)"

# This run's approvals and limits, for this process only (.env is not edited; load_dotenv never overrides these).
export JUDGE2_LIVE_OK=1 JUDGE2_BACKGROUND=file JUDGE2_BACKGROUND_FILE="$BG"
export JEV_LIVE_OK=1 JEV_DAILY_TOKEN_CAP="${JEV_DAILY_TOKEN_CAP_OVERRIDE:-10000000}"
export PYTHONUNBUFFERED=1

declare -a RESULTS=()
step() {  # step <name> <command...>: run, log to its own file, record the outcome, never stop the script
  local name="$1"; shift
  local log="$OUT/$name.log" t0=$SECONDS rc
  say ""; say "=== $name   $(date '+%H:%M')   $*"
  "$@" > "$log" 2>&1; rc=$?
  RESULTS+=("$(printf '%-16s rc=%-3s %4d min' "$name" "$rc" $(((SECONDS - t0) / 60)))")
  tail -n 4 "$log" | sed 's/^/    /' | tee -a "$SUMMARY" >/dev/null
  return 0
}

if [ "$DRY" = true ]; then
  [ "$DO_JEV" = true ] && step jev_dryrun "$PY" -u finder.py jev run --eval-set --dry-run --show 0 --background-file "$BG"
  [ "$DO_GEMMA" = true ] && step gemma_dryrun "$PY" -u finder.py judge2 run --eval-set --rerun --run-tag "$TAG" \
    --dry-run --show 0 --background file --background-file "$BG"
  say ""; say "dry run done: nothing live was called. Re-run without --dry-run to go."; exit 0
fi

# ---------------------------------------------------------------- 1. Jev live passes (fast)
if [ "$DO_JEV" = true ]; then
  [ -f db/jev_sentinel_ids.txt ] || step jev_sentinel_init "$PY" -u finder.py jev sentinel --init
  step jev_gold      "$PY" -u finder.py jev run --eval-set  --background-file "$BG" --i-have-approval
  step jev_repeat_r2 "$PY" -u finder.py jev run --eval-set  --run-tag r2 --background-file "$BG" --i-have-approval
  step jev_repeat_r3 "$PY" -u finder.py jev run --eval-set  --run-tag r3 --background-file "$BG" --i-have-approval
  step jev_injection "$PY" -u finder.py jev run --injection --background-file "$BG" --i-have-approval
  step jev_sentinel  "$PY" -u finder.py jev run --sentinel --run-tag "$SENT_TAG" --background-file "$BG" --i-have-approval
fi

# ---------------------------------------------------------------- 2. Gemma re-ask (slow, free tier)
if [ "$DO_GEMMA" = true ]; then
  step gemma_gold "$PY" -u finder.py judge2 run --eval-set --rerun --run-tag "$TAG" \
    --background file --background-file "$BG" --i-have-approval
fi

# ---------------------------------------------------------------- 3. every eval, last
if [ "$DO_GEMMA" = true ]; then
  step gemma_eval    "$PY" -u finder.py judge2 eval --prompt-version "$J2PV:$TAG" --background file --background-file "$BG"
  step gemma_compare "$PY" -u finder.py judge2 eval --compare "$J2PV" "$J2PV:$TAG"
fi
if [ "$DO_JEV" = true ]; then
  j2arg=(); [ "$DO_GEMMA" = true ] && j2arg=(--judge2-pv "$J2PV:$TAG")
  step jev_eval   "$PY" -u finder.py jev eval --tags r2,r3 --sentinel-tag "$SENT_TAG" "${j2arg[@]}" --background-file "$BG"
  step jev_status "$PY" -u finder.py jev status
fi

say ""; say "=== done $(date '+%a %H:%M')"
for r in "${RESULTS[@]}"; do say "  $r"; done
say ""
say "Morning read: the full reports are $OUT/gemma_eval.log, gemma_compare.log and jev_eval.log."
say "Gemma before/after the splitter: gemma_compare.log ($J2PV vs $J2PV:$TAG)."
