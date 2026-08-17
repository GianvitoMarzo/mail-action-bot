#!/usr/bin/env bash
#
# Start the Telegram bot in a Terminal window.
#
# Meant to be launched by the macOS app that scripts/make-macos-app.sh builds,
# but it is a perfectly ordinary script -- run it directly if you prefer.
#
# It checks the things that actually go wrong before starting, and keeps the
# window open on failure so the error is readable instead of flashing past.

set -uo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_DIR/.venv/bin/python"

printf '\033]0;BidooBot\007'  # window title

die() {
    printf '\n\033[1;31m✗ %s\033[0m\n\n' "$1"
    printf 'Press return to close this window. '
    read -r _
    exit 1
}

cd "$PROJECT_DIR" || die "Cannot enter $PROJECT_DIR"

if [ ! -x "$PYTHON" ]; then
    die "No virtualenv at $PYTHON.
   Create it with:
     cd $PROJECT_DIR
     python3 -m venv .venv && .venv/bin/pip install -e ."
fi

if [ ! -f "$PROJECT_DIR/.env" ]; then
    die "No .env in $PROJECT_DIR.
   Copy .env.example to .env and fill in TELEGRAM_BOT_TOKEN and
   TELEGRAM_ALLOWED_USER_IDS (see: bidoo-bot telegram-whoami)."
fi

# Telegram allows exactly one long-polling client per bot: a second instance
# would fight the first for updates and both would fail with a 409.
#
# A PID file rather than `pgrep -f bidoo_bot`, which matches on *any* process
# whose command line merely mentions the string -- an editor with this file
# open, a grep, a shell history expansion -- and refuses to start for no
# reason. Here the recorded process either exists or it does not.
LOCKFILE="$PROJECT_DIR/.local/bidoo-bot.pid"
mkdir -p "$(dirname "$LOCKFILE")"
if [ -f "$LOCKFILE" ]; then
    other=$(cat "$LOCKFILE" 2>/dev/null || true)
    if [ -n "$other" ] && kill -0 "$other" 2>/dev/null; then
        die "bidoo-bot is already running (pid $other).
   Telegram only allows one polling client per bot. Stop the other window
   first (Ctrl-C), or just use it instead of this one."
    fi
    rm -f "$LOCKFILE"  # stale: the process is gone
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

printf '\033[1m🎁 bidoo-bot\033[0m\n'
printf '   %s\n' "$PROJECT_DIR"
"$PYTHON" -m bidoo_bot check-config 2>/dev/null | sed -n 's/^  /   /p' | head -6
printf '\n   Press Ctrl-C to stop.\n\n'

"$PYTHON" -m bidoo_bot bot
status=$?

# 130 is Ctrl-C, which is how you are meant to stop it.
if [ "$status" -ne 0 ] && [ "$status" -ne 130 ]; then
    die "bidoo-bot exited with status $status (see the log above)."
fi

printf '\n\033[32m✓ bidoo-bot stopped.\033[0m\n'
