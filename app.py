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
MAX_SOURCE_DURATION = 3600  # guard against exceptionally long videos
MAX_SOURCE_BYTES = 35 * 1024 * 1024
MAX_GIF_BYTES = 5 * 1024 * 1024  # Compact share-friendly animated GIF
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


def convert_x_post(url: str, output_format: str = "gif") -> bytes:
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
            "-f", "b[ext=mp4]/b", "--merge-output-format", "mp4",
            "-o", str(folder / "source.%(ext)s"), url,
        ]
        result = run_command(cmd, timeout=85)
        candidates = [p for p in folder.glob("source.*") if p.suffix.lower() in {".mp4", ".webm", ".mkv", ".mov"}]
        if result.returncode != 0 or not candidates:
            app.logger.error(
                "yt-dlp media fetch failed | exit=%s | files=%s | stdout=%s | stderr=%s",
                result.returncode, [p.name for p in folder.iterdir()],
                result.stdout[-1500:], result.stderr[-1500:]
            )
            raise ConvertError("Could not fetch this post's media. It may be private, unavailable, or X may be blocking downloads.")
        source = candidates[0]
        if source.stat().st_size > MAX_SOURCE_BYTES:
            raise ConvertError("Source video is too large.")
        duration_result = run_command(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(source)], 15)
        try:
            duration = float(duration_result.stdout.strip())
        except ValueError as exc:
            raise ConvertError("Could not read the video duration.") from exc
        if duration <= 0 or duration > MAX_SOURCE_DURATION:
            raise ConvertError("Source video duration is unsupported.")

        if output_format == "mp4":
            # WhatsApp iOS exposes its GIF toggle for clips <= 6 seconds.
            # 5.5 seconds avoids edge cases at the duration threshold.
            short_video = folder / "whatsapp-gif-source.mp4"
            video_result = run_command([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-t", "5.5",
                "-i", str(source), "-an", "-vf", "fps=15,scale=480:-2:flags=lanczos",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(short_video)
            ], timeout=50)
            if video_result.returncode != 0 or not short_video.exists():
                raise ConvertError("Could not prepare the WhatsApp video.")
            if short_video.stat().st_size > MAX_GIF_BYTES:
                raise ConvertError("Video too large to share.")
            return short_video.read_bytes()

        # Smaller, short animated GIFs are less likely to be mishandled by
        # intermediary share targets, but WhatsApp/iOS behavior is not controllable.
        clip_seconds = min(duration, 5.5)
        gif = folder / "output.gif"
        for width, fps, colors in [(360, 10, 96), (280, 8, 64), (240, 7, 48)]:
            if gif.exists():
                gif.unlink()
            scale = f"fps={fps},scale={width}:-1:flags=lanczos"
            filtergraph = (f"[0:v]{scale},split[a][b];"
                           f"[a]palettegen=max_colors={colors}:stats_mode=diff[p];"
                           f"[b][p]paletteuse=dither=bayer:bayer_scale=4")
            gif_result = run_command([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-t", str(clip_seconds), "-i", str(source),
                "-filter_complex", filtergraph, "-loop", "0", str(gif)
            ], timeout=60)
            if gif_result.returncode == 0 and gif.exists() and 0 < gif.stat().st_size <= MAX_GIF_BYTES:
                frame_result = run_command([
                    "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                    "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", str(gif)
                ], timeout=15)
                try:
                    if int(frame_result.stdout.strip()) >= 2:
                        return gif.read_bytes()
                except ValueError:
                    pass
                raise ConvertError("The converted media is not animated. Try a different X post.")
        raise ConvertError("This clip is too large for a compact GIF. Try a shorter post.")



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


@app.post("/api/whatsapp-video")
def whatsapp_video():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(error="Expected a JSON request."), 400
    try:
        url = normalize_post_url(data.get("url", ""))
    except (ValueError, TypeError) as exc:
        return jsonify(error=str(exc)), 400
    if limited(request.remote_addr or "unknown"):
        return jsonify(error="Hourly conversion limit reached. Please try again later."), 429
    if not WORK_SLOTS.acquire(blocking=False):
        return jsonify(error="Converter is busy. Try again in a moment."), 429
    try:
        clip = convert_x_post(url, output_format="mp4")
        return send_file(io.BytesIO(clip), mimetype="video/mp4", as_attachment=True,
                         download_name="whatsapp-gif-source.mp4", max_age=0)
    except ConvertError as exc:
        return jsonify(error=str(exc)), 422
    except Exception:
        app.logger.exception("Unexpected video conversion failure")
        return jsonify(error="Something went wrong while preparing the video."), 500
    finally:
        WORK_SLOTS.release()


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=int(os.getenv("PORT", "8080")), debug=False)
