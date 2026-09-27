"""YTConvert Online - the converter as a public web service.

Same engine as the desktop app (app.py): format picking, frame-exact cuts,
the preview proxy. What this file adds for strangers on the internet:

  * every visitor has a session id (sent as X-YTC-Session) and only sees and
    can touch their own jobs
  * finished files are served back as downloads and deleted after an hour
  * limits: video length, file size, jobs per visitor, requests per minute

Runs behind Caddy (TLS) on 127.0.0.1; see deploy/.

    python server.py --port 8080 --data /var/lib/ytconvert
"""

import argparse
import json
import re
import shutil
import threading
import time
import urllib.parse
import uuid
from collections import defaultdict, deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import app
import yt_dlp

ROOT = Path(__file__).resolve().parent
WEB = ROOT / "web"  # shared with the desktop app; "/" serves online.html

MAX_DURATION = 3 * 3600          # full videos longer than this are refused
MAX_CLIP = 30 * 60               # a cut may be at most this long
MAX_FILESIZE = 4 * 1024 ** 3     # yt-dlp skips formats bigger than this
KEEP_FILES = 3600                # seconds a finished file stays downloadable
ACTIVE_PER_SESSION = 2
SESSION_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
ACTIVE = {"queued", "preparing", "downloading", "processing"}

JOBS_DIR = None  # set in main()
TRUST_PROXY = False  # --trust-proxy: behind a hosting front end (e.g. Hugging Face)


# ---------------------------------------------------------------- limits

class RateLimit:
    """At most `n` hits per `window` seconds per key."""

    def __init__(self, n, window):
        self.n, self.window = n, window
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key):
        now = time.time()
        with self.lock:
            q = self.hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.n:
                return False
            q.append(now)
            return True


info_limit = RateLimit(30, 60)
download_limit = RateLimit(12, 600)
preview_limit = RateLimit(20, 600)


# -------------------------------------------------------------- cleanup

def janitor():
    """Delete finished jobs (files and records) once they are old enough."""
    while True:
        time.sleep(60)
        now = time.time()
        with app.jobs_lock:
            old = [j for j in app.jobs.values()
                   if j["status"] not in ACTIVE and now - j.get("finished", now) > KEEP_FILES]
            for j in old:
                app.jobs.pop(j["id"], None)
        for j in old:
            shutil.rmtree(j["_req"]["output_dir"], ignore_errors=True)
        # anything on disk that no job knows about any more (e.g. after a restart)
        known = {Path(j["_req"]["output_dir"]).name for j in list(app.jobs.values())}
        for d in JOBS_DIR.iterdir():
            if d.name not in known and now - d.stat().st_mtime > KEEP_FILES:
                shutil.rmtree(d, ignore_errors=True)


def stamp_finished():
    """Record when each job stopped, so the janitor knows its age."""
    while True:
        time.sleep(2)
        now = time.time()
        with app.jobs_lock:
            for j in app.jobs.values():
                if j["status"] not in ACTIVE and "finished" not in j:
                    j["finished"] = now


# ----------------------------------------------------------------- http

MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
        ".ico": "image/x-icon", ".webmanifest": "application/manifest+json"}


def public(j):
    out = app.public_job(j)
    out.pop("file", None)  # server path - never shown
    if j["status"] == "done" and j.get("filename"):
        out["download"] = f"/dl/{j['id']}/{urllib.parse.quote(j['filename'])}"
        out["expires"] = j.get("finished", time.time()) + KEEP_FILES
    return out


class Handler(BaseHTTPRequestHandler):
    server_version = "YTConvert"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    # -- helpers
    def client_ip(self):
        # A reverse proxy (Caddy on the same box, or the host's own front end
        # with --trust-proxy) puts the visitor's address in X-Forwarded-For.
        fwd = self.headers.get("X-Forwarded-For")
        if fwd and (TRUST_PROXY or self.client_address[0] == "127.0.0.1"):
            return fwd.split(",")[0].strip()
        return self.client_address[0]

    def session(self):
        s = self.headers.get("X-YTC-Session") or ""
        return s if SESSION_RE.match(s) else None

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 64 * 1024:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            return {}

    def _same_origin(self):
        origin = self.headers.get("Origin")
        host = self.headers.get("Host", "")
        return not origin or urllib.parse.urlparse(origin).netloc == host

    # -- GET
    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/state":
            sid = self.session()
            with app.jobs_lock:
                mine = [public(j) for j in app.jobs.values() if j.get("owner") == sid]
            mine.sort(key=lambda j: -j["created"])
            return self._json({"jobs": mine, "version": yt_dlp.version.__version__,
                               "keep_minutes": KEEP_FILES // 60})
        if path == "/api/thumb":
            return app.Handler._proxy_thumb(self)
        if path.startswith("/stream/"):
            return app.serve_stream(self, path.rsplit("/", 1)[1])
        if path.startswith("/dl/"):
            return self._download(path)
        return self._static(path)

    def _static(self, path):
        rel = "online.html" if path in ("/", "", "/index.html") else path.lstrip("/")
        f = (WEB / rel).resolve()
        if WEB not in f.parents or not f.is_file():
            return self.send_error(404)
        data = f.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(f.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def _download(self, path):
        parts = path.split("/", 3)  # ['', 'dl', id, name]
        if len(parts) < 4:
            return self.send_error(404)
        job = app.jobs.get(parts[2])
        if not job or job["status"] != "done" or not job.get("file"):
            return self.send_error(404, "This file has expired - convert it again.")
        f = Path(job["file"])
        if not f.is_file() or JOBS_DIR not in f.resolve().parents:
            return self.send_error(404)
        size = f.stat().st_size
        name = f.name
        ascii_name = re.sub(r"[^\x20-\x7e]|[\"\\]", "_", name)
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg" if f.suffix == ".mp3" else "video/mp4")
        self.send_header("Content-Length", str(size))
        self.send_header("Content-Disposition",
                         f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{urllib.parse.quote(name)}")
        self.end_headers()
        with open(f, "rb") as fh:
            shutil.copyfileobj(fh, self.wfile, 1024 * 1024)

    # -- POST
    def do_POST(self):
        if not self._same_origin():
            return self._json({"error": "forbidden"}, 403)
        path = urllib.parse.urlparse(self.path).path
        body = self._body()
        ip = self.client_ip()
        sid = self.session()
        try:
            if path == "/api/info":
                if not info_limit.allow(ip):
                    return self._json({"error": "Too many requests - wait a minute and try again."}, 429)
                url = str(body.get("url") or "").strip()
                if not url:
                    return self._json({"error": "Paste a YouTube link first."}, 400)
                try:
                    info = app.fetch_info(url)
                except Exception as e:  # noqa: BLE001
                    app.log("info failed", ip, url, repr(e)[:300])
                    return self._json({"error": app.friendly_error(e)}, 400)
                info["max_duration"] = MAX_DURATION
                info["max_clip"] = MAX_CLIP
                return self._json(info)

            if path == "/api/preview":
                if not preview_limit.allow(ip):
                    return self._json({"error": "Too many previews - wait a few minutes."}, 429)
                try:
                    return self._json({"src": app.make_preview(str(body.get("url") or "").strip())})
                except Exception as e:  # noqa: BLE001
                    return self._json({"error": app.friendly_error(e)}, 400)

            if path == "/api/download":
                return self._start(body, sid, ip)

            if path == "/api/cancel":
                job = app.jobs.get(str(body.get("id")))
                if job and job.get("owner") == sid:
                    job["_cancel"].set()
                    if job.get("_proc"):
                        job["_proc"].kill()
                    if job["status"] == "queued":
                        app.job_update(job, status="cancelled", stage="Cancelled")
                return self._json({"ok": True})

            if path == "/api/clear":
                with app.jobs_lock:
                    for k in [k for k, j in app.jobs.items() if j.get("owner") == sid and j["status"] not in ACTIVE]:
                        j = app.jobs.pop(k)
                        shutil.rmtree(j["_req"]["output_dir"], ignore_errors=True)
                return self._json({"ok": True})

            return self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            app.log("request failed", path, repr(e))
            return self._json({"error": "Something went wrong on our side. Try again."}, 500)

    def _start(self, body, sid, ip):
        if not sid:
            return self._json({"error": "Reload the page and try again."}, 400)
        if not download_limit.allow(ip):
            return self._json({"error": "That's a lot of downloads - wait a few minutes."}, 429)
        with app.jobs_lock:
            mine = sum(1 for j in app.jobs.values() if j.get("owner") == sid and j["status"] in ACTIVE)
        if mine >= ACTIVE_PER_SESSION:
            return self._json({"error": f"You can run {ACTIVE_PER_SESSION} conversions at a time - wait for one to finish."}, 429)
        mode = body.get("mode")
        if mode not in ("mp4", "mp3"):
            return self._json({"error": "Pick MP4 or MP3."}, 400)
        url = str(body.get("url") or "").strip()
        cached = app.INFO_CACHE.get(url)
        if cached and not body.get("clip") and (cached.get("duration") or 0) > MAX_DURATION:
            return self._json({"error": f"Videos longer than {MAX_DURATION // 3600} hours can't be converted whole - "
                                        "use Cut a part to take a section."}, 400)
        clip = body.get("clip")
        if clip:
            try:
                start, end = float(clip["start"]), float(clip["end"])
            except (KeyError, TypeError, ValueError):
                return self._json({"error": "Invalid part."}, 400)
            if end - start > MAX_CLIP:
                return self._json({"error": f"A cut can be at most {MAX_CLIP // 60} minutes."}, 400)
            clip = {"start": start, "end": end}
        # Only keys the engine knows; never anything path-like from the client.
        keep = ("url", "mode", "title", "thumbnail", "compat", "video_fid", "audio_fid", "height",
                "label", "muxed", "display", "bitrate", "expected_parts")
        req = {k: body[k] for k in keep if k in body}
        req["label"] = re.sub(r"[^\w .+-]", "", str(req.get("label") or ""))[:24]
        if clip:
            req["clip"] = clip
        work = JOBS_DIR / uuid.uuid4().hex
        work.mkdir(parents=True)
        req["output_dir"] = str(work)
        job = app.start_job(req)
        job["owner"] = sid
        job["ip"] = ip
        return self._json(public(job))


def main():
    global JOBS_DIR, TRUST_PROXY
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data", default=str(ROOT / "online-data"))
    ap.add_argument("--trust-proxy", action="store_true", help="take the visitor IP from X-Forwarded-For")
    args = ap.parse_args()

    JOBS_DIR = (Path(args.data) / "jobs").resolve()
    TRUST_PROXY = args.trust_proxy
    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    app.PORT = args.port  # the clip range proxy lives on this server
    app.EXTRA_YDL_OPTS.update({
        "max_filesize": MAX_FILESIZE,
        "cachedir": str(Path(args.data) / "cache"),
    })
    threading.Thread(target=janitor, daemon=True).start()
    threading.Thread(target=stamp_finished, daemon=True).start()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    app.log(f"YTConvert Online on http://{args.host}:{args.port}  yt-dlp {yt_dlp.version.__version__}  ffmpeg={app.FFMPEG}")
    server.serve_forever()


if __name__ == "__main__":
    main()
