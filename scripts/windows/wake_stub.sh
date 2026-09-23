#!/bin/bash
# ============================================================================
# jobsearch - Wake Stub (copied from Meridian's scripts/windows/wake_stub.sh)
# Executed by the Windows scheduled wake task so WSL is up before launch.sh's
# start time. It does nothing else: launch.sh itself is already waiting.
# ============================================================================

echo "[Wake Stub] Windows woke the machine at $(date)"
