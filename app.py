"""One-page X/Twitter video-to-GIF converter. Public posts only."""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, send_file

app = Flask(__name__, static_folder="static", static_url_path="")
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024

MAX_DURATION = 20  # seconds used from each post
MAX_SOURCE_DURATION = 120  # refuse exceptionally long videos
MAX_SOURCE_BYTES = 35 * 1024 * 1024
MAX_GIF_BYTES = 15 * 1024 * 1024
MAX_CONCURRENT = 2
WORK_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENT)
# Best-effort single-instance throttling to reduce free-host CPU abuse.
# For serious production traffic use a shared store / edge rate limiter.
RATE_LOCK = threading.Lock()
RATE_HISTORY = defaultdict(deque)
RATE_WINDOW_SEC = 3600
RATE_MAX_PER_IP = 12


def limited(remote: str) -> bool:
    now = time.monotonic()
    with RATE_LOCK:
        attempts = RATE_HISTORY[remote]
        while attempts and attempts[0] < now - RATE_WINDOW_SEC:
            attempts.popleft()
        if len(attempts) >= RATE_MAX_PER_IP:
            return True
        attempts.append(now)
        if len(RATE_HISTORY) > 2000:
            RATE_HISTORY.clear()
        return False
POST_RE = re.compile(r"^/(?:[^/]+/status|i/status)/(\d{10,25})(?:/|$)", re.IGNORECASE)
ALLOWED_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"}


def normalize_post_url(raw: str) -> str:
    """Accept a public X status link, never arbitrary resource URLs."""
    if not isinstance(raw, str) or len(raw) > 2048:
        raise ValueError("Paste a valid X post link.")
    parts = urlsplit(raw.strip())
    if parts.scheme != "https" or (parts.hostname or "").lower() not in ALLOWED_HOSTS:
        raise ValueError("Only https://x.com or twitter.com post links are supported.")
    if parts.port not in (None, 443) or parts.username or parts.password:
        raise ValueError("Invalid X post link.")
    match = POST_RE.match(parts.path)
    if not match:
        raise ValueError("Link must point to an X post, for example x.com/user/status/123...")
    return f"https://x.com/i/status/{match.group(1)}"


class ConvertError(Exception):
    pass


def run_command(args: list[str], timeout: int = 70) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise ConvertError("X took too long to respond. Please try again.") from exc
    except FileNotFoundError as exc:
        raise ConvertError("Server converter is not installed correctly.") from exc


def convert_x_post(url: str) -> bytes:
    if not shutil.which("yt-dlp") or not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ConvertError("Server needs yt-dlp and FFmpeg installed.")

    with tempfile.TemporaryDirectory(prefix="xgif-") as tmp:
        folder = Path(tmp)
        source = folder / "source.mp4"
        # yt-dlp is used without a shell; URL is fully normalized above.
        cmd = [
            "yt-dlp", "--no-playlist", "--no-warnings", "--no-progress",
            "--socket-timeout", "12", "--no-config", "--retries", "1", "--fragment-retries", "1",
            "--max-filesize", str(MAX_SOURCE_BYTES),
            "--match-filter", f"duration <= {MAX_SOURCE_DURATION}",
            "-f", "b[ext=mp4]/b", "--merge-output-format", "mp4",
            "-o", str(folder / "source.%(ext)s"), url,
        ]
        result = run_command(cmd, timeout=85)
        candidates = [p for p in folder.glob("source.*") if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}]
        if result.returncode != 0 or not candidates:
            app.logger.error(
                "yt-dlp failed | URL: %s | exit_code: %s | stderr: %s",
                url,
                result.returncode,
                result.stderr[-3000:]
            )
            raise ConvertError(
                "Could not fetch this post's media. It may be private, unavailable, or X may be blocking downloads."
            )
        source = candidates[0]
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise ConvertError("Source video is too large.")
        duration_result = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)], 15)
        try:
            duration = float(duration_result.stdout.strip())
        except ValueError as exc:
            raise ConvertError("Could not read the video duration.") from exc
        if duration <= 0 or duration > MAX_SOURCE_DURATION:
            raise ConvertError("Only short clips (up to 2 minutes) are supported.")

        clip_seconds = min(duration, MAX_DURATION)
        palette = folder / "palette.png"
        gif = folder / "output.gif"
        filters = "fps=10,scale=360:-1:flags=lanczos"
        palette_result = run_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-t", str(clip_seconds), "-i", str(source),
            "-vf", f"{filters},palettegen=max_colors=96:stats_mode=diff", str(palette)
        ], timeout=45)
        if palette_result.returncode != 0:
            raise ConvertError("Could not prepare GIF palette.")
        gif_result = run_command([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-t", str(clip_seconds), "-i", str(source),
            "-i", str(palette), "-lavfi", f"{filters}[x];[x][1:v]paletteuse=dither=bayer:bayer_scale=4", "-loop", "0", str(gif)
        ], timeout=60)
        if gif_result.returncode != 0 or not gif.exists():
            raise ConvertError("Could not convert this clip to GIF.")
        if gif.stat().st_size > MAX_GIF_BYTES:
            raise ConvertError("GIF is too large to share. Try a shorter post.")
        return gif.read_bytes()


@app.get("/")
def index():
    return app.send_static_file("index.html")


@app.get("/outreach")
def outreach():
    return app.send_static_file("outreach.html")


@app.get("/health")
def health():
    return jsonify(status="ok")


@app.post("/api/convert")
def convert():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Expected a JSON request."), 400
    try:
        url = normalize_post_url(data.get("url", ""))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    # Do not trust an arbitrary forwarded-for header from the client.
    if limited(request.remote_addr or "unknown"):
        return jsonify(error="Hourly conversion limit reached. Please try again later."), 429
    if not WORK_SLOTS.acquire(blocking=False):
        return jsonify(error="Converter is busy. Try again in a moment."), 429
    try:
        gif = convert_x_post(url)
        return send_file(io.BytesIO(gif), mimetype="image/gif", as_attachment=True,
                         download_name="x-to-whatsapp.gif", max_age=0)
    except ConvertError as exc:
        return jsonify(error=str(exc)), 422
    except Exception:
        app.logger.exception("Unexpected conversion failure")
        return jsonify(error="Something went wrong while converting this post."), 500
    finally:
        WORK_SLOTS.release()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8080")), debug=False)
