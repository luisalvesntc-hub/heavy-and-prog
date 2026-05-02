#!/bin/bash
# One-time installer for the local weekly fetch.
#
# What it does:
#   1. Marks scripts as executable.
#   2. Templates the launchd plist with this Mac's actual paths.
#   3. Installs + bootstraps the LaunchAgent so it fires Fri/Sat/Sun at
#      12:00 local without any further action.
#   4. Drops a "Fetch Music Releases.command" file on the Desktop for
#      ad-hoc / on-demand fetches.
#
# Re-run safely — bootstrap is idempotent.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
UID_NUM="$(id -u)"

PLIST_TEMPLATE="$SCRIPT_DIR/com.luisalves.heavy-and-prog.fetch.plist"
PLIST_INSTALLED="$HOME/Library/LaunchAgents/com.luisalves.heavy-and-prog.fetch.plist"
DESKTOP_BUTTON="$HOME/Desktop/Fetch Music Releases.command"

echo "▸ Repo: $REPO_DIR"

# 1. Make scripts executable.
chmod +x "$SCRIPT_DIR/local_fetch.sh"
chmod +x "$SCRIPT_DIR/install_local_fetch.sh"

# 2. Template the plist with concrete paths.
mkdir -p "$HOME/Library/LaunchAgents"
sed -e "s|__REPO_DIR__|$REPO_DIR|g" -e "s|__HOME__|$HOME|g" \
  "$PLIST_TEMPLATE" > "$PLIST_INSTALLED"
echo "▸ LaunchAgent: $PLIST_INSTALLED"

# 3. (Re)bootstrap.
launchctl bootout "gui/$UID_NUM/com.luisalves.heavy-and-prog.fetch" 2>/dev/null || true
launchctl bootstrap "gui/$UID_NUM" "$PLIST_INSTALLED"
echo "▸ Scheduled: Fri/Sat/Sun at 12:00 local"

# 4. Desktop button.
cat > "$DESKTOP_BUTTON" <<EOF
#!/bin/bash
"$SCRIPT_DIR/local_fetch.sh"
echo
echo "Done. Press any key to close."
read -n 1
EOF
chmod +x "$DESKTOP_BUTTON"
echo "▸ Desktop button: $DESKTOP_BUTTON"

echo
echo "✓ Installed. Logs land in:"
echo "    $HOME/Library/Logs/heavy-and-prog-fetch.log"
echo "    $HOME/Library/Logs/heavy-and-prog-launchd.log"
echo
echo "Test it now? Run:"
echo "    \"$SCRIPT_DIR/local_fetch.sh\""
