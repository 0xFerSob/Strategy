#!/bin/bash
# Daily updater for Strategy dashboard. Run from cron or launchd.
# Executes update_data.py, then commits + pushes data/snapshots.json if it changed.
#
# To install as a scheduled job on macOS, see .claude/com.strategy.dashboard.updater.plist

set -e

cd "$(dirname "$0")"

echo "=== $(date -u +'%Y-%m-%d %H:%M:%SZ') — Strategy dashboard updater ==="

# Run the Python script — updates data/snapshots.json in place
python3 update_data.py

# Commit + push only if snapshots.json changed
if ! git diff --quiet data/snapshots.json; then
  git add data/snapshots.json
  git commit -m "data: daily EDGAR update $(date -u +'%Y-%m-%d')"
  git push origin HEAD:main
  echo "✓ Pushed updated snapshots to GitHub"
else
  echo "— No changes to snapshots; skipping commit"
fi
