# ClipDock

Self-hosted, single-container web app: paste a YouTube URL (or anything yt-dlp
supports), optionally set IN/OUT timecodes, and get an `.mp4` video or an
`.m4a` / `.mp3` audio file you can download from any browser.

- **Full video or section** — timecodes like `00:00` → `00:10` download only
  that range (`--download-sections` with `--force-keyframes-at-cuts`, so cuts
  are accurate rather than snapped to the nearest existing keyframe).
- **MP4 video or audio only** — video streams are selected to prefer mp4/m4a
  and merged with `--merge-output-format mp4`. Audio mode saves M4A or MP3
  instead (see below).
- **Library** — finished clips persist in a Docker volume and are listed in
  the UI for download or delete from any machine.
- **Optional shared password** — one `APP_PASSWORD` gates the whole API.

## Run it

```bash
docker compose up -d --build
# open http://localhost:8080
```

Edit `docker-compose.yml` first — at minimum change `APP_PASSWORD`.

| Env var | Default | Purpose |
|---|---|---|
| `APP_PASSWORD` | *(empty)* | If set, UI/API require this key. **Set it if the app is reachable from the internet** — an open yt-dlp endpoint will get found and abused. |
| `RETENTION_HOURS` | `0` | Auto-delete clips older than N hours (`0` = never). |
| `MAX_CONCURRENT_JOBS` | `2` | Parallel download limit. |
| `UPDATE_YTDLP_ON_START` | `true` | Runs `yt-dlp -U` at boot. YouTube changes frequently; stale yt-dlp is the #1 cause of failures. |
| `YTDLP_EXTRA_ARGS` | *(empty)* | Extra yt-dlp flags, e.g. `--extractor-args youtube:player_client=web_safari`. |
| `COOKIES_FILE` | *(empty)* | Path to a Netscape `cookies.txt` for age-gated or bot-checked videos. |

## Audio output

Set **Output** to **Audio** to save just the soundtrack. The Quality control
is replaced by a **Format** control:

| Format | What happens | Notes |
|---|---|---|
| **M4A** (default) | yt-dlp picks YouTube's AAC stream and extracts it with a stream copy | No re-encode, so no quality loss and it's fast. |
| **MP3** | The best audio stream is transcoded with `--audio-quality 0` (best VBR) | For players that don't handle AAC. |

Audio composes with **Range**, so you can save a full track or just a
section. Audio sections use the same fallback as video (below); the local
cut uses `-vn` with `aac -b:a 192k` for M4A or `libmp3lame -q:a 2` for MP3.

Via the API, send `"mode": "audio"` and `"audio_format": "m4a"` or `"mp3"`
to `POST /api/jobs`. Both default to video / m4a.

## How clipping works (and what happens when it fails)

Clipping tries the fast path first: `--download-sections`, where ffmpeg
fetches only the byte ranges it needs. YouTube frequently refuses those range
requests, and ffmpeg then dies with **exit code 8**.

When that happens ClipDock automatically retries: it downloads the full video
with the native downloader, then cuts your range locally with ffmpeg
(`-c:v libx264 -crf 20`, frame-accurate edges). The UI shows the stage change
from `downloading` to `downloading full video` to `cutting`. In audio mode
the stage reads `downloading full audio`, and the cut encodes straight to
M4A or MP3, so the audio is only encoded once. It's slower on
long source videos, but it works when the fast path doesn't — and the finished
clip is identical.

## Exposing it on your domain

Terminate TLS at your reverse proxy and forward to port 8080. Example
(Caddy — two lines, automatic HTTPS):

```
clips.example.com {
    reverse_proxy clipdock:8080
}
```

nginx equivalent:

```nginx
server {
    server_name clips.example.com;
    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_read_timeout 600s;   # long downloads
        client_max_body_size 0;
    }
    # ...your usual TLS config
}
```

Suggestions for anything public-facing:

1. Keep `APP_PASSWORD` set (the UI asks for it once per page load), or put
   proxy-level basic auth / your SSO (Authelia, Cloudflare Access, etc.) in
   front.
2. Set `RETENTION_HOURS` so the volume doesn't grow forever.

## Maintenance

- **Downloads suddenly failing?** Restart the container (it self-updates
  yt-dlp on boot), or run `docker compose exec clipdock yt-dlp -U`.
- Clips live in the `clipdock-data` volume at `/data/clips`.

## Layout

```
clipdock/
├── Dockerfile
├── docker-compose.yml
└── app/
    ├── main.py            # FastAPI backend (probe, jobs, files, auth)
    ├── start.sh
    └── static/index.html  # the whole frontend
```

## Notes

- Timecodes accept `SS`, `MM:SS`, or `HH:MM:SS`.
- Section downloads re-encode a few frames around each cut point; everything
  else is stream-copied, so clipping is fast and quality is preserved.
- Works with any site yt-dlp supports, not just YouTube.
- Only download content you have the rights to save.
