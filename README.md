# ReClip

A self-hosted, open-source video and audio downloader with a clean web UI. Paste links from YouTube, TikTok, Instagram, Twitter/X, and 1000+ other sites — download as MP4 or MP3.

![Python](https://img.shields.io/badge/python-3.8+-blue)
![License](https://img.shields.io/badge/license-MIT-green)

https://github.com/user-attachments/assets/419d3e50-c933-444b-8cab-a9724986ba05

![ReClip MP3 Mode](assets/preview-mp3.png)

## Features

- Download videos from 1000+ supported sites (via [yt-dlp](https://github.com/yt-dlp/yt-dlp))
- Download torrents via magnet links or `.torrent` URLs (via [aria2](https://aria2.github.io/))
- MP4 video or MP3 audio extraction
- Quality/resolution picker
- Bulk downloads — paste multiple URLs at once
- Automatic URL deduplication
- Clean, responsive UI — no frameworks, no build step
- Single Python file backend (~150 lines)

## Quick Start

```bash
brew install yt-dlp ffmpeg aria2    # or apt install ffmpeg aria2 && pip install yt-dlp
git clone https://github.com/averygan/reclip.git
cd reclip
./reclip.sh
```

Open **http://localhost:8899**.

Or with Docker:

```bash
docker build -t reclip . && docker run -p 8899:8899 reclip
```

Or with Docker Compose:

```bash
docker compose up --build
```

Notes for Docker Compose:

- `.env` is loaded by Compose.
- `PORT` is read from `.env` (default `8899`).
- `HOST` is forced to `0.0.0.0` inside the container so the app is reachable from your host.
- Downloads are persisted to the host folder defined by `DOWNLOAD_DIR` in `.env`.

On Windows, you can also run:

```bat
reclip.bat
```

On Linux/macOS:

```bash
./reclip.sh
```

## Environment (.env)

ReClip reads `.env` automatically with `python-dotenv`.

Quick setup:

```bash
cp .env.example .env
```

On Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

Example:

```env
DOWNLOAD_DIR="P:\\"
PREVIEW_TTL_HOURS=24
HOST=127.0.0.1
PORT=8899
```

Supported variables:

- `DOWNLOAD_DIR`: folder where final downloads are saved. If missing, defaults to `./downloads`.
- `PREVIEW_TTL_HOURS`: how long preview files are kept (default `24`).
- `HOST`: bind address for Flask (default `127.0.0.1`).
- `PORT`: app port (default `8899`).

Docker note:

- In Docker Compose, the app writes to `/app/downloads` inside the container and that path is bind-mounted from `DOWNLOAD_DIR` on your machine.

Notes:

- `.env` is ignored by Git in `.gitignore`.
- Keeping local paths in `.env` is fine; they are not uploaded unless you force-add the file.

## Verify .env Is Not Going To GitHub

Run these checks before pushing:

```bash
git check-ignore -v .env
git ls-files .env .env.local .env.production .env.development
git status --short
```

Expected result:

- `git check-ignore` shows `.gitignore` rule for `.env`.
- `git ls-files ...` prints nothing for env files.
- `git status --short` does not list `.env`.

## Usage

1. Paste one or more video URLs into the input box
2. Choose **MP4** (video) or **MP3** (audio)
3. Click **Fetch** to load video info and thumbnails
4. Select quality/resolution if available
5. Click **Download** on individual videos, or **Download All**

## Supported Sites

Anything [yt-dlp supports](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), including:

YouTube, TikTok, Instagram, Twitter/X, Reddit, Facebook, Vimeo, Twitch, Dailymotion, SoundCloud, Loom, Streamable, Pinterest, Tumblr, Threads, LinkedIn, and many more.

## Stack

- **Backend:** Python + Flask (~150 lines)
- **Frontend:** Vanilla HTML/CSS/JS (single file, no build step)
- **Download engine:** [yt-dlp](https://github.com/yt-dlp/yt-dlp) + [ffmpeg](https://ffmpeg.org/)
- **Torrent engine:** [aria2](https://aria2.github.io/)
- **Python dependencies:** 2 (Flask, yt-dlp)

## Disclaimer

This tool is intended for personal use only. Please respect copyright laws and the terms of service of the platforms you download from. The developers are not responsible for any misuse of this tool.

## License

[MIT](LICENSE)
