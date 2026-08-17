#!/usr/bin/env bash
#
# Build a small macOS app that starts the bot in a Terminal window, so it can
# be found in Spotlight and kept in the Dock.
#
#   ./scripts/make-macos-app.sh [APP_NAME] [DESTINATION]
#
# Defaults to "BidooBot" in ~/Applications (no admin rights needed; Spotlight
# indexes it just the same as /Applications).
#
# Built with osacompile, which ships with macOS. Automator would produce an
# equivalent app, but its editor is not installed on every Mac -- and the
# actual work is one line either way, because an Automator "Run Shell Script"
# action runs headless and would still have to ask Terminal to show itself.

set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_NAME="${1:-BidooBot}"
DEST_DIR="${2:-$HOME/Applications}"
APP_PATH="$DEST_DIR/$APP_NAME.app"
LAUNCHER="$PROJECT_DIR/scripts/run-bot.sh"

[ -f "$LAUNCHER" ] || { echo "✗ Missing $LAUNCHER" >&2; exit 1; }
chmod +x "$LAUNCHER"
mkdir -p "$DEST_DIR"
rm -rf "$APP_PATH"

# The app is deliberately tiny: everything that can change lives in the shell
# script, so editing the launcher never means rebuilding the app.
#
# `open -a Terminal <script>` rather than `tell application "Terminal" to do
# script`: the AppleScript form is an Apple Event, which macOS gates behind the
# Automation permission. The app would then do nothing at all until that prompt
# is accepted -- and if it is ever declined, silently nothing forever. Handing
# the script to Launch Services needs no permission and behaves the same.
osacompile -o "$APP_PATH" <<APPLESCRIPT
on run
    do shell script "/usr/bin/open -a Terminal " & quoted form of "$LAUNCHER"
end run
APPLESCRIPT

PLIST="$APP_PATH/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName $APP_NAME" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleIdentifier string com.github.bidoo-bot.launcher" \
    "$PLIST" 2>/dev/null \
    || /usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.github.bidoo-bot.launcher" "$PLIST"
# Nothing here needs to appear in the Dock while it runs; it just opens Terminal.
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || true

# Nudge Spotlight so the app is findable immediately rather than eventually.
touch "$APP_PATH"
mdimport "$APP_PATH" 2>/dev/null || true

echo "✓ Built $APP_PATH"
echo
echo "  Launches: $LAUNCHER"
echo "  Find it:  Spotlight (⌘-Space) -> \"$APP_NAME\""
echo "  Dock:     open $DEST_DIR, then drag $APP_NAME there"
echo
echo "  No Automation permission prompt: the app hands the script to Launch"
echo "  Services instead of sending Terminal an Apple Event."
