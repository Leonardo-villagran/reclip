import os
import uuid
import glob
import json
import subprocess
import threading
import shutil
import re
import time
import atexit
import mimetypes
import hashlib
import logging
from urllib.parse import urlparse, parse_qs, unquote
from flask import Flask, request, jsonify, send_file, render_template
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
BASE_DIR = os.path.dirname(__file__)
DEFAULT_DOWNLOAD_DIR = os.path.join(BASE_DIR, "downloads")
DOWNLOAD_DIR = os.path.abspath(os.environ.get("DOWNLOAD_DIR") or DEFAULT_DOWNLOAD_DIR)
try:
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
except OSError:
    DOWNLOAD_DIR = DEFAULT_DOWNLOAD_DIR
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
PREVIEW_DIR = os.path.join(DOWNLOAD_DIR, ".preview")
os.makedirs(PREVIEW_DIR, exist_ok=True)
PREVIEW_TTL_HOURS = float(os.environ.get("PREVIEW_TTL_HOURS", "24"))
PREVIEW_VERSION = "v2"
AUTO_RESUME_RETRIES = max(0, int(os.environ.get("AUTO_RESUME_RETRIES", "3")))
AUTO_RESUME_DELAY_SECONDS = max(1, int(os.environ.get("AUTO_RESUME_DELAY_SECONDS", "5")))
VIDEO_ENCODER = (os.environ.get("VIDEO_ENCODER", "auto") or "auto").strip().lower()

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger("reclip")

jobs = {}
AVAILABLE_FFMPEG_ENCODERS = None


def is_magnet_url(url):
    return url.lower().startswith("magnet:")


def is_torrent_source(url):
    if is_magnet_url(url):
        return True
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.path.lower().endswith(".torrent")


def guess_torrent_title(url):
    if is_magnet_url(url):
        parsed = urlparse(url)
        q = parse_qs(parsed.query)
        dn = q.get("dn", [""])[0]
        return unquote(dn).strip() if dn else "Torrent"

    parsed = urlparse(url)
    name = os.path.basename(parsed.path)
    return unquote(name).strip() or "Torrent"


def list_download_files():
    found = []
    for root, _, files in os.walk(DOWNLOAD_DIR):
        for name in files:
            found.append(os.path.join(root, name))
    return found


def choose_best_torrent_file(files):
    if not files:
        return None
    try:
        return max(files, key=lambda p: os.path.getsize(p))
    except OSError:
        return files[0]


def safe_windows_name(name):
    cleaned = "".join(c for c in (name or "") if c not in r'\\/:*?"<>|').strip().strip(".")
    return cleaned


def make_unique_path(directory, filename):
    base, ext = os.path.splitext(filename)
    candidate = os.path.join(directory, filename)
    i = 1
    while os.path.exists(candidate):
        candidate = os.path.join(directory, f"{base} ({i}){ext}")
        i += 1
    return candidate


def finalize_output_file(chosen, title, fallback_name):
    ext = os.path.splitext(chosen)[1]
    safe_title = safe_windows_name(title)
    final_name = f"{safe_title}{ext}" if safe_title else fallback_name
    final_path = make_unique_path(os.path.dirname(chosen), final_name)

    if os.path.abspath(chosen) != os.path.abspath(final_path):
        try:
            os.replace(chosen, final_path)
            chosen = final_path
        except OSError:
            pass

    return chosen, os.path.basename(chosen)


def resolve_aria2c():
    cmd = shutil.which("aria2c")
    if cmd:
        return cmd

    # Fallback for fresh Winget installs when PATH is not refreshed yet.
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pattern = os.path.join(
            local,
            "Microsoft",
            "WinGet",
            "Packages",
            "aria2.aria2_Microsoft.Winget.Source_8wekyb3d8bbwe",
            "*",
            "aria2c.exe",
        )
        matches = glob.glob(pattern)
        if matches:
            return matches[-1]

    return None


def update_job(job, *, status=None, progress=None, message=None, error=None, filename=None):
    if status is not None:
        job["status"] = status
    if progress is not None:
        job["progress"] = max(0, min(100, int(progress)))
    if message is not None:
        job["message"] = message
    if error is not None:
        job["error"] = error
    if filename is not None:
        job["filename"] = filename
    job["updated_at"] = time.time()


def log_job(job_id, event, **fields):
    payload = " ".join(f"{key}={json.dumps(value, ensure_ascii=True)}" for key, value in fields.items())
    logger.info("job=%s event=%s %s", job_id, event, payload)


def get_available_ffmpeg_encoders():
    global AVAILABLE_FFMPEG_ENCODERS
    if AVAILABLE_FFMPEG_ENCODERS is not None:
        return AVAILABLE_FFMPEG_ENCODERS

    ffmpeg_cmd = shutil.which("ffmpeg")
    if not ffmpeg_cmd:
        AVAILABLE_FFMPEG_ENCODERS = set()
        return AVAILABLE_FFMPEG_ENCODERS

    try:
        result = subprocess.run(
            [ffmpeg_cmd, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            AVAILABLE_FFMPEG_ENCODERS = set()
            return AVAILABLE_FFMPEG_ENCODERS
        AVAILABLE_FFMPEG_ENCODERS = set(re.findall(r"\b([a-z0-9_]+)\b", result.stdout.lower()))
    except Exception:
        AVAILABLE_FFMPEG_ENCODERS = set()
    return AVAILABLE_FFMPEG_ENCODERS


def choose_h264_encoder():
    available = get_available_ffmpeg_encoders()
    allowed = {"auto", "h264_nvenc", "h264_amf", "h264_qsv", "libx264"}
    configured = VIDEO_ENCODER if VIDEO_ENCODER in allowed else "auto"
    if configured != "auto":
        return configured if configured in available else "libx264"
    for encoder in ("h264_nvenc", "h264_amf", "h264_qsv", "libx264"):
        if encoder in available:
            return encoder
    return "libx264"


def build_h264_encoding_args(encoder_name):
    if encoder_name == "h264_nvenc":
        return [
            "-c:v", "h264_nvenc",
            "-preset", "p4",
            "-rc:v", "vbr",
            "-cq:v", "19",
            "-b:v", "0",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
        ]
    if encoder_name == "h264_amf":
        return [
            "-c:v", "h264_amf",
            "-quality", "quality",
            "-rc", "cqp",
            "-qp_i", "19",
            "-qp_p", "19",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
        ]
    if encoder_name == "h264_qsv":
        return [
            "-c:v", "h264_qsv",
            "-global_quality", "19",
            "-look_ahead", "0",
            "-pix_fmt", "nv12",
            "-profile:v", "high",
        ]
    return [
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-profile:v", "high",
    ]


def summarize_ffmpeg_error(stderr_text):
    lines = [line.strip() for line in (stderr_text or "").splitlines() if line.strip()]
    if not lines:
        return "FFmpeg conversion failed"
    for line in reversed(lines):
        lower = line.lower()
        if "error" in lower or "failed" in lower or "cannot" in lower:
            return line
    return lines[-1]


def set_transfer_stats(job, line):
    speed, eta = parse_transfer_stats(line)
    if speed is not None:
        job["speed"] = speed
    if eta is not None:
        job["eta"] = eta
    if speed is not None or eta is not None:
        job["updated_at"] = time.time()


def parse_percent(text):
    m = re.search(r"(\d{1,3}(?:\.\d+)?)%", text)
    if not m:
        return None
    return float(m.group(1))


def parse_aria2_eta(token):
    m = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", token.strip().lower())
    if not m:
        return token
    h = int(m.group(1) or 0)
    mm = int(m.group(2) or 0)
    ss = int(m.group(3) or 0)
    if h > 0:
        return f"{h}:{mm:02d}:{ss:02d}"
    return f"{mm:02d}:{ss:02d}"


def parse_transfer_stats(line):
    speed = None
    eta = None

    m_speed = re.search(r"\b(?:at|DL:)\s*([0-9]+(?:\.[0-9]+)?\s*[KMGTP]?i?B(?:/s)?)", line, re.IGNORECASE)
    if m_speed:
        speed = m_speed.group(1).replace(" ", "")
        if not speed.lower().endswith("/s"):
            speed += "/s"

    m_eta = re.search(r"\bETA\s*[: ]\s*([0-9:]+|\d+h\d+m\d+s|\d+h\d+m|\d+m\d+s|\d+s)", line, re.IGNORECASE)
    if m_eta:
        raw = m_eta.group(1).strip()
        if re.fullmatch(r"\d+h\d+m\d+s|\d+h\d+m|\d+m\d+s|\d+s", raw.lower()):
            eta = parse_aria2_eta(raw)
        else:
            eta = raw

    return speed, eta


def build_resume_key(url, format_choice, format_id):
    # Stable key so retries/restarts can reuse the same .part files.
    raw = f"{url}|{format_choice}|{format_id or ''}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def is_non_retryable_download_error(output_text):
    text = (output_text or "").lower()
    patterns = [
        "unsupported url",
        "unsupported site",
        "video unavailable",
        "this video is private",
        "requested format is not available",
        "sign in to confirm",
        "login required",
    ]
    return any(p in text for p in patterns)


def run_with_progress(cmd, on_line, timeout=None):
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        universal_newlines=True,
    )
    lines = []
    try:
        start = time.time()
        while True:
            if timeout and (time.time() - start) > timeout:
                process.kill()
                raise subprocess.TimeoutExpired(cmd, timeout)

            line = process.stdout.readline() if process.stdout else ""
            if line:
                lines.append(line)
                on_line(line.rstrip("\n"))
            if process.poll() is not None:
                break
            if not line:
                time.sleep(0.1)

        if process.stdout:
            tail = process.stdout.read()
            if tail:
                lines.append(tail)
                for line in tail.splitlines():
                    on_line(line)

        return process.returncode or 0, "".join(lines)
    finally:
        if process.stdout:
            process.stdout.close()


def transcode_video_for_editing(source_file, job):
    ffmpeg_cmd = shutil.which("ffmpeg")
    if not ffmpeg_cmd:
        raise RuntimeError("ffmpeg is required to export Vegas-compatible MP4 files")

    directory = os.path.dirname(source_file)
    base_name = os.path.splitext(os.path.basename(source_file))[0]
    target_file = make_unique_path(directory, f"{base_name}.vegas.mp4")
    job_id = job.get("job_id", "unknown")
    primary_encoder = choose_h264_encoder()
    encoders_to_try = [primary_encoder]
    if primary_encoder != "libx264":
        encoders_to_try.append("libx264")

    last_error = "FFmpeg conversion failed"
    for attempt_index, encoder_name in enumerate(encoders_to_try, start=1):
        cmd = [
            ffmpeg_cmd,
            "-y",
            "-i",
            source_file,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-fps_mode",
            "cfr",
        ] + build_h264_encoding_args(encoder_name) + [
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-ac",
            "2",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            "-max_muxing_queue_size",
            "1024",
            target_file,
        ]

        transcode_started_at = time.time()
        log_job(
            job_id,
            "transcode_start",
            source=source_file,
            target=target_file,
            encoder=encoder_name,
            attempt=attempt_index,
            ffmpeg=ffmpeg_cmd,
            command=cmd,
        )
        update_job(
            job,
            status="downloading",
            progress=96,
            message=f"Converting to Sony Vegas compatible MP4 ({encoder_name})...",
        )
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        transcode_elapsed = round(time.time() - transcode_started_at, 3)
        if result.returncode == 0 and os.path.exists(target_file):
            try:
                size_bytes = os.path.getsize(target_file)
            except OSError:
                size_bytes = None
            log_job(
                job_id,
                "transcode_done",
                elapsed_seconds=transcode_elapsed,
                returncode=result.returncode,
                encoder=encoder_name,
                output=target_file,
                size_bytes=size_bytes,
            )
            return target_file

        last_error = summarize_ffmpeg_error(result.stderr)
        stderr_tail = [line.strip() for line in (result.stderr or "").splitlines() if line.strip()][-12:]
        log_job(
            job_id,
            "transcode_error",
            elapsed_seconds=transcode_elapsed,
            returncode=result.returncode,
            encoder=encoder_name,
            error=last_error,
            stderr_tail=stderr_tail,
        )
        if os.path.exists(target_file):
            try:
                os.remove(target_file)
            except OSError:
                pass

        if encoder_name != "libx264":
            update_job(
                job,
                status="downloading",
                progress=96,
                message=f"{encoder_name} failed, retrying final conversion on CPU...",
            )

    raise RuntimeError(last_error)


def ensure_preview_file(job_id, source_file):
    # Browsers often fail to decode audio tracks inside MKV; build a playable sidecar.
    if not source_file.lower().endswith(".mkv"):
        return source_file

    cleanup_old_previews()

    preview_file = os.path.join(PREVIEW_DIR, f"{job_id}.preview.{PREVIEW_VERSION}.mp4")
    if os.path.exists(preview_file):
        return preview_file

    ffmpeg_cmd = shutil.which("ffmpeg")
    if not ffmpeg_cmd:
        return source_file

    cmd = [
        ffmpeg_cmd,
        "-y",
        "-i",
        source_file,
        "-map",
        "0:v:0?",
        "-map",
        "0:a:0?",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "23",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ac",
        "2",
        "-ar",
        "48000",
        "-max_muxing_queue_size",
        "1024",
        "-movflags",
        "+faststart",
        preview_file,
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        if result.returncode == 0 and os.path.exists(preview_file):
            return preview_file
    except Exception:
        pass

    return source_file


def cleanup_old_previews():
    ttl_seconds = max(0, int(PREVIEW_TTL_HOURS * 3600))
    now = time.time()
    for path in glob.glob(os.path.join(PREVIEW_DIR, "*.preview*.mp4")):
        try:
            age = now - os.path.getmtime(path)
            if ttl_seconds == 0 or age >= ttl_seconds:
                os.remove(path)
        except OSError:
            pass


cleanup_old_previews()
atexit.register(cleanup_old_previews)


def run_download(job_id, url, format_choice, format_id):
    job = jobs[job_id]
    job["job_id"] = job_id
    job_started_at = time.time()
    log_job(job_id, "job_start", url=url, format_choice=format_choice, format_id=format_id)

    if is_torrent_source(url):
        aria2c_cmd = resolve_aria2c()
        if not aria2c_cmd:
            update_job(job, status="error", error="Torrent support requires aria2c to be installed")
            return

        update_job(job, status="downloading", progress=0, message="Connecting to peers...")
        job["speed"] = ""
        job["eta"] = ""
        before = set(list_download_files())
        cmd = [
            aria2c_cmd,
            "--dir",
            DOWNLOAD_DIR,
            "--seed-time=0",
            "--summary-interval=1",
            "--console-log-level=notice",
            "--download-result=hide",
            url,
        ]

        try:
            torrent_started_at = time.time()
            def on_torrent_line(line):
                set_transfer_stats(job, line)
                percent = parse_percent(line)
                if percent is not None:
                    update_job(job, progress=percent, message=f"Downloading... {int(percent)}%")
                    return
                if "SEEDING" in line.upper():
                    update_job(job, progress=100, message="Download complete")
                    return
                if "ERR" in line.upper():
                    update_job(job, message=line.strip())

            return_code, output = run_with_progress(cmd, on_torrent_line, timeout=7200)
            log_job(
                job_id,
                "torrent_download_done",
                elapsed_seconds=round(time.time() - torrent_started_at, 3),
                returncode=return_code,
            )
            if return_code != 0:
                last = [l for l in output.splitlines() if l.strip()]
                err = last[-1] if last else "Torrent download failed"
                update_job(job, status="error", error=err)
                return

            after = set(list_download_files())
            new_files = [f for f in after - before if os.path.isfile(f)]
            chosen = choose_best_torrent_file(new_files)

            if not chosen:
                update_job(job, status="error", error="Torrent finished but no file was found")
                return

            title = job.get("title", "").strip()
            chosen, final_name = finalize_output_file(chosen, title, os.path.basename(chosen))
            job["file"] = chosen
            job["filename"] = final_name
            if chosen.lower().endswith(".mkv"):
                threading.Thread(target=ensure_preview_file, args=(job_id, chosen), daemon=True).start()
            update_job(job, status="done", progress=100, message="Completed", filename=job["filename"])
            log_job(
                job_id,
                "job_done",
                elapsed_seconds=round(time.time() - job_started_at, 3),
                output=job["file"],
                filename=job["filename"],
            )
            return
        except subprocess.TimeoutExpired:
            update_job(job, status="error", error="Torrent download timed out (2h limit)")
            return
        except Exception as e:
            update_job(job, status="error", error=str(e))
            return

    resume_key = job.get("resume_key") or build_resume_key(url, format_choice, format_id)
    out_template = os.path.join(DOWNLOAD_DIR, f"{resume_key}.%(ext)s")
    existing_parts = glob.glob(os.path.join(DOWNLOAD_DIR, f"{resume_key}*"))

    cmd = ["yt-dlp", "--no-playlist", "-o", out_template]

    if format_choice == "audio":
        cmd += ["-x", "--audio-format", "mp3"]
    elif format_id:
        cmd += ["-f", f"{format_id}+bestaudio/best", "--merge-output-format", "mp4"]
    else:
        cmd += ["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mp4"]

    cmd.append(url)

    try:
        job["speed"] = ""
        job["eta"] = ""
        cmd = [
            "yt-dlp",
            "--newline",
            "--no-playlist",
            "--continue",
            "--part",
            "--retries",
            "20",
            "--fragment-retries",
            "20",
            "-o",
            out_template,
        ] + cmd[4:]

        def on_ytdlp_line(line):
            line = line.strip()
            if not line:
                return
            set_transfer_stats(job, line)
            percent = parse_percent(line)
            if percent is not None:
                update_job(job, progress=percent, message=f"Downloading... {int(percent)}%")
                return
            if "Merging formats" in line or "Extracting audio" in line:
                update_job(job, progress=95, message="Processing file...")
                return
            if line.startswith("ERROR"):
                update_job(job, message=line)

        attempts = AUTO_RESUME_RETRIES + 1
        output = ""
        download_started_at = time.time()
        for attempt in range(1, attempts + 1):
            has_partial = bool(glob.glob(os.path.join(DOWNLOAD_DIR, f"{resume_key}*")))
            if attempt == 1:
                start_message = "Resuming download..." if (existing_parts or has_partial) else "Starting download..."
                update_job(job, status="downloading", progress=0, message=start_message)
            else:
                update_job(
                    job,
                    status="downloading",
                    message=f"Connection lost, retrying automatically ({attempt}/{attempts})...",
                )

            try:
                return_code, output = run_with_progress(cmd, on_ytdlp_line, timeout=300)
            except subprocess.TimeoutExpired:
                return_code = 124
                output = "Download stalled or connection lost"
            log_job(
                job_id,
                "ytdlp_attempt_done",
                attempt=attempt,
                returncode=return_code,
                elapsed_seconds=round(time.time() - download_started_at, 3),
            )

            if return_code == 0:
                break

            should_retry = attempt < attempts and not is_non_retryable_download_error(output)
            if should_retry:
                update_job(
                    job,
                    status="downloading",
                    message=(
                        f"Connection interrupted, auto-resume in {AUTO_RESUME_DELAY_SECONDS}s "
                        f"({attempt}/{attempts})"
                    ),
                )
                time.sleep(AUTO_RESUME_DELAY_SECONDS)
                continue

            last = [l for l in output.splitlines() if l.strip()]
            err = last[-1] if last else "Download failed"
            update_job(job, status="error", error=err)
            return

        files = glob.glob(os.path.join(DOWNLOAD_DIR, f"{resume_key}.*"))
        if not files:
            update_job(job, status="error", error="Download completed but no file was found")
            return

        if format_choice == "audio":
            target = [f for f in files if f.endswith(".mp3")]
            chosen = target[0] if target else files[0]
        else:
            target = [f for f in files if f.endswith(".mp4")]
            chosen = target[0] if target else files[0]

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        title = job.get("title", "").strip()
        log_job(
            job_id,
            "download_stage_done",
            elapsed_seconds=round(time.time() - download_started_at, 3),
            chosen=chosen,
            title=title,
        )
        if format_choice != "audio":
            update_job(job, status="downloading", progress=95, message="Download finished, starting final conversion...")
            chosen = transcode_video_for_editing(chosen, job)
            log_job(job_id, "transcode_stage_selected", output=chosen)

        chosen, final_name = finalize_output_file(chosen, title, os.path.basename(chosen))
        job["file"] = chosen
        job["filename"] = final_name
        log_job(job_id, "finalize_done", output=chosen, filename=final_name)

        for f in files:
            if f != chosen:
                try:
                    os.remove(f)
                except OSError:
                    pass

        if chosen.lower().endswith(".mkv"):
            threading.Thread(target=ensure_preview_file, args=(job_id, chosen), daemon=True).start()
        update_job(job, status="done", progress=100, message="Completed", filename=job["filename"])
        log_job(
            job_id,
            "job_done",
            elapsed_seconds=round(time.time() - job_started_at, 3),
            output=job["file"],
            filename=job["filename"],
        )
    except subprocess.TimeoutExpired:
        update_job(job, status="error", error="Download timed out after automatic retries")
        log_job(job_id, "job_error", elapsed_seconds=round(time.time() - job_started_at, 3), error="Download timed out after automatic retries")
    except Exception as e:
        update_job(job, status="error", error=str(e))
        log_job(job_id, "job_error", elapsed_seconds=round(time.time() - job_started_at, 3), error=str(e))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.json
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400

    if is_torrent_source(url):
        if not resolve_aria2c():
            return jsonify({"error": "Torrent support needs aria2c installed"}), 400

        return jsonify({
            "title": guess_torrent_title(url),
            "thumbnail": "",
            "duration": None,
            "uploader": "Torrent",
            "formats": [],
            "is_torrent": True,
        })

    cmd = ["yt-dlp", "--no-playlist", "-j", url]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return jsonify({"error": result.stderr.strip().split("\n")[-1]}), 400

        info = json.loads(result.stdout)

        # Build quality options — keep best format per resolution
        best_by_height = {}
        for f in info.get("formats", []):
            height = f.get("height")
            if height and f.get("vcodec", "none") != "none":
                tbr = f.get("tbr") or 0
                if height not in best_by_height or tbr > (best_by_height[height].get("tbr") or 0):
                    best_by_height[height] = f

        formats = []
        for height, f in best_by_height.items():
            formats.append({
                "id": f["format_id"],
                "label": f"{height}p",
                "height": height,
            })
        formats.sort(key=lambda x: x["height"], reverse=True)

        return jsonify({
            "title": info.get("title", ""),
            "thumbnail": info.get("thumbnail", ""),
            "duration": info.get("duration"),
            "uploader": info.get("uploader", ""),
            "formats": formats,
        })
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Timed out fetching video info"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/api/download", methods=["POST"])
def start_download():
    data = request.json
    url = data.get("url", "").strip()
    format_choice = data.get("format", "video")
    format_id = data.get("format_id")
    title = data.get("title", "")

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    job_id = uuid.uuid4().hex[:10]
    if not title and is_torrent_source(url):
        title = guess_torrent_title(url)

    jobs[job_id] = {
        "status": "downloading",
        "url": url,
        "title": title,
        "resume_key": build_resume_key(url, format_choice, format_id),
        "progress": 0,
        "speed": "",
        "eta": "",
        "message": "Queued",
        "updated_at": time.time(),
    }

    thread = threading.Thread(target=run_download, args=(job_id, url, format_choice, format_id))
    thread.daemon = True
    thread.start()

    return jsonify({"job_id": job_id})


@app.route("/api/status/<job_id>")
def check_status(job_id):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "Job not found"}), 404
    stalled = False
    if job.get("status") == "downloading":
        last = job.get("updated_at") or 0
        stalled = (time.time() - last) > 20

    return jsonify({
        "status": job["status"],
        "error": job.get("error"),
        "filename": job.get("filename"),
        "progress": job.get("progress", 0),
        "speed": job.get("speed", ""),
        "eta": job.get("eta", ""),
        "message": job.get("message", ""),
        "download_dir": DOWNLOAD_DIR,
        "stalled": stalled,
    })


@app.route("/api/file/<job_id>")
def download_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    return send_file(job["file"], as_attachment=True, download_name=job["filename"])


@app.route("/api/preview/<job_id>")
def preview_file(job_id):
    job = jobs.get(job_id)
    if not job or job["status"] != "done":
        return jsonify({"error": "File not ready"}), 404
    file_to_serve = ensure_preview_file(job_id, job["file"])
    guessed, _ = mimetypes.guess_type(file_to_serve)
    return send_file(file_to_serve, as_attachment=False, conditional=True, mimetype=guessed)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8899))
    host = os.environ.get("HOST", "127.0.0.1")
    app.run(host=host, port=port)
