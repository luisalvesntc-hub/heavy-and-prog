#!/bin/bash
# Weekly fetch driver that runs on this Mac instead of GitHub Actions.
# Pulls latest main, fetches this week's + next week's release data,
# commits anything that changed, and pushes back to origin/main.
#
# Why this exists: MA (metal-archives.com) sits behind Cloudflare which
# blanket-blocks GitHub Actions IP ranges. A residential IP (this Mac)
# isn't filtered, so MA data flows through normally — no scraping API,
# no quotas, no setup beyond the one-time installer.

set -euo pipefail

# Resolve the repo dir from this script's location so installs are
# portable (no hard-coded /Users/... paths).
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

LOG_DIR="$HOME/Library/Logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/heavy-and-prog-fetch.log"

# Append everything below to the log file (and stdout/stderr if run
# interactively from a Terminal).
exec > >(tee -a "$LOG_FILE") 2>&1

echo
echo "===== $(date '+%Y-%m-%d %H:%M:%S %Z') ====="
echo "repo: $REPO_DIR"

cd "$REPO_DIR"

# Make sure we're on main and up to date with origin before generating
# new commits — otherwise a push race produces a non-fast-forward.
git fetch origin main
git checkout main >/dev/null 2>&1
git pull --rebase origin main

# Fetch this week's releases.
python3 scripts/fetch_releases.py

# And the upcoming-week preview so the "next week" tab populates.
NEXT="$(python3 -c "
from datetime import date, timedelta
t = date.today()
wd = t.weekday()
# Match scripts/fetch_releases.py:previous_friday — snap to this calendar
# week's Friday (Mon-Fri), or to next week's Friday (Sat-Sun); then +7 for
# the *upcoming* chart week's preview.
this_friday = t + timedelta(days=(4 - wd if wd <= 4 else 11 - wd))
print((this_friday + timedelta(days=7)).isoformat())
")"
python3 scripts/fetch_releases.py --week-of "$NEXT"

# Commit only if data/ actually changed.
git add -A data/
if git diff --cached --quiet -- data/; then
  echo "no changes"
  exit 0
fi

git -c user.name="release-bot (local)" -c user.email="release-bot-local@users.noreply.github.com" \
  commit -m "data: refresh releases ($(date '+%Y-%m-%d'))"
git push origin main
echo "pushed."
