#!/usr/bin/env bash
# Hot-reload wrapper for sony-remote-gui.py
# Watches the script for changes and restarts automatically.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP="$SCRIPT_DIR/sony-remote-gui.py"
PID=""

cleanup() {
    [ -n "$PID" ] && kill "$PID" 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

start_app() {
    # Remember which window has focus right now
    PREV_WINDOW=$(xdotool getactivewindow 2>/dev/null || true)

    [ -n "$PID" ] && kill "$PID" 2>/dev/null && wait "$PID" 2>/dev/null
    echo "[dev] Starting sony-remote-gui.py ..."
    python3 "$APP" &
    PID=$!

    # Give the app a moment to open, then restore focus
    if [ -n "$PREV_WINDOW" ]; then
        (sleep 1.5 && xdotool windowactivate "$PREV_WINDOW" 2>/dev/null) &
    fi
}

get_mtime() {
    stat -c %Y "$1" 2>/dev/null
}

LAST_MTIME="$(get_mtime "$APP")"
start_app

while true; do
    sleep 1
    CURRENT_MTIME="$(get_mtime "$APP")"
    if [ "$CURRENT_MTIME" != "$LAST_MTIME" ]; then
        echo "[dev] Change detected, restarting..."
        LAST_MTIME="$CURRENT_MTIME"
        start_app
    fi
done
