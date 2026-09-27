"""Assemble the Hugging Face Space for YTConvert Online in deploy/hf-space/.

    python deploy/make_hf.py

The Space is a Docker Space. Everything is flat at the repo root (so it can
be uploaded through the website's file picker); the Dockerfile puts the UI
files into web/ where server.py expects them.
"""

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "deploy" / "hf-space"
WEB_FILES = ["online.html", "app.js", "cut.js", "style.css", "icon.svg"]

README = """---
title: YTConvert
emoji: 🎬
colorFrom: red
colorTo: pink
sdk: docker
app_port: 7860
pinned: false
short_description: YouTube to MP4 & MP3 in original quality - or cut a part
---

# YTConvert Online

Paste a YouTube link, pick the quality (MP4 up to 4K, MP3 up to 320 kbps) or
cut out just the part you need, and download it.

Files are deleted from the server after 60 minutes. Only download videos you
own or have permission to download. Not affiliated with YouTube or Google.

Built on yt-dlp and FFmpeg. Desktop app: https://ytconvert-app.vercel.app
"""

DOCKERFILE = """FROM python:3.11-slim

RUN apt-get update \\
 && apt-get install -y --no-install-recommends ffmpeg curl unzip ca-certificates \\
 && rm -rf /var/lib/apt/lists/*

# yt-dlp needs a JavaScript runtime to solve YouTube's player challenges.
RUN curl -fsSL https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip -o /tmp/deno.zip \\
 && unzip -q /tmp/deno.zip -d /usr/local/bin && rm /tmp/deno.zip && deno --version

# Spaces run as uid 1000; give it its own venv so start.sh can update yt-dlp.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user PATH=/home/user/venv/bin:/usr/local/bin:/usr/bin:/bin
RUN python -m venv /home/user/venv && pip install --no-cache-dir "yt-dlp[default]"

WORKDIR /home/user/app
COPY --chown=user app.py server.py start.sh ./
COPY --chown=user online.html app.js cut.js style.css icon.svg ./web/

EXPOSE 7860
CMD ["sh", "start.sh"]
"""

START = """#!/bin/sh
# YouTube changes often: pick up the newest yt-dlp every time the Space starts.
pip install -q -U --no-cache-dir "yt-dlp[default]" || true
exec python server.py --host 0.0.0.0 --port 7860 --data /tmp/ytconvert --trust-proxy
"""


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    OUT.mkdir(parents=True)
    for f in ("app.py", "server.py"):
        shutil.copy2(ROOT / f, OUT / f)
    for f in WEB_FILES:
        shutil.copy2(ROOT / "web" / f, OUT / f)
    for name, text in (("README.md", README), ("Dockerfile", DOCKERFILE), ("start.sh", START)):
        (OUT / name).write_text(text, encoding="utf-8", newline="\n")
    for p in sorted(OUT.iterdir()):
        print(f"{p.name:14s} {p.stat().st_size:>8,} bytes")


if __name__ == "__main__":
    main()
