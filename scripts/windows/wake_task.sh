#!/bin/bash
# ============================================================================
# Windows wake-task helpers (WSL only) — sourceable library, no main line.
#
# COPIED 2026-09-22 from Meridian (scripts/windows/wake_task.sh), where it has run nightly batches for
# weeks. Kept as a copy on purpose so this repo never depends on another checkout. Changes from the
# original, and nothing else: jobsearch's own task-name prefix (so neither launcher's sweep can delete the
# other's pending wake), Meridian's news-lane prefix dropped, and the keep-awake lease path made
# overridable with JOBSEARCH_KEEP_AWAKE_LEASE (default: the machine-wide lease Meridian's keep_awake.sh
# writes -- one laptop, one keep-awake).
#
# EXTRACTED VERBATIM from scripts/schedule_runs.sh, which has been driving
# Ralph's overnight loops for months and which its owner describes as working
# great. Nothing here is new logic; it is lifted so that the batch launcher
# can use the same mechanism instead of growing a second, subtly different
# copy of ~90 lines of localized-date PowerShell.
#
# WHY A WAKE TASK RATHER THAN STAYING AWAKE. The obvious approach — hold the
# machine awake from now until the start time — burns hours of power and heat
# for nothing. This registers a Windows scheduled task at (start - 5 min) with
# WakeToRun set, so the laptop SLEEPS through the wait and wakes itself just
# in time. That property is the whole trick: schtasks alone will not wake a
# sleeping machine, it has to be set afterwards through Set-ScheduledTask.
#
# Two details that look like noise and are not:
#   * schtasks demands the date in the CURRENT CULTURE's short-date pattern,
#     so the date and time are resolved through PowerShell rather than
#     date(1). A hardcoded %m/%d/%Y silently fails on a non-US locale.
#   * /z asks Windows to delete the task after its final run, and
#     cleanup_all_wake_tasks() sweeps any that outlived their run — otherwise
#     stale MeridianWake_* tasks accumulate forever and can wake the machine
#     for a run that no longer exists.
#
# TODO (deliberate debt, recorded 2026-07-25): scripts/schedule_runs.sh still
# carries its own copy. It is working, battle-tested code and was not touched
# late in a long session while a 4-hour job was in flight. Point it at this
# library next time it is opened.
# ============================================================================

IS_WSL=false
if [ -n "${WSL_DISTRO_NAME:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
  IS_WSL=true
fi


# ── task-name namespace ─────────────────────────────────────────────────
# launch.sh (this file) and ralph/schedule_runs.sh can run at the SAME TIME --
# an overnight batch and Ralph loops on one laptop -- and each sweeps "every
# task matching my prefix" on startup and at exit. They previously BOTH used
# the bare `MeridianWake_` prefix, so whichever swept second deleted the
# other's pending wake and that run silently never woke the machine. The
# defect was invisible until 2026-08-04 only because the `/z` flag meant no
# wake task could be created at all (see create_wake_task).
#
# So the prefixes MUST NOT overlap, and the sweep MUST be scoped to this
# script's own prefix. Per-epoch task names are what makes concurrent
# schedulers safe; do not collapse them to one shared reusable task.
WAKE_TASK_PREFIX="JobsearchWake_launch_"


# Encapsulated module for managing Windows wake tasks.
# Both functions take the prefix/namespace as an OPTIONAL argument defaulting
# to WAKE_TASK_PREFIX, so every existing call site is unchanged.
cleanup_all_wake_tasks() {
  if [ "$IS_WSL" = "false" ]; then
    return 0
  fi
  local prefix="${1:-$WAKE_TASK_PREFIX}"
  echo "[Scheduler] Cleaning up any remaining ${prefix}* tasks..."
  local tasks
  # Query Task Scheduler, strip prefix, filter to THIS script's own wake tasks,
  # and remove Windows carriage returns.
  tasks=$("${SCHTASKS_BIN:-/mnt/c/Windows/System32/schtasks.exe}" /query /fo list 2>/dev/null | grep -i "^TaskName:" | sed -E 's/^TaskName:\s+\\?//i' | grep "^${prefix}" | tr -d '\r') || true

  if [ -n "$tasks" ]; then
    echo "$tasks" | while IFS= read -r task; do
      if [ -n "$task" ]; then
        echo "[Scheduler] Deleting task: $task"
        "${SCHTASKS_BIN:-/mnt/c/Windows/System32/schtasks.exe}" /delete /tn "$task" /f >/dev/null 2>&1 || true
      fi
    done
  fi
}

create_wake_task() {
  local epoch="$1"
  local wake_stub_path="$2"
  local prefix="${3:-$WAKE_TASK_PREFIX}"

  if [ "$IS_WSL" = "false" ]; then
    echo "[Scheduler] Running natively (OptiPlex) — skipping Windows wake task creation."
    return 0
  fi

  local wake_epoch=$((epoch - 300))
  local wake_task_name="${prefix}${epoch}"

  # Format localized date and time via PowerShell to guarantee compatibility with Windows locale settings
  local wake_date_win
  local wake_time_win
  local wsl_distro="${WSL_DISTRO_NAME:-}"
  local wsl_run_cmd

  if [ -n "$wsl_distro" ]; then
    wsl_run_cmd="wsl.exe -d $wsl_distro -e $wake_stub_path"
  else
    wsl_run_cmd="wsl.exe -e $wake_stub_path"
  fi

  local now
  now=$(date +%s)
  if [ "$wake_epoch" -le "$now" ]; then
    echo "[Scheduler] Target wake time is in the past. Skipping wake task creation."
    return 0
  fi

  echo "[Scheduler] Resolving localized Windows wake parameters for epoch $epoch..."

  # Get locale-specific short date and 24h short time format from PowerShell
  if ! wake_date_win=$(/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command "\$culture = [System.Globalization.CultureInfo]::CurrentCulture; \$pattern = \$culture.DateTimeFormat.ShortDatePattern -replace '(?<!d)d(?!d)', 'dd' -replace '(?<!M)M(?!M)', 'MM'; ([DateTimeOffset]::FromUnixTimeSeconds($wake_epoch).LocalDateTime).ToString(\$pattern, \$culture)" 2>/dev/null | tr -d '\r\n') || [ -z "$wake_date_win" ]; then
    echo "[Warning] Failed to query localized date using PowerShell. Falling back to local date."
    wake_date_win=$(date -d "@$wake_epoch" "+%m/%d/%Y" 2>/dev/null || date -d "@$wake_epoch" "+%Y-%m-%d")
  fi

  if ! wake_time_win=$(/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command "([DateTimeOffset]::FromUnixTimeSeconds($wake_epoch).LocalDateTime).ToString('HH:mm')" 2>/dev/null | tr -d '\r\n') || [ -z "$wake_time_win" ]; then
    echo "[Warning] Failed to query localized time using PowerShell. Falling back to standard time."
    wake_time_win=$(date -d "@$wake_epoch" "+%H:%M")
  fi

  echo "[Scheduler] Wake task target: $wake_date_win at $wake_time_win (epoch $wake_epoch)"

  # Delete if exists
  if "${SCHTASKS_BIN:-/mnt/c/Windows/System32/schtasks.exe}" /query /tn "$wake_task_name" >/dev/null 2>&1; then
    "${SCHTASKS_BIN:-/mnt/c/Windows/System32/schtasks.exe}" /delete /tn "$wake_task_name" /f >/dev/null 2>&1 || true
  fi

  # Create Windows wake task
  # NO /z HERE, DELIBERATELY -- do not re-add it. `/z` (self-delete after the
  # one run fires) requires the task XML to carry an <EndBoundary>, and
  # `/sc ONCE` on its own does not generate one, so schtasks rejects the whole
  # create with "The task XML is missing a required element or attribute.
  # (46,4):EndBoundary". Measured 2026-08-04 on Windows 10.0.26200: identical
  # command with /z fails, without /z succeeds. Forester's byte-identical copy
  # failed the same way, which is how we know it was never a Meridian-layout
  # problem.
  #
  # `/et` is NOT the fix: it computes the end boundary on the same date as
  # `/sd`, so an overnight slot (start 23:55, end 01:55) yields an EndBoundary
  # BEFORE the StartBoundary and errors out -- and the overnight slot is the
  # main use case. Cleanup is handled explicitly instead: cleanup_all_wake_tasks
  # at startup and on the EXIT trap, plus delete_wake_task per slot.
  echo "[Scheduler] Creating Windows wake task '$wake_task_name'..."
  if ! "${SCHTASKS_BIN:-/mnt/c/Windows/System32/schtasks.exe}" /create \
    /tn "$wake_task_name" \
    /tr "$wsl_run_cmd" \
    /sc once \
    /st "$wake_time_win" \
    /sd "$wake_date_win" \
    /rl highest \
    /f >/dev/null; then
    echo "[Error] Failed to create wake task '$wake_task_name' using schtasks.exe."
    return 1
  fi

  # Enable wake-from-sleep
  echo "[Scheduler] Setting WakeToRun property for '$wake_task_name'..."
  local ps_err
  local ps_status
  # SAVE the caller's errexit state, do not ASSUME it. `set -e` here used to be
  # unconditional, which is not a restore -- it is an introduction: this file is
  # SOURCED, and `launch.sh` is deliberately `set -uo pipefail` with no `-e`.
  # The leak killed launch.sh on the first non-zero `run_batch.sh`, before
  # `STATUS=$?` and the "launch: run_batch.sh exited N" line could run, so the
  # operator was told nothing (findings/launch.md V-3, LEDGER launch V-3).
  #
  # It was latent until 2026-08-04 only because the `/z` defect made
  # create_wake_task return at :141, before ever reaching this block. Fixing
  # that made this reachable. `case $- in *e*)` is POSIX and bash 3.2 safe.
  local ps_errexit
  case $- in
    *e*) ps_errexit=1 ;;
    *) ps_errexit=0 ;;
  esac
  set +e
  ps_err=$(/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe -NoProfile -Command \
    "\$task = Get-ScheduledTask -TaskName '$wake_task_name'; \$task.Settings.WakeToRun = \$true; \$task | Set-ScheduledTask" 2>&1)
  ps_status=$?
  [ "$ps_errexit" = 1 ] && set -e
  if [ $ps_status -ne 0 ]; then
    echo "[Error] Failed to set WakeToRun property for task '$wake_task_name'. Cleaning up task."
    echo "[PowerShell Error Output]:"
    echo "$ps_err"
    "${SCHTASKS_BIN:-/mnt/c/Windows/System32/schtasks.exe}" /delete /tn "$wake_task_name" /f >/dev/null 2>&1 || true
    return 1
  fi

  echo "[Scheduler] Wake task '$wake_task_name' scheduled successfully."
  return 0
}
restore_sleep() {
  if [ "$IS_WSL" = "false" ]; then
    return 0
  fi
  # A manual keep-awake is a lease nothing automated may revoke (see
  # scripts/windows/keep_awake.sh, 2026-08-16).
  local lease="${JOBSEARCH_KEEP_AWAKE_LEASE:-${MERIDIAN_KEEP_AWAKE_LEASE:-$HOME/.meridian/keep_awake.lease}}"
  if [ -f "$lease" ]; then
    echo "[Scheduler] Keep-awake lease present ($lease) -- leaving sleep disabled; release it with Meridian's scripts/windows/resume_sleep.sh."
    return 0
  fi
  # Overridable so the tests can drive both branches with a stub.
  local schtasks_bin="${SCHTASKS_BIN:-/mnt/c/Windows/System32/schtasks.exe}"
  # Keep `|| true` -- this file is SOURCED into launch.sh and a missing task
  # must never abort the caller -- but branch on the status rather than
  # printing success unconditionally (maintenance V-9).
  local status=0
  "$schtasks_bin" /run /tn RestoreSleep >/dev/null 2>&1 || status=$?
  if [ "$status" -eq 0 ]; then
    echo "[Scheduler] Requested Windows to restore its normal sleep timeout."
  else
    echo "[Scheduler] WARNING: could not run the Windows scheduled task 'RestoreSleep' (schtasks.exe exited $status)."
    echo "[Scheduler] Normal sleep behavior may NOT be restored -- the task is probably missing or renamed."
  fi
}
