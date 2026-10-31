#!/usr/bin/env bash
# Start the app at http://127.0.0.1:8000
cd "$(dirname "$0")"
[ -f .env ] || { echo "Missing .env. Run: cp .env.example .env  and add your keys."; exit 1; }
command -v ffmpeg >/dev/null || { echo "ffmpeg is required (install it and try again)."; exit 1; }
exec python3 -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT:-8000}"
