#!/usr/bin/env bash
# Aggregator sweep (Adzuna, Jooble, USAJobs) with the rule-based pre-screen.
# Output: output/sweep_<stamp>.md (triage), .csv (every row with reasons), raw_<stamp>.json (re-screenable).
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] && set -a && . ./.env && set +a
# Dedup reads JOBSEARCH_VAULT_DIR's tracker; refresh that repo first if it is a git checkout.
if [ -n "${JOBSEARCH_VAULT_DIR:-}" ]; then
  top="$(git -C "$JOBSEARCH_VAULT_DIR" rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$top" ] && { git -C "$top" pull -q --rebase || echo "vault pull skipped (local changes)"; }
fi
.venv/bin/python sweep.py --days "${1:-7}"
