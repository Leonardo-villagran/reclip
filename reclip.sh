#!/bin/bash
set -e
cd "$(dirname "$0")"

# Check prerequisites
missing=""

if ! command -v python3 &> /dev/null; then
    missing="$missing python3"
fi

if ! command -v yt-dlp &> /dev/null; then
    missing="$missing yt-dlp"
fi

if ! command -v ffmpeg &> /dev/null; then
    missing="$missing ffmpeg"
fi

if [ -n "$missing" ]; then
    echo "Missing required tools:$missing"
    echo ""
    if command -v brew &> /dev/null; then
        echo "Install with:  brew install$missing"
    elif command -v apt &> /dev/null; then
        echo "Install with:  sudo apt install$missing"
    else
        echo "Please install:$missing"
    fi
    exit 1
fi

# Set up venv and install Python deps
if [ ! -d "venv" ]; then
    echo "Setting up virtual environment..."
    python3 -m venv venv
    source venv/bin/activate
    pip install -q flask yt-dlp
else
    source venv/bin/activate
fi

PORT="${PORT:-8899}"
HOST="${HOST:-127.0.0.1}"
export PORT
export HOST

OPEN_HOST="$HOST"
if [ "$OPEN_HOST" = "0.0.0.0" ] || [ "$OPEN_HOST" = "127.0.0.1" ]; then
    OPEN_HOST="localhost"
fi

APP_URL="http://$OPEN_HOST:$PORT"

COMET_CMD=""
if command -v comet >/dev/null 2>&1; then
    COMET_CMD="comet"
else
    maybe_comet="$(compgen -c | grep -E '^comet' | head -n1 || true)"
    if [ -n "$maybe_comet" ] && command -v "$maybe_comet" >/dev/null 2>&1; then
        COMET_CMD="$maybe_comet"
    fi
fi

echo ""
echo "  ReClip is running at $APP_URL"
echo ""

if [ -n "$COMET_CMD" ]; then
    "$COMET_CMD" "$APP_URL" >/dev/null 2>&1 &
elif command -v xdg-open >/dev/null 2>&1; then
    xdg-open "$APP_URL" >/dev/null 2>&1 &
elif command -v open >/dev/null 2>&1; then
    open "$APP_URL" >/dev/null 2>&1 &
fi

python3 app.py
