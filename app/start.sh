#!/bin/sh
# Optionally pull the latest yt-dlp on every boot (YouTube changes often)
if [ "${UPDATE_YTDLP_ON_START:-true}" = "true" ]; then
  yt-dlp -U || echo "yt-dlp self-update failed; continuing with bundled version"
fi
exec uvicorn main:app --host 0.0.0.0 --port 8080