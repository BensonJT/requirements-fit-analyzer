#!/usr/bin/env bash
# Inbox Watch, hourly from cron: job email in Outlook -> tracker + alerts (backend/inbox_watch.py).
#   0 * * * * $HOME/jobsearch/scripts/inbox_watch.sh
# Trial mode unless INBOX_WATCH_LIVE=1 in .env. A missed hour (laptop asleep, WSL down) is fine: each run
# reads everything since the last successful one. Log: logs/inbox_watch.log.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1
[ -f .env ] && set -a && . ./.env && set +a
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
mkdir -p logs
{ echo "== $(date '+%F %T')"; .venv/bin/python -m backend.inbox_watch run; } >> logs/inbox_watch.log 2>&1
