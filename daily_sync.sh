#!/usr/bin/env bash
# Daily re-upload + re-schedule, run from cron (see `crontab -l`).
#
# Re-uploading replaces every workout, which hands each one a fresh workoutId and
# drops whatever it was scheduled for -- so --schedule has to run in the same
# invocation, and it does: liftwsarah.py schedules after uploading.
#
# Login uses the cached tokens in ~/.garminconnect. There is no TTY under cron, so
# if those tokens ever expire the MFA prompt hits EOF and the run fails; the log
# below is where that shows up. Fix by running the script once by hand.
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG="$HOME/.local/state/garmin/daily_sync.log"
mkdir -p "$(dirname "$LOG")"

# Keep the log from growing without bound: trim to the last 2000 lines each run.
if [ -f "$LOG" ] && [ "$(wc -l < "$LOG")" -gt 2000 ]; then
    tail -n 2000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
fi

{
    echo "=== $(date -Is) ==="
    "$REPO/.venv/bin/python" "$REPO/liftwsarah.py" --schedule < /dev/null
    echo "--- exit $? ---"
} >> "$LOG" 2>&1
