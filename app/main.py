"""ClipDock — self-hosted YouTube clip downloader.

FastAPI backend that wraps yt-dlp + ffmpeg:
  - POST /api/probe      -> fetch title / duration / thumbnail for a URL
  - POST /api/jobs       -> start a download (optionally a timecode section)
  - GET  /api/jobs/{id}  -> poll job status + progress
  - GET  /api/files      -> list finished clips
  - GET  /api/files/{n}  -> download a clip
  - DELETE /api/files/{n}
Optional auth: set APP_PASSWORD and every /api call must send
"X-App-Password: <password>".
"""

import asyncio
import os
import re
import json
import time
import uuid
import shutil
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------- config

CLIPS_DIR = Path(os.environ.get("CLIPS_DIR", "/data/clips"))
CLIPS_DIR.mkdir(parents=True, exist_ok=True)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
RETENTION_HOURS = float(os.environ.get("RETENTION_HOURS", "0"))  # 0 = keep forever
MAX_CONCURRENT_JOBS = int(os.environ.get("MAX_CONCURRENT_JOBS", "2"))

YTDLP = shutil.which("yt-dlp") or "yt-dlp"
FFMPEG = shutil.which("ffmpeg") or "ffmpeg"

# Extra yt-dlp flags, space-separated
# (e.g. --extractor-args "youtube:player_client=web")
YTDLP_EXTRA_ARGS = os.environ.get("YTDLP_EXTRA_ARGS", "").split()
# Path to a Netscape-format cookies.txt, for age-gated / bot-checked videos
COOKIES_FILE = os.environ.get("COOKIES_FILE", "").strip()

app = FastAPI(title="ClipDock")

# ---------------------------------------------------------------- auth


@app.middleware("http")
async def auth(request: Request, call_next):
    """Simple shared-password gate for all /api routes (except /api/config).

    Send it as an "X-App-Password" header, or as "?key=" on direct
    file-download links (so browser downloads still work)."""
    if APP_PASSWORD and request.url.path.startswith("/api/") \
            and request.url.path != "/api/config":
        supplied = request.headers.get("x-app-password", "") \
            or request.query_params.get("key", "")
        if supplied != APP_PASSWORD:
            from fastapi.responses import JSONResponse
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    return await call_next(request)


# ---------------------------------------------------------------- helpers

TIMECODE_RE = re.compile(r"^(?:(\d{1,3}):)?(?:(\d{1,2}):)?(\d{1,2})(?:\.(\d{1,3}))?$")


def parse_timecode(tc: str) -> float:
    """Accept SS, MM:SS or HH:MM:SS (optionally .ms) -> seconds."""
    tc = tc.strip()
    m = TIMECODE_RE.match(tc)
    if not m:
        raise ValueError(f"Invalid timecode: {tc!r}")
    a, b, c, ms = m.groups()
    parts = [p for p in (a, b, c) if p is not None]
    secs = 0.0
    for p in parts:
        secs = secs * 60 + int(p)
    if ms:
        secs += int(ms) / (10 ** len(ms))
    return secs


def fmt_timecode(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600:02d}:{(s % 3600) // 60:02d}:{s % 60:02d}"


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(". ")
    return name[:120] or "clip"


def safe_clip_path(filename: str) -> Path:
    p = (CLIPS_DIR / filename).resolve()
    if p.parent != CLIPS_DIR.resolve():
        raise HTTPException(400, "Invalid filename")
    return p


# ---------------------------------------------------------------- models

class ProbeRequest(BaseModel):
    url: str


class JobRequest(BaseModel):
    url: str
    start: Optional[str] = None   # "MM:SS" / "HH:MM:SS" / seconds
    end: Optional[str] = None
    quality: str = "best"         # best | 1080 | 720 | 480


# ---------------------------------------------------------------- jobs

JOBS: dict[str, dict] = {}
JOB_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_JOBS)

PROGRESS_RE = re.compile(r"\[download\]\s+(\d+(?:\.\d+)?)%")


def build_cmd(out_tmpl: str, req: JobRequest, section: Optional[str]) -> list[str]:
    cmd = [
        YTDLP,
        "--no-playlist",
        "--newline",
        "--no-warnings",
        "--merge-output-format", "mp4",
        "-o", out_tmpl,
        "--print", "pre_process:%(title)s",
        "--no-simulate",
    ]
    if COOKIES_FILE:
        cmd += ["--cookies", COOKIES_FILE]

    # Prefer mp4/m4a streams so the merged file is a clean .mp4
    if req.quality in ("1080", "720", "480"):
        h = req.quality
        cmd += ["-f", f"bv*[height<={h}]+ba/b[height<={h}]/b",
                "-S", f"res:{h},ext:mp4:m4a"]
    else:
        cmd += ["-f", "bv*+ba/b", "-S", "ext:mp4:m4a"]

    if section:
        # Fast path: ffmpeg fetches only the needed byte ranges. This is
        # quick but depends on the CDN honouring range requests, which
        # YouTube often refuses (ffmpeg then exits with code 8). run_job
        # falls back to download-then-trim when that happens.
        cmd += ["--download-sections", f"*{section}", "--force-keyframes-at-cuts"]

    cmd += YTDLP_EXTRA_ARGS
    cmd.append(req.url)
    return cmd


async def stream_ytdlp(cmd: list[str], job: dict) -> tuple[int, list[str]]:
    """Run yt-dlp, feeding progress into the job dict. Returns (code, tail)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    tail: list[str] = []
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").rstrip()
        if not line:
            continue
        tail.append(line)
        tail[:] = tail[-20:]
        if not job.get("title") and not line.startswith("["):
            job["title"] = line.strip()
        m = PROGRESS_RE.search(line)
        if m:
            job["progress"] = float(m.group(1))
        if "[Merger]" in line:
            job["status"] = "merging"
            job["progress"] = 100.0
    return await proc.wait(), tail


FFMPEG_TIME_RE = re.compile(r"out_time_ms=(\d+)")


async def ffmpeg_trim(src: Path, dest: Path, start: float, end: float,
                      job: dict) -> tuple[int, list[str]]:
    """Cut [start, end] out of src. Re-encodes for frame-accurate edges."""
    duration = end - start
    cmd = [
        FFMPEG, "-hide_banner", "-nostdin", "-y",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{duration:.3f}",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats", "-loglevel", "error",
        str(dest),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    tail: list[str] = []
    assert proc.stdout is not None
    async for raw in proc.stdout:
        line = raw.decode(errors="replace").strip()
        if not line:
            continue
        m = FFMPEG_TIME_RE.search(line)
        if m and duration > 0:
            done = int(m.group(1)) / 1_000_000
            job["progress"] = max(0.0, min(100.0, done / duration * 100))
        elif not line.startswith(("frame=", "fps=", "bitrate=", "total_size=",
                                  "out_time", "dup_frames", "drop_frames",
                                  "speed=", "progress=", "stream_")):
            tail.append(line)
            tail[:] = tail[-10:]
    return await proc.wait(), tail


async def run_job(job_id: str, req: JobRequest, section: Optional[str]):
    job = JOBS[job_id]
    scratch = CLIPS_DIR / f"{job_id}.%(ext)s"

    def produced() -> Optional[Path]:
        found = sorted(CLIPS_DIR.glob(f"{job_id}.*"))
        return found[0] if found else None

    async with JOB_SEMAPHORE:
        try:
            job["status"] = "downloading"
            code, tail = await stream_ytdlp(build_cmd(str(scratch), req, section), job)

            # ---- fallback -------------------------------------------------
            # The ffmpeg range-fetch downloader used by --download-sections
            # fails often (exit code 8 = input I/O error). Retry by pulling
            # the whole video with the native downloader, then cutting locally.
            if code != 0 and section:
                job["fallback"] = True
                job["status"] = "downloading full video"
                job["progress"] = 0.0
                for stale in CLIPS_DIR.glob(f"{job_id}.*"):
                    stale.unlink(missing_ok=True)
                code, tail = await stream_ytdlp(
                    build_cmd(str(scratch), req, None), job)

            if code != 0:
                job["status"] = "error"
                job["error"] = "\n".join(tail[-6:]) or f"yt-dlp exited with {code}"
                return

            src = produced()
            if not src:
                job["status"] = "error"
                job["error"] = "Download finished but no output file was found."
                return

            # ---- local trim, if the fallback path was taken ---------------
            if section and job.get("fallback"):
                job["status"] = "cutting"
                job["progress"] = 0.0
                cut = CLIPS_DIR / f"{job_id}-cut.mp4"
                code, tail = await ffmpeg_trim(
                    src, cut, job["start_s"], job["end_s"], job)
                if code != 0 or not cut.exists():
                    job["status"] = "error"
                    job["error"] = "\n".join(tail[-6:]) or \
                        f"ffmpeg exited with {code} while cutting the clip."
                    cut.unlink(missing_ok=True)
                    return
                src.unlink(missing_ok=True)
                src = cut

            # ---- name it ---------------------------------------------------
            title = sanitize_filename(job.get("title") or "clip")
            suffix = f" [{fmt_timecode(job['start_s'])}-{fmt_timecode(job['end_s'])}]" \
                if section else ""
            dest = CLIPS_DIR / f"{title}{suffix}{src.suffix}"
            n = 2
            while dest.exists():
                dest = CLIPS_DIR / f"{title}{suffix} ({n}){src.suffix}"
                n += 1
            src.rename(dest)
            job["status"] = "done"
            job["progress"] = 100.0
            job["filename"] = dest.name
            job["size"] = dest.stat().st_size
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)
        finally:
            for stale in CLIPS_DIR.glob(f"{job_id}.*"):
                stale.unlink(missing_ok=True)
            for stale in CLIPS_DIR.glob(f"{job_id}-cut.*"):
                stale.unlink(missing_ok=True)


# ---------------------------------------------------------------- routes

@app.get("/api/config")
async def config():
    return {"auth_required": bool(APP_PASSWORD)}


@app.post("/api/probe")
async def probe(req: ProbeRequest):
    if not re.match(r"^https?://", req.url.strip()):
        raise HTTPException(400, "Enter a valid http(s) URL.")
    cmd = [YTDLP, "--no-playlist", "--no-warnings", "-J", req.url.strip()]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=45)
    except asyncio.TimeoutError:
        proc.kill()
        raise HTTPException(504, "Timed out fetching video info.")
    if proc.returncode != 0:
        msg = err.decode(errors="replace").strip().splitlines()
        raise HTTPException(422, msg[-1] if msg else "Could not read that URL.")
    info = json.loads(out)
    return {
        "title": info.get("title"),
        "duration": info.get("duration"),
        "uploader": info.get("uploader") or info.get("channel"),
        "thumbnail": info.get("thumbnail"),
    }


@app.post("/api/jobs")
async def create_job(req: JobRequest):
    url = req.url.strip()
    if not re.match(r"^https?://", url):
        raise HTTPException(400, "Enter a valid http(s) URL.")
    req.url = url

    section = None
    start_s = end_s = 0.0
    if req.start or req.end:
        if not (req.start and req.end):
            raise HTTPException(400, "Provide both a start and an end timecode.")
        try:
            start_s = parse_timecode(req.start)
            end_s = parse_timecode(req.end)
        except ValueError as e:
            raise HTTPException(400, str(e))
        if end_s <= start_s:
            raise HTTPException(400, "End timecode must be after the start.")
        section = f"{fmt_timecode(start_s)}-{fmt_timecode(end_s)}"

    job_id = uuid.uuid4().hex[:12]
    JOBS[job_id] = {
        "id": job_id, "status": "queued", "progress": 0.0,
        "title": None, "filename": None, "error": None,
        "section": section, "start_s": start_s, "end_s": end_s,
        "created": time.time(),
    }
    asyncio.create_task(run_job(job_id, req, section))
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
async def job_status(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Unknown job.")
    return {k: job[k] for k in
            ("id", "status", "progress", "title", "filename", "error", "section")}


@app.get("/api/files")
async def list_files():
    files = []
    for p in sorted(CLIPS_DIR.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.is_file() and not re.match(r"^[0-9a-f]{12}\.", p.name):
            files.append({"name": p.name, "size": p.stat().st_size,
                          "mtime": p.stat().st_mtime})
    return {"files": files}


@app.get("/api/files/{filename}")
async def get_file(filename: str):
    p = safe_clip_path(filename)
    if not p.is_file():
        raise HTTPException(404, "File not found.")
    return FileResponse(p, filename=p.name, media_type="video/mp4")


@app.delete("/api/files/{filename}")
async def delete_file(filename: str):
    p = safe_clip_path(filename)
    if not p.is_file():
        raise HTTPException(404, "File not found.")
    p.unlink()
    return {"ok": True}


# ---------------------------------------------------------------- cleanup

async def retention_loop():
    while True:
        await asyncio.sleep(3600)
        if RETENTION_HOURS <= 0:
            continue
        cutoff = time.time() - RETENTION_HOURS * 3600
        for p in CLIPS_DIR.iterdir():
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
            except OSError:
                pass


@app.on_event("startup")
async def startup():
    asyncio.create_task(retention_loop())
    # sweep any half-finished temp files from previous runs
    for p in CLIPS_DIR.glob("*.part"):
        p.unlink(missing_ok=True)


# static frontend (mounted last so /api wins)
app.mount("/", StaticFiles(directory=Path(__file__).parent / "static",
                           html=True), name="static")